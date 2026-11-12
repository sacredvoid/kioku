"""Checks for suggest_seams (notes/editing-power-research.md, X4). uv run test_seams.py  (no ffmpeg; caches are written by hand)"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import json
import os
import sys
import tempfile
import types
from pathlib import Path

T = Path(tempfile.mkdtemp())
os.environ["CS_DB"] = str(T / "t.db")
os.environ["KIOKU_DATA"] = str(T / "data")
os.environ["KIOKU_DB_KEY"] = "0" * 64

import numpy as np  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import beats  # noqa: E402
import core  # noqa: E402

testenv.check()
import edit  # noqa: E402
import library  # noqa: E402
import media  # noqa: E402
import moments  # noqa: E402
import music  # noqa: E402
import seams  # noqa: E402

assert str(core.DATA).startswith(str(T)), core.DATA

# ---- pure rules
base = dict(beat="downbeat", energy_out=8, energy_in=8, similarity=0.3, similarity_by="embedding", place_change=False,
            gap_hours=0.1, motion_out={"amount": 6.0, "label": "fast", "direction": "right"},
            motion_in={"amount": 6.0, "label": "fast", "direction": "right"})


def S(**kw):
    return seams.suggest({**base, **kw})


w = S()
assert w["type"] == "whip-right" and w["sfx"] == "whoosh" and w["duration"] <= edit.MAX_WHIP, w
assert S(beat="beat")["type"] == "cut"                                           # whips only on a downbeat
assert S(beat=None, energy_out=None, energy_in=None)["type"] == "cut"            # unsynced: never a whip
assert S(energy_out=2, energy_in=3)["type"] == "cut"                             # and only in a loud bar
assert S(motion_in={"direction": "left"})["type"] == "cut"                       # opposite motion: no whip
assert S(motion_out={"amount": 0.5, "direction": "right"})["type"] == "cut"      # nothing moving
assert S(motion_out={"amount": 6.0, "direction": None})["type"] == "cut"         # no direction to follow
assert S(motion_out={"amount": 6.0, "direction": "up"}, motion_in={})["type"] == "whip-up"
assert S(similarity=0.9)["type"] == "cut" and "match cut" in S(similarity=0.9)["reason"]
still = dict(motion_out={})
assert S(**still, place_change=True)["type"] == "zoom-in"                        # new place, downbeat, loud
assert S(**still, place_change=True, energy_out=2, energy_in=2)["type"] == "dissolve"
assert S(**still, place_change=True, beat="beat")["type"] == "cut"               # music is moving: stay on the beat
assert S(**still, place_change=None, gap_hours=30, energy_out=None, energy_in=None, beat=None)["type"] == "dissolve"
assert S(**still, energy_out=7, energy_in=2, beat="beat")["type"] == "dissolve"  # energy drop
assert S(**still, beat="beat", energy_out=5, energy_in=5)["type"] == "cut"
assert S(**still, similarity=None, similarity_by=None, motion_in=None, beat=None, energy_out=None, energy_in=None,
         gap_hours=None, place_change=None)["type"] == "cut"                      # all unknown: hard cut


def seam(at, typ):
    return {"at": at, "suggestion": {"type": typ, "duration": .3, "sfx": None, "reason": "x"}}


p = seams.plan([seam(2, "whip-left"), seam(5, "whip-right"), seam(11, "whip-up")] + [seam(20 + i, "cut") for i in range(7)])
assert [x["suggestion"]["type"] for x in p[:3]] == ["whip-left", "cut", "whip-up"], p   # whips at least 8 s apart
assert "dropped" in p[1]["suggestion"]["reason"]
p = seams.plan([seam(i, t) for i, t in enumerate(["dissolve", "dissolve", "zoom-in", "cut"] * 2 + ["cut", "dissolve"])])
kept = [x["suggestion"]["type"] for x in p if x["suggestion"]["type"] != "cut"]  # 10 seams: at most 3, zoom ranks first
assert len(kept) == 3 and kept.count("zoom-in") == 2, kept

# ---- a project with cached analysis only; any attempt to analyse fails the test
sys.modules["captions"] = types.SimpleNamespace(start_transcription=lambda cid: None, likely_speech=lambda u: False)


def boom(*a, **k):
    raise AssertionError("suggest_seams must never analyse anything")


song = T / "song.wav"
song.write_bytes(b"x")
AN = {"tempo": 120.0, "beats": [i * 0.5 for i in range(64)], "downbeats": [i * 2.0 for i in range(16)], "duration": 32.0}
beats.CACHE.mkdir(parents=True, exist_ok=True)
(beats.CACHE / f"{beats._key(song)}.json").write_text(json.dumps(AN))
M = {"v": 1, "tempo": 120.0, "duration": 32.0, "bar_s": 2.0, "bars": [i * 2.0 for i in range(16)], "end": 32.0,
     "energy": "2288" + "9999" + "9999" + "1111",
     "sections": [{"start": 0.0, "end": 4.0, "bar": 0, "bars": 2, "level": 2}, {"start": 4.0, "end": 16.0, "bar": 2, "bars": 6, "level": 8}],
     "drop_s": 4.0, "drop_bar": 2, "vocals": None}
music.CACHE.mkdir(parents=True, exist_ok=True)
(music.CACHE / f"{music._key(song)}.json").write_text(json.dumps(M))
music.analyze = moments.signals = media.analyze = media.ensure_proxy = boom
edit.audio_info = lambda p: (str(p), 32.0)  # the song is a stub file

db = core.connect()
rows = [("A", "2026-11-01T10:00:00", "Kyoto", 35.0, 135.7), ("B", "2026-11-01T10:30:00", "Kyoto", 35.0, 135.7),
        ("C", "2026-11-02T18:00:00", "Osaka", 34.7, 135.5), ("D", "2026-11-02T18:05:00", None, None, None)]
for u, cr, pl, la, lo in rows:
    db.execute("insert into assets(uuid, kind, duration, meta, created) values (?,?,?,?,?)", (u, "video", 12.0, "", cr))
    db.execute("insert into asset_info(uuid, place, lat, lon) values (?,?,?,?)", (u, pl, la, lo))
db.execute("insert into asset_info(uuid, place, lat, lon) values ('E', 'Gion, Kyoto', 35.01, 135.71)")  # named finer, 1.4 km from A
assert seams._place(db, "A", "E", None, None)[0] is False, "same town named at a different detail is not a new place"
for u, vec in (("A", [1, 0]), ("B", [.99, .141]), ("C", [0, 1])):  # A and B alike; D has no vector and no proxy
    db.execute("insert into segments(uuid, t0, t1, source, vec) values (?,?,?,?,?)", (u, 0, 12, "scene", np.array(vec, np.float32).tobytes()))
db.commit()


def write_sig(uuid, dx):  # 4 Hz, 12 s, busy shot whose saliency centre drifts dx per second
    n = 48
    ser = {k: [0.0] * n for k in moments.SERIES}
    ser["motion"] = [6.0] * n
    ser["sal_x"] = [.5 + dx * (i / 4 - 6) for i in range(n)]
    ser["sal_y"] = [.5] * n
    moments.MOMENTS.mkdir(parents=True, exist_ok=True)
    moments.cache_path(uuid).write_text(json.dumps({"version": moments.VERSION, "mtime": 0, "hz": 4, "duration": 12.0,
                                                    "cuts": [], "series": ser}))


write_sig("A", 0.15)    # picture drifts right
write_sig("B", 0.0)
write_sig("C", -0.15)   # D: never analysed

app = FastAPI()
app.include_router(library.router)
app.include_router(edit.router)
app.include_router(seams.router)
c = TestClient(app)
cid = c.post("/collections", json={"name": "p", "kind": "project"}).json()["id"]
c.post(f"/collections/{cid}/items", json={"uuids": ["A", "B", "C", "D"]})
ids = [i["item_id"] for i in c.get(f"/collections/{cid}").json()["items"]]
for iid in ids:
    c.patch(f"/collections/{cid}/items/{iid}", json={"t0": 4.0, "t1": 8.0})  # 4 s each

# unsynced: no music facts and no whips; place, time, similarity and motion still come from the caches
r = c.get(f"/collections/{cid}/edit/seams").json()
assert r["summary"]["seams"] == 3 and [s["seam"] for s in r["seams"]] == [1, 2, 3], r
s1, s2, s3 = r["seams"]
assert s1["beat"] is None and s1["energy_in"] is None and s1["similarity_by"] == "embedding" and s1["similarity"] > .98, s1
assert s1["suggestion"]["type"] == "cut" and "similar" in s1["suggestion"]["reason"]
assert s1["motion_out"]["direction"] == "right" and s1["motion_out"]["label"] == "fast", s1["motion_out"]
assert s2["place_change"] is True and s2["gap_hours"] == 31.5 and s2["suggestion"]["type"] == "dissolve", s2
assert s2["motion_out"]["direction"] is None and s2["motion_out"]["label"] == "fast"
assert s3["place_change"] is None and s3["similarity"] is None and s3["motion_in"]["label"] == "unknown", s3
assert s3["suggestion"]["type"] == "cut"

# synced with cached beats and map: beat, bar position, energy; 2 s clips (4 beats at 120 bpm) put seams on 2, 4, 6 s
beats.analyze = lambda p: AN  # the one allowed analysis: turning sync on (the edit's own route does it, not suggest_seams)
assert c.put(f"/collections/{cid}/edit/audio/m", json={"path": str(song), "start": 0, "in": 0, "out": 32}).status_code == 200
assert c.patch(f"/collections/{cid}/edit", json={"sync_track": "m", "sync_grid": "beat"}).status_code == 200
for iid in ids:
    assert c.patch(f"/collections/{cid}/edit/clips/{iid}", json={"beats": 4}).status_code == 200
beats.analyze = boom
r = c.get(f"/collections/{cid}/edit/seams").json()
assert [s["at"] for s in r["seams"]] == [1.967, 3.967, 5.967], [s["at"] for s in r["seams"]]  # 1 frame before the beat
assert [s["beat"] for s in r["seams"]] == ["downbeat"] * 3 and [s["bar_pos"] for s in r["seams"]] == [1, 1, 1], r["seams"]
assert [(s["energy_out"], s["energy_in"]) for s in r["seams"]] == [(2, 2), (2, 8), (8, 8)], r["seams"]
assert r["seams"][1]["section_change"] is True and r["seams"][0]["section_change"] is False
assert r["seams"][0]["suggestion"]["type"] == "cut"   # A and B look alike: a match cut, not a whip
# new place on a downbeat into a loud bar would be zoom-in, but 3 seams allow one transition: the whip on seam 3 ranks higher
assert "zoom-in dropped" in r["seams"][1]["suggestion"]["reason"], r["seams"][1]
w = r["seams"][2]["suggestion"]
assert w["type"] == "whip-left" and w["sfx"] == "whoosh" and r["summary"]["transitions_suggested"] == 1, r["seams"][2]

# read-only: the spec, the items and every file on disk are unchanged by repeated calls
c.put(f"/collections/{cid}/edit/transitions/{ids[0]}", json={"type": "dissolve", "duration": 0.5})


def snap():
    d = core.connect()
    s = [d.execute(q).fetchall() for q in ("select * from edit_specs", "select * from collection_items", "select * from segments")]
    d.close()
    return s, sorted(str(p.relative_to(T)) for p in T.rglob("*") if p.is_file() and not p.name.startswith("t.db"))


before = snap()
for _ in range(3):
    assert c.get(f"/collections/{cid}/edit/seams").status_code == 200
assert snap() == before
r = c.get(f"/collections/{cid}/edit/seams").json()
assert r["seams"][0]["current"]["type"] == "dissolve" and r["seams"][0]["change"] is True, r["seams"][0]

# no cached beats (file changed): clip timing stays unsynced, facts say unknown, a note says why
song.write_bytes(b"xy")
r = c.get(f"/collections/{cid}/edit/seams").json()
assert r["seams"][0]["beat"] is None and any("not cached" in n for n in r["notes"]), r["notes"]

# albums have no edit; unknown ids 404
al = c.post("/collections", json={"name": "a", "kind": "album"}).json()["id"]
assert c.get(f"/collections/{al}/edit/seams").status_code == 422
assert c.get("/collections/99999/edit/seams").status_code == 404
print("seam checks passed")
