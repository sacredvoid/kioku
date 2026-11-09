"""Free music and sound effects from Openverse, plus the user's own files under ~/Music.

Every byte goes through the engine: the app's CSP only allows the engine, and a download URL is never taken from
the client. For a free sound the engine re-reads the Openverse record server-side and only fetches that record's own
https URL, capped at 50 MB, then checks with ffprobe that it really is audio before it lands in ~/Kioku/sounds.
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

import core
import edit
import settings

router = APIRouter()
log = logging.getLogger("kioku.sounds")

OV = "https://api.openverse.org/v1"
UA = {"User-Agent": "Kioku/0.1 (personal video editor)"}
SOUNDS = core.DATA / "sounds"
MUSIC = Path(os.environ.get("KIOKU_MUSIC") or Path.home() / "Music")
MAX_BYTES = 50 * 1024 * 1024
TIMEOUT = 60
TTL = 600  # search / record / waveform cache, seconds
ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
EXTS = {e.lstrip(".") for e in edit.AUDIO_EXT}  # what the timeline can mix
# Openverse filetype spellings -> file extension (Jamendo reports its MP3s as mp32). ffprobe checks the real content.
# ponytail: ogg/flac/opus results are dropped; transcode to m4a on download if too much good music is missing
FILETYPE = {"mp32": "mp3", "mpeg": "mp3", "aif": "aiff", "x-wav": "wav", "wave": "wav"}
LOCAL_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".aiff"}
# license sets per setting; share-alike and no-derivatives are never offered (a video edit is a derivative)
LICENSES = {"commercial": ["cc0", "pdm", "by"], "noncommercial": ["cc0", "pdm", "by", "by-nc"]}

SCHEMA = """
create table if not exists sounds(
  id text primary key, title text, creator text, license text not null, license_version text, license_url text,
  attribution text, provider text, source_url text, path text not null, added real);
"""

_cache: dict[str, tuple[float, object]] = {}
_dl_lock = threading.Lock()  # ponytail: one download at a time; per-id locks if many previews start together


def _cached(key, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    if len(_cache) > 512:
        _cache.clear()
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _ov(path, params=None):
    """GET an Openverse JSON endpoint, with errors the app can show."""
    url = f"{OV}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise HTTPException(429, "Openverse rate limit reached; try again in a minute")
        if e.code == 404:
            raise HTTPException(404, "that sound is no longer on Openverse")
        raise HTTPException(502, f"Openverse error {e.code}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise HTTPException(502, f"could not reach Openverse ({e})")


def allowed() -> list[str]:
    return LICENSES[settings.get().sound_licenses]


def norm(r: dict) -> dict:
    return {"id": r["id"], "title": r.get("title") or "Untitled", "creator": r.get("creator") or "unknown",
            "license": r.get("license", ""), "license_version": r.get("license_version"),
            "license_url": r.get("license_url"), "attribution": r.get("attribution") or "",
            "provider": r.get("provider") or r.get("source"), "duration_s": round((r.get("duration") or 0) / 1000, 1),
            "tags": [t["name"] for t in (r.get("tags") or []) if t.get("name")][:8],
            "filetype": FILETYPE.get(ft := (r.get("filetype") or "").lower(), ft), "preview": f"/sounds/{r['id']}/preview",
            "needs_credit": r.get("license", "").startswith("by")}


@router.get("/sounds/search")
def search(q: str = Query(..., min_length=1, max_length=200), kind: str = "music", max_dur: float | None = None,
           page: int = Query(1, ge=1, le=20)):
    if kind not in ("music", "sfx"):
        raise HTTPException(422, "kind must be music or sfx")
    lic = allowed()
    # Freesound records carry no category, so category=sound_effect finds nothing; ask Freesound itself for effects
    where = {"category": "music"} if kind == "music" else {"source": "freesound"}
    params = {"q": q, **where, "license": ",".join(lic),
              "page": page, "page_size": 20}  # anonymous max; more needs an API key (401)
    data = _cached(json.dumps(params, sort_keys=True), lambda: _ov("/audio/", params))
    out = [norm(r) for r in data.get("results", [])]
    # Openverse's license filter is trusted but re-checked; formats the timeline can't mix are dropped
    out = [s for s in out if s["license"] in lic and s["filetype"] in EXTS]
    if max_dur:
        out = [s for s in out if 0 < s["duration_s"] <= max_dur]
    return {"results": out, "page": page, "more": page < (data.get("page_count") or 1), "licenses": lic}


def record(sid: str) -> dict:
    if not ID.match(sid):
        raise HTTPException(422, "not an Openverse sound id")
    return _cached(f"rec:{sid}", lambda: _ov(f"/audio/{sid}/"))


def download(sid: str) -> Path:
    """The sound as a local file in ~/Kioku/sounds, fetched once from its own Openverse record."""
    r = record(sid)
    s = norm(r)
    if s["license"] not in allowed():
        raise HTTPException(422, f"license {s['license']} is not allowed by your settings")
    if s["filetype"] not in EXTS:
        raise HTTPException(422, f"{s['filetype'] or 'unknown'} files can't be mixed")
    url = r.get("url") or ""
    host = urllib.parse.urlparse(url)
    if host.scheme != "https" or not host.hostname:
        raise HTTPException(422, "sound has no https download")
    dest = SOUNDS / f"{sid}.{s['filetype']}"
    with _dl_lock:
        if dest.is_file():
            return dest
        SOUNDS.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as resp:
                final = urllib.parse.urlparse(resp.geturl())
                if final.scheme != "https":  # a redirect may change host (CDNs do) but never drop https
                    raise HTTPException(422, "download redirected away from https")
                if int(resp.headers.get("Content-Length") or 0) > MAX_BYTES:
                    raise HTTPException(422, "sound is larger than 50 MB")
                n = 0
                with open(tmp, "wb") as f:
                    while chunk := resp.read(1 << 16):
                        n += len(chunk)
                        if n > MAX_BYTES:
                            raise HTTPException(422, "sound is larger than 50 MB")
                        f.write(chunk)
            # tmp ends in .part, so probe a copy-free view by renaming into place only after it checks out
            probe = tmp.with_name(f"{sid}.probe.{s['filetype']}")
            tmp.replace(probe)
            try:
                edit.audio_info(str(probe))  # raises 422 when it isn't readable audio
            except HTTPException:
                probe.unlink(missing_ok=True)
                raise
            probe.replace(dest)  # atomic: a half download is never served
        except (urllib.error.URLError, TimeoutError) as e:
            raise HTTPException(502, f"download failed ({e})")
        finally:
            tmp.unlink(missing_ok=True)
    return dest


@router.get("/sounds/{sid}/preview")
def preview(sid: str):
    return FileResponse(download(sid))  # Range supported, so <audio> can seek


@router.get("/sounds/{sid}/waveform")
def waveform(sid: str):
    if not ID.match(sid):
        raise HTTPException(422, "not an Openverse sound id")
    pts = _cached(f"wave:{sid}", lambda: _ov(f"/audio/{sid}/waveform/").get("points") or [])
    step = max(1, len(pts) // 120)
    return {"points": [round(max(pts[i:i + step]), 3) for i in range(0, len(pts), step)]}


def _db(c):
    c.executescript(SCHEMA)


class SoundAdd(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cid: int
    start: float = Field(0, ge=0)
    track_id: str | None = None
    volume: float | None = None
    fade_in: float | None = None
    fade_out: float | None = None


@router.post("/sounds/{sid}/add")
def add(sid: str, body: SoundAdd):
    path = download(sid).resolve()  # the spec stores resolved paths; credits match on them
    s = norm(record(sid))
    with core.connect() as c:
        _db(c)
        c.execute("insert or replace into sounds values (?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, s["title"], s["creator"], s["license"], s["license_version"], s["license_url"],
                   s["attribution"], s["provider"], record(sid).get("foreign_landing_url"), str(path), time.time()))
    tid = body.track_id or f"s{sid[:8]}"
    put = edit.AudioPut(path=str(path), start=body.start, volume=body.volume, fade_in=body.fade_in, fade_out=body.fade_out)
    out = edit.put_audio(body.cid, tid, put)
    return {**out, "track_id": tid, "credit": s["attribution"] if s["needs_credit"] else None}


def credits(cid: int) -> list[dict]:
    """Free sounds used by this project's audio tracks, with their credit lines."""
    spec, _ = edit.load(cid)
    paths = {str(Path(t["path"]).resolve()): t["id"] for t in spec["audio"]}
    if not paths:
        return []
    with core.connect() as c:
        _db(c)
        rows = c.execute(f"select title, creator, license, license_version, attribution, provider, source_url, path "
                         f"from sounds where path in ({','.join('?' * len(paths))})", list(paths)).fetchall()
    return [{"track_id": paths[r[7]], "title": r[0], "creator": r[1], "license": r[2], "license_version": r[3],
             "attribution": r[4], "provider": r[5], "source_url": r[6], "needs_credit": (r[2] or "").startswith("by")}
            for r in rows]


def credits_text(cid: int) -> str:
    lines = [c["attribution"] + (f" Source: {c['source_url']}" if c["source_url"] else "") for c in credits(cid)
             if c["needs_credit"]]  # public domain needs no line; CC BY does
    return ("Music and sound credits\n\n" + "\n".join(lines) + "\n") if lines else ""


@router.get("/collections/{cid}/credits")
def get_credits(cid: int):
    cs = credits(cid)
    return {"credits": cs, "text": credits_text(cid), "required": any(c["needs_credit"] for c in cs)}


def write_credits(cid: int, folder: Path, stem: str = "credits") -> str | None:
    """credits.txt next to an export or render when any track needs attribution (CC BY)."""
    if not any(c["needs_credit"] for c in credits(cid)):
        return None
    p = Path(folder) / f"{stem}.txt"
    p.write_text(credits_text(cid))
    return str(p)


# ---------- your own files under ~/Music

_local: dict[str, tuple[float, dict | None]] = {}  # path -> (mtime, info or None when unusable)
_local_at = 0.0


def _probe(p: Path) -> dict | None:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=title,artist,album"
                            ":stream=codec_type,codec_name,codec_tag_string", "-of", "json", str(p)],
                           capture_output=True, text=True, timeout=15)
        j = json.loads(r.stdout or "{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    audio = [s for s in j.get("streams", []) if s.get("codec_type") == "audio"]
    # FairPlay-protected audio shows up as codec tag drms (or no decodable codec): skip it
    if not audio or any(s.get("codec_tag_string") == "drms" or not s.get("codec_name") for s in audio):
        return None
    f = j.get("format", {})
    tags = {k.lower(): v for k, v in (f.get("tags") or {}).items()}
    try:
        dur = float(f.get("duration") or 0)
    except ValueError:
        dur = 0
    if dur <= 0:
        return None
    return {"path": str(p), "title": tags.get("title") or p.stem, "artist": tags.get("artist") or "",
            "album": tags.get("album") or "", "duration_s": round(dur, 1)}


def local_index(force=False) -> list[dict]:
    """Every usable audio file under ~/Music. Re-walked at most every 5 minutes; only new or changed files are probed."""
    global _local_at
    if force or time.time() - _local_at > 300:
        seen = set()
        root = MUSIC.resolve()
        # ponytail: probes serially and caps at 5000 files; a background indexer if libraries are bigger
        for p in sorted(root.rglob("*"))[:20000]:
            if p.suffix.lower() not in LOCAL_EXTS or not p.is_file() or len(seen) >= 5000:
                continue
            key, mt = str(p), p.stat().st_mtime
            seen.add(key)
            if key not in _local or _local[key][0] != mt:
                _local[key] = (mt, _probe(p))
        for k in set(_local) - seen:
            del _local[k]
        _local_at = time.time()
    return [v[1] for v in _local.values() if v[1]]


def in_music(path: str) -> Path:
    """path resolved, or 422 unless it is a usable audio file inside ~/Music."""
    try:
        p = Path(path).expanduser().resolve(strict=True)
    except OSError:
        raise HTTPException(404, "file not found")
    if not p.is_relative_to(MUSIC.resolve()) or p.suffix.lower() not in LOCAL_EXTS or not p.is_file():
        raise HTTPException(422, "only audio files inside ~/Music can be browsed")
    return p


@router.get("/sounds/local")
def local(q: str = "", limit: int = Query(60, ge=1, le=500)):
    if not MUSIC.is_dir():
        return {"results": [], "root": str(MUSIC), "message": "no ~/Music folder"}
    words = q.lower().split()
    hits = [s for s in local_index() if all(w in f"{s['title']} {s['artist']} {s['album']} {Path(s['path']).name}".lower()
                                            for w in words)]
    hits.sort(key=lambda s: (s["artist"].lower(), s["title"].lower()))
    return {"results": [{**s, "id": hashlib.sha1(s["path"].encode()).hexdigest()[:16],
                         "preview": f"/sounds/local/file?path={urllib.parse.quote(s['path'])}"} for s in hits[:limit]],
            "root": str(MUSIC)}


@router.get("/sounds/local/file")
def local_file(path: str):
    return FileResponse(in_music(path))
