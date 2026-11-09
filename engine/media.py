"""Video understanding for editing: playable proxies, scene detection, contact sheets, scene embeddings.

Per clip, once (cached):
  1. get the original (local file, or downloaded from iCloud to a temp folder)
  2. make a 360/540/720p proxy (settings.proxy_height) in data/proxies (~1-2 MB/min) + speech audio; delete the temp original
  3. detect scenes (PySceneDetect, adaptive content detector) on the proxy
  4. per scene: sharpness + motion scores, middle frame embedded so search lands on the right moment
  5. contact sheet image (one frame per scene, labelled) that Claude can look at
"""
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

import core
import settings

router = APIRouter()
log = logging.getLogger("kioku.media")
PROXIES = core.DATA / "proxies"
SHEETS = core.DATA / "sheets"
STRIPS = core.DATA / "strips"
STRIP_W, STRIP_H, STRIP_MAX = 160, 90, 120
VIDEO_EXT = {".mov", ".mp4", ".m4v"}
MAX_SCENE = 8.0  # split longer scenes into even pieces so cuts can land inside long takes
_locks: dict[str, threading.Lock] = {}
# live work, for the app's progress panel: uuid -> {stage, started, label}; plus counters since engine start
ACTIVE: dict[str, dict] = {}
DONE = {"ok": 0, "failed": 0}


def stage(uuid, name, label=None):
    import time as _t
    if name is None:
        ACTIVE.pop(uuid, None)
        return
    cur = ACTIVE.setdefault(uuid, {"started": _t.time(), "label": label or label_for(uuid)})
    cur["stage"], cur["stage_started"] = name, _t.time()


def label_for(uuid):
    with core.connect() as c:
        row = c.execute("select meta, duration from assets where uuid = ?", (uuid,)).fetchone()
    if not row:
        return "video"
    words = [w for w in (row[0] or "").split(" ; ") if w and w not in ("video", "photo")][:3]
    return f"{', '.join(words) or 'video'} · {int(row[1] or 0)}s"
_locks_guard = threading.Lock()
_gpu = threading.Lock()

SCHEMA = """
create table if not exists scenes(uuid text not null, idx integer not null, t0 real not null, t1 real not null,
  sharp real, motion real, primary key(uuid, idx));
"""


def lock_for(uuid):
    with _locks_guard:
        return _locks.setdefault(uuid, threading.Lock())


def video_row(c, uuid):
    row = c.execute("select a.kind, a.path, a.duration, i.local_original from assets a "
                    "left join asset_info i using(uuid) where a.uuid = ?", (uuid,)).fetchone()
    if not row:
        raise HTTPException(404, "not indexed")
    if row[0] == "image":
        raise HTTPException(422, "that's a photo, not a video")
    return row


def download_original(uuid, dest):
    """iCloud original -> dest via PhotoKit. (Photos' AppleScript export is not used: it hangs on iCloud items.)"""
    exe = str(Path(sys.executable).parent / "osxphotos")
    limit = settings.get().download_seconds
    try:
        r = subprocess.run([exe, "export", str(dest), "--uuid", uuid, "--download-missing", "--use-photokit",
                            "--skip-edited", "--skip-live", "--skip-raw", "--filename", "{uuid}", "--no-progress"],
                           capture_output=True, text=True, timeout=limit)
        out = (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"iCloud download took over {limit} s (Settings > iCloud download timeout); skipped this video")
    f = next((x for x in dest.iterdir() if x.stem == uuid and x.suffix.lower() in VIDEO_EXT), None)
    if f:
        return f
    log.warning("download %s returned no file (exit %s): %s", uuid, r.returncode, out[-2000:])
    if "authorization" in out.lower():
        raise RuntimeError("no Photos access for the app running the engine (System Settings > Privacy & Security > Photos)")
    raise RuntimeError("iCloud download failed: " + (out.splitlines()[-1] if out else "no file returned"))


def local_original(uuid):
    """The original on disk if Photos has it locally (not iCloud-only), else None."""
    with core.connect() as c:
        _, path, _, _ = video_row(c, uuid)
    return Path(path) if path and Path(path).suffix.lower() in VIDEO_EXT and Path(path).is_file() else None


def ensure_proxy(uuid):
    out = PROXIES / f"{uuid}.mp4"
    if out.is_file():
        return out
    local = local_original(uuid)
    PROXIES.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="cs-orig-"))  # only this folder is ever deleted
    try:
        if not local:
            stage(uuid, "downloading from iCloud")
        src = local or download_original(uuid, tmp)
        stage(uuid, "making a playable copy")
        part = out.with_suffix(".part.mp4")
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
                            "-vf", f"scale=-2:'min({settings.get().proxy_height},ih)'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
                            "-pix_fmt", "yuv420p", "-g", "30", "-keyint_min", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(part)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg: {r.stderr.strip()[-300:]}")
        part.replace(out)
        import index  # reuse: keep speech-quality audio for transcription later
        index.extract_audio(src, uuid)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def detect_scenes(proxy):
    from scenedetect import AdaptiveDetector, detect
    found = [(a.get_seconds(), b.get_seconds()) for a, b in detect(str(proxy), AdaptiveDetector())]
    dur = probe_duration(proxy)
    if not found:
        found = [(0.0, dur)]
    out = []
    for t0, t1 in found:  # split long takes so a cut can start mid-shot
        n = max(1, int(np.ceil((t1 - t0) / MAX_SCENE)))
        step = (t1 - t0) / n
        out += [(round(t0 + k * step, 2), round(t0 + (k + 1) * step, 2)) for k in range(n)]
    return out


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def frame_at(cap, t):
    import cv2
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, frame = cap.read()
    return frame if ok else None


def score_scene(cap, t0, t1):
    """sharpness = Laplacian variance of the middle frame (higher = crisper);
    motion = mean abs difference between 3 frames across the scene (higher = more movement / shake)."""
    import cv2
    ts = [t0 + (t1 - t0) * f for f in (0.2, 0.5, 0.8)]
    frames = [frame_at(cap, t) for t in ts]
    frames = [f for f in frames if f is not None]
    if not frames:
        return None, 0.0, 0.0
    grays = [cv2.cvtColor(cv2.resize(f, (320, int(320 * f.shape[0] / f.shape[1]))), cv2.COLOR_BGR2GRAY) for f in frames]
    mid = frames[len(frames) // 2]
    sharp = float(cv2.Laplacian(grays[len(grays) // 2], cv2.CV_64F).var())
    motion = float(np.mean([np.abs(a.astype(int) - b.astype(int)).mean() for a, b in zip(grays, grays[1:])])) if len(grays) > 1 else 0.0
    return mid, round(sharp, 1), round(motion, 2)


def contact_sheet(frames, scenes, out):
    """One labelled frame per scene, 4 across."""
    from PIL import Image, ImageDraw
    import cv2
    W, cols = 300, 4
    tiles = []
    for f, (t0, t1) in zip(frames, scenes):
        if f is None:
            continue
        im = Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))
        im.thumbnail((W, W))
        tiles.append((im, f"#{len(tiles)}  {t0:.1f}-{t1:.1f}s"))
    if not tiles:
        return None
    h = max(t[0].height for t in tiles) + 22
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (W + 6) + 6, rows * (h + 6) + 6), (12, 11, 11))
    d = ImageDraw.Draw(sheet)
    for k, (im, label) in enumerate(tiles):
        x, y = 6 + (k % cols) * (W + 6), 6 + (k // cols) * (h + 6)
        sheet.paste(im, (x + (W - im.width) // 2, y))
        d.text((x + 4, y + h - 18), label, fill=(255, 140, 90))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=82)
    return out


RUNNING = 0  # videos being downloaded/analyzed right now; guarded by settings.changed


def video_slot(uuid, label=None):
    """Wait for one of Settings.max_agents video slots: parallel enough to overlap iCloud downloads,
    capped so a burst of requests doesn't start dozens of downloads at once."""
    global RUNNING
    with settings.changed:
        if RUNNING >= settings.get().max_agents:
            stage(uuid, "waiting for a free slot", label)
        while RUNNING >= settings.get().max_agents:
            settings.changed.wait(2)
        RUNNING += 1


def release_video_slot():
    global RUNNING
    with settings.changed:
        RUNNING -= 1
        settings.changed.notify_all()


def fetch(uuid, mode, tmp):
    """A file ffmpeg can read for this video: mode "preview" = the proxy, "final" = the original (local, or
    downloaded into tmp). Lock order is lock_for(uuid), then video_slot, then release, same as analyze;
    call it before taking any render slot."""
    with lock_for(uuid):
        video_slot(uuid)
        try:
            if mode == "preview":
                return ensure_proxy(uuid)
            if mode != "final":
                raise ValueError(f"mode must be preview or final, not {mode!r}")
            src = local_original(uuid)
            if src:
                return src
            stage(uuid, "downloading from iCloud")
            import projects  # lazy: projects imports library/settings, keep media importable on its own
            got = projects.originals([uuid], Path(tmp)).get(uuid)
            if not got:
                raise RuntimeError("iCloud download failed")
            return got
        finally:
            release_video_slot()
            stage(uuid, None)


def analyze(uuid, embed=True):
    """Cached: returns scenes for a video, building proxy / scenes / sheet / scene vectors on first call."""
    with lock_for(uuid):
        with core.connect() as c:
            c.executescript(SCHEMA)
            video_row(c, uuid)
            rows = c.execute("select idx, t0, t1, sharp, motion from scenes where uuid = ? order by idx", (uuid,)).fetchall()
        sheet = SHEETS / f"{uuid}.jpg"
        if rows and sheet.is_file():
            return result(uuid, rows)
        video_slot(uuid)
        try:
            return _analyze(uuid, embed, sheet)
        except Exception as e:
            DONE["failed"] += 1
            # RuntimeError = expected failure with a readable message; anything else is a bug, keep the traceback
            log.error("analyze %s failed: %s", uuid, e, exc_info=not isinstance(e, RuntimeError))
            raise
        finally:
            release_video_slot()
            stage(uuid, None)


def _analyze(uuid, embed, sheet):
        import cv2
        log.info("analyzing %s (%s)", uuid, label_for(uuid))
        stage(uuid, "starting")
        proxy = ensure_proxy(uuid)
        stage(uuid, "finding scenes")
        scenes = detect_scenes(proxy)
        cap = cv2.VideoCapture(str(proxy))
        scored = [score_scene(cap, t0, t1) for t0, t1 in scenes]
        cap.release()
        contact_sheet([s[0] for s in scored], scenes, sheet)
        stage(uuid, "understanding scenes")
        vecs = embed_frames([s[0] for s in scored]) if embed else [None] * len(scenes)
        with core.connect() as c:
            c.execute("delete from scenes where uuid = ?", (uuid,))
            c.execute("delete from segments where uuid = ? and source = 'scene'", (uuid,))
            for i, ((t0, t1), (_, sharp, motion), v) in enumerate(zip(scenes, scored, vecs)):
                c.execute("insert into scenes values (?,?,?,?,?,?)", (uuid, i, t0, t1, sharp, motion))
                if v is not None:
                    c.execute("insert into segments(uuid, t0, t1, source, vec) values (?,?,?,?,?)",
                              (uuid, t0, t1, "scene", v.astype(np.float32).tobytes()))
            c.commit()
            rows = c.execute("select idx, t0, t1, sharp, motion from scenes where uuid = ? order by idx", (uuid,)).fetchall()
        DONE["ok"] += 1
        log.info("analyzed %s: %d scenes", uuid, len(rows))
        return result(uuid, rows)


def embed_frames(frames):
    from PIL import Image
    import cv2
    ims = [Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)) if f is not None else None for f in frames]
    ok = [im for im in ims if im is not None]
    if not ok:
        return [None] * len(frames)
    with _gpu:  # parallel Claude edits analyze different videos at once: load the model once, one encode at a time
        vecs = iter(core.model().encode([{"image": im} for im in ok], batch_size=8, normalize_embeddings=True,
                                        processing_kwargs={"image": {"max_soft_tokens": core.IMAGE_TOKENS}}))
    return [next(vecs) if im is not None else None for im in ims]


def result(uuid, rows):
    return {"uuid": uuid, "scenes": [dict(zip(("idx", "t0", "t1", "sharpness", "motion"), r)) for r in rows],
            "proxy": f"/media/{uuid}/proxy", "sheet": f"/media/{uuid}/sheet"}


# ---------- routes

@router.get("/media/activity")
def activity():
    """What the engine is fetching/analyzing right now, for the app's progress panel."""
    import time as _t
    now = _t.time()
    return {"active": [{"uuid": u, "label": a.get("label"), "stage": a.get("stage"),
                        "seconds": round(now - a["started"]), "stage_seconds": round(now - a.get("stage_started", now))}
                       for u, a in list(ACTIVE.items())], **DONE}


@router.post("/media/{uuid}/analyze")
def analyze_route(uuid: str):
    try:
        return analyze(uuid)
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.get("/media/{uuid}/scenes")
def scenes_route(uuid: str):
    with core.connect() as c:
        c.executescript(SCHEMA)
        rows = c.execute("select idx, t0, t1, sharp, motion from scenes where uuid = ? order by idx", (uuid,)).fetchall()
    if not rows:
        raise HTTPException(404, "not analyzed yet")
    return result(uuid, rows)


@router.get("/media/{uuid}/proxy")
def proxy_route(uuid: str):
    f = PROXIES / f"{uuid}.mp4"
    if not f.is_file() or f.parent != PROXIES:
        raise HTTPException(404, "no playable copy yet")
    return FileResponse(f, media_type="video/mp4")  # supports Range, so <video> can seek


@router.get("/media/{uuid}/sheet")
def sheet_route(uuid: str):
    f = SHEETS / f"{uuid}.jpg"
    if not f.is_file() or f.parent != SHEETS:
        raise HTTPException(404, "not analyzed yet")
    return FileResponse(f, media_type="image/jpeg")


def make_strip(proxy, out):
    """One row of 160x90 tiles, centre-cropped to 16:9, n = min(120, ceil(duration)) frames spread evenly in time."""
    import cv2
    dur = probe_duration(proxy)
    if not dur > 0:
        raise RuntimeError("the playable copy has no length")
    n = min(STRIP_MAX, int(np.ceil(dur)))
    cap = cv2.VideoCapture(str(proxy))
    try:
        tiles, last = [], np.zeros((STRIP_H, STRIP_W, 3), np.uint8)
        for k in range(n):
            f = frame_at(cap, (k + 0.5) * dur / n)
            if f is not None:
                h, w = f.shape[:2]
                cw, ch = (int(h * 16 / 9), h) if w * 9 > h * 16 else (w, int(w * 9 / 16))
                x, y = (w - cw) // 2, (h - ch) // 2
                last = cv2.resize(f[y:y + ch, x:x + cw], (STRIP_W, STRIP_H), interpolation=cv2.INTER_AREA)
            tiles.append(last)  # a frame that will not decode repeats its neighbour
    finally:
        cap.release()
    ok, buf = cv2.imencode(".jpg", np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 70])
    if not ok:
        raise RuntimeError("could not encode the filmstrip")
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(f".{threading.get_ident()}.part")
    part.write_bytes(buf.tobytes())
    part.replace(out)


@router.get("/media/{uuid}/strip")
def strip_route(uuid: str):
    """Filmstrip of the whole source clip for the timeline block; built from the proxy, cached, rebuilt if the proxy is newer."""
    proxy, f = PROXIES / f"{uuid}.mp4", STRIPS / f"{uuid}.jpg"
    if proxy.parent != PROXIES or not proxy.is_file():
        raise HTTPException(404, "no playable copy yet")
    if f.parent != STRIPS:
        raise HTTPException(404, "no such clip")
    if not f.is_file() or f.stat().st_mtime < proxy.stat().st_mtime:
        try:  # no lock_for here: analyze holds it for minutes. Two racing builds write the same bytes, replace is atomic
            make_strip(proxy, f)
        except RuntimeError as e:
            raise HTTPException(502, str(e))
    return FileResponse(f, media_type="image/jpeg")
