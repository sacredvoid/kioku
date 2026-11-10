"""Checks for the edit spec: validation, storage, timeline math, looks.  uv run test_edit.py  (ffprobe and ffmpeg for one tiny audio fixture)"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import core  # noqa: E402
assert "data/index.db" not in str(core.DB_PATH), core.DB_PATH
import edit  # noqa: E402
import library  # noqa: E402

started = []
sys.modules["captions"] = types.SimpleNamespace(start_transcription=lambda cid: started.append(cid), likely_speech=lambda u: u == "V")

db = core.connect()
for u, kind, dur in [("P", "image", 0), ("V", "video", 12.0), ("S", "poster", 30.0)]:
    db.execute("insert into assets(uuid, kind, duration, meta, created) values (?,?,?,?,?)", (u, kind, dur, "", "2026-11-01"))
db.commit()

app = FastAPI()
app.include_router(library.router)
app.include_router(edit.router)
app.add_exception_handler(library.RequestValidationError, library.validation_error)
c = TestClient(app)


def project(*uuids):
    cid = c.post("/collections", json={"name": "p", "kind": "project"}).json()["id"]
    c.post(f"/collections/{cid}/items", json={"uuids": list(uuids)})
    return cid, [i["item_id"] for i in c.get(f"/collections/{cid}").json()["items"]]


def trim(cid, iid, t0, t1):
    assert c.patch(f"/collections/{cid}/items/{iid}", json={"t0": t0, "t1": t1}).status_code == 200


# defaults, and albums have no edit
cid, ids = project("V", "V", "P")
g = c.get(f"/collections/{cid}/edit").json()
assert g["spec"] == edit.default_spec() and g["rev"] == 0 and g["options"]["looks"] == [*sorted(edit.LOOKS), *edit.LUTS], g
alb = c.post("/collections", json={"name": "a", "kind": "album"}).json()["id"]
assert c.get(f"/collections/{alb}/edit").status_code == 422
assert c.patch(f"/collections/{alb}/edit", json={"fps": 24}).status_code == 422
assert c.get("/collections/9999/edit").status_code == 404

# clamps are reported; sparse storage; defaults delete the entry
a = ids[0]
r = c.patch(f"/collections/{cid}/edit/clips/{a}", json={"speed": 9, "volume": 3, "exposure": 0.2}).json()
assert {x["field"]: (x["from"], x["to"]) for x in r["clamped"]} == {"speed": (9, 4), "volume": (3, 2)}, r
assert r["spec"]["clips"][str(a)] == {"speed": 4, "volume": 2, "adjust": {"exposure": 0.2}}, r["spec"]
r = c.patch(f"/collections/{cid}/edit/clips/{a}", json={"speed": 1, "volume": None, "exposure": 0}).json()
assert r["spec"]["clips"] == {} and r["rev"] == 2, r
# bad enum, unknown key, bad look: 422 with the allowed values
for body in ({"reframe_mode": "zoom"}, {"nope": 1}, {"look": "sepia"}):
    r = c.patch(f"/collections/{cid}/edit/clips/{a}", json=body)
    assert r.status_code == 422, (body, r.text)
assert "warm" in c.patch(f"/collections/{cid}/edit/clips/{a}", json={"look": "sepia"}).text
r = c.patch(f"/collections/{cid}/edit", json={"aspect": "3:2"})
assert r.status_code == 422 and "16:9" in r.text, r.text
assert c.patch(f"/collections/{cid}/edit", json={"size": 999}).status_code == 422
assert c.patch(f"/collections/{cid}/edit", json={"aspect": None}).status_code == 422
assert c.patch(f"/collections/{cid}/edit", content=b'{"look_strength": NaN}',
               headers={"content-type": "application/json"}).status_code == 422
assert c.patch(f"/collections/{cid}/edit/clips/{ids[2]}", json={"speed": 2}).status_code == 422  # photo
J = {"content-type": "application/json"}
assert c.patch(f"/collections/{cid}/edit/clips/{a}", content='{"speed": Infinity}', headers=J).status_code == 422  # was clamped, then 500 on the reply
assert c.patch(f"/collections/{cid}/edit", content='{"look_strength": NaN}', headers=J).status_code == 422
assert c.patch(f"/collections/{cid}/edit/clips/{ids[2]}", json={"speed": 1}).status_code == 200
assert c.patch(f"/collections/{cid}/edit/clips/99999", json={"volume": 1}).status_code == 404
assert c.get(f"/collections/{cid}/edit").json()["rev"] == 3  # failed requests changed nothing

# project settings, project look, clip look override, strength alone
r = c.patch(f"/collections/{cid}/edit", json={"aspect": "9:16", "look": "warm", "look_strength": 1.5}).json()
assert r["spec"]["format"]["aspect"] == "9:16" and r["spec"]["look"] == {"name": "warm", "strength": 1.0}
assert r["clamped"] == [{"field": "look_strength", "from": 1.5, "to": 1.0}]
assert c.patch(f"/collections/{cid}/edit/clips/{a}", json={"look_strength": 0.5}).json()["spec"]["clips"][str(a)]["look"] == {"name": "warm", "strength": 0.5}
tl, _ = edit.snapshot(cid)
assert tl.segs[0].look == ("warm", 0.5) and tl.segs[1].look == ("warm", 1.0)
c.patch(f"/collections/{cid}/edit/clips/{a}", json={"look": "none"})
assert edit.snapshot(cid)[0].segs[0].look is None
c.patch(f"/collections/{cid}/edit/clips/{a}", json={"look": None})
assert edit.snapshot(cid)[0].segs[0].look == ("warm", 1.0)
c.patch(f"/collections/{cid}/edit", json={"look": None})
assert edit.snapshot(cid)[0].segs[1].look is None
assert c.patch(f"/collections/{cid}/edit/clips/{ids[1]}", json={"look_strength": 0.5}).status_code == 422  # no look to adjust

# captions: transcription starts only when the flag flips on
c.patch(f"/collections/{cid}/edit", json={"captions": True, "caption_style": "box"})
c.patch(f"/collections/{cid}/edit", json={"captions": True})
c.patch(f"/collections/{cid}/edit", json={"captions": False})
c.patch(f"/collections/{cid}/edit", json={"captions": True})
assert started == [cid, cid], started
# turning captions on with nothing picked preselects the clips that likely have speech (not the photo); later flips keep the picks
sp = c.get(f"/collections/{cid}/edit").json()["spec"]
assert sp["clips"][str(ids[0])]["captions"] and sp["clips"][str(ids[1])]["captions"] and str(ids[2]) not in sp["clips"], sp
c.patch(f"/collections/{cid}/edit", json={"caption_clips": [ids[0]]})
assert started == [cid] * 3, "picking clips starts the transcription"
c.patch(f"/collections/{cid}/edit", json={"captions": False})
c.patch(f"/collections/{cid}/edit", json={"captions": True})
sp = c.get(f"/collections/{cid}/edit").json()["spec"]
assert sp["clips"][str(ids[0])]["captions"] and "captions" not in sp["clips"].get(str(ids[1]), {}), "an existing pick is not overridden"
assert started == [cid] * 4
c.patch(f"/collections/{cid}/edit", json={"caption_clips": []})
sp = c.get(f"/collections/{cid}/edit").json()["spec"]
assert not any("captions" in e for e in sp["clips"].values()), sp
assert c.patch(f"/collections/{cid}/edit/clips/{ids[1]}", json={"captions": True}).status_code == 200
assert started == [cid] * 6 and edit.load(cid)[0]["clips"][str(ids[1])]["captions"] is True
c.patch(f"/collections/{cid}/edit/clips/{ids[1]}", json={"captions": False})
assert str(ids[1]) not in edit.load(cid)[0]["clips"], "false is the default and is not stored"
assert c.patch(f"/collections/{cid}/edit/clips/{ids[2]}", json={"captions": True}).status_code == 422  # photo
for bad in ({"caption_clips": [ids[2]]}, {"caption_clips": [99999]}, {"caption_style": "neon"}, {"caption_position": "left"},
            {"caption_font": "comic"}, {"caption_size": "XL"}, {"caption_color": "red"}, {"caption_highlight_color": "#FFF"},
            {"caption_max_words": 13}, {"caption_max_words": -1}):
    assert c.patch(f"/collections/{cid}/edit", json=bad).status_code == 422, bad
n_started = len(started)
good = {"caption_style": "pop", "caption_position": "middle", "caption_font": "impact", "caption_size": "L", "caption_color": "#00ff00",
        "caption_highlight_color": "#FF0000", "caption_highlight": True, "caption_uppercase": True, "caption_max_words": 2}
assert c.patch(f"/collections/{cid}/edit", json=good).status_code == 200
cp = edit.load(cid)[0]["captions"]
assert cp == {"on": True, "style": "pop", "position": "middle", "font": "impact", "size": "L", "color": "#00ff00",
              "highlight_color": "#FF0000", "highlight": True, "uppercase": True, "max_words": 2}, cp
assert len(started) == n_started + 1, "pop / highlight need word times: transcription is asked again"
assert set(c.get(f"/collections/{cid}/edit").json()["options"]["caption_fonts"]) == set(edit.CAPTION_FONTS)
# a spec saved before the style options reads back with the defaults filled in
with edit.conn() as d:
    d.execute("update edit_specs set spec = ? where collection_id = ?", ('{"version":1,"format":{"aspect":"16:9","size":1080,"fps":30},"look":{"name":null,"strength":1.0},"clips":{},"transitions":{},"captions":{"on":true,"style":"box"}}', cid))
    d.commit()
assert edit.load(cid)[0]["captions"] == {**edit.CAPTIONS_DEFAULT, "on": True, "style": "box"}
assert edit.snapshot(cid)[1]["captions"]["position"] == "bottom"

# orphans are pruned when an item is removed; deleting the collection deletes the spec
c.put(f"/collections/{cid}/edit/transitions/{ids[1]}", json={"type": "dissolve"})
c.patch(f"/collections/{cid}/edit/clips/{ids[1]}", json={"volume": 0.5})
c.delete(f"/collections/{cid}/items/{ids[1]}")
sp = c.get(f"/collections/{cid}/edit").json()["spec"]
assert str(ids[1]) not in sp["clips"] and str(ids[1]) not in sp["transitions"], sp
c.delete(f"/collections/{cid}")
with edit.conn() as d:
    assert d.execute("select count(*) from edit_specs where collection_id = ?", (cid,)).fetchone()[0] == 0

# mutate under 8 threads: nothing lost, rev counts every mutation
cid, ids = project(*["V"] * 8)
errs = []


def work(iid):
    try:
        edit.mutate(cid, lambda spec, col: spec["clips"].setdefault(str(iid), {}).update(volume=0.5) or [])
    except Exception as e:  # noqa: BLE001
        errs.append(e)


ts = [threading.Thread(target=work, args=(i,)) for i in ids]
[t.start() for t in ts]
[t.join() for t in ts]
sp, rev = edit.load(cid)
assert not errs and rev == 8 and sorted(sp["clips"]) == sorted(map(str, ids)), (errs, rev, sp)

# a failing mutation rolls back
try:
    edit.mutate(cid, lambda spec, col: spec["clips"].clear() or 1 / 0)
except ZeroDivisionError:
    pass
assert edit.load(cid)[1] == 8 and len(edit.load(cid)[0]["clips"]) == 8

# timeline: speed 2 (2 s) -0.5 s dissolve- speed 0.5 (4 s) -cut- photo (3 s), at 30 fps
cid, (i1, i2, i3) = project("V", "V", "P")
trim(cid, i1, 0, 4)
trim(cid, i2, 0, 2)
c.patch(f"/collections/{cid}/edit/clips/{i1}", json={"speed": 2})
c.patch(f"/collections/{cid}/edit/clips/{i2}", json={"speed": 0.5})
c.put(f"/collections/{cid}/edit/transitions/{i1}", json={"type": "dissolve", "duration": 0.5})
c.put(f"/collections/{cid}/edit/transitions/{i2}", json={"type": "cut"})
tl, _ = edit.snapshot(cid)
assert [s.frames for s in tl.segs] == [60, 120, 90] and [s.start_frame for s in tl.segs] == [0, 45, 165], tl
assert tl.total_frames == 255 and tl.duration == 8.5 and tl.warnings == ()
assert tl.segs[0].trans_after == ("fade", 15) and tl.segs[1].trans_after is None
assert tl.segs[2].kind == "photo" and (tl.segs[2].src_in, tl.segs[2].src_out) == (0.0, 3.0)
g = c.get(f"/collections/{cid}/edit").json()
assert g["duration"] == 8.5 and g["clips"][1]["start"] == 1.5 and g["clips"][1]["end"] == 5.5 and g["edit_hash"] == tl.edit_hash
# edit_hash: stable for a no-op patch, changes with an in/out edit
c.patch(f"/collections/{cid}/edit/clips/{i1}", json={"speed": 2})
assert edit.snapshot(cid)[0].edit_hash == tl.edit_hash
trim(cid, i1, 0, 5)
assert edit.snapshot(cid)[0].edit_hash != tl.edit_hash
# duration 9 on a poster with no trim uses the asset duration; an untrimmed photo is 3 s
assert edit.snapshot(project("S")[0])[0].total_frames == 900

# transition caps: 2 s on 1 s clips is halved with a warning; neighbours never overlap more than the clip
cid, ids = project("V", "V", "V")
for i in ids:
    trim(cid, i, 0, 1)
for i in ids[:2]:
    c.put(f"/collections/{cid}/edit/transitions/{i}", json={"type": "wipe-left", "duration": 5})
tl, spec = edit.snapshot(cid)
assert spec["transitions"][str(ids[0])]["duration"] == 2  # clamped on write
tf = [s.trans_after[1] if s.trans_after else 0 for s in tl.segs]
assert tf == [15, 15, 0] and len(tl.warnings) == 2 and tl.segs[0].trans_after[0] == "wipeleft", (tf, tl.warnings)
assert all(tf[i - 1 if i else 0] * (i > 0) + tf[i] <= s.frames for i, s in enumerate(tl.segs))
assert tl.total_frames == 30 * 3 - 30
# a transition that would be under 3 frames becomes a cut
cid, ids = project("V", "V")
trim(cid, ids[0], 0, 0.1)
trim(cid, ids[1], 0, 2)
c.put(f"/collections/{cid}/edit/transitions/{ids[0]}", json={"type": "dissolve", "duration": 0.5})
tl, _ = edit.snapshot(cid)
assert tl.segs[0].trans_after is None and tl.total_frames == 3 + 60 and any("cut" in w for w in tl.warnings) and any("only" in w for w in tl.warnings), tl.warnings
assert edit.compute_timeline([], edit.default_spec()).total_frames == 0

# reframe and adjust reach the Seg
cid, (i1,) = project("V")
c.patch(f"/collections/{cid}/edit/clips/{i1}", json={"reframe_mode": "pan", "reframe_from": 0.2, "reframe_to": 2, "contrast": 1.1, "warmth": -5})
s = edit.snapshot(cid)[0].segs[0]
assert s.reframe == ("pan", 0.2, 1) and s.adjust == (0, 1.1, 1, -1), s

# output_size: even, right orientation, preview short side at most 540
assert edit.output_size("9:16", 1080, False) == (1080, 1920) and edit.output_size("16:9", 1080, False) == (1920, 1080)
assert edit.output_size("16:9", 1080, True) == (960, 540) and edit.output_size("9:16", 720, True) == (540, 960)
assert edit.output_size("4:5", 1080, False) == (1080, 1350) and edit.output_size("1:1", 2160, True) == (540, 540)
for asp in edit.ASPECTS:
    for sz in edit.SIZES:
        for pv in (True, False):
            w, h = edit.output_size(asp, sz, pv)
            assert w % 2 == 0 and h % 2 == 0, (asp, sz, pv, w, h)
try:
    edit.output_size("3:2", 1080, False)
    raise SystemExit("bad aspect accepted")
except ValueError:
    pass

# looks: only safe characters, nothing for none, identity at strength 0, strength scales
safe = re.compile(r"^[a-z0-9_=:.,-]*$")
assert edit.look_filters(None) == "" and edit.look_filters((None, 1.0)) == ""
for name in edit.LOOKS:
    for k in (1.0, 0.5):
        f = edit.look_filters((name, k))
        assert f and safe.match(f), (name, k, f)
    assert edit.look_filters((name, 0)) == "", name
assert edit.look_filters(("warm", 1.0)) == ("eq=contrast=1.06:saturation=1.2:brightness=0.02,"
                                            "colorbalance=rs=0.1:bs=-0.1:rm=0.09:bm=-0.09:rh=0.06:bh=-0.08")
assert edit.look_filters(("warm", 0.5)) == ("eq=contrast=1.03:saturation=1.1:brightness=0.01,"
                                            "colorbalance=rs=0.05:bs=-0.05:rm=0.045:bm=-0.045:rh=0.03:bh=-0.04")
assert edit.look_filters(("mono", 1.0)) == "eq=contrast=1.25:saturation=0:brightness=-0.02"

# fades, clip_volume, duck: defaults keep old renders identical, clamps, warning for a transition after the last clip
cid, ids = project("V", "V")
tl, spec = edit.snapshot(cid)
assert (tl.fade_in, tl.fade_out, tl.audio, tl.clip_volume, tl.duck) == (0.0, 0.0, (), 1.0, True)
r = c.patch(f"/collections/{cid}/edit", json={"fade_in": 1.5, "fade_out": 9, "clip_volume": 0.5, "duck": False}).json()
assert r["spec"]["format"]["fade_in"] == 1.5 and r["spec"]["format"]["fade_out"] == 3.0 and r["clamped"][0]["field"] == "fade_out"
assert r["spec"]["clip_volume"] == 0.5 and r["spec"]["duck"] is False
tl, _ = edit.snapshot(cid)
assert (tl.fade_in, tl.fade_out, tl.clip_volume, tl.duck) == (1.5, 3.0, 0.5, False)
assert c.patch(f"/collections/{cid}/edit", json={"fade_in": -1}).json()["spec"]["format"]["fade_in"] == 0
assert c.patch(f"/collections/{cid}/edit", json={"clip_volume": 4}).json()["spec"]["clip_volume"] == 1.0
assert not any("last clip" in w for w in tl.warnings)
c.put(f"/collections/{cid}/edit/transitions/{ids[1]}", json={"type": "dissolve", "duration": 0.5})
tl, spec = edit.snapshot(cid)
assert any("last clip" in w for w in tl.warnings) and tl.segs[-1].trans_after is None, tl.warnings
# a spec saved before fades / audio existed gains the defaults
with edit.conn() as db2:
    old = edit.default_spec()
    for k in ("audio", "clip_volume", "duck"):
        del old[k]
    del old["format"]["fade_in"], old["format"]["fade_out"]
    db2.execute("insert or replace into edit_specs values (?,?,?,?)", (cid, json.dumps(old), 7, 0.0))
    db2.commit()
assert c.get(f"/collections/{cid}/edit").json()["spec"]["audio"] == []

# mute: per clip, becomes volume 0 in the Seg, default removes the key
cid, (i1, i2) = project("V", "V")
c.patch(f"/collections/{cid}/edit/clips/{i1}", json={"mute": True, "volume": 1.5})
tl, spec = edit.snapshot(cid)
assert tl.segs[0].volume == 0.0 and tl.segs[1].volume == 1.0 and spec["clips"][str(i1)]["mute"] is True
c.patch(f"/collections/{cid}/edit/clips/{i1}", json={"mute": False})
assert "mute" not in edit.snapshot(cid)[1]["clips"][str(i1)] and edit.snapshot(cid)[0].segs[0].volume == 1.5

# audio tracks: validation, merge on update, limit, delete, hash
voice = os.path.join(tempfile.mkdtemp(), "vo.wav")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=500:d=6", voice], check=True)
txt = voice.replace(".wav", ".txt")
open(txt, "w").write("not audio")
fake = voice.replace(".wav", ".mp3")
open(fake, "w").write("not audio")
A = f"/collections/{cid}/edit/audio"
h0 = edit.snapshot(cid)[0].edit_hash
for bad in ({"path": txt}, {"path": "/nonexistent/x.wav"}, {"path": fake}, {}, {"path": voice, "bogus": 1}):
    assert c.put(f"{A}/vo", json=bad).status_code == 422, bad
assert c.put(f"{A}/bad id", json={"path": voice}).status_code in (404, 422)
assert c.put(f"{A}/{'x' * 40}", json={"path": voice}).status_code == 422
r = c.put(f"{A}/vo", json={"path": voice, "start": 1, "volume": 3}).json()
t = r["spec"]["audio"][0]
assert t == {"id": "vo", "start": 1.0, "in": 0.0, "out": 6.0, "volume": 2.0, "fade_in": 0.0, "fade_out": 0.0,
             "path": str(Path(voice).resolve())}, t
assert r["clamped"][0]["field"] == "volume"
r = c.put(f"{A}/vo", json={"in": 1, "out": 99, "fade_in": 50}).json()  # no path: the rest is kept, clamped to the file
t = r["spec"]["audio"][0]
assert (t["in"], t["out"], t["start"], t["fade_in"]) == (1.0, 6.0, 1.0, 5.0), t
assert c.put(f"{A}/new", json={"volume": 1}).status_code == 422  # a new track needs a path
for n in ("m1", "m2"):
    assert c.put(f"{A}/{n}", json={"path": voice}).status_code == 200
assert c.put(f"{A}/m3", json={"path": voice}).status_code == 422  # at most 3
tl, spec = edit.snapshot(cid)
assert [t["id"] for t in tl.audio] == ["vo", "m1", "m2"] and tl.edit_hash != h0
assert c.delete(f"{A}/m1").status_code == 200 and c.delete(f"{A}/m1").status_code == 404
assert [t["id"] for t in c.get(f"/collections/{cid}/edit").json()["spec"]["audio"]] == ["vo", "m2"]
assert c.put(f"{A}/vo", json={"volume": 0.5}).json()["spec"]["audio"][0]["volume"] == 0.5
alb = c.post("/collections", json={"name": "a2", "kind": "album"}).json()["id"]
assert c.put(f"/collections/{alb}/edit/audio/vo", json={"path": voice}).status_code == 422

# size limit
cid, (i1,) = project("V")
big = lambda spec, col: spec["clips"].__setitem__(str(i1), {"junk": "x" * 200_000}) or []  # noqa: E731
try:
    edit.mutate(cid, big)
    raise SystemExit("oversize spec accepted")
except Exception as e:  # noqa: BLE001
    assert getattr(e, "status_code", None) == 422, e
print("edit checks passed")
