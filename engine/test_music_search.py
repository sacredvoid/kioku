"""Music search v2: shortlist ranking and analysis, Jamendo and Incompetech records, licensing, missing key, and the
~/Music library searched by feel. No network (sources are stubbed), synthetic audio only.
uv run test_music_search.py   (needs ffmpeg for the tagged mp3)"""
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import wave
from pathlib import Path

T = Path(tempfile.mkdtemp())
os.environ["KIOKU_DATA"] = str(T / "data")
os.environ["CS_DB"] = str(T / "t.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64
os.environ["KIOKU_MUSIC"] = str(T / "Music")
os.environ.pop("KIOKU_JAMENDO_CLIENT_ID", None)

import numpy as np  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import beats  # noqa: E402
import core  # noqa: E402
sys.modules["captions"] = types.SimpleNamespace(start_transcription=lambda cid: None, likely_speech=lambda u: False)
import edit  # noqa: E402
import library  # noqa: E402
import music  # noqa: E402
import music_sources as ms  # noqa: E402
import settings  # noqa: E402
import sounds  # noqa: E402

assert str(core.DATA).startswith(str(T))
settings.FILE = T / "settings.json"
settings._cur = settings.Settings()
app = FastAPI()
for r in (library.router, edit.router, sounds.router):
    app.include_router(r)
c = TestClient(app)

# ---------- ranking: pure
cands = [
    {"id": "a", "title": "Slow Piano", "tags": ["calm"], "duration_s": 200, "tempo": 70, "instrumental": True, "searched": True},
    {"id": "b", "title": "Fast Beat", "tags": ["upbeat"], "duration_s": 200, "tempo": 128, "instrumental": False, "searched": True},
    {"id": "c", "title": "Fast Short", "tags": ["upbeat"], "duration_s": 20, "tempo": 128, "instrumental": True, "searched": True},
    {"id": "d", "title": "Unknown", "tags": [], "duration_s": 200, "tempo": None, "instrumental": None, "searched": True},
]
order = [s["id"] for s in ms.rank([dict(x) for x in cands], "upbeat", 100, 140, None, 60)]
assert order[0] == "b" and order.index("c") > order.index("b") and order[-1] == "a", order  # in range and long enough wins
order = [s["id"] for s in ms.rank([dict(x) for x in cands], "upbeat", 100, 140, True, 60)]
assert order[0] == "c" or order[0] == "d", order  # a vocal track loses when instrumental is asked for
assert order.index("b") > order.index("c")
order = [s["id"] for s in ms.rank([dict(x) for x in cands], "calm", 60, 80, True, None)]
assert order[0] == "a", order

# ---------- Incompetech: catalog stub, license, bpm filter, disk cache, stale fallback
PIECES = [
    {"title": "Calm Waters", "filename": "Calm Waters.mp3", "length": "00:03:10", "instruments": "Piano, Strings", "genre": "3",
     "bpm": "72", "description": "Peaceful and slow", "feel": "Calming, Peaceful", "isrc": "USUAN1100001"},
    {"title": "Drive Hard", "filename": "Drive Hard.mp3", "length": "00:02:00", "instruments": "Drums, Guitar", "genre": "5",
     "bpm": "140", "description": "Action driving rock", "feel": "Driving, Action", "isrc": "USUAN1100002"},
    {"title": "Choir Of Angels", "filename": "Choir.mp3", "length": "00:01:30", "instruments": "Choir", "genre": "3",
     "bpm": "90", "description": "calm choral", "feel": "Calming", "isrc": None},
    {"title": "", "filename": "broken.mp3"},  # skipped: no title
]
gets = []


def fake_get(url):
    gets.append(url)
    if "pieces.json" in url:
        return PIECES
    raise AssertionError("unexpected url " + url)


ms._get = fake_get
j = c.get("/sounds/search", params={"q": "calm", "provider": "incompetech"}).json()
ids = {s["title"]: s for s in j["results"]}
assert set(ids) == {"Calm Waters", "Choir Of Angels"}, ids
cw = ids["Calm Waters"]
assert cw["id"].startswith("ic-") and cw["license"] == "by" and cw["license_version"] == "4.0" and cw["needs_credit"], cw
assert cw["tempo"] == 72 and cw["instrumental"] is True and ids["Choir Of Angels"]["instrumental"] is None, ids
assert cw["duration_s"] == 190.0 and "text" not in cw and "searched" not in cw
assert j["results"][0]["title"] == "Calm Waters"  # calm + tempo asked: stated bpm 72 is in range
j = c.get("/sounds/search", params={"q": "drive calm", "provider": "incompetech", "tempo_min": 100}).json()
assert [s["title"] for s in j["results"]] == ["Drive Hard"], j  # bpm outside the range is dropped at the source
n = len(gets)
c.get("/sounds/search", params={"q": "calm", "provider": "incompetech"})
assert len(gets) == n, "catalog is cached on disk"
rec = sounds.record(cw["id"])
assert rec["attribution"] == ('"Calm Waters" Kevin MacLeod (incompetech.com) Licensed under Creative Commons: By Attribution '
                              '4.0 License http://creativecommons.org/licenses/by/4.0/'), rec["attribution"]
assert rec["url"] == "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Calm%20Waters.mp3"
assert c.get("/sounds/ic-000000000000/preview").status_code == 404  # unknown id; never a client-supplied URL
assert c.get("/sounds/ic-xyz/preview").status_code == 422 and c.get("/sounds/jm-12ab/preview").status_code == 422
os.utime(ms.IC_FILE, (time.time() - 30 * 86400,) * 2)  # stale copy, and the site is down: the stale copy still serves
sounds._cache.clear()
ms._get = lambda url: (_ for _ in ()).throw(ms.SourceError("down"))
assert [s["title"] for s in c.get("/sounds/search", params={"q": "drive", "provider": "incompetech"}).json()["results"]] == ["Drive Hard"]
ms.IC_FILE.unlink()
r = c.get("/sounds/search", params={"q": "drive", "provider": "incompetech"})
assert r.status_code == 502 and "Incompetech" in r.text, r.text
ms._get = fake_get

# ---------- Jamendo: no key = off with a clear message and no request; with a key = filters, license, download flag
sounds._cache.clear()
gets.clear()
j = c.get("/sounds/search", params={"q": "calm", "provider": "jamendo"}).json()
assert j["results"] == [] and any("Jamendo is off" in n and "Settings" in n for n in j["notes"]), j
assert not gets, "no request without a key"
j = c.get("/sounds/search", params={"q": "calm", "provider": "incompetech"}).json()
assert not any("Jamendo" in n for n in j["notes"])  # only asked-for sources are reported on


def jm(i, name, ccurl, dl=True, vi="instrumental", speed="medium", dur=180):
    return {"id": str(i), "name": name, "artist_name": "Ann", "duration": dur, "license_ccurl": ccurl, "shareurl": f"https://jamen.do/t/{i}",
            "audiodownload": f"https://api.jamendo.com/v3.0/download/{i}" if dl else "", "audiodownload_allowed": dl,
            "musicinfo": {"vocalinstrumental": vi, "speed": speed, "tags": {"genres": ["pop"], "instruments": ["piano"], "vartags": ["happy"]}}}


JM = [jm(1, "Happy Day", "https://creativecommons.org/licenses/by/3.0/"),
      jm(2, "Nc Song", "https://creativecommons.org/licenses/by-nc/3.0/"),
      jm(3, "No Download", "https://creativecommons.org/licenses/by/3.0/", dl=False),
      jm(4, "Nd Song", "https://creativecommons.org/licenses/by-nd/3.0/"),
      jm(5, "Sung Song", "https://creativecommons.org/licenses/by/4.0/", vi="vocal")]


def jm_get(url):
    gets.append(url)
    return {"headers": {"status": "success"}, "results": JM}


ms._get = jm_get
settings._cur = settings.Settings(jamendo_client_id="abc123")
j = c.get("/sounds/search", params={"q": "happy", "provider": "jamendo", "instrumental": "true", "tempo_min": 100,
                                    "tempo_max": 125, "max_dur": 300}).json()
q = gets[-1]
assert "client_id=abc123" in q and "vocalinstrumental=instrumental" in q and "speed=medium" in q and "durationbetween=30_300" in q, q
assert {s["id"] for s in j["results"]} == {"jm-1", "jm-5"}, j  # NC, ND and no-download tracks are dropped (commercial setting)
h = next(s for s in j["results"] if s["id"] == "jm-1")
assert h["license"] == "by" and h["needs_credit"] and h["instrumental"] is True and h["provider"] == "jamendo", h
assert "Ann" in h["attribution"] and "Jamendo" in h["attribution"] and "by 3.0" not in h["attribution"] and "CC BY 3.0" in h["attribution"]
assert j["results"][0]["id"] == "jm-1", "instrumental asked: the vocal track ranks below"
settings._cur = settings.Settings(jamendo_client_id="abc123", sound_licenses="noncommercial")
sounds._cache.clear()
assert {s["id"] for s in c.get("/sounds/search", params={"q": "happy", "provider": "jamendo"}).json()["results"]} == {"jm-1", "jm-2", "jm-5"}
settings._cur = settings.Settings(jamendo_client_id="abc123")
sounds._cache.clear()
ms._get = lambda url: {"headers": {"status": "failed", "error_message": "invalid client_id"}}
r = c.get("/sounds/search", params={"q": "happy", "provider": "jamendo"})
assert r.status_code == 502 and "invalid client_id" in r.text, r.text
os.environ["KIOKU_JAMENDO_CLIENT_ID"] = "fromenv"  # the env var is a fallback when settings has none
settings._cur = settings.Settings()
assert ms.jamendo_key() == "fromenv"
os.environ.pop("KIOKU_JAMENDO_CLIENT_ID")
assert settings.Settings(jamendo_client_id="abc").jamendo_client_id == "abc"
try:
    settings.Settings(jamendo_client_id="a b&c=1")
    raise SystemExit("key with odd characters must be rejected")
except ValueError:
    pass

# ---------- shortlist: analysis of the top N, bounded and cached; credits for the added sound
SR = 22050
t = np.arange(32 * SR) / SR
x = 0.02 * np.sin(2 * np.pi * 440 * t) + np.where(t >= 12, 0.9 * np.sin(2 * np.pi * 55 * t) * np.exp(-8 * (t % 0.5)), 0)
song = T / "song.wav"
with wave.open(str(song), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes((x * 32767).astype("<i2").tobytes())
AN = {"tempo": 120.0, "beats": [i * 0.5 for i in range(64)], "downbeats": [i * 2.0 for i in range(16)], "duration": 32.0}
analysed = []


def fake_analyze(p):
    analysed.append(str(p))
    return AN


beats.analyze = fake_analyze
downloads = []
sounds.download = lambda sid: (downloads.append(sid), song)[1]
settings._cur = settings.Settings()
sounds._cache.clear()
ms._get = fake_get
j = c.get("/sounds/search", params={"q": "calm drive", "provider": "incompetech", "analyze": 2, "target_seconds": 60}).json()
assert len(j["results"]) == 3 and all("analysis" in s for s in j["results"][:2]) and "analysis" not in j["results"][2], j  # top 2 only
j["results"] = j["results"][:2]
a = j["results"][0]["analysis"]
assert a["tempo"] == 120.0 and a["length_s"] == 32.0 and a["drop_s"] == 12.0 and a["shape"] == "builds" and a["vocals"] is None, a
assert len(a["curve"]) == 8 and a["curve"][0] < a["curve"][-1] and "drop at 12s" in a["summary"], a
assert j["results"][0]["tempo"] == 72 and j["results"][0]["analysis"]["tempo"] == 120.0  # the stated bpm is kept
assert not any("length_note" in s for s in j["results"]), j  # 190 s and 120 s both cover the 60 s edit
assert "length_note" in c.get("/sounds/search", params={"q": "drive", "provider": "incompetech", "target_seconds": 300}).json()["results"][0]
before = len(analysed)
assert before == 1, analysed  # same file stub: beats.analyze runs once, then music.analyze reads its cache
c.get("/sounds/search", params={"q": "calm drive", "provider": "incompetech", "analyze": 2})
assert len(analysed) == before, "second search is served from the analysis cache"
assert c.get("/sounds/search", params={"q": "calm", "analyze": 6}).status_code == 422  # at most 5
assert all("analysis" not in s for s in c.get("/sounds/search", params={"q": "calm", "provider": "incompetech"}).json()["results"])

# a slow or failing analysis never sinks the search: the rest still comes back with a reason
slow_ids = []


def slow_or_bad(sid):
    if sid == sorted_ids[0]:
        time.sleep(1.5)
        return {"tempo": 1}
    raise RuntimeError("no clear bar structure")


sorted_ids = [s["id"] for s in c.get("/sounds/search", params={"q": "calm drive", "provider": "incompetech"}).json()["results"]]
sounds._analyse = slow_or_bad
sounds.ANALYZE_BUDGET = 0.3
t0 = time.time()
j = c.get("/sounds/search", params={"q": "calm drive", "provider": "incompetech", "analyze": 2}).json()
assert time.time() - t0 < 1.2, "bounded wait"
errs = {s["id"]: s["analysis_error"] for s in j["results"] if "analysis_error" in s}
assert "still analysing" in errs[sorted_ids[0]] and "bar structure" in errs[sorted_ids[1]], errs
assert any("could not be analysed" in n for n in j["notes"])

# add a Kevin MacLeod track: credit line, credits.txt text with the source link
sounds._analyse = lambda sid: None
tone = T / "tone.mp3"
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=2", str(tone)], check=True)
sounds.download = lambda sid: tone
cid = c.post("/collections", json={"name": "p", "kind": "project"}).json()["id"]
r = c.post(f"/sounds/{cw['id']}/add", json={"cid": cid})
assert r.status_code == 200 and "Kevin MacLeod" in r.json()["credit"], r.text
cr = c.get(f"/collections/{cid}/credits").json()
assert cr["required"] and "Calm Waters" in cr["text"] and "isrc=USUAN1100001" in cr["text"] and cr["credits"][0]["provider"] == "incompetech", cr

# ---------- the library, by feel
M = Path(os.environ["KIOKU_MUSIC"])
(M / "Artist").mkdir(parents=True)


def clicks(path, bpm, amp, secs=30):
    xs = np.zeros(secs * SR, dtype="float32")
    for b in np.arange(0, secs, 60 / bpm):
        i = int(b * SR)
        n = min(2000, xs.size - i)
        xs[i:i + n] += amp * np.sin(2 * np.pi * 60 * np.arange(n) / SR) * np.exp(-np.arange(n) / 400)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((xs * 32767).astype("<i2").tobytes())


clicks(M / "Artist" / "Sleepy Lake.wav", 75, 0.2)
clicks(M / "Artist" / "Road Rage.wav", 140, 0.9)
clicks(M / "Artist" / "Plain.wav", 100, 0.5)
tagged = M / "Artist" / "Tagged.mp3"
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=330:d=12", "-metadata", "title=Tagged Tune", "-metadata",
                "genre=Synthwave", "-metadata", "TBPM=100", "-id3v2_version", "3", str(tagged)], check=True)
sounds.BG_INDEX = False
sounds._local.clear()
sounds._local_at = 0

j = c.get("/sounds/local", params={"q": "synthwave"}).json()  # a tag word needs no analysis
assert [s["title"] for s in j["results"]] == ["Tagged Tune"] and "indexing" not in j, j
assert j["results"][0]["genre"] == "Synthwave"
j = c.get("/sounds/local", params={"q": "upbeat music"}).json()
assert j["results"] == [] and j["indexing"] == {"analysed": 0, "total": 4} and "still being analysed" in j["message"], j
assert sounds.index_features() == 4 and sounds.index_features() == 0
j = c.get("/sounds/local", params={"q": "upbeat"}).json()
assert [s["title"] for s in j["results"]] == ["Road Rage"] and j["results"][0]["energy"] >= 6, j
assert "message" not in j and j["indexing"] == {"analysed": 4, "total": 4}
assert [s["title"] for s in c.get("/sounds/local", params={"q": "chill"}).json()["results"]][:1] == ["Sleepy Lake"]
assert [s["title"] for s in c.get("/sounds/local", params={"q": "slow"}).json()["results"]] == ["Sleepy Lake"]
assert [s["title"] for s in c.get("/sounds/local", params={"q": "fast"}).json()["results"]] == ["Road Rage"]
j = c.get("/sounds/local", params={"q": "140 bpm"}).json()
assert [s["title"] for s in j["results"]] == ["Road Rage"] and j["results"][0]["tempo_source"] == "estimate", j
assert abs(j["results"][0]["tempo"] - 140) <= 6
j = c.get("/sounds/local", params={"q": "100bpm"}).json()
assert j["results"][0]["title"] == "Tagged Tune" and j["results"][0]["tempo_source"] == "tag" and j["results"][0]["tempo"] == 100, j
j = c.get("/sounds/local", params={"q": "calm road"}).json()  # feel filters; plain words only rank
assert j["results"][0]["title"] == "Sleepy Lake", j  # calm filters by energy; "road" matches nothing calm and only ranks
assert "feat" not in j["results"][0] and "bpm_tag" not in j["results"][0]
# text search without feel works as before
assert [s["title"] for s in c.get("/sounds/local", params={"q": "road rage"}).json()["results"]] == ["Road Rage"]
# features come from the disk cache after a restart: nothing is decoded again
sounds._local.clear()
sounds._local_at = 0
orig_quick = music.quick_features
music.quick_features = lambda *a, **k: (_ for _ in ()).throw(AssertionError("recomputed"))
assert sounds.index_features() == 0
assert [s["title"] for s in c.get("/sounds/local", params={"q": "fast"}).json()["results"]] == ["Road Rage"]
# a file that cannot be decoded is cached as an error and never matches a feel
bad = M / "Artist" / "Broken.wav"
bad.write_bytes(b"RIFF" + b"\0" * 100)
sounds._local_at = 0
music.quick_features = orig_quick
assert sounds.index_features() == 0  # ffprobe finds no audio: not in the index at all
assert "Broken" not in [s["title"] for s in c.get("/sounds/local", params={"q": "calm"}).json()["results"]]
print("ok")
