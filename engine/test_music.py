"""Checks for the music map and auto fit (notes/editing-power-research.md M1, M2).
uv run test_music.py   (no ffmpeg needed; beats.analyze is stubbed with the exact grid of the synthetic track)"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import os
import sys
import tempfile
import types
import wave
from pathlib import Path

T = Path(tempfile.mkdtemp())
os.environ["CS_DB"] = str(T / "t.db")
os.environ["KIOKU_DATA"] = str(T / "data")
os.environ["KIOKU_DB_KEY"] = "0" * 64

import numpy as np  # noqa: E402

import beats  # noqa: E402
import core  # noqa: E402

testenv.check()
import edit  # noqa: E402
import library  # noqa: E402
import music  # noqa: E402

assert str(core.DATA).startswith(str(T)), core.DATA

# 120 BPM, 2 s bars, 16 bars: quiet for 6 bars, then a kick-driven drop at 12 s
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
beats.analyze = lambda p: AN

m = music.analyze(song)
assert m["drop_s"] == 12.0 and m["drop_bar"] == 6 and len(m["bars"]) == 16 and m["vocals"] is None, m
assert m["energy"][:6] < m["energy"][6:7] and m["energy"][6] == "9", m["energy"]
assert [s["start"] for s in m["sections"]][:2] == [0.0, 12.0], m["sections"]
assert music.cached(song) == m  # second call comes from the cache
assert music.analyze(song) == m
try:
    music.build(x[: 3 * SR].astype("float32"), {"downbeats": [0.0, 2.0]})
    raise SystemExit("two downbeats should not be a map")
except RuntimeError:
    pass

# fit: cuts on bars, drop on drop_at, out - in never past the target, fade over the last bar
f = music.fit(m, 8.0, 4.0)
assert (f["in"], f["out"], f["drop_out_s"], f["music_end_s"], f["fade_out"]) == (8.0, 16.0, 4.0, 8.0, 2.0), f
f = music.fit(m, 9.0, 3.0)  # 4 whole bars fit in 9 s; the drop lands within half a bar of 3 s
assert f["out"] - f["in"] == 8.0 and abs(f["drop_out_s"] - 3.0) <= 1.0 and f["in"] % 2 == 0, f
f = music.fit(m, 100.0)  # longer than the track: plays whole, says so
assert (f["in"], f["out"]) == (0.0, 32.0) and f["notes"], f
assert music.fit({**m, "drop_s": None, "drop_bar": None}, 8.0, 3.0)["in"] == 0.0

# API: fit through put_audio, explicit values win, old tracks untouched, get_edit shows energy
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
sys.modules["captions"] = types.SimpleNamespace(start_transcription=lambda cid: None, likely_speech=lambda u: False)
db = core.connect()
db.execute("insert into assets(uuid, kind, duration, meta, created) values ('V','video',12.0,'','2026-11-01')")
db.commit()
app = FastAPI()
app.include_router(library.router)
app.include_router(edit.router)
c = TestClient(app)
cid = c.post("/collections", json={"name": "p", "kind": "project"}).json()["id"]
c.post(f"/collections/{cid}/items", json={"uuids": ["V", "V", "V"]})
ids = [i["item_id"] for i in c.get(f"/collections/{cid}").json()["items"]]
for i, iid in enumerate(ids):
    c.patch(f"/collections/{cid}/items/{iid}", json={"t0": i * 4.0, "t1": i * 4.0 + 4.0})
A = f"/collections/{cid}/edit/audio"

r = c.put(f"{A}/plain", json={"path": str(song)}).json()  # no fit: whole file, as before
assert r["spec"]["audio"][0]["in"] == 0.0 and r["spec"]["audio"][0]["out"] == 32.0 and "fit" not in r, r
assert c.delete(f"{A}/plain").status_code == 200

r = c.put(f"{A}/m", json={"path": str(song), "fit": True, "target_seconds": 8, "drop_at": 4}).json()
tr = r["spec"]["audio"][0]
assert (tr["in"], tr["out"], tr["start"], tr["fade_out"]) == (8.0, 16.0, 0.0, 2.0) and r["fit"]["drop_out_s"] == 4.0, r
r = c.put(f"{A}/m", json={"fit": True, "target_seconds": 8, "drop_at": 4, "fade_out": 0.5, "start": 1}).json()  # explicit wins
assert (r["spec"]["audio"][0]["fade_out"], r["spec"]["audio"][0]["start"]) == (0.5, 1.0), r
r = c.put(f"{A}/m", json={"fit": True, "start": 0}).json()  # no target: the edit's own 12 s
assert r["spec"]["audio"][0]["out"] - r["spec"]["audio"][0]["in"] == 12.0 and r["fit"]["music_end_s"] == 12.0, r
assert c.put(f"{A}/n", json={"fit": True}).status_code == 422  # a new track needs a path

mp = c.get(f"{A}/m/map").json()
assert mp["drop_s"] == 12.0 and len(mp["energy"]) == 16, mp
assert c.get(f"{A}/nope/map").status_code == 404

# sync on: get_edit reports the drop on the output timeline and each clip's bar energy
c.put(f"{A}/m", json={"fit": True, "target_seconds": 12, "drop_at": 4})
assert c.patch(f"/collections/{cid}/edit", json={"sync_track": "m"}).status_code == 200
g = c.get(f"/collections/{cid}/edit").json()
assert g["music"] == {"track": "m", "drop_out_s": 4.0}, g["music"]
assert [x["energy"] for x in g["clips"]][0] <= [x["energy"] for x in g["clips"]][-1] and "energy" in g["clips"][0], g["clips"]
print("music checks passed")
