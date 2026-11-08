"""Local search API for the Safelight app. Keeps the model loaded so queries are fast.

  uv run server.py                        # http://127.0.0.1:8765
  curl "127.0.0.1:8765/search?q=dog&k=5"
  curl "127.0.0.1:8765/recent?limit=50"
  curl -X POST "127.0.0.1:8765/reindex?since=2026-11-01"
"""
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse

import core
import library
import projects
import media

state = {"index": None, "reindex": "idle", "checked": 0.0, "stamp": None}
lock = threading.Lock()
RELOAD_EVERY = 30  # seconds; picks up items a running indexer has added
THUMB_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
THUMBS = core.DATA / "thumbs"
THUMB_PX = 400  # tiles are ~170 css px; 400 covers 2x retina with room to spare
KINDS = {"image", "video"}


def stamp(db):
    return db.execute("select count(*), max(indexed_at), (select count(*) from segments) from assets").fetchone()


def load():
    db = core.connect()
    state["index"], state["stamp"], state["checked"] = core.Index(db), stamp(db), time.time()


def fresh_index():
    """Reload the in-memory index if the db changed (checked at most every RELOAD_EVERY s)."""
    if time.time() - state["checked"] > RELOAD_EVERY:
        state["checked"] = time.time()
        if stamp(core.connect()) != state["stamp"]:
            load()
    return state["index"]


@asynccontextmanager
async def lifespan(_app):
    core.query_model()  # CPU text model only; the GPU model lives in the indexer process
    load()
    yield


app = FastAPI(title="Safelight engine", lifespan=lifespan)
# The Tauri webview (dev server and bundled app) calls this API directly.
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:1420", "tauri://localhost",
                                                   "http://tauri.localhost"],
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Content-Type"])
app.add_middleware(GZipMiddleware, minimum_size=1024)  # timeline JSON compresses ~5x
app.include_router(library.router)
app.include_router(projects.router)
app.include_router(media.router)


@app.get("/health")
def health():
    idx = state["index"]
    return {"assets": len(idx.assets) if idx else 0, "reindex": state["reindex"]}


@app.get("/search")
def search(q: str = Query(min_length=1, max_length=500), k: int = Query(60, ge=1, le=500),
           kind: str | None = None):
    if kind and kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(KINDS)}")
    return fresh_index().search(q, k, kind)


@app.get("/recent")
def recent(offset: int = Query(0, ge=0), limit: int = Query(120, ge=1, le=500), kind: str | None = None):
    """Browse newest first, without a query."""
    if kind and kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(KINDS)}")
    kinds = {"image": ("image",), "video": ("video", "poster")}.get(kind, ("image", "video", "poster"))
    rows = core.connect().execute(
        f"select uuid, kind, path, duration, meta, created from assets where kind in ({','.join('?' * len(kinds))}) "
        "order by created desc limit ? offset ?", (*kinds, limit, offset)).fetchall()
    return [dict(zip(("uuid", "kind", "path", "duration", "meta", "created"), r)) for r in rows]


@app.get("/timeline")
def timeline(kind: str | None = None):
    """Every item, newest first, as compact columns. The app lays out the whole grid from this."""
    if kind and kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(KINDS)}")
    kinds = {"image": ("image",), "video": ("video", "poster")}.get(kind, ("image", "video", "poster"))
    rows = core.connect().execute(
        f"select uuid, kind, duration, created from assets where kind in ({','.join('?' * len(kinds))}) "
        "order by created desc", kinds).fetchall()
    return {"uuid": [r[0] for r in rows], "video": [r[1] != "image" for r in rows],
            "dur": [round(r[2] or 0) for r in rows], "created": [(r[3] or "")[:10] for r in rows]}


def preview_path(uuid):
    """Local preview for an indexed item. Only paths stored in the index are ever served."""
    row = core.connect().execute("select path from assets where uuid = ?", (uuid,)).fetchone()
    if not row or not row[0] or Path(row[0]).suffix.lower() not in THUMB_EXT or not Path(row[0]).is_file():
        raise HTTPException(404, "no local preview")
    return Path(row[0])


CACHE = {"Cache-Control": "max-age=604800, immutable"}


@app.get("/thumb/{uuid}")
def thumb(uuid: str):
    """Grid tile: ~400px JPEG (~25 KB), generated once and cached on disk."""
    src = preview_path(uuid)  # validates uuid against the index before touching the filesystem
    out = THUMBS / uuid[:2] / f"{uuid}.jpg"
    if not out.is_file():
        make_thumb(src, out)
    return FileResponse(out, media_type="image/jpeg", headers=CACHE)


@app.get("/full/{uuid}")
def full(uuid: str):
    """Viewer: the full local preview (~1024px)."""
    return FileResponse(preview_path(uuid), headers=CACHE)


def make_thumb(src, out):
    from PIL import Image, ImageOps
    out.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im.draft("RGB", (THUMB_PX * 2, THUMB_PX * 2))  # JPEG decodes at reduced size: much faster
        im = ImageOps.exif_transpose(im).convert("RGB")
        scale = THUMB_PX / min(im.size)  # shortest side = THUMB_PX, so square tiles stay sharp
        if scale < 1:
            im = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.LANCZOS)
        tmp = out.with_suffix(f".{threading.get_ident()}.tmp")  # per-thread name: parallel requests can't clobber each other
        im.save(tmp, "JPEG", quality=80, optimize=True)
        tmp.replace(out)  # atomic: a concurrent request never sees a half-written file


def run_reindex(since):
    try:
        cmd = [sys.executable, str(Path(__file__).parent / "index.py")]
        if since:
            cmd += ["--since", since]
        r = subprocess.run(cmd, capture_output=True, text=True)
        state["reindex"] = "idle" if r.returncode == 0 else f"failed: {r.stderr.strip()[-300:]}"
        load()
    finally:
        lock.release()


@app.post("/reindex", status_code=202)
def reindex(since: date | None = None):
    """Index new items only (already-indexed ones are skipped). Runs in the background."""
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "reindex already running")
    state["reindex"] = "running"
    threading.Thread(target=run_reindex, args=(since.isoformat() if since else None,), daemon=True).start()
    return {"reindex": "started"}


if __name__ == "__main__":
    # localhost only: this exposes your photo library's previews and file paths
    uvicorn.run(app, host="127.0.0.1", port=8765)
