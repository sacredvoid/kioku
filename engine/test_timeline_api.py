"""Timeline editor routes: strip, replace, split, insert at pos, cues, audio file + peaks, proxy GOP.
uv run test_timeline_api.py   (ffmpeg lavfi fixtures, no network, no Photos)"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import os
import subprocess
import tempfile
from pathlib import Path

os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import captions  # noqa: E402
import core  # noqa: E402
testenv.check()
import edit  # noqa: E402
import library  # noqa: E402
import media  # noqa: E402

tmp = Path(tempfile.mkdtemp())
media.PROXIES, media.STRIPS, edit.PEAKS = tmp / "proxies", tmp / "strips", tmp / "peaks"
media.PROXIES.mkdir()


def lavfi(src, out, *extra):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, *extra, str(out)], check=True)


db = core.connect()
for u, kind, dur in [("V", "video", 12.0), ("W", "video", 5.0), ("L", "video", 400.0), ("N", "video", 8.0), ("P", "image", 0)]:
    db.execute("insert into assets(uuid, kind, duration, meta, created) values (?,?,?,?,?)", (u, kind, dur, "", "2026-11-01"))
db.commit()

app = FastAPI()
for m in (library, edit, media, captions):
    app.include_router(m.router)
app.add_exception_handler(library.RequestValidationError, library.validation_error)
c = TestClient(app)


def project(*uuids):
    cid = c.post("/collections", json={"name": "p", "kind": "project"}).json()["id"]
    c.post(f"/collections/{cid}/items", json={"uuids": list(uuids)})
    return cid


def items(cid):
    return c.get(f"/collections/{cid}").json()["items"]


# ---- strip
import cv2  # noqa: E402
from PIL import Image  # noqa: E402

lavfi("testsrc2=s=640x360:r=10:d=12", media.PROXIES / "V.mp4", "-pix_fmt", "yuv420p")
assert c.get("/media/W/strip").status_code == 404  # no proxy
r = c.get("/media/V/strip")
assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg", r.status_code
assert Image.open(media.STRIPS / "V.jpg").size == (12 * 160, 90)
opened = []
real_cap = cv2.VideoCapture
cv2.VideoCapture = lambda *a: (opened.append(a), real_cap(*a))[1]
assert c.get("/media/V/strip").status_code == 200 and not opened, "second call must come from the cache"
os.utime(media.PROXIES / "V.mp4", (1e10, 1e10))  # proxy newer than the strip -> rebuilt
assert c.get("/media/V/strip").status_code == 200 and opened
cv2.VideoCapture = real_cap
os.utime(media.PROXIES / "V.mp4", None)
lavfi("color=c=blue:s=64x36:r=1:d=400", media.PROXIES / "L.mp4", "-pix_fmt", "yuv420p")  # long: capped
assert c.get("/media/L/strip").status_code == 200
assert Image.open(media.STRIPS / "L.jpg").size == (120 * 160, 90)
# portrait source is cropped to 16:9, not squashed
lavfi("testsrc2=s=360x640:r=5:d=3", media.PROXIES / "N.mp4", "-pix_fmt", "yuv420p")
assert c.get("/media/N/strip").status_code == 200 and Image.open(media.STRIPS / "N.jpg").size == (3 * 160, 90)

# ---- proxy GOP: -g 30 on new proxies
seen = []
real_run = subprocess.run
media.local_original = lambda u: Path("/dev/null/x.mp4")
media.stage = lambda *a, **k: None
import index  # noqa: E402
index.extract_audio = lambda *a, **k: None


def fake_run(cmd, *a, **k):
    if cmd[0] == "ffprobe":  # the encode timeout probes the source's length first
        return subprocess.CompletedProcess(cmd, 0, "", "")
    seen.append(cmd)
    Path(cmd[-1]).write_bytes(b"x")
    return subprocess.CompletedProcess(cmd, 0, "", "")


media.subprocess.run = fake_run
media.ensure_proxy("G")
media.subprocess.run = real_run
cmd = seen[0]
assert cmd[cmd.index("-g") + 1] == "30" and cmd[cmd.index("-keyint_min") + 1] == "30", cmd

# ---- replace
cid = project("V", "W")
a, b = [i["item_id"] for i in items(cid)]
c.patch(f"/collections/{cid}/edit/clips/{a}", json={"speed": 2})
c.put(f"/collections/{cid}/edit/transitions/{a}", json={"type": "dissolve"})
h0 = c.get(f"/collections/{cid}/edit").json()["edit_hash"]
r = c.put(f"/collections/{cid}/items/{a}", json={"uuid": "N", "t0": 1, "t1": 4})
assert r.status_code == 200 and r.json() == {"item_id": a, "uuid": "N", "t0": 1, "t1": 4}, r.json()
it = items(cid)
assert [i["item_id"] for i in it] == [a, b] and it[0]["uuid"] == "N" and it[0]["pos"] == 1
e = c.get(f"/collections/{cid}/edit").json()
assert e["spec"]["clips"][str(a)]["speed"] == 2 and str(a) in e["spec"]["transitions"] and e["edit_hash"] != h0
r = c.put(f"/collections/{cid}/items/{a}", json={"uuid": "W"})  # new uuid starts untrimmed
assert r.json()["t0"] is None and r.json()["t1"] is None
assert c.put(f"/collections/{cid}/items/{a}", json={"t1": 6}).status_code == 422  # W is 5 s
assert c.put(f"/collections/{cid}/items/{a}", json={"t0": 3, "t1": 2}).status_code == 422
assert c.put(f"/collections/{cid}/items/{a}", json={"uuid": "NOPE"}).status_code == 422
assert c.put(f"/collections/{cid}/items/{a}", json={"uuid": "P"}).status_code == 422  # photo for video
assert c.put(f"/collections/{cid}/items/9999", json={"t0": 1}).status_code == 404
assert c.put("/collections/9999/items/1", json={"t0": 1}).status_code == 404
assert items(cid)[0]["uuid"] == "W"  # failed requests changed nothing

# ---- pos on POST items
cid = project("V", "W")
a, b = [i["item_id"] for i in items(cid)]
r = c.post(f"/collections/{cid}/items", json={"uuids": ["N"], "pos": 1.5})
assert r.json() == {"added": 1, "skipped": 0}
it = items(cid)
assert [i["uuid"] for i in it] == ["V", "N", "W"] and [i["pos"] for i in it] == [1, 2, 3], it
c.post(f"/collections/{cid}/items", json={"uuids": ["P", "P"], "pos": 0.5})
assert [i["uuid"] for i in items(cid)] == ["P", "P", "V", "N", "W"]
assert c.post(f"/collections/{cid}/items", json={"uuids": ["V"]}).status_code == 200
assert items(cid)[-1]["uuid"] == "V"  # no pos still appends

# ---- split
cid = project("V", "W")
a, b = [i["item_id"] for i in items(cid)]
trim = c.patch(f"/collections/{cid}/items/{a}", json={"t0": 2, "t1": 10})
c.patch(f"/collections/{cid}/edit/clips/{a}", json={"speed": 2, "captions": True, "look": "warm"})
c.put(f"/collections/{cid}/edit/transitions/{a}", json={"type": "dissolve", "duration": 0.4})
rev0 = c.get(f"/collections/{cid}/edit").json()["rev"]
r = c.post(f"/collections/{cid}/items/{a}/split", json={"at": 5})
assert r.status_code == 200, r.text
j = r.json()
new = j["item_id"]
assert new not in (a, b) and j["rev"] == rev0 + 1 and "spec" in j and j["clamped"] == [], j
it = items(cid)
assert [i["item_id"] for i in it] == [a, new, b] and [i["pos"] for i in it] == [1, 2, 3], it
assert (it[0]["t0"], it[0]["t1"], it[1]["t0"], it[1]["t1"]) == (2, 5, 5, 10), it
sp = j["spec"]
assert sp["clips"][str(new)] == sp["clips"][str(a)] and sp["clips"][str(new)]["speed"] == 2, sp["clips"]
assert str(a) not in sp["transitions"] and sp["transitions"][str(new)] == {"type": "dissolve", "duration": 0.4}, sp["transitions"]
assert c.get(f"/collections/{cid}/edit").json()["spec"] == sp
# untrimmed clip splits against the asset duration and keeps the open end
r = c.post(f"/collections/{cid}/items/{b}/split", json={"at": 2})
assert r.status_code == 200
it = items(cid)
assert (it[3]["uuid"], it[3]["t0"], it[3]["t1"]) == ("W", 2, None) and it[2]["t1"] == 2, it
# bounds, photo, unknown
for at in (2.1, 5.0, 9.9, 10, 0, -1):
    n_before = len(items(cid))
    assert c.post(f"/collections/{cid}/items/{new}/split", json={"at": at}).status_code == 422 or 5 < at < 9.8, at
    assert len(items(cid)) == n_before
assert c.post(f"/collections/{cid}/items/9999/split", json={"at": 3}).status_code == 404
pc = project("P")
assert c.post(f"/collections/{pc}/items/{items(pc)[0]['item_id']}/split", json={"at": 1}).status_code == 422
# atomicity: a failure after the first write rolls the whole thing back
cid = project("V")
a = items(cid)[0]["item_id"]
c.patch(f"/collections/{cid}/edit/clips/{a}", json={"speed": 2})
before, rev_before = items(cid), c.get(f"/collections/{cid}/edit").json()["rev"]
real_renumber = library.renumber
library.renumber = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    c.post(f"/collections/{cid}/items/{a}/split", json={"at": 5})
    raise AssertionError("expected the injected failure")
except RuntimeError:
    pass
library.renumber = real_renumber
assert items(cid) == before and c.get(f"/collections/{cid}/edit").json()["rev"] == rev_before

# ---- cues
cid = project("V", "W")
a, b = [i["item_id"] for i in items(cid)]
for u, lines in (("V", [(1.0, 3.0, "hello there"), (4.0, 6.0, "second line")]), ("W", [(0.0, 2.0, "other clip")])):
    captions.store(u, "ok", "en", [captions.Line(t0, t1, text, ()) for t0, t1, text in lines])
c.patch(f"/collections/{cid}/edit/clips/{a}", json={"captions": True})
captions.run_whisper = captions.transcribe = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not transcribe"))
r = c.get(f"/collections/{cid}/captions/cues")
assert r.status_code == 200, r.text
cues = r.json()["cues"]
assert [(x["text"], x["item_id"]) for x in cues] == [("hello there", a), ("second line", a)], cues
assert abs(cues[0]["t0"] - 1.0) < 0.05 and abs(cues[0]["t1"] - 3.0) < 0.05
by = {x["item_id"]: x for x in r.json()["clips"]}
assert by[a]["selected"] and not by[b]["selected"] and by[a]["speech"] == "speech" and by[a]["lang"] == "en", by
# second clip's cues land after the first clip (output time) once it is on too
c.patch(f"/collections/{cid}/edit/clips/{b}", json={"captions": True})
cues = c.get(f"/collections/{cid}/captions/cues").json()["cues"]
assert [x["item_id"] for x in cues] == [a, a, b] and cues[2]["t0"] >= 12.0 - 1e-6, cues
c.patch(f"/collections/{cid}/edit/clips/{a}", json={"captions": False})
c.patch(f"/collections/{cid}/edit/clips/{b}", json={"captions": False})
assert c.get(f"/collections/{cid}/captions/cues").json()["cues"] == []
assert c.get("/collections/9999/captions/cues").status_code == 404
ac = c.post("/collections", json={"name": "a", "kind": "album"}).json()["id"]
assert c.get(f"/collections/{ac}/captions/cues").status_code == 422

# ---- audio file + peaks
wav = tmp / "tone.wav"
lavfi("sine=f=440:d=3", wav)
cid = project("V")
assert c.put(f"/collections/{cid}/edit/audio/vo", json={"path": str(wav)}).status_code == 200
r = c.get(f"/collections/{cid}/edit/audio/vo/file")
assert r.status_code == 200 and r.content == wav.read_bytes()
assert c.get(f"/collections/{cid}/edit/audio/vo/file", headers={"Range": "bytes=0-99"}).status_code == 206
r = c.get(f"/collections/{cid}/edit/audio/vo/peaks")
p = r.json()
assert r.status_code == 200 and len(p["peaks"]) == 600 and abs(p["duration"] - 3.0) < 0.05, (r.status_code, len(p["peaks"]), p["duration"])
assert all(0 <= v <= 1 for v in p["peaks"]) and max(p["peaks"]) >= 0.99 and sum(p["peaks"]) / 600 > 0.5
assert len(list(edit.PEAKS.glob("*.json"))) == 1
real_run = subprocess.run
edit.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("second call must come from the cache"))
assert c.get(f"/collections/{cid}/edit/audio/vo/peaks").json() == p
edit.subprocess.run = real_run
assert len(c.get(f"/collections/{cid}/edit/audio/vo/peaks?n=50").json()["peaks"]) == 50
assert c.get(f"/collections/{cid}/edit/audio/vo/peaks?n=0").status_code == 422
assert c.get(f"/collections/{cid}/edit/audio/vo/peaks?n=999999").status_code == 422
# only recorded tracks are served: unknown id, other project, deleted track, vanished file
assert c.get(f"/collections/{cid}/edit/audio/nope/file").status_code == 404
assert c.get(f"/collections/{cid}/edit/audio/nope/peaks").status_code == 404
other = project("W")
assert c.get(f"/collections/{other}/edit/audio/vo/file").status_code == 404
c.put(f"/collections/{cid}/edit/audio/gone", json={"path": str(wav)})
gone = tmp / "gone.wav"
wav.rename(gone)
assert c.get(f"/collections/{cid}/edit/audio/gone/file").status_code == 404
assert c.get(f"/collections/{cid}/edit/audio/gone/peaks").status_code == 404
gone.rename(wav)
c.delete(f"/collections/{cid}/edit/audio/vo")
assert c.get(f"/collections/{cid}/edit/audio/vo/file").status_code == 404
assert c.get(f"/collections/{ac}/edit/audio/vo/file").status_code == 422

print("ok")
