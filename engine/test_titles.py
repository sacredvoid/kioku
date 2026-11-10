"""Checks for animated titles and LUT looks (notes/pro-editing-design.md phase 3).
uv run test_titles.py   (ffmpeg; a few seconds)"""
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
assert str(core.DATA).startswith(str(T)), core.DATA
import edit  # noqa: E402
import render  # noqa: E402
import titles  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

FPS = 30

# ---------- easing: cubic is monotonic 0..1, back overshoots then lands on 1
xs = [i / 100 for i in range(101)]
c = [titles.ease_out_cubic(x) for x in xs]
assert c[0] == 0 and c[-1] == 1 and all(b >= a for a, b in zip(c, c[1:]))
b = [titles.ease_out_back(x) for x in xs]
assert abs(b[0]) < 1e-9 and abs(b[-1] - 1) < 1e-9 and max(b) > 1.05
assert titles.ease_out_cubic(-1) == 0 and titles.ease_out_cubic(2) == 1


def tx(template, text, n=90, **kw):
    return {**titles.DEFAULT, "id": "a", "template": template, "text": text, "start": 0, "dur": n / FPS,
            "start_frame": 0, "frames": n, **kw}


# ---------- frame counts: only the animations make distinct frames; the hold is one frame
for t in titles.TEMPLATES:
    x = tx(t, "Summer in Lisbon" if t != "big-number" else "2024")
    ti, to = titles.phase_frames(x, 90, FPS)
    keys = {titles.frame_key(i, 90, ti, to) for i in range(90)}
    assert len(keys) == ti + to + 1, (t, len(keys), ti, to)
    assert 0.25 <= to / FPS <= 0.4 and ti / FPS >= 0.25, (t, ti, to)
# too short for both animations: they shrink to fit
ti, to = titles.phase_frames(tx("title-pop", "Hi", 10), 10, FPS)
assert ti + to <= 10
# degrade: past MAX_FRAMES distinct frames the animations step
many = types.SimpleNamespace(fps=FPS, duration=60.0, texts=tuple(
    tx("word-stagger", " ".join(["w"] * 12), 60, id=f"t{k}", start_frame=k * 60) for k in range(30)))
per, step, n = titles._per_frame(many, 0, 1800)
assert step > 1 and n <= titles.MAX_FRAMES, (step, n)

# ---------- safe margins: every template's text stays inside the title-safe area (10% each side) at hold
for dims in ((540, 960), (960, 540), (540, 540)):
    W, H = dims
    for t in titles.TEMPLATES:
        for pos in titles.POSITIONS:
            long = {"big-number": "1,234,567", "location-tag": "Sintra, Portugal, by the old palace"}.get(
                t, "A very long title that will need to wrap over lines")
            x = tx(t, long, position=pos)
            ti, to = titles.phase_frames(x, 90, FPS)
            im = titles.frame(x, dims, ti, to, FPS)
            box = im.getchannel("A").point(lambda v: 255 if v > 128 else 0).getbbox()
            assert box, (t, pos, dims)
            m = 0.1
            assert box[0] >= W * m - 2 and box[2] <= W * (1 - m) + 2 and box[1] >= H * m - 2 and box[3] <= H * (1 - m) + 2, \
                (t, pos, dims, box)
            if t != "big-number" and pos == "auto":  # auto never sits on the middle of the frame
                assert not (box[1] < H * .45 and box[3] > H * .55), (t, dims, box)
# first in-frame is empty or nearly (things animate in), last frame is gone
im = titles.frame(tx("title-pop", "Hello"), (540, 960), 0, 9, FPS)
assert not im.getchannel("A").point(lambda v: 255 if v > 200 else 0).getbbox()

# ---------- spec -> timeline: snapped to the grid when synced, clipped to the edit, warned past the end
def item(i, t1=4.0):
    return {"item_id": i, "uuid": f"U{i}", "kind": "video", "t0": 0.0, "t1": t1, "duration": 10.0, "pos": i}


s = edit.default_spec()
s["texts"] = [{"id": "a", "template": "title-pop", "text": "Hi", "start": 1.1, "dur": 2.0},
              {"id": "b", "template": "lower-third", "text": "Late", "start": 7.0, "dur": 5.0},
              {"id": "c", "template": "location-tag", "text": "Gone", "start": 20.0, "dur": 2.0}]
tl = edit.compute_timeline([item(1), item(2)], s)
assert [t["id"] for t in tl.texts] == ["a", "b"] and tl.texts[0]["start_frame"] == 33 and tl.texts[1]["frames"] == 30
assert any("'c'" in w for w in tl.warnings), tl.warnings
s["audio"] = [{"id": "m", "path": "/x.wav", "start": 0, "in": 0, "out": 60, "volume": 1, "fade_in": 0, "fade_out": 0}]
s["sync"] = {"track": "m", "grid": "beat", "lead": 0}
tl2 = edit.compute_timeline([item(1), item(2)], s, [i * 0.5 for i in range(40)])
assert tl2.texts[0]["start_frame"] == 30 and s["texts"][0]["start"] == 1.1  # snapped in the math, stored as entered
# texts change the edit hash, so previews re-render
h = tl.edit_hash
s2 = edit.default_spec()
s2["texts"] = [dict(s["texts"][0], text="Hey")]
assert edit.compute_timeline([item(1), item(2)], s2).edit_hash != h
assert edit.compute_timeline([item(1), item(2)], edit.default_spec()).edit_hash != h

# ---------- overlay: titles merged with captions into ONE ffconcat stream, window aware
W = T / "w"
W.mkdir()
cap = types.SimpleNamespace(runs=(("cap.png", 60), (None, 60)), list_file=W / "cues.txt")
c_im = Image.new("RGBA", (360, 640), (0, 0, 0, 0))
c_im.paste((0, 255, 0, 255), (0, 600, 360, 640))
c_im.save(W / "cap.png")
tl3 = types.SimpleNamespace(fps=FPS, duration=4.0, texts=(tx("title-pop", "Hello", 60, start_frame=30),))
lst = titles.build(tl3, (360, 640), W, None, cap)
rows = lst.read_text().splitlines()
durs = [float(r.split()[1]) for r in rows if r.startswith("duration")]
assert abs(sum(durs) - 4.0) < 1e-3, durs
assert titles.build(types.SimpleNamespace(fps=FPS, duration=4.0, texts=()), (360, 640), W, None, cap) == cap.list_file
win = titles.build(tl3, (360, 640), T / "w2", (1.5, 3.0), None)
assert abs(sum(float(r.split()[1]) for r in win.read_text().splitlines() if r.startswith("duration")) - 1.5) < 1e-3

# real finish pass: one overlay input, caption bar and title both drawn, title gone after its end
src = W / "s.mov"
subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x202020:s=360x640:r=30:d=4", "-f", "lavfi",
                "-i", "anullsrc=r=48000:cl=stereo", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(src)],
               check=True)
out = W / "o.mp4"
cmd = render.finish_command([src], [120], [], None, lst, FPS, out, W, ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"])
assert cmd.count("-i") == 2, cmd
r = subprocess.run(cmd, capture_output=True, text=True, cwd=W)
assert r.returncode == 0, r.stderr[-800:]


def frame_at(f):
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(out), "-vf", f"select=eq(n\\,{f})", "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    return Image.frombytes("RGB", (360, 640), raw)


def lit(im):  # white title pixels in the frame
    return int((np.asarray(im).min(axis=2) > 200).sum())


a, m, z = frame_at(10), frame_at(60), frame_at(100)
assert a.getpixel((180, 620))[1] > 200 and z.getpixel((180, 620))[1] < 80  # caption bar until frame 60
assert lit(a) == 0 and lit(m) > 200 and lit(z) == 0, (lit(a), lit(m), lit(z))

# ---------- LUT looks: lut3d exists, strength blends toward identity, and the filter runs
assert edit.look_filters(("teal-orange", 0.0)) == ""
f1 = edit.look_filters(("teal-orange", 1.0))
assert "teal-orange.cube" in f1 and "luts" in f1
f5 = edit.look_filters(("teal-orange", 0.52))
p5 = Path(f5.split("'")[1])
assert p5.is_file() and p5.name == "teal-orange-0.50.cube"
for name in edit.LUTS:
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x64:d=0.1", "-vf", edit.look_filters((name, 0.7)),
                        "-f", "null", "-"], capture_output=True, text=True)
    assert r.returncode == 0, (name, r.stderr[-400:])

# ---------- API: validation, partial updates, removal, options
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import library  # noqa: E402
sys.modules.setdefault("captions", types.SimpleNamespace(start_transcription=lambda cid: None, likely_speech=lambda u: False))
db = core.connect()
db.execute("insert into assets(uuid, kind, duration, meta, created) values ('V','video',12.0,'','2026-11-01')")
db.commit()
app = FastAPI()
app.include_router(library.router)
app.include_router(edit.router)
cl = TestClient(app)
cid = cl.post("/collections", json={"name": "p", "kind": "project"}).json()["id"]
assert cl.put(f"/collections/{cid}/edit/texts/a", json={"template": "title-pop", "text": "Hi"}).status_code == 422  # no clips
cl.post(f"/collections/{cid}/items", json={"uuids": ["V"]})  # 12 s
u = f"/collections/{cid}/edit/texts"
assert cl.put(f"{u}/a", json={"text": "Hi"}).status_code == 422                       # new id needs a template
assert cl.put(f"{u}/a", json={"template": "spin", "text": "Hi"}).status_code == 422   # unknown template
assert cl.put(f"{u}/a", json={"template": "title-pop", "text": "x" * 81}).status_code == 422
assert cl.put(f"{u}/a", json={"template": "title-pop", "text": "   "}).status_code == 422
assert cl.put(f"{u}/bad id", json={"template": "title-pop", "text": "Hi"}).status_code == 422
assert cl.put(f"{u}/a", json={"template": "title-pop", "text": "Hi", "color": "red"}).status_code == 422
r = cl.put(f"{u}/a", json={"template": "title-pop", "text": "Hi  there", "start": 11.9, "dur": 9})
assert r.status_code == 200, r.text
t = r.json()["spec"]["texts"][0]
assert t["text"] == "Hi there" and t["start"] == 11.5 and t["dur"] == 0.5 and {n["field"] for n in r.json()["clamped"]} == {"start", "dur"}
r = cl.put(f"{u}/a", json={"start": 1, "dur": 2, "accent": "#00FFAA"})
t = r.json()["spec"]["texts"][0]
assert (t["template"], t["start"], t["dur"], t["accent"]) == ("title-pop", 1, 2, "#00FFAA"), t
# a text sound: validated, kept when other fields change, removed by "none"
assert cl.put(f"{u}/a", json={"sfx": "boom"}).status_code == 422
assert cl.put(f"{u}/a", json={"sfx": "pop"}).json()["spec"]["texts"][0]["sfx"] == "pop"
assert cl.put(f"{u}/a", json={"text": "Hello"}).json()["spec"]["texts"][0]["sfx"] == "pop"
assert "sfx" not in cl.put(f"{u}/a", json={"sfx": "none"}).json()["spec"]["texts"][0]
assert cl.put(f"{u}/a", json={"sfx": "pop"}).status_code == 200
for k in range(1, titles.MAX_TEXTS):
    assert cl.put(f"{u}/t{k}", json={"template": "location-tag", "text": "X"}).status_code == 200
assert cl.put(f"{u}/one-more", json={"template": "location-tag", "text": "X"}).status_code == 422
g = cl.get(f"/collections/{cid}/edit").json()
assert g["options"]["text_templates"] == list(titles.TEMPLATES) and "anton" in g["options"]["text_fonts"]
assert "teal-orange" in g["options"]["looks"] and g["texts"][0] == {"id": "a", "start": 1.0, "end": 3.0}, g["texts"][0]
assert cl.delete(f"{u}/a").status_code == 200 and cl.delete(f"{u}/a").status_code == 404
assert cl.patch(f"/collections/{cid}/edit", json={"look": "warm-film"}).status_code == 200
assert cl.patch(f"/collections/{cid}/edit", json={"look": "sepia"}).status_code == 422
print("titles checks passed")
