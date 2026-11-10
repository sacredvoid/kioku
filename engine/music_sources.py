"""Extra music sources for sounds.search (notes/editing-power-research.md M3): Jamendo and Incompetech, plus the ranking.

Both return records shaped like Openverse audio records, so sounds.norm / download / add and the credits table work
unchanged. Ids are "jm-<jamendo id>" and "ic-<hash of the file name>"; sounds.record() re-reads the record here
server side, so a download URL is never taken from the client.

Terms checked 2026-11-10 (web):
- Jamendo API (devportal.jamendo.com/api_terms_of_use): free for non-commercial apps (35,000 requests a month), needs the
  user's own free client_id, must credit the artist and Jamendo and link back to each track, must respect each track's
  audiodownload_allowed flag, and must not be built to cache content beyond what the app needs (we keep one file per track
  that was previewed or added). Kioku is a personal app, so it is enabled when a key is present; commercial use of the API
  needs a licence from Jamendo.
- Incompetech (incompetech.com/agent-section): static catalog for automated use, Kevin MacLeod's CC BY 4.0; the credit line
  below is the one his site asks for.
"""
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import core
import settings

log = logging.getLogger("kioku.music_sources")
UA = {"User-Agent": "Kioku/0.1 (personal video editor)"}
JM = "https://api.jamendo.com/v3.0"
IC = "https://incompetech.com/music/royalty-free"
IC_FILE = core.DATA / "incompetech.json"
IC_TTL = 7 * 86400
NEED_KEY = ("Jamendo is off: add a free client id in Settings (Jamendo client id) or set KIOKU_JAMENDO_CLIENT_ID "
            "(get one at devportal.jamendo.com; non-commercial apps only)")
# Jamendo speed labels, by tempo. ponytail: bands are a guess (Jamendo does not publish bpm cut-offs); musicinfo.speed is
# only a hint, so the shortlist's own analysis overrules it.
SPEEDS = [("verylow", 0, 70), ("low", 70, 95), ("medium", 95, 120), ("high", 120, 150), ("veryhigh", 150, 999)]
VOCAL = re.compile(r"vocal|voice|choir|sing", re.I)


class SourceError(Exception):
    """A source could not answer; the message is fit to show the user."""


def _get(url):
    """GET JSON. The one place the network is touched, so tests replace it."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SourceError(f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise SourceError(str(e))


def jamendo_key() -> str:
    return (settings.get().jamendo_client_id or os.environ.get("KIOKU_JAMENDO_CLIENT_ID") or "").strip()


# ---------- Jamendo

def _cc(url):
    """("by-nc", "3.0") from a Creative Commons URL; ("cc0", "1.0") for public domain dedication; ("", "") if unknown."""
    m = re.search(r"/licenses/([a-z\-]+)/([\d.]+)", url or "")
    if m:
        return m.group(1), m.group(2)
    return ("cc0", "1.0") if "/publicdomain/zero/" in (url or "") else ("", "")


def _jm_record(t):
    lic, ver = _cc(t.get("license_ccurl"))
    mi = t.get("musicinfo") or {}
    tags = mi.get("tags") or {}
    names = [*(tags.get("vartags") or []), *(tags.get("genres") or []), *(tags.get("instruments") or [])]
    vi = mi.get("vocalinstrumental")
    sid = f"jm-{t['id']}"
    spec = lic.upper()
    return {"id": sid, "title": t.get("name"), "creator": t.get("artist_name"), "license": lic, "license_version": ver,
            "license_url": t.get("license_ccurl"), "provider": "jamendo", "duration": float(t.get("duration") or 0) * 1000,
            "attribution": (f'"{t.get("name")}" by {t.get("artist_name")} is licensed under CC {spec} {ver}. '
                            f"Music provided by Jamendo." if lic.startswith("by") else
                            f'"{t.get("name")}" by {t.get("artist_name")} (public domain). Music provided by Jamendo.'),
            "tags": [{"name": n} for n in names], "filetype": "mp3", "url": t.get("audiodownload") or "",
            "foreign_landing_url": t.get("shareurl"), "instrumental": {"instrumental": True, "vocal": False}.get(vi),
            "speed": mi.get("speed"),
            "ok": bool(t.get("audiodownload_allowed")) and bool(t.get("audiodownload"))}


def _jm_call(path, params):
    key = jamendo_key()
    if not key:
        raise SourceError(NEED_KEY)
    j = _get(f"{JM}{path}?" + urllib.parse.urlencode({"client_id": key, "format": "json", **params}))
    h = j.get("headers") or {}
    if h.get("status") != "success":
        raise SourceError(f"Jamendo said: {h.get('error_message') or 'request failed'}")
    return j.get("results") or []


def jamendo_search(q, tempo_min=None, tempo_max=None, instrumental=None, max_dur=None, limit=20):
    """Openverse-shaped records for downloadable Jamendo tracks. License filtering happens in sounds (allowed())."""
    p = {"search": q, "limit": limit, "include": "musicinfo", "boost": "popularity_month", "audiodlformat": "mp32"}
    if instrumental is True:
        p["vocalinstrumental"] = "instrumental"
    if tempo_min is not None or tempo_max is not None:
        lo, hi = tempo_min or 0, tempo_max or 999
        p["speed"] = "+".join(n for n, a, b in SPEEDS if a < hi and b > lo)
    if max_dur:
        p["durationbetween"] = f"30_{int(max_dur)}"
    return [r for r in map(_jm_record, _jm_call("/tracks/", p)) if r["ok"]]


def jamendo_record(sid):
    rows = _jm_call("/tracks/", {"id": sid[3:], "include": "musicinfo", "audiodlformat": "mp32"})
    recs = [r for r in map(_jm_record, rows) if r["ok"]]
    if not recs:
        raise SourceError("that Jamendo track is gone or not downloadable")
    return recs[0]


# ---------- Incompetech

def _secs(s):
    try:
        h, m, sec = (int(v) for v in s.split(":"))
        return h * 3600 + m * 60 + sec
    except (AttributeError, ValueError):
        return 0


def _ic_record(p):
    title = p["title"]
    inst = [i.strip() for i in (p.get("instruments") or "").split(",") if i.strip()]
    feel = [i.strip() for i in (p.get("feel") or "").split(",") if i.strip()]
    try:
        bpm = float(p.get("bpm") or 0) or None
    except ValueError:
        bpm = None
    return {"id": "ic-" + hashlib.sha1(p["filename"].encode()).hexdigest()[:12], "title": title,
            "creator": "Kevin MacLeod", "license": "by", "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/", "provider": "incompetech",
            "duration": _secs(p.get("length")) * 1000, "tags": [{"name": n} for n in [*feel, *inst]], "filetype": "mp3",
            "attribution": f'"{title}" Kevin MacLeod (incompetech.com) Licensed under Creative Commons: By Attribution 4.0 '
                           "License http://creativecommons.org/licenses/by/4.0/",
            "url": f"{IC}/mp3-royaltyfree/{urllib.parse.quote(p['filename'])}", "bpm": bpm,
            "instrumental": None if VOCAL.search(p.get("instruments") or "") else True,
            "foreign_landing_url": f"https://incompetech.com/music/royalty-free/index.html?isrc={p.get('isrc')}"
            if p.get("isrc") else "https://incompetech.com/music/royalty-free/music.html",
            "text": f"{title} {p.get('description') or ''}".lower()}


def incompetech_catalog():
    """All Incompetech records, from a week-old copy in ~/Kioku when there is one; a stale copy beats failing offline."""
    try:
        if time.time() - IC_FILE.stat().st_mtime < IC_TTL:
            return [_ic_record(p) for p in json.loads(IC_FILE.read_text())]
    except (OSError, ValueError, KeyError):
        pass
    try:
        raw = _get(f"{IC}/pieces.json")
        rows = [p for p in raw if isinstance(p, dict) and p.get("title") and p.get("filename")]
        recs = [_ic_record(p) for p in rows]
    except (SourceError, KeyError, TypeError) as e:
        try:
            return [_ic_record(p) for p in json.loads(IC_FILE.read_text())]
        except (OSError, ValueError, KeyError):
            raise SourceError(f"Incompetech catalog unavailable ({e})")
    IC_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = IC_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows))
    tmp.replace(IC_FILE)
    return recs


def incompetech_search(q, tempo_min=None, tempo_max=None, limit=20):
    words = q.lower().split()
    out = []
    for r in incompetech_catalog():
        hay = f"{r['text']} {' '.join(t['name'] for t in r['tags']).lower()}"
        hits = sum(w in hay for w in words)
        bpm = r["bpm"]
        if not hits or (bpm and ((tempo_min and bpm < tempo_min) or (tempo_max and bpm > tempo_max))):
            continue
        out.append((hits, r))
    out.sort(key=lambda x: -x[0])  # stable: catalog order (newest first) among equals
    return [r for _, r in out[:limit]]


def incompetech_record(sid):
    for r in incompetech_catalog():
        if r["id"] == sid:
            return r
    raise SourceError("that Incompetech track is not in the catalog")


# ---------- ranking

def rank(cands, q="", tempo_min=None, tempo_max=None, instrumental=None, target=None):
    """cands sorted best first by fit to the request. Each gets `score`. Needs keys: title, tags, duration_s, tempo (bpm or
    None = unknown, neutral), instrumental (True, False or None). Stable, so equal scores keep the source's own order."""
    words = q.lower().split()

    def score(s):
        hay = f"{s['title']} {' '.join(s['tags'])} {s.get('text') or ''}".lower()
        frac = sum(w in hay for w in words) / max(len(words), 1)
        v = 3 * (max(frac, 0.5) if s.get("searched") else frac)  # the source's own search already matched those
        t = s.get("tempo")
        if t and (tempo_min or tempo_max):
            d = max((tempo_min or 0) - t, t - (tempo_max or 999), 0)
            v += 2 if d == 0 else -min(3, d / 15)
        if instrumental is True and s.get("instrumental") is not None:
            v += 2 if s["instrumental"] else -3
        if target and s["duration_s"]:
            v += 1 if s["duration_s"] >= target * 0.9 else -2 * (1 - s["duration_s"] / target)
        return round(v, 3)
    for s in cands:
        s["score"] = score(s)
    return sorted(cands, key=lambda s: -s["score"])
