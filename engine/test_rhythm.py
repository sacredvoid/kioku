"""Checks for beat sync, hits, ramps, motion, flash, shake and seam sounds (notes/pro-editing-design.md phase 1).
uv run test_rhythm.py   (ffmpeg; the API part runs beat_this once on a synthetic 120 BPM track, about 2 s)"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

T = Path(tempfile.mkdtemp())
os.environ["CS_DB"] = str(T / "t.db")
os.environ["KIOKU_DATA"] = str(T / "data")
os.environ["KIOKU_DB_KEY"] = "0" * 64

import core  # noqa: E402

testenv.check()
assert str(core.DATA).startswith(str(T)), core.DATA
import beats  # noqa: E402
import edit  # noqa: E402
import render  # noqa: E402

FPS = 30


def item(i, kind="video", t0=0.0, t1=4.0, dur=10.0):
    return {"item_id": i, "uuid": f"U{i}", "kind": kind, "t0": t0, "t1": t1, "duration": dur, "pos": i}


def spec(**kw):
    s = edit.default_spec()
    s["audio"] = [{"id": "m", "path": "/x.wav", "start": 0.0, "in": 0.0, "out": 60.0, "volume": 1, "fade_in": 0, "fade_out": 0}]
    s["sync"] = {"track": "m", "grid": "beat", "lead": 0}
    for k, v in kw.items():
        s[k] = v
    return s


GRID = [i * 0.5 for i in range(200)]  # 120 BPM: a beat every 15 frames

# ---------- timeline: cuts land on absolute grid frames, auto beats from natural length, explicit beats win
items = [item(1, t1=1.0), item(2, t1=2.1), item(3, t1=0.4)]
s = spec(clips={"3": {"beats": 3}})
tl = edit.compute_timeline(items, s, GRID)
starts = [x.start_frame for x in tl.segs]
assert starts == [0, 30, 90], starts                      # 1.0 s = 2 beats, 2.1 s = 4 beats
assert [x.frames for x in tl.segs] == [30, 60, 45], [x.frames for x in tl.segs]
assert all(f % 15 == 0 for f in starts) and tl.total_frames == 135
assert tl.segs[2].src_out - tl.segs[2].src_in == 1.5      # 3 beats of source

# lead: every cut one frame early, no drift after many clips
s = spec()
s["sync"]["lead"] = 1
tl = edit.compute_timeline([item(i, t1=0.5) for i in range(1, 41)], s, GRID)
assert [x.start_frame for x in tl.segs][1:] == [15 * k - 1 for k in range(1, 40)], tl.segs[-1].start_frame

# off-grid tempo (a beat every 14.4 frames): rounding per point, never accumulated
g = [i * 0.48 for i in range(400)]
tl = edit.compute_timeline([item(i, t1=0.48) for i in range(1, 101)], spec(), g)
assert all(x.start_frame == round(k * 0.48 * FPS) for k, x in enumerate(tl.segs)), [x.start_frame for x in tl.segs[:5]]

# transition overlap is centred on the beat
s = spec(transitions={"1": {"type": "dissolve", "duration": 0.4}})
tl = edit.compute_timeline([item(1, t1=1.0), item(2, t1=1.0)], s, GRID)
a, b = tl.segs
assert b.start_frame == 30 - 6 and a.frames == 30 + 6 and b.frames == 36 and tl.total_frames == 60, (a, b)

# hit: the in point slides so the hit lands on the nearest beat inside the clip
s = spec(clips={"1": {"hit": 3.3, "beats": 4}})
tl = edit.compute_timeline([item(1, t0=2.0, t1=4.0)], s, GRID)
x = tl.segs[0]
assert abs((3.3 - x.src_in) * FPS - 45) < 1e-6 and abs(x.src_out - x.src_in - 2.0) < 1e-9, (x.src_in, x.src_out)

# short source holds with a warning; beat_frames list the beats inside each clip
tl = edit.compute_timeline([item(1, t0=0, t1=1.0, dur=1.0)], spec(clips={"1": {"beats": 4}}), GRID)
assert tl.segs[0].src_out == 1.0 and tl.segs[0].frames == 60 and any("holds" in w for w in tl.warnings), tl.warnings
assert tl.segs[0].beat_frames == (15, 30, 45), tl.segs[0].beat_frames

# no grid (sync off): lengths come from in/out as before; ramps change the output length by their average rate
s = spec(clips={"1": {"ramp": "slow-mid"}})
s["sync"]["track"] = None
tl = edit.compute_timeline([item(1, t1=4.08)], s, GRID)
assert tl.segs[0].frames == round(4.08 / edit.ramp_factor("slow-mid") * FPS) and tl.segs[0].volume == 0.0, tl.segs[0]

# seam sound sits on the cut (on the beat when synced)
s = spec(sfx={"1": "whoosh"})
tl = edit.compute_timeline([item(1, t1=1.0), item(2, t1=1.0)], s, GRID)
assert tl.sfx == ((30, "whoosh"),), tl.sfx
fx = render.audio_fx(tl, None, tl.duration)
assert len(fx["tracks"]) == 2 and abs(fx["tracks"][1]["delay"] - (1.0 - render.SFX_META["whoosh"]["before"])) < 1e-6, fx["tracks"]

# beats.grid: shifted by the track start and in point, extended both ways
g = beats.grid({"start": 1.0, "in": 0.25, "out": 2.0}, {"beats": [0.25, 0.75, 1.25, 1.75], "downbeats": [0.25], "duration": 3}, "beat", 4)
assert g[:4] == [0.0, 0.5, 1.0, 1.5] and g[-1] == 4.0, g

# ---------- render: motion / flash / ramp / shake segments are valid ffmpeg and have the right length
W = T / "w"
W.mkdir()
src = W / "src.mp4"
subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=4", "-f", "lavfi", "-i",
                "sine=f=440:d=4", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
info = render.probe_source(src)
s = spec(clips={"1": {"motion": "push", "flash": True}, "2": {"motion": "punch", "shake": True, "beats": 4},
                "3": {"ramp": "slow-mid", "motion": "drift-left"}, "4": {"beats": 8}})
tl = edit.compute_timeline([item(1, t1=1.0, dur=4), item(2, t1=1.0, dur=4), item(3, t1=1.5, dur=4), item(4, t1=1.0, dur=1.0)], s, GRID)
for x in tl.segs:
    out = W / f"{x.item_id}.mov"
    cmd = render.seg_command(x, info, src, out, "preview", (360, 640), FPS, W)
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, (x.item_id, r.stderr[-800:])
    n = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames",
                        "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
    assert int(n) == x.frames, (x.item_id, n, x.frames)

# only moving clips pay for the perspective pass
assert "perspective" in render.motion_filter(tl.segs[0], FPS) and render.motion_filter(edit.compute_timeline([item(9)], spec(), GRID).segs[0], FPS) == ""


# ---------- eased motion: zoom read back from the perspective corners at frame x (W = H = 1, so x0 = 0.5 + dx - 1/(2z))
def zoom_at(seg, x):
    import math
    import re
    e = re.search(r"x0='([^']*)'", render.motion_filter(seg, FPS)).group(1)
    e = re.sub(r"\bin\b", "x", e).replace("W", "1").replace("H", "1")
    return 1 / (2 * (0.5 - eval(e, {"x": x, "min": min, "max": max, "exp": math.exp, "gte": lambda a, b: float(a >= b)})))


from dataclasses import replace  # noqa: E402
base = tl.segs[3]  # no fx of its own
push = replace(base, frames=60, motion=("push", 1.0), beat_frames=())
zs = [zoom_at(push, x) for x in range(60)]
assert abs(zs[0] - 1) < 1e-6 and abs(zs[-1] - 1.3) < 1e-6 and abs(zs[30] - 1.15) < 0.01, zs
assert zs[1] - zs[0] < 0.002 and zs[31] - zs[30] > 0.007 and zs[-1] - zs[-2] < 0.002, zs  # eases in and out
short = replace(push, frames=20)  # under 1 s: linear, one move through the cut
assert abs((zoom_at(short, 1) - zoom_at(short, 0)) - (zoom_at(short, 10) - zoom_at(short, 9))) < 1e-6
punch = replace(base, frames=60, motion=("punch", 1.0), beat_frames=(15, 30, 45))
pz = [zoom_at(punch, x) for x in range(60)]
assert "mod(" not in render.motion_filter(punch, FPS) and max(pz[:15]) == 1, pz
assert pz[17] > 1.27 and max(pz) <= 1.3 + 1e-9 and pz[29] < 1.1, pz  # snaps in within 2 frames, eased out by the next beat
assert 1.05 < pz[22] < pz[17], pz  # no hold: it relaxes between beats

# ---------- whips: a slide plus directional blur while the seam moves; other transitions untouched
g = render._graph(2, [60, 60], [("whipleft", 9)], FPS, dims=(540, 960))
assert "transition=slideleft" in g and g.count("dblur=angle=0:radius=24") == 2 and "enable='between(t,1.7166" in g, g
g = render._graph(2, [60, 60], [("whipup", 9)], FPS, dims=(540, 960))
assert "transition=slideup" in g and g.count("dblur=angle=90") == 2, g
assert "gblur" in render._graph(2, [60, 60], [("zoomin", 9)], FPS, dims=(540, 960))
assert "blur" not in render._graph(2, [60, 60], [("fade", 9)], FPS)
try:
    render._graph(2, [60, 60], [("whipright", 9)], FPS)
    raise AssertionError("a whip without dims must fail")
except ValueError:
    pass
assert {edit.XFADE[f"whip-{d}"] for d in ("left", "right", "up", "down")} == set(render.WHIPS)
j = W / "whip.mov"  # the whip graph runs in ffmpeg and keeps the overlap length
r = subprocess.run(render.join_command([W / "1.mov", W / "2.mov"], [tl.segs[0].frames, tl.segs[1].frames], [("whipdown", 9)], FPS, j, W,
                                       ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le"], (360, 640)), capture_output=True, text=True)
assert r.returncode == 0, r.stderr[-800:]
n = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames",
                    "-of", "csv=p=0", str(j)], capture_output=True, text=True).stdout.strip()
assert int(n) == tl.segs[0].frames + tl.segs[1].frames - 9, n
# too many whips warn; whip-up / whip-down exist
s = spec(transitions={str(i): {"type": t, "duration": 0.3} for i, t in ((1, "whip-up"), (2, "whip-down"), (3, "whip-left"))})
w = edit.compute_timeline([item(i, t1=2.0) for i in range(1, 5)], s, GRID)
assert w.segs[0].trans_after[0] == "whipup" and any("whips" in x for x in w.warnings), w.warnings

# ---------- API: sync needs an existing track, analyses the music, and get_edit reports the grid
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import library  # noqa: E402
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
    c.patch(f"/collections/{cid}/items/{iid}", json={"t0": i * 2.0, "t1": i * 2.0 + 1.1})
song = T / "song.wav"
subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                "aevalsrc='0.9*sin(2*PI*55*t)*exp(-30*mod(t,0.5))+0.1*sin(2*PI*220*t)':s=44100:d=20", str(song)], check=True)
assert c.patch(f"/collections/{cid}/edit", json={"sync_track": "m"}).status_code == 422  # no such track yet
assert c.put(f"/collections/{cid}/edit/audio/m", json={"path": str(song)}).status_code == 200
r = c.patch(f"/collections/{cid}/edit", json={"sync_track": "m", "sync_lead": 0})
assert r.status_code == 200 and r.json()["spec"]["sync"] == {"track": "m", "grid": "beat", "lead": 0}, r.text
g = c.get(f"/collections/{cid}/edit").json()
assert g["grid"] and abs(g["grid"][2] - g["grid"][1] - 0.5) < 0.03, g["grid"][:5]
starts = [round(x["start"], 3) for x in g["clips"]]
assert all(any(abs(s0 - t) < 1 / FPS for t in [0.0, *g["grid"]]) for s0 in starts), (starts, g["grid"][:8])
# clip fx validation
assert c.patch(f"/collections/{cid}/edit/clips/{ids[0]}", json={"motion": "zoom"}).status_code == 422
r = c.patch(f"/collections/{cid}/edit/clips/{ids[0]}", json={"motion": "push", "motion_amount": 3, "beats": 2, "hit": 0.5})
assert r.status_code == 200 and r.json()["clamped"][0]["to"] == 1, r.text
assert c.patch(f"/collections/{cid}/edit/clips/{ids[0]}", json={"hit": 99}).status_code == 422
assert c.patch(f"/collections/{cid}/edit/clips/{ids[0]}", json={"shake": True}).status_code == 200
assert c.patch(f"/collections/{cid}/edit/clips/{ids[1]}", json={"shake": True}).status_code == 422  # more than half
assert c.put(f"/collections/{cid}/edit/transitions/{ids[0]}", json={"type": "whip-left", "duration": 0.3, "sfx": "whoosh"}).status_code == 200
sp = c.get(f"/collections/{cid}/edit").json()["spec"]
assert sp["sfx"] == {str(ids[0]): "whoosh"} and sp["transitions"][str(ids[0])]["type"] == "whip-left", sp
r = c.put(f"/collections/{cid}/edit/transitions/{ids[0]}", json={"type": "whip-up", "duration": 0.8})
assert r.status_code == 200 and r.json()["clamped"][0]["to"] == edit.MAX_WHIP, r.text  # a slow whip is a blurry slide
assert c.put(f"/collections/{cid}/edit/transitions/{ids[0]}", json={"type": "cut"}).status_code == 200
assert c.get(f"/collections/{cid}/edit").json()["spec"]["sfx"] == {str(ids[0]): "whoosh"}  # a cut keeps its sound
# sfx="auto" resolves by transition type and is stored as the name; unknown names are refused; types without a default stay silent
assert c.put(f"/collections/{cid}/edit/transitions/{ids[1]}", json={"type": "slide-left", "duration": 0.3, "sfx": "auto"}).status_code == 200
assert c.get(f"/collections/{cid}/edit").json()["spec"]["sfx"][str(ids[1])] == "swish"
assert c.put(f"/collections/{cid}/edit/transitions/{ids[1]}", json={"type": "dissolve", "sfx": "auto"}).status_code == 200
assert str(ids[1]) not in c.get(f"/collections/{cid}/edit").json()["spec"]["sfx"]
assert c.put(f"/collections/{cid}/edit/transitions/{ids[1]}", json={"type": "cut", "sfx": "boom"}).status_code == 422
assert c.put(f"/collections/{cid}/edit/transitions/{ids[1]}", json={"type": "cut", "sfx": "shutter"}).status_code == 200
assert c.get(f"/collections/{cid}/edit").json()["spec"]["sfx"][str(ids[1])] == "shutter"
assert c.delete(f"/collections/{cid}/edit/audio/m").status_code == 200
assert c.get(f"/collections/{cid}/edit").json()["spec"]["sync"]["track"] is None  # deleting the music turns sync off
print("rhythm checks passed")
