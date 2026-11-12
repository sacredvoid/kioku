"""Free music and sound effects (Openverse, plus Jamendo and Incompetech for music, see music_sources.py), and the
user's own files under ~/Music, searchable by feel.

Every byte goes through the engine: the app's CSP only allows the engine, and a download URL is never taken from
the client. For a free sound the engine re-reads the Openverse record server-side and only fetches that record's own
https URL, capped at 50 MB, then checks with ffprobe that it really is audio before it lands in ~/Kioku/sounds.
"""
import concurrent.futures
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
import media
import music
import music_sources
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
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")  # Openverse
ID = re.compile(r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|jm-\d{1,12}|ic-[0-9a-f]{12})$")  # + Jamendo, Incompetech
ANALYZE_BUDGET = 45  # seconds a search waits for candidate analysis; unfinished ones carry on in the background and cache
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="shortlist")
PROVIDERS = ("all", "openverse", "jamendo", "incompetech")
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
            "needs_credit": r.get("license", "").startswith("by"),
            # what the source says (None = it does not say). `text` only feeds ranking; search drops it before replying.
            "tempo": r.get("bpm"), "instrumental": r.get("instrumental"), "speed": r.get("speed"), "text": r.get("text"),
            "searched": r.get("provider") != "incompetech"}  # matched the query server side (Incompetech is matched here)


def _openverse(q, kind, page, lic):
    # Freesound records carry no category, so category=sound_effect finds nothing; ask Freesound itself for effects
    where = {"category": "music"} if kind == "music" else {"source": "freesound"}
    params = {"q": q, **where, "license": ",".join(lic),
              "page": page, "page_size": 20}  # anonymous max; more needs an API key (401)
    data = _cached(json.dumps(params, sort_keys=True), lambda: _ov("/audio/", params))
    return [norm(r) for r in data.get("results", [])], data


def _analyse(sid):
    """Music-map summary of one candidate: downloads it once (capped, checked) and analyses it; both are cached."""
    return music.summary(music.analyze(download(sid)))


def _shortlist(out, n, notes):
    """Attach `analysis` (tempo, energy curve, drop, length) to the first n results. Bounded: n at most 5 (the caller
    clamps), 3 at a time, and the search waits at most ANALYZE_BUDGET seconds; anything unfinished keeps running in
    the background and is cached, so the same search again returns it."""
    futs = {_pool.submit(_analyse, s["id"]): s for s in out[:n]}
    done, _ = concurrent.futures.wait(futs, timeout=ANALYZE_BUDGET)
    for f, s in futs.items():
        if f not in done:
            s["analysis_error"] = "still analysing; search again in a minute and it will be there"
            continue
        try:
            s["analysis"] = f.result()
        except HTTPException as e:
            s["analysis_error"] = str(e.detail)
        except Exception as e:  # analysis is optional: a model or decode failure must not sink the search
            log.warning("analysis of %s failed: %s", s["id"], e)
            s["analysis_error"] = str(e) or "analysis failed"
    if any("analysis_error" in s for s in out[:n]):
        notes.append("some candidates could not be analysed (see analysis_error); judge those by their tags and length")


@router.get("/sounds/search")
def search(q: str = Query(..., min_length=1, max_length=200), kind: str = "music", max_dur: float | None = None,
           page: int = Query(1, ge=1, le=20), provider: str = "all",
           tempo_min: float | None = Query(None, ge=20, le=300), tempo_max: float | None = Query(None, ge=20, le=300),
           instrumental: bool | None = None, target_seconds: float | None = Query(None, gt=0, le=3600),
           analyze: int = Query(0, ge=0, le=5), limit: int = Query(20, ge=1, le=40)):
    """Music search over Openverse, Jamendo (needs a free key) and Incompetech, ranked by fit to the request. With
    analyze=N the top N get a music-map summary so a pick is made on data. Sound effects only come from Openverse."""
    if kind not in ("music", "sfx"):
        raise HTTPException(422, "kind must be music or sfx")
    if provider not in PROVIDERS:
        raise HTTPException(422, f"provider must be one of {', '.join(PROVIDERS)}")
    lic = allowed()
    notes: list[str] = []
    more = False
    if kind == "sfx" or provider == "openverse":
        out, data = _openverse(q, kind, page, lic)
        more = page < (data.get("page_count") or 1)
    else:
        out, errs, live = [], [], 0
        want = lambda p: provider in ("all", p)  # noqa: E731
        if want("jamendo") and page == 1:
            try:
                out += [norm(r) for r in music_sources.jamendo_search(q, tempo_min, tempo_max, instrumental, max_dur)]
                live += 1
            except music_sources.SourceError as e:
                (notes if str(e) == music_sources.NEED_KEY else errs).append(str(e))
        if want("incompetech") and page == 1:
            try:
                out += [norm(r) for r in music_sources.incompetech_search(q, tempo_min, tempo_max)]
                live += 1
            except music_sources.SourceError as e:
                errs.append(str(e))
        if want("openverse"):
            try:
                ov, data = _openverse(q, kind, page, lic)
                out += ov
                more, live = page < (data.get("page_count") or 1), live + 1
            except HTTPException as e:
                errs.append(str(e.detail))
        if not live and not out and errs:
            raise HTTPException(502, "; ".join([*errs, *notes]) or "no music source answered")
        notes += [f"a source failed: {e}" for e in errs]
    # every source's license is re-checked here; formats the timeline can't mix are dropped
    out = [s for s in out if s["license"] in lic and s["filetype"] in EXTS]
    if max_dur:
        out = [s for s in out if 0 < s["duration_s"] <= max_dur]
    if kind == "music":
        out = music_sources.rank(out, q, tempo_min, tempo_max, instrumental, target_seconds)
    out = out[:limit]
    if analyze and kind == "music" and out:
        _shortlist(out, analyze, notes)
        for s in out:
            a = s.get("analysis")
            if a and s["tempo"] is None:
                s["tempo"] = a["tempo"]  # the source did not say; the analysis did
        out = music_sources.rank(out, q, tempo_min, tempo_max, instrumental, target_seconds)
    for s in out:
        s.pop("text", None)
        s.pop("searched", None)
        if target_seconds and s["duration_s"] and s["duration_s"] < target_seconds * 0.9:
            s["length_note"] = f"{s['duration_s']:.0f}s is shorter than the {target_seconds:.0f}s edit; it would play once and end"
    return {"results": out, "page": page, "more": more, "licenses": lic, "notes": notes}


def record(sid: str) -> dict:
    """The source's own record of a sound, re-read server side. Openverse ids are UUIDs; jm- and ic- are Jamendo and Incompetech."""
    if not ID.match(sid):
        raise HTTPException(422, "not a known sound id")
    if sid[:3] in ("jm-", "ic-"):
        fn = music_sources.jamendo_record if sid[:3] == "jm-" else music_sources.incompetech_record
        try:
            return _cached(f"rec:{sid}", lambda: fn(sid))
        except music_sources.SourceError as e:
            raise HTTPException(404 if "gone" in str(e) or "not in" in str(e) else 502, str(e))
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
    with media.lock_for(f"sound:{sid}"):  # per sound: the shortlist pool downloads in parallel
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
        raise HTTPException(422, "not a known sound id")
    if not UUID.match(sid):
        return {"points": []}  # Jamendo and Incompetech have no waveform endpoint; the row works without one
    pts =_cached(f"wave:{sid}", lambda: _ov(f"/audio/{sid}/waveform/").get("points") or [])
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
    fit: bool = False
    target_seconds: float | None = None
    drop_at: float | None = None


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
    put = edit.AudioPut(path=str(path), start=body.start, volume=body.volume, fade_in=body.fade_in, fade_out=body.fade_out,
                       fit=body.fit, target_seconds=body.target_seconds, drop_at=body.drop_at)
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


# ---------- a named song or a link, from YouTube (or any site yt-dlp reads)

YT = SOUNDS / "yt"
TRACK_MIN_S, TRACK_MAX_S = 60, 12 * 60  # a song, not a 90 min live set or a 10 s short


class TrackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str = Field(min_length=1, max_length=300)


def _ytdlp(args: list[str], timeout: int) -> str:
    import reference  # its yt-dlp lookup handles the app's bare PATH
    exe = reference.ytdlp_bin()
    if not exe:
        raise HTTPException(422, "finding songs needs yt-dlp: run `brew install yt-dlp deno`, or give a file instead")
    try:
        r = subprocess.run([exe, "--no-playlist", "--no-progress", *args], capture_output=True, text=True,
                           timeout=timeout, env=reference.tool_env())
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "YouTube took too long, try again")
    if r.returncode != 0:
        why = (r.stderr.strip().splitlines() or ["yt-dlp failed"])[-1]
        if "403" in r.stderr or "JavaScript runtime" in r.stderr:
            why = "YouTube refused this yt-dlp (out of date or no JavaScript runtime). Run `brew install yt-dlp deno`"
        raise HTTPException(502, f"could not get the song: {why[:300]}")
    return r.stdout


def _yt_search(q: str) -> dict:
    j = json.loads(_ytdlp(["--flat-playlist", "-J", "--", f"ytsearch15:{q}"], 60) or "{}")
    out = []
    for e in j.get("entries") or []:
        d = e.get("duration")
        if not e.get("id") or "/watch?" not in (e.get("url") or "") or not d or not TRACK_MIN_S <= d <= TRACK_MAX_S:
            continue  # channels, playlists, live sets, shorts
        ch = e.get("channel") or e.get("uploader") or ""
        out.append({"url": e["url"], "title": e.get("title") or "", "channel": ch, "seconds": round(d),
                    "official_audio": ch.endswith(" - Topic")})  # YouTube's auto "Artist - Topic" channels = studio audio
    return {"candidates": out[:8],
            "notes": "" if out else f"no 1 to 12 minute songs found for “{q}”; try artist and song title"}


def fetch_track(url: str) -> dict:
    """Audio of one link as m4a in ~/Kioku/sounds/yt, downloaded once. Returns path, title, channel, seconds."""
    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    dest, meta = YT / f"{key}.m4a", YT / f"{key}.json"
    with media.lock_for(f"yt:{key}"):
        if dest.is_file() and meta.is_file():
            return json.loads(meta.read_text())
        YT.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmpl = str(YT / f"{key}.part-dl.%(ext)s")
        try:
            printed = _ytdlp(["-f", "bestaudio[ext=m4a]/bestaudio", "-x", "--audio-format", "m4a",
                              "--match-filter", f"duration<={TRACK_MAX_S * 2} & !is_live", "--max-filesize", "100M",
                              "-o", tmpl, "--print", "after_move:%(title)s\t%(channel,uploader)s\t%(webpage_url)s",
                              "--no-simulate", "--", url], 600)
            got = next((f for f in YT.glob(f"{key}.part-dl.*") if f.suffix == ".m4a"), None)
            if not got:
                raise HTTPException(422, f"no audio downloaded: longer than {TRACK_MAX_S * 2 // 60} min, live, or not a video")
            edit.audio_info(str(got))  # 422 unless it is readable audio
            info = _probe(got) or {}
            got.replace(dest)
        finally:
            for f in YT.glob(f"{key}.part-dl.*"):
                f.unlink(missing_ok=True)
        title, channel, page = ((printed.strip().splitlines() or [""])[-1].split("\t") + ["", ""])[:3]
        res = {"path": str(dest.resolve()), "title": title, "channel": channel, "url": page or url,
               "seconds": info.get("duration_s")}
        meta.write_text(json.dumps(res))
        return res


@router.post("/sounds/track")
def track(body: TrackBody):
    """Text: up to 8 song-length YouTube candidates (nothing downloaded). A link: its audio, ready for set_audio."""
    import reference
    q = body.q.strip()
    return fetch_track(q) if reference.is_link(q) else _yt_search(q)


# ---------- your own files under ~/Music

_local: dict[str, tuple[float, dict | None]] = {}  # path -> (mtime, info or None when unusable)
_local_at = 0.0
_local_lock = threading.Lock()


def _probe(p: Path) -> dict | None:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags"
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
    try:  # a BPM tag is the artist's own number, better than our estimate (ID3 TBPM, m4a tmpo)
        bpm = float(next((tags[k] for k in ("bpm", "tbpm", "tmpo") if tags.get(k)), 0)) or None
    except ValueError:
        bpm = None
    return {"path": str(p), "title": tags.get("title") or p.stem, "artist": tags.get("artist") or "",
            "album": tags.get("album") or "", "genre": tags.get("genre") or "", "bpm_tag": bpm, "duration_s": round(dur, 1)}


def local_index(force=False) -> list[dict]:
    """Every usable audio file under ~/Music. Re-walked at most every 5 minutes; only new or changed files are probed."""
    global _local_at
    with _local_lock:  # one walk at a time: overlapping requests used to re-probe the same files in parallel
        if force or time.time() - _local_at > 300:
            _walk_music()
            _local_at = time.time()
        return [v[1] for v in list(_local.values()) if v[1]]


def _walk_music():
    """Probe new or changed files under ~/Music into _local; caller holds _local_lock."""
    seen = set()
    root = MUSIC.resolve()
    # ponytail: probes serially and caps at 5000 files; a background indexer if libraries are bigger
    for p in sorted(root.rglob("*"))[:20000]:
        if p.suffix.lower() not in LOCAL_EXTS or not p.is_file() or len(seen) >= 5000:
            continue
        key, mt = str(p), p.stat().st_mtime
        seen.add(key)
        if key not in _local or _local[key][0] != mt:
            info = _probe(p)
            if info and (f := music.cached_quick(p)):
                info["feat"] = f  # computed in an earlier session
            _local[key] = (mt, info)
    for k in set(_local) - seen:
        del _local[k]


def in_music(path: str) -> Path:
    """path resolved, or 422 unless it is a usable audio file inside ~/Music."""
    try:
        p = Path(path).expanduser().resolve(strict=True)
    except OSError:
        raise HTTPException(404, "file not found")
    if not p.is_relative_to(MUSIC.resolve()) or p.suffix.lower() not in LOCAL_EXTS or not p.is_file():
        raise HTTPException(422, "only audio files inside ~/Music can be browsed")
    return p


HIGH = {"upbeat", "energetic", "driving", "hype", "lively", "punchy", "intense", "party", "workout", "pumping"}
LOW = {"calm", "chill", "relaxing", "relaxed", "mellow", "soft", "quiet", "gentle", "peaceful", "ambient", "sleepy"}
FAST, SLOW = {"fast", "uptempo", "quick"}, {"slow", "downtempo"}
FILLER = {"music", "song", "songs", "track", "tracks", "a", "the", "for", "some", "and", "with"}
BG_INDEX = True  # tests switch the background thread off and call index_features() themselves
_idx_lock = threading.Lock()


def _feel(q: str):
    """(plain words, wanted feel, bpm) from a query: 'upbeat summer 120 bpm' -> (['summer'], {'high'}, 120.0)."""
    m = re.search(r"(\d{2,3})\s*bpm", q, re.I)
    bpm = float(m.group(1)) if m else None
    if m:
        q = q[:m.start()] + " " + q[m.end():]
    words = [w for w in re.findall(r"[\w'-]+", q.lower()) if w not in FILLER]
    feel = {n for n, ws in (("high", HIGH), ("low", LOW), ("fast", FAST), ("slow", SLOW)) if ws & set(words)}
    return [w for w in words if not (w in HIGH | LOW | FAST | SLOW)], feel, bpm


def _tempo(s):
    """(bpm, 'tag' | 'estimate') of a library file, or (None, None) while unanalysed."""
    f = s.get("feat") or {}
    return (s["bpm_tag"], "tag") if s.get("bpm_tag") else (f.get("tempo"), "estimate") if f.get("tempo") else (None, None)


def index_features(max_files: int | None = None) -> int:
    """Quick tempo/energy for library files that have none yet, one at a time (cached on disk, so each file once per
    change). Returns how many were done. Does nothing when another pass is already running."""
    if not _idx_lock.acquire(blocking=False):
        return 0
    try:
        todo = [s for s in local_index() if "feat" not in s][:max_files]
        for s in todo:
            s["feat"] = music.library_features(s["path"], s["duration_s"])
        return len(todo)
    finally:
        _idx_lock.release()


@router.get("/sounds/local")
def local(q: str = "", limit: int = Query(60, ge=1, le=500)):
    """Your ~/Music by words (title, artist, album, genre, file name) and by feel: 'upbeat', 'calm', 'fast', 'slow',
    '120 bpm'. Feel needs a quick analysis of each file (tempo and energy from 45 s of audio), done in the background
    the first time and cached; `indexing` says how far it is. With feel words, other words only rank."""
    if not MUSIC.is_dir():
        return {"results": [], "root": str(MUSIC), "message": "no ~/Music folder"}
    words, feel, bpm = _feel(q)
    files = local_index()
    need = bool(feel or bpm)
    if need and any("feat" not in s for s in files) and BG_INDEX and not _idx_lock.locked():
        threading.Thread(target=index_features, daemon=True, name="music-index").start()
    ready = [s for s in files if "feat" in s]
    hits = []
    for s in (ready if need else files):
        f = s.get("feat") or {}
        t, src = _tempo(s)
        e = f.get("energy")
        hay = f"{s['title']} {s['artist']} {s['album']} {s.get('genre', '')} {Path(s['path']).name}".lower()
        n = sum(w in hay for w in words)
        if not need and n < len(words):
            continue
        if ("high" in feel and (e is None or e < 6)) or ("low" in feel and (e is None or e > 3)):
            continue
        if ("fast" in feel and not (t and t >= 125)) or ("slow" in feel and not (t and t <= 95)):
            continue
        if bpm and not (t and abs(t - bpm) <= 8):
            continue
        hits.append((n, s, t, src, e))
    # most word matches first; then the most (or least) energetic for upbeat (or calm); then closest to the asked bpm
    hits.sort(key=lambda h: (-h[0], -(h[4] or 0) if "high" in feel else (h[4] or 0) if "low" in feel else 0,
                             abs(h[2] - bpm) if bpm and h[2] else 0, h[1]["artist"].lower(), h[1]["title"].lower()))
    out = {"results": [{**{k: v for k, v in s.items() if k not in ("feat", "bpm_tag")}, "tempo": t, "tempo_source": src,
                        "energy": e, "id": hashlib.sha1(s["path"].encode()).hexdigest()[:16],
                        "preview": f"/sounds/local/file?path={urllib.parse.quote(s['path'])}"}
                       for _, s, t, src, e in hits[:limit]], "root": str(MUSIC)}
    if need:
        out["indexing"] = {"analysed": len(ready), "total": len(files)}
        if len(ready) < len(files):
            out["message"] = (f"{len(files) - len(ready)} of {len(files)} files are still being analysed for tempo and "
                              "energy; ask again in a minute for the rest")
    return out


@router.get("/sounds/local/file")
def local_file(path: str):
    return FileResponse(in_music(path))
