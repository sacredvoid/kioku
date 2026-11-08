"""Video understanding for editing: playable proxies, scene detection, contact sheets, scene embeddings.

Per clip, once (cached):
  1. get the original (local file, or downloaded from iCloud to a temp folder)
  2. make a 540p proxy in data/proxies (~1-2 MB/min) + speech audio; delete the temp original
  3. detect scenes (PySceneDetect, adaptive content detector) on the proxy
  4. per scene: sharpness + motion scores, middle frame embedded so search lands on the right moment
  5. contact sheet image (one frame per scene, labelled) that Claude can look at
"""
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

router = APIRouter()
PROXIES = core.DATA / "proxies"
SHEETS = core.DATA / "sheets"
VIDEO_EXT = {".mov", ".mp4", ".m4v"}
MAX_SCENE = 8.0  # split longer scenes into even pieces so cuts can land inside long takes
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

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
    """iCloud original -> dest. Tries PhotoKit, then Photos' AppleScript export."""
    exe = str(Path(sys.executable).parent / "osxphotos")
    base = [exe, "export", str(dest), "--uuid", uuid, "--download-missing", "--skip-edited", "--skip-live",
            "--skip-raw", "--filename", "{uuid}", "--no-progress"]
    for extra in (["--use-photokit"], []):
        subprocess.run(base + extra, capture_output=True, text=True, timeout=600)
        f = next((x for x in dest.iterdir() if x.stem == uuid and x.suffix.lower() in VIDEO_EXT), None)
        if f:
            return f
    raise RuntimeError("couldn't download the original from iCloud. Check that Photos is signed in and that the "
                       "app running Safelight has Photos access (System Settings > Privacy & Security > Photos).")


def ensure_proxy(uuid):
    out = PROXIES / f"{uuid}.mp4"
    if out.is_file():
        return out
    with core.connect() as c:
        kind, path, _, local = video_row(c, uuid)
    PROXIES.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="cs-orig-"))  # only this folder is ever deleted
    try:
        src = Path(path) if (path and Path(path).suffix.lower() in VIDEO_EXT and Path(path).is_file()) else download_original(uuid, tmp)
        part = out.with_suffix(".part.mp4")
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
                            "-vf", "scale=-2:'min(540,ih)'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(part)],
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
        import cv2
        proxy = ensure_proxy(uuid)
        scenes = detect_scenes(proxy)
        cap = cv2.VideoCapture(str(proxy))
        scored = [score_scene(cap, t0, t1) for t0, t1 in scenes]
        cap.release()
        contact_sheet([s[0] for s in scored], scenes, sheet)
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
        return result(uuid, rows)


def embed_frames(frames):
    from PIL import Image
    import cv2
    ims = [Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)) if f is not None else None for f in frames]
    ok = [im for im in ims if im is not None]
    if not ok:
        return [None] * len(frames)
    vecs = iter(core.model().encode([{"image": im} for im in ok], batch_size=8, normalize_embeddings=True,
                                    processing_kwargs={"image": {"max_soft_tokens": core.IMAGE_TOKENS}}))
    return [next(vecs) if im is not None else None for im in ims]


def result(uuid, rows):
    return {"uuid": uuid, "scenes": [dict(zip(("idx", "t0", "t1", "sharpness", "motion"), r)) for r in rows],
            "proxy": f"/media/{uuid}/proxy", "sheet": f"/media/{uuid}/sheet"}


# ---------- routes

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
