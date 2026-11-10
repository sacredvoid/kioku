"""Local search API for the Kioku app. Keeps the model loaded so queries are fast.

  uv run server.py                        # http://127.0.0.1:8765
  curl "127.0.0.1:8765/search?q=dog&k=5"
  curl "127.0.0.1:8765/recent?limit=50"
  curl -X POST "127.0.0.1:8765/reindex?since=2026-11-01"
"""
import json
import logging
import logging.handlers
import re
import subprocess
import sys
import threading
import time
import tomllib
from contextlib import asynccontextmanager, closing
from datetime import date
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse

import core
import library
import projects
import media
import settings
# `uv run server.py` loads this file as __main__; register it as `server` too, so a lazy `import server`
# (albums.search) gets this running instance and its loaded index, not a second empty copy.
sys.modules.setdefault("server", sys.modules[__name__])
import albums  # noqa: E402
import captions
import edit
import moments
import sounds
import reference
import direction
import render

log = logging.getLogger("kioku")
LOG_FILE = core.DATA / "engine.log"
# the app polls these every 1-5 s (and loads hundreds of thumbs); successful hits are noise in the access log
QUIET = re.compile(r"^/(health|index/info|media/activity|collections|drafts|collections/\d+/(ask|export|render/(preview|final)|captions/job)|drafts/\d+/job|collections/\d+/captions/cues|collections/\d+/edit/audio/[^/]+/(peaks|file)|thumb/.+|full/.+|media/[^/]+/(proxy|sheet|scenes|strip))$")


class QuietPolls(logging.Filter):
    def filter(self, rec):
        try:
            _, method, path, _, status = rec.args
            return not (method == "GET" and int(status) < 400 and QUIET.match(path.split("?")[0]))
        except (TypeError, ValueError):
            return True


def setup_logging():
    """Engine log to the terminal and to ~/Kioku/engine.log (1 MB x 3, rotated)."""
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    core.DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    core.DATA.chmod(0o700)  # also tighten a folder that already existed
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
    root = logging.getLogger("kioku")
    root.setLevel(logging.INFO)
    root.handlers[:] = [fh, sh]
    root.propagate = False
    for name in ("uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addHandler(fh)
    logging.getLogger("uvicorn.access").addFilter(QuietPolls())


state = {"index": None, "reindex": "idle", "checked": 0.0, "stamp": None, "last_run": None}
lock = threading.Lock()
RELOAD_EVERY = 30  # seconds; picks up items a running indexer has added
THUMB_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
THUMBS = core.DATA / "thumbs"
THUMB_PX = 400  # tiles are ~170 css px; 400 covers 2x retina with room to spare
KINDS = {"image", "video"}


def stamp(db):
    return db.execute("select count(*), max(indexed_at), (select count(*) from segments) from assets").fetchone()


def load():
    with closing(core.connect()) as db:
        state["index"], state["stamp"], state["checked"] = core.Index(db), stamp(db), time.time()


def one(sql, args=()):
    """Run one query on a fresh connection and close it (the server leaked connections until it ran out of files)."""
    with closing(core.connect()) as db:
        return db.execute(sql, args).fetchall()


rebuilding = threading.Lock()  # held while a background reload runs: at most one at a time


def reload_if_changed():
    try:
        with closing(core.connect()) as db:
            changed = stamp(db) != state["stamp"]
        if changed:
            load()
    except Exception:
        log.exception("index reload failed; keeping the old index")
    finally:
        rebuilding.release()


def fresh_index():
    """Current index. Every RELOAD_EVERY s, check the db in a background thread and swap in a rebuilt index when
    it changed; requests keep using the old one meanwhile."""
    if time.time() - state["checked"] > RELOAD_EVERY and rebuilding.acquire(blocking=False):
        state["checked"] = time.time()
        threading.Thread(target=reload_if_changed, daemon=True).start()
    return state["index"]


@asynccontextmanager
async def lifespan(_app):
    setup_logging()  # after uvicorn has configured its own loggers
    log.info("engine starting; log file %s", LOG_FILE)
    core.query_model()  # CPU text model only; the GPU model lives in the indexer process
    load()
    log.info("ready: %d items indexed", len(state["index"].assets))
    yield


app = FastAPI(title="Kioku engine", lifespan=lifespan)
# The Tauri webview (dev server and bundled app) calls this API directly.
ORIGINS = {"http://localhost:1420", "tauri://localhost", "http://tauri.localhost"}
app.add_middleware(CORSMiddleware, allow_origins=sorted(ORIGINS),
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Content-Type"])


@app.middleware("http")
async def app_origin_only(request: Request, call_next):
    """CORS only hides responses; a web page you visit can still *send* a simple POST here (start an Ask or an
    export). Browsers mark such requests with Origin, so refuse any origin that isn't the app. curl and the MCP
    server send no Origin and pass."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in ORIGINS:
        return JSONResponse(status_code=403, content={"detail": f"origin {origin} not allowed"})
    return await call_next(request)


# DNS rebinding: a web page whose domain resolves to 127.0.0.1 would otherwise read your library same-origin
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
# level 1: on localhost the bytes are nearly free; level 9 spent ~110 ms squeezing the 3.5 MB timeline
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=1)
app.include_router(library.router)
app.include_router(projects.router)
app.include_router(media.router)
app.include_router(moments.router)
app.include_router(settings.router)
app.include_router(edit.router)
app.include_router(render.router)
app.include_router(captions.router)
app.include_router(albums.router)
app.include_router(sounds.router)
app.include_router(reference.router)
app.include_router(direction.router)
app.add_exception_handler(RequestValidationError, library.validation_error)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """Any bug or unexpected failure: log the traceback, give the app a readable JSON message instead of a bare 500."""
    log.exception("%s %s failed", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"Engine error ({type(exc).__name__}): {exc}"[:500]})


# The version this process started with; the app replaces an engine whose version differs from its own.
VERSION = tomllib.loads((Path(__file__).parent / "pyproject.toml").read_text())["project"]["version"]


@app.get("/health")
def health():
    idx = fresh_index()
    out = {"assets": len(idx.assets) if idx else 0, "reindex": state["reindex"], "version": VERSION}
    if state["reindex"] == "running":
        out["progress"] = index_progress()
    return out


def index_progress():
    """This run's items done / total, from the progress.json index.py writes (status.py reads it the same way).
    total is None until index.py has listed what's new."""
    try:
        run = json.loads((core.DATA / "progress.json").read_text())
    except (OSError, ValueError):
        return {"done": 0, "total": None}
    if run.get("started", 0) < (state["last_run"] or {}).get("started", 0):
        return {"done": 0, "total": None}  # the previous run's file: this run hasn't listed its items yet
    done = one("select count(*) from assets where indexed_at >= ?", (run["started"],))[0][0]
    return {"done": done, "total": run.get("total")}


@app.get("/index/info")
def index_info():
    """What's in the search index: counts by kind, date span, when it last changed, which model and image detail."""
    # one scan of assets (issue #8: was four)
    rows = one("select kind, count(*), max(created), min(created), max(indexed_at), sum(error is not null) from assets group by kind")
    kinds = {r[0]: r[1] for r in rows}
    pick = lambda i, f: f([r[i] for r in rows if r[i] is not None], default=None)  # noqa: E731
    newest, oldest, last = pick(2, max), pick(3, min), pick(4, max)
    return {"assets": sum(kinds.values()), "kinds": kinds, "newest": newest, "oldest": oldest, "last_indexed": last,
            "failed": sum(r[5] or 0 for r in rows),
            "model": core.MODEL_ID, "image_tokens": core.IMAGE_TOKENS,
            "reindex": state["reindex"], "last_run": state["last_run"]}


@app.get("/search")
def search(q: str = Query(min_length=1, max_length=500), k: int = Query(60, ge=1, le=500),
           kind: str | None = None, date: str | None = Query(None, pattern=r"^\d{4}(-\d{2})?$")):
    if kind and kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(KINDS)}")
    return fresh_index().search(q, k, kind, date)


@app.get("/dates")
def dates(kind: str | None = None):
    """Items per month ('YYYY-MM' -> count), for the year and month filters. Undated items are left out."""
    if kind and kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(KINDS)}")
    kinds = {"image": ("image",), "video": ("video", "poster")}.get(kind, ("image", "video", "poster"))
    rows = one(f"select substr(created, 1, 7), count(*) from assets where kind in ({','.join('?' * len(kinds))}) "
               "and created is not null and created != '' group by 1 order by 1 desc", kinds)
    return {ym: n for ym, n in rows}


@app.get("/recent")
def recent(offset: int = Query(0, ge=0), limit: int = Query(120, ge=1, le=500), kind: str | None = None):
    """Browse newest first, without a query."""
    if kind and kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(KINDS)}")
    kinds = {"image": ("image",), "video": ("video", "poster")}.get(kind, ("image", "video", "poster"))
    rows = one(
        f"select uuid, kind, path, duration, meta, created from assets where kind in ({','.join('?' * len(kinds))}) "
        "order by created desc limit ? offset ?", (*kinds, limit, offset))
    return [dict(zip(("uuid", "kind", "path", "duration", "meta", "created"), r)) for r in rows]


@app.get("/timeline")
def timeline(kind: str | None = None):
    """Every item, newest first, as compact columns. The app lays out the whole grid from this."""
    if kind and kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(KINDS)}")
    kinds = {"image": ("image",), "video": ("video", "poster")}.get(kind, ("image", "video", "poster"))
    rows = one(
        f"select uuid, kind, duration, created from assets where kind in ({','.join('?' * len(kinds))}) "
        "order by created desc", kinds)
    # JSONResponse directly: FastAPI's jsonable_encoder walks every element (~60 ms for 58k items)
    return JSONResponse({"uuid": [r[0] for r in rows], "video": [r[1] != "image" for r in rows],
                         "dur": [round(r[2] or 0) for r in rows], "created": [(r[3] or "")[:10] for r in rows]})


def preview_path(uuid):
    """Local preview for an indexed item. Only paths stored in the index are ever served."""
    row = next(iter(one("select path from assets where uuid = ?", (uuid,))), None)
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
        log.info("reindex started (since %s)", since or "beginning")
        # count only this run's date range, so a separate indexer running at the same time doesn't inflate "added"
        count = lambda: one("select count(*) from assets where created >= ?", (since or "",))[0][0]
        before = count()
        run = state["last_run"] = {"since": since, "started": time.time(), "finished": None, "added": None}
        r = subprocess.run(cmd, capture_output=True, text=True)
        run["finished"] = time.time()
        run["added"] = count() - before
        state["reindex"] = "idle" if r.returncode == 0 else f"failed: {r.stderr.strip()[-300:]}"
        if r.returncode:
            log.error("reindex failed (exit %d): %s", r.returncode, r.stderr.strip()[-2000:])
        load()
    except Exception as e:
        state["reindex"] = f"failed: {e}"
        log.exception("reindex crashed")
    finally:
        lock.release()


@app.post("/reindex", status_code=202)
def reindex(since: date | None = None, newer: bool = False):
    """Index new items only (already-indexed ones are skipped). Runs in the background.
    newer=true: only items from the newest indexed item's day onward (the "fetch new photos" button)."""
    if newer:
        newest = one("select max(created) from assets")[0][0]
        since = date.fromisoformat(newest[:10]) if newest else since
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "reindex already running")
    state["reindex"] = "running"
    threading.Thread(target=run_reindex, args=(since.isoformat() if since else None,), daemon=True).start()
    return {"reindex": "started"}


if __name__ == "__main__":
    # localhost only: this exposes your photo library's previews and file paths
    uvicorn.run(app, host="127.0.0.1", port=8765)
