"""Embed the Photos library into data/index.db. Resumable; nothing is embedded twice.

Photos: embedded from the small copies Photos already keeps on this Mac (no downloads).
Videos: first pass embeds the poster frame (no downloads). `--videos` then downloads
originals in small batches to a temp folder, embeds the real video, keeps a tiny 16 kHz
audio file for later transcription, and deletes the temp copy before the next batch.

  uv run index.py --since 2026-08-01            # photos + video posters
  uv run index.py --since 2026-08-01 --videos   # then real video embeddings
  uv run index.py --folder ~/some/folder        # testing
  uv run index.py --rebuild-meta                # refresh Apple metadata only (fast)
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import numpy as np

import core

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
BATCH = 8
VIDEO_BATCH = 10
MIN_FREE_GB = 20  # stop downloading if the disk gets this full


def dur(p):
    return (p.exif_info.duration if p.exif_info else None) or 0


def meta_text(p):
    """Apple's own metadata as searchable words: ML labels, named people, place, albums, date."""
    parts = [*p.labels, *[x for x in p.persons if x != "_UNKNOWN_"], *p.albums, *p.keywords,
             p.title or "", p.description or "", "video" if p.ismovie else "photo"]
    if p.place:
        parts.append(p.place.name or "")
    if p.date:
        parts.append(p.date.strftime("%B %Y"))
    return " ; ".join(x for x in parts if x)


def from_photos(since=None, until=None):
    import osxphotos
    for p in osxphotos.PhotosDB().photos(images=True, movies=True, from_date=since, to_date=until):
        derivs = p.path_derivatives or []
        created = p.date.isoformat() if p.date else None
        if p.ismovie and p.path:
            yield p.uuid, "video", p.path, dur(p), created, meta_text(p)
        elif derivs or p.path:
            # derivatives: ~1024px local JPEGs Photos keeps even with Optimize Storage on
            kind = "poster" if p.ismovie else "image"  # poster = iCloud-only video, still frame for now
            yield p.uuid, kind, derivs[0] if derivs else p.path, dur(p), created, meta_text(p)
        else:
            yield p.uuid, "missing", None, 0, created, meta_text(p)


def from_folder(folder):
    for f in sorted(Path(folder).expanduser().rglob("*")):
        ext = f.suffix.lower()
        if ext in IMAGE_EXT | VIDEO_EXT:
            uid = hashlib.sha1(str(f).encode()).hexdigest()
            yield uid, "video" if ext in VIDEO_EXT else "image", str(f), 0, None, f.stem.replace("_", " ")


def save_asset(db, item, error=None):
    uuid, kind, path, duration, created, meta = item
    db.execute("insert or replace into assets(uuid, kind, created, duration, path, indexed_at, error, meta) "
               "values (?,?,?,?,?,?,?,?)", (uuid, kind, created, duration, path, time.time(), error, meta))
    core.refresh_fts(db, uuid)


def save_vec(db, uuid, vec, t1, source):
    db.execute("insert into segments(uuid, t0, t1, source, vec) values (?,?,?,?,?)",
               (uuid, 0, t1, source, vec.astype(np.float32).tobytes()))


def encode(inputs):
    """Encode a batch; on failure retry one by one so a single bad file doesn't sink the batch."""
    m = core.model()
    kw = dict(normalize_embeddings=True, processing_kwargs={"image": {"max_soft_tokens": core.IMAGE_TOKENS}})
    try:
        return list(m.encode(inputs, batch_size=len(inputs), **kw)), [None] * len(inputs)
    except Exception:
        vecs, errs = [], []
        for x in inputs:
            try:
                vecs.append(m.encode([x], **kw)[0]); errs.append(None)
            except Exception as e:
                vecs.append(None); errs.append(f"{type(e).__name__}: {e}"[:300])
        return vecs, errs


def progress(n, total, t, fails):
    print(f"\r{n}/{total}  {n / max(time.time() - t, 1e-9):.2f} items/s  {fails} failed", end="", flush=True)


def index_items(db, todo):
    for it in todo:
        if it[1] == "missing":
            save_asset(db, it, error="no local file")
    media = [it for it in todo if it[1] != "missing"]
    stills = [it for it in media if it[1] != "video"]
    videos = [it for it in media if it[1] == "video"]
    batches = [stills[i:i + BATCH] for i in range(0, len(stills), BATCH)] + [[v] for v in videos]
    t, n, fails = time.time(), 0, 0
    for b in batches:
        vecs, errs = encode([{"video" if it[1] == "video" else "image": it[2]} for it in b])
        for it, v, e in zip(b, vecs, errs):
            save_asset(db, it, e)
            if v is not None:
                save_vec(db, it[0], v, it[3], it[1])
            fails += e is not None
        db.commit()  # commit per batch: a crash loses at most one batch
        n += len(b)
        progress(n, len(media), t, fails)
    el = time.time() - t
    print(f"\nstills+posters: {len(stills)}, local videos: {len(videos)}, failed: {fails}, "
          f"{len(media) / el if el else 0:.2f} items/s")


def extract_audio(video, uuid):
    """Keep ~0.1 MB/min of speech-quality audio so P4 transcription never needs a re-download."""
    core.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    out = core.AUDIO_DIR / f"{uuid}.opus"
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1",
                        "-ar", "16000", "-c:a", "libopus", "-b:a", "16k", str(out)], capture_output=True, text=True)
    if r.returncode != 0:
        out.unlink(missing_ok=True)  # no audio track, or unreadable: fine, skip


def download(uuids, dest):
    ufile = dest / "uuids.txt"
    ufile.write_text("\n".join(uuids))
    osxphotos = Path(sys.executable).parent / "osxphotos"
    return subprocess.run([str(osxphotos), "export", str(dest), "--uuid-from-file", str(ufile),
                           "--download-missing", "--use-photokit", "--skip-edited", "--skip-live",
                           "--skip-raw", "--filename", "{uuid}", "--no-progress"],
                          capture_output=True, text=True)


def index_videos(db, since, limit):
    """Download iCloud-only videos in batches, embed, keep audio, delete the temp copies."""
    q = "select uuid, duration, created from assets where kind = 'poster'"
    rows = [r for r in db.execute(q).fetchall() if not since or (r[2] and r[2] >= since.isoformat())]
    if limit:
        rows = rows[:limit]
    print(f"{len(rows)} iCloud-only videos to download and embed, {VIDEO_BATCH} at a time")
    t, n, fails = time.time(), 0, 0
    for i in range(0, len(rows), VIDEO_BATCH):
        if shutil.disk_usage(core.DATA).free / 1e9 < MIN_FREE_GB:
            print(f"\nstopping: under {MIN_FREE_GB} GB free"); break
        batch = rows[i:i + VIDEO_BATCH]
        tmp = Path(tempfile.mkdtemp(prefix="cs-videos-"))  # only this folder is ever deleted
        try:
            r = download([u for u, _, _ in batch], tmp)
            if "authorization" in (r.stdout + r.stderr).lower():
                sys.exit("No Photos access for this terminal app. Grant it in System Settings > "
                         "Privacy & Security > Photos, or run this from a terminal that has it.")
            for uuid, duration, _ in batch:
                f = next((x for x in tmp.iterdir() if x.stem == uuid and x.suffix.lower() in VIDEO_EXT), None)
                if not f:
                    fails += 1
                    db.execute("update assets set error = 'download failed' where uuid = ?", (uuid,))
                    continue
                vecs, errs = encode([{"video": str(f)}])
                if vecs[0] is not None:
                    save_vec(db, uuid, vecs[0], duration, "video")  # poster vector stays as a second segment
                    db.execute("update assets set kind = 'video', error = null where uuid = ?", (uuid,))
                else:
                    fails += 1
                    db.execute("update assets set error = ? where uuid = ?", (errs[0], uuid))
                extract_audio(f, uuid)
            db.commit()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        n += len(batch)
        progress(n, len(rows), t, fails)
    print(f"\nvideos done: {n - fails} embedded, {fails} failed")


def save_info(db, p):
    loc = p.location or (None, None)
    db.execute("insert or replace into asset_info(uuid, place, lat, lon, albums, local_original) values (?,?,?,?,?,?)",
               (p.uuid, p.place.name if p.place else None, loc[0], loc[1], " ; ".join(p.albums), int(bool(p.path))))


def rebuild_meta(db, only=None):
    """Refresh Photos metadata (labels, place, albums, local/iCloud) for indexed items. No embedding."""
    import osxphotos
    known = set(only) if only is not None else {r[0] for r in db.execute("select uuid from assets")}
    c = 0
    for p in osxphotos.PhotosDB().photos(images=True, movies=True):
        if p.uuid in known:
            db.execute("update assets set meta = ? where uuid = ?", (meta_text(p), p.uuid))
            save_info(db, p)
            core.refresh_fts(db, p.uuid)
            c += 1
            if c % 500 == 0:
                db.commit()
    db.commit()
    print(f"metadata refreshed for {c} items")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--since", type=lambda d: datetime.fromisoformat(d).astimezone(), help="YYYY-MM-DD")
    ap.add_argument("--until", type=lambda d: datetime.fromisoformat(d).astimezone(), help="YYYY-MM-DD, exclusive")
    ap.add_argument("--tokens", type=int, choices=(70, 140, 280, 560, 1120), help="image tokens (default core.IMAGE_TOKENS)")
    ap.add_argument("--videos", action="store_true", help="download + embed iCloud-only videos")
    ap.add_argument("--rebuild-meta", action="store_true")
    a = ap.parse_args()

    if a.tokens:
        core.IMAGE_TOKENS = a.tokens  # read at encode time by encode() and media.embed_frames
    db = core.connect()
    if a.rebuild_meta:
        return rebuild_meta(db)
    done = {r[0] for r in db.execute("select uuid from assets")}
    todo = [it for it in (from_folder(a.folder) if a.folder else from_photos(a.since, a.until)) if it[0] not in done]
    if a.limit:
        todo = todo[: a.limit]
    span = f", {a.since:%Y-%m-%d} to {a.until:%Y-%m-%d}" if a.since and a.until else ""
    print(f"{len(done)} already indexed, {len(todo)} new ({core.IMAGE_TOKENS} tokens{span})")
    # for status.py: what this run is working through
    (core.DATA / "progress.json").write_text(json.dumps({"total": len(todo), "started": time.time()}))
    core.model()  # load before timing
    if todo:
        index_items(db, todo)
        if not a.folder:
            rebuild_meta(db, [it[0] for it in todo])  # place, albums, local/iCloud for the new items
    if a.videos:
        index_videos(db, a.since, a.limit)


if __name__ == "__main__":
    main()
