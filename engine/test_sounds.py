"""Free sounds + ~/Music browsing with Openverse mocked: no network.  uv run test_sounds.py  (needs ffmpeg/ffprobe)"""
import io
import os
import subprocess
import sys
import tempfile
import types
import urllib.error
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["KIOKU_DATA"] = str(tmp / "data")
os.environ["CS_DB"] = str(tmp / "t.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain
os.environ["KIOKU_MUSIC"] = str(tmp / "Music")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import core  # noqa: E402
sys.modules["captions"] = types.SimpleNamespace(start_transcription=lambda cid: None, likely_speech=lambda u: False)
import edit  # noqa: E402
import library  # noqa: E402
import sounds  # noqa: E402

app = FastAPI()
for r in (library.router, edit.router, sounds.router):
    app.include_router(r)
c = TestClient(app)

# a real tiny mp3, and a fake "mp3" that is not audio
good = tmp / "tone.mp3"
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=2", str(good)], check=True)
GOOD = good.read_bytes()

A, B, NC, BAD, BIG, HTTP, OGG = (f"{n:08x}-0000-4000-8000-000000000000" for n in range(1, 8))
REC = {
    A: {"license": "by", "url": "https://cdn.example.org/a.mp3", "filetype": "mp3", "body": GOOD},
    B: {"license": "cc0", "url": "https://cdn.example.org/b.mp3", "filetype": "mp3", "body": GOOD},
    NC: {"license": "by-nc", "url": "https://cdn.example.org/n.mp3", "filetype": "mp3", "body": GOOD},
    BAD: {"license": "cc0", "url": "https://cdn.example.org/x.mp3", "filetype": "mp3", "body": b"<html>not audio</html>"},
    BIG: {"license": "cc0", "url": "https://cdn.example.org/big.mp3", "filetype": "mp3", "body": b"\0" * (sounds.MAX_BYTES + 10)},
    HTTP: {"license": "cc0", "url": "http://cdn.example.org/h.mp3", "filetype": "mp3", "body": GOOD},
    OGG: {"license": "cc0", "url": "https://cdn.example.org/o.ogg", "filetype": "ogg", "body": GOOD},
}


def rec(i):
    r = REC[i]
    return {"id": i, "title": f"T{i[:8]}", "creator": "Ann", "license": r["license"], "license_version": "4.0",
            "license_url": "https://cc/", "attribution": f'"T{i[:8]}" by Ann is licensed under CC {r["license"]}.',
            "provider": "freesound", "duration": 2000, "tags": [{"name": "calm"}], "filetype": r["filetype"],
            "url": r["url"], "foreign_landing_url": f"https://freesound.org/{i}"}


calls = []


def fake_ov(path, params=None):
    calls.append((path, params))
    if path == "/audio/":
        return {"results": [rec(i) for i in REC], "page_count": 1}
    sid = path.split("/")[2]
    if path.endswith("/waveform/"):
        return {"points": [i / 300 for i in range(300)]}
    if sid not in REC:
        raise sounds.HTTPException(404, "gone")
    return rec(sid)


class Resp(io.BytesIO):
    def __init__(self, url, body):
        super().__init__(body)
        self.url, self.headers = url, {}

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


fetched = []


def fake_urlopen(req, timeout=None):
    url = req.full_url
    fetched.append(url)
    for r in REC.values():
        if r["url"] == url:
            return Resp(url, r["body"])
    raise urllib.error.URLError("unexpected url " + url)


sounds._ov = fake_ov
sounds.urllib.request.urlopen = fake_urlopen

# search: normalized, license-filtered (no NC by default), unmixable formats dropped
j = c.get("/sounds/search", params={"q": "calm", "kind": "music"}).json()
ids = {s["id"] for s in j["results"]}
assert NC not in ids and OGG not in ids and {A, B} <= ids, ids
s = next(s for s in j["results"] if s["id"] == A)
assert s["duration_s"] == 2.0 and s["tags"] == ["calm"] and s["needs_credit"] and s["preview"] == f"/sounds/{A}/preview"
assert calls[-1][1]["category"] == "music" and calls[-1][1]["license"] == "cc0,pdm,by"
assert c.get("/sounds/search", params={"q": "x", "kind": "sfx"}).status_code == 200 and calls[-1][1]["source"] == "freesound"
assert c.get("/sounds/search", params={"q": "x", "kind": "bogus"}).status_code == 422
assert c.get("/sounds/search", params={"q": "calm", "max_dur": 1}).json()["results"] == []

# downloads: only the record's own https URL; bad ids, http, non-audio, oversized and disallowed licenses refused
assert c.get("/sounds/../../etc/preview").status_code == 404
assert c.get("/sounds/not-a-uuid/preview").status_code == 422
assert c.get(f"/sounds/{HTTP}/preview").status_code == 422
assert c.get(f"/sounds/{NC}/preview").status_code == 422
assert c.get(f"/sounds/{BAD}/preview").status_code == 422
assert c.get(f"/sounds/{BIG}/preview").status_code == 422
assert not list(sounds.SOUNDS.glob("*.part")) and not list(sounds.SOUNDS.glob("*probe*")), list(sounds.SOUNDS.iterdir())
assert not (sounds.SOUNDS / f"{BAD}.mp3").exists() and not (sounds.SOUNDS / f"{BIG}.mp3").exists()
r = c.get(f"/sounds/{A}/preview")
assert r.status_code == 200 and r.content == GOOD
n = len(fetched)
assert c.get(f"/sounds/{A}/preview").status_code == 200 and len(fetched) == n  # cached on disk
assert len(c.get(f"/sounds/{A}/waveform").json()["points"]) <= 150

# add to a project: a track appears, credits list the CC BY one, cc0 needs no credit
cid = c.post("/collections", json={"name": "p", "kind": "project"}).json()["id"]
r = c.post(f"/sounds/{A}/add", json={"cid": cid, "start": 1.5})
assert r.status_code == 200, r.text
j = r.json()
assert j["track_id"] == f"s{A[:8]}" and "Ann" in j["credit"]
t = next(t for t in c.get(f"/collections/{cid}/edit").json()["spec"]["audio"] if t["id"] == j["track_id"])
assert t["start"] == 1.5 and t["path"].endswith(f"{A}.mp3")
assert c.post(f"/sounds/{B}/add", json={"cid": cid}).status_code == 200
cr = c.get(f"/collections/{cid}/credits").json()
assert cr["required"] and len(cr["credits"]) == 2 and "Ann" in cr["text"] and f"freesound.org/{A}" in cr["text"]
assert f'"T{B[:8]}"' not in cr["text"]  # public domain: listed in credits, no line needed
out = tmp / "out"
out.mkdir()
assert sounds.write_credits(cid, out) == str(out / "credits.txt") and "Ann" in (out / "credits.txt").read_text()
cid2 = c.post("/collections", json={"name": "q", "kind": "project"}).json()["id"]
c.post(f"/sounds/{B}/add", json={"cid": cid2})
assert sounds.write_credits(cid2, out, "q") is None  # cc0 only: nothing to credit

# ~/Music: only usable audio inside it, searchable by tags/filename; nothing outside is served
music = Path(os.environ["KIOKU_MUSIC"])
(music / "Album").mkdir(parents=True)
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=330:d=3", "-metadata", "title=Night Drive",
                "-metadata", "artist=Lumen", str(music / "Album" / "01.mp3")], check=True)
(music / "Album" / "fake.m4a").write_bytes(b"nope")
(music / "Album" / "drm.m4p").write_bytes(b"nope")
j = c.get("/sounds/local", params={"q": "lumen night"}).json()
assert [s["title"] for s in j["results"]] == ["Night Drive"] and j["results"][0]["duration_s"] == 3.0, j
assert c.get("/sounds/local", params={"q": "fake"}).json()["results"] == []
assert c.get("/sounds/local/file", params={"path": str(music / "Album" / "01.mp3")}).status_code == 200
assert c.get("/sounds/local/file", params={"path": str(good)}).status_code == 422  # outside ~/Music
assert c.get("/sounds/local/file", params={"path": str(music / "Album" / ".." / ".." / "tone.mp3")}).status_code == 422
print("ok")
