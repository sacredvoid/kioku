"""Seam clip transitions (seamfx): frame exact seams, exact join lengths, untouched neighbours, a real effect mid-seam,
caching, validation. uv run test_seamfx.py   (ffmpeg + VideoToolbox, about 15 s)"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

T = Path(tempfile.mkdtemp())
os.environ["CS_DB"] = str(T / "t.db")
os.environ["KIOKU_DATA"] = str(T / "data")
os.environ["KIOKU_DB_KEY"] = "0" * 64

import numpy as np  # noqa: E402

import core  # noqa: E402

testenv.check()
assert str(core.DATA).startswith(str(T)), core.DATA
import edit  # noqa: E402
import render  # noqa: E402
import seamfx  # noqa: E402

FPS, W, H, N = 30, 270, 480, 60
DIMS = (W, H)
CODEC = render.SEG_CODEC["preview"]
JOIN = ["-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le"]  # lossless, so comparisons see only the graph


def seg(out, src):
    """A cached-segment lookalike: N frames, yuv420p tv range, pcm audio, preview codec."""
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-f", "lavfi", "-i",
                    "sine=f=330:r=48000", "-vf", f"scale={W}:{H},format=yuv420p,setsar=1", "-frames:v", str(N), *CODEC,
                    "-c:a", "pcm_s16le", "-ac", "2", "-t", str(N / FPS), "-f", "mov", str(out)], check=True)


def count(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return int(r.stdout.strip())


def frames(path):
    """Raw YUV planes (no matrix conversion, so files tagged differently compare by their pixels)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-pix_fmt", "yuv444p", "-f", "rawvideo", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, 3, H, W).astype(np.int16)


def diff(x, y):
    return float(np.abs(x - y).mean())


A, B = T / "a.mov", T / "b.mov"
seg(A, f"testsrc2=s={W}x{H}:r={FPS}")  # bright, busy, moving
seg(B, f"mandelbrot=s={W}x{H}:r={FPS}")  # dark, different palette
FA, FB = frames(A), frames(B)

# ---------- plain dissolve for comparison
TF = {"zoomthrough": 12, "lightleak": 18, "lumafade": 18, "glitch": 9}
assert set(TF) == set(seamfx.FX) and {edit.XFADE[k] for k in edit.FX_DUR} == set(seamfx.FX)
plain = {}
for tf in set(TF.values()):
    out = T / f"plain{tf}.mov"
    subprocess.run(render.join_command([A, B], [N, N], [("fade", tf)], FPS, out, T, JOIN, DIMS),
                   check=True)
    plain[tf] = frames(out)

# ---------- each effect: exact seam length, exact join length, neighbours untouched, a different mid frame
for name, tf in TF.items():
    sf = T / f"seam_{name}.mov"
    t0 = time.time()
    seamfx.build(name, tf, A, N, B, sf, "preview", DIMS, FPS, 7, CODEC)
    took = time.time() - t0
    assert count(sf) == tf, (name, count(sf))
    out = T / f"join_{name}.mov"
    r = subprocess.run(render.join_command([A, B], [N, N], [(name, tf)], FPS, out, T, JOIN, DIMS,
                                           {0: sf}), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    J, S = frames(out), frames(sf)
    assert len(J) == 2 * N - tf, (name, len(J))
    a = abs(float(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration",
                                  "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout) - (2 * N - tf) / FPS)
    assert a < 0.03, (name, "audio length", a)  # audio keeps acrossfade: same overlap as the picture
    o = N - tf  # first seam frame on the joined timeline
    assert diff(J[o - 1], FA[o - 1]) < 3 and diff(J[o - 5], FA[o - 5]) < 3, (name, "A before the seam changed")
    assert diff(J[o + tf], FB[tf]) < 3 and diff(J[o + tf + 5], FB[tf + 5]) < 3, (name, "B after the seam changed")
    for j in (0, tf // 2, tf - 1):  # the seam clip is exactly what shows, frame for frame
        assert diff(J[o + j], S[j]) < 3, (name, j, diff(J[o + j], S[j]))
    m = tf // 2
    d = diff(J[o + m], plain[tf][o + m])
    assert d > 8, (name, "mid frame looks like a dissolve", d)
    print(f"{name}: {tf} frames at {W}x{H} in {took:.2f}s, mid frame differs from a dissolve by {d:.1f}")
# colour continuity: luma-fade's first frame is almost all A, so it must match A (no matrix or range shift)
S = frames(T / "seam_lumafade.mov")
assert diff(S[0], FA[N - TF["lumafade"]]) < 3, diff(S[0], FA[N - TF["lumafade"]])
# deterministic: the same seed draws the same glitch
seamfx.build("glitch", 9, A, N, B, T / "g2.mov", "preview", DIMS, FPS, 7, CODEC)
assert diff(frames(T / "g2.mov")[4], frames(T / "seam_glitch.mov")[4]) < 1
# a short source repeats its last frame instead of failing; bad input is refused
short = T / "short.mov"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(A), "-frames:v", "50", "-c", "copy", str(short)], check=True)
seamfx.build("lumafade", 12, short, N, B, T / "s.mov", "preview", DIMS, FPS, 1, CODEC)
assert count(T / "s.mov") == 12
for bad in (("nope", 6), ("glitch", 0), ("glitch", N + 1)):
    try:
        seamfx.build(bad[0], bad[1], A, N, B, T / "x.mov", "preview", DIMS, FPS, 1, CODEC)
        raise AssertionError(bad)
    except ValueError:
        pass
ev = threading.Event()
ev.set()
try:
    seamfx.build("glitch", 9, A, N, B, T / "c.mov", "preview", DIMS, FPS, 1, CODEC, ev)
    raise AssertionError("cancel ignored")
except RuntimeError as e:
    assert "Cancelled" in str(e)

# ---------- cache: ensure builds once per key, keys change with either segment
parts = []
k = seamfx.key("glitch", 9, "ka", "kb", "preview", DIMS, FPS)
assert k != seamfx.key("glitch", 9, "ka", "kc", "preview", DIMS, FPS) != seamfx.key("glitch", 10, "ka", "kb", "preview", DIMS, FPS)
cdir = T / "cache"
cdir.mkdir()
runs = seamfx.RUNS
f1 = seamfx.ensure("glitch", 9, A, N, B, k, cdir, "preview", DIMS, FPS, CODEC, parts)
f2 = seamfx.ensure("glitch", 9, A, N, B, k, cdir, "preview", DIMS, FPS, CODEC, parts)
assert f1 == f2 and f1.is_file() and seamfx.RUNS == runs + 1 and not list(cdir.glob("*.part*")), list(cdir.iterdir())

# ---------- graphs: ordinary seams unchanged; seam inputs come after captions and tracks; a missing seam fails
assert "overlay" not in render._graph(3, [60, 60, 60], [("fade", 9), None], FPS)
g = render._graph(3, [60, 60, 60], [None, ("glitch", 9)], FPS, seams={1: 5})
assert "[5:v]fps=30,setpts=PTS-STARTPTS+111[s2]" in g and "transition=fade" in g, g
try:
    render._graph(2, [60, 60], [("lumafade", 9)], FPS)
    raise AssertionError("missing seam clip must fail")
except ValueError:
    pass
fx = {"D": 4, "fi": 0, "fo": 0, "cv": 1, "duck": False, "tracks": [dict(path="/m.wav", a=0, dur=4, rem=4, delay=0, vol=1, fi=0, fo=0)]}
cmd = render.finish_command([A, B], [N, N], [("glitch", 9)], None, T / "cues.txt", FPS, T / "o.mp4", T, ["-c:v", "x"], fx, DIMS,
                            {0: Path("/seam.mov")})
g = (T / "join_o.mp4.txt").read_text()
assert cmd[cmd.index("/seam.mov") - 1] == "-i" and "[4:v]fps=30" in g, (cmd, g)  # 2 clips, cues (2), track (3), seam (4)

# ---------- edit: types, duration caps, taste warnings
FXT = edit.FX_DUR


def item(i, t1=4.0):
    return {"item_id": i, "uuid": f"U{i}", "kind": "video", "t0": 0.0, "t1": t1, "duration": 10.0, "pos": i}


def spec(trans, clips=None):
    s = edit.default_spec()
    s["transitions"] = {str(i): {"type": t, "duration": d} for i, (t, d) in trans.items()}
    s["clips"] = clips or {}
    return s


tl = edit.compute_timeline([item(i) for i in range(1, 4)], spec({1: ("zoom-through", 0.4)}))
assert tl.segs[0].trans_after == ("zoomthrough", 12) and tl.total_frames == 3 * 120 - 12 and not tl.warnings, tl
busy = edit.compute_timeline([item(i, 2.0) for i in range(1, 5)], spec({1: ("glitch", 0.3), 2: ("light-leak", 0.6)},
                                                                        {"4": {"flash": True}, "3": {"flash": True}}))
w = " | ".join(busy.warnings)
assert "effect transitions in" in w and "back to back" in w and "clip 3 flashes" in w, w
# API: clamps per effect, at most FX_MAX per edit
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
c = TestClient(app)
cid = c.post("/collections", json={"name": "p", "kind": "project"}).json()["id"]
c.post(f"/collections/{cid}/items", json={"uuids": ["V"] * 6})
ids = [i["item_id"] for i in c.get(f"/collections/{cid}").json()["items"]]
r = c.put(f"/collections/{cid}/edit/transitions/{ids[0]}", json={"type": "glitch", "duration": 1.0})
assert r.status_code == 200 and r.json()["clamped"][0]["to"] == FXT["glitch"][1], r.text
r = c.put(f"/collections/{cid}/edit/transitions/{ids[1]}", json={"type": "light-leak", "duration": 0.2})
assert r.status_code == 200 and r.json()["clamped"][0]["to"] == FXT["light-leak"][0], r.text
assert c.put(f"/collections/{cid}/edit/transitions/{ids[2]}", json={"type": "luma-fade"}).status_code == 200
assert c.put(f"/collections/{cid}/edit/transitions/{ids[2]}", json={"type": "zoom-through"}).status_code == 200  # replace, not add
r = c.put(f"/collections/{cid}/edit/transitions/{ids[3]}", json={"type": "zoom-through"})
assert r.status_code == 422 and "at most" in r.text, r.text
assert c.put(f"/collections/{cid}/edit/transitions/{ids[3]}", json={"type": "dissolve"}).status_code == 200  # xfade ones are not capped
opts = c.get(f"/collections/{cid}/edit").json()["options"]["transitions"]
assert all(t in opts for t in FXT), opts
print("seamfx checks passed")
