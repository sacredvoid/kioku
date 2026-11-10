"""Auto reframe: subject track, smoothed crop path, timeline fallback, cache key, and a real render of a moving subject.
uv run test_reframe.py   (ffmpeg)"""
import json
import os
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np

T = Path(tempfile.mkdtemp())
os.environ["CS_DB"] = str(T / "t.db")
os.environ["KIOKU_DATA"] = str(T / "data")
os.environ["KIOKU_DB_KEY"] = "0" * 64

import core  # noqa: E402
assert str(core.DATA).startswith(str(T)), core.DATA
import edit  # noqa: E402
import moments  # noqa: E402
import reframe  # noqa: E402
import render  # noqa: E402

HZ, FPS = 4, 30
n = 24  # 6 s of 4 Hz samples


def cached(sal_x, sal_y=None, **extra):
    return {"version": moments.VERSION, "hz": HZ, "series": {"sal_x": sal_x, "sal_y": sal_y or [0.45] * len(sal_x), **extra}}


# ---------- track: faces beat saliency, gaps hold, nothing usable -> None
c = cached([0.2] * n, face_x=[0.8] * n, face_y=[0.4] * n, faces=[0.0] * 12 + [1.0] * 12)
tr = reframe.track(c, 0, 6, 25)
assert tr[0][0] < 0.3 and tr[-1][0] > 0.7, tr  # saliency in the first half, the face in the second
c = cached([0.3] * 8 + [0.5] * 8 + [0.3] * 8, [0.45] * 8 + [0.5] * 8 + [0.45] * 8)  # exact centre = Vision found nothing
tr = reframe.track(c, 0, 6, 25)
assert all(abs(x - 0.3) < 0.05 for x, _ in tr), tr  # the undetected stretch holds 0.3
assert reframe.track(cached([0.5] * n, [0.5] * n), 0, 6, 25) is None
assert reframe.track({"series": {"sal_x": [0.3]}, "hz": HZ}, 0, 6, 25) is None  # no sal_y

# ---------- path: still subject = still crop; jitter absorbed; fast jumps rate limited; always inside 0..1
f, L = 0.316, 6.0
assert len(reframe.path([0.5] * 25, f, L)) == 1
rng = np.random.default_rng(1)
assert len(reframe.path(list(0.5 + rng.uniform(-0.04, 0.04, 25)), f, L)) == 1  # jitter inside the dead zone
jump = reframe.path([0.2] * 12 + [0.8] * 13, f, L)
ps = [p for _, p in jump]
assert all(0 <= p <= 1 for p in ps) and ps[0] < 0.2 and ps[-1] > 0.5 and len(jump) <= reframe.MAX_KEYS, jump
slope = max(abs(b - a) / (tb - ta) for (ta, a), (tb, b) in zip(jump, jump[1:])) * (1 - f)  # source widths per second
assert slope <= reframe.MAX_SPEED * 1.05, slope


# ---------- timeline: auto reads the cache; no cache falls back to the manual position with a warning; others unchanged
def item(i, uuid, t1=6.0):
    return {"item_id": i, "uuid": uuid, "kind": "video", "t0": 0.0, "t1": t1, "duration": 10.0, "pos": i}


def spec(mode, **rf):
    s = edit.default_spec()
    s["clips"] = {"1": {"reframe": {"mode": mode, **rf}}}
    return s


moments.MOMENTS.mkdir(parents=True, exist_ok=True)
(moments.MOMENTS / "U1.json").write_text(json.dumps(cached([round(0.25 + 0.5 * i / (n - 1), 3) for i in range(n)])))
s1 = edit.compute_timeline([item(1, "U1")], spec("auto")).segs[0]
assert s1.reframe[0] == "auto" and s1.track, s1
tl0 = edit.compute_timeline([item(1, "nocache")], spec("auto", pos=0.3))
assert tl0.segs[0].reframe == ("manual", 0.3, 0.3) and tl0.segs[0].track is None and any("auto reframe" in w for w in tl0.warnings)
for mode, want in (("center", ("center", .5, .5)), ("manual", ("manual", .7, .7))):  # saved edits render as before
    x = edit.compute_timeline([item(1, "U1")], spec(mode, pos=.7)).segs[0]
    assert x.reframe == want and x.track is None, x

# ---------- cache key: follows the track; unchanged for old modes
k = lambda s: render.seg_key(s, "sid", "preview", (360, 640), FPS)  # noqa: E731
assert k(s1) != k(replace(s1, track=tuple((x + 0.1, y) for x, y in s1.track))) and k(s1) == k(replace(s1))
assert k(tl0.segs[0]) == k(replace(tl0.segs[0], track=None))

# ---------- render: a box crossing a wide frame; the 9:16 crop must follow it smoothly
src = T / "wide.mp4"
subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=6",
                "-f", "lavfi", "-i", "color=c=white:s=60x60:r=30:d=6", "-filter_complex",
                "[0][1]overlay=x='250+130*t':y=330", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(src)], check=True)
info = render.probe_source(src)
(moments.MOMENTS / "U1.json").write_text(json.dumps(cached([(250 + 130 * i / HZ + 30) / 1280 for i in range(n)])))
seg = edit.compute_timeline([item(1, "U1")], spec("auto")).segs[0]
out = T / "o.mov"
r = subprocess.run(render.seg_command(seg, info, src, out, "preview", (360, 640), FPS, T), capture_output=True, text=True)
assert r.returncode == 0, r.stderr[-800:]
raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                     capture_output=True).stdout
fr = np.frombuffer(raw, np.uint8).reshape(-1, 640, 360)
assert len(fr) == seg.frames, (len(fr), seg.frames)
cols = [np.where(f.max(0) > 200)[0] for f in fr]
assert all(len(c) for c in cols), "the subject left the frame"
bx = np.array([c.mean() for c in cols])  # box centre inside the crop, output pixels
cx = (250 + 130 * np.arange(len(bx)) / FPS + 30) - bx * (405 / 360)  # crop left edge in source px
d = cx[6:] - cx[:-6]  # over 6 frames: the box centroid itself is only good to a pixel or two
assert cx[-1] > cx[0] + 100, (cx[0], cx[-1])               # it followed the subject across the frame
assert d.min() > -3 and d.max() < 6 * 8.6, (d.min(), d.max())  # no reversals or jerks (speed cap is about 8.5 source px per frame)
if os.environ.get("KEEP_FRAMES"):
    for i in (0, 60, 120, 179):
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(out), "-vf", f"select=eq(n\\,{i})", "-frames:v", "1",
                        os.environ["KEEP_FRAMES"] + f"/f{i}.png"], check=True)
print("ok")
