"""Colour match and finish layer: the match math converges on tinted frames, measuring through ffmpeg, the API, cache
keys, presets, and that edits without either render with the same filter graphs as before.
uv run test_color.py   (ffmpeg, a few seconds)"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import numpy as np

T = Path(tempfile.mkdtemp())
os.environ["CS_DB"] = str(T / "t.db")
os.environ["KIOKU_DATA"] = str(T / "data")
os.environ["KIOKU_DB_KEY"] = "0" * 64

import core  # noqa: E402
assert str(core.DATA).startswith(str(T)), core.DATA
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import color  # noqa: E402
import edit  # noqa: E402
import library  # noqa: E402
import media  # noqa: E402
import render  # noqa: E402
from timeline import Seg  # noqa: E402

rng = np.random.default_rng(1)


def scene():
    """A plausible frame: a sky-to-ground gradient, some texture, a few bright and dark patches."""
    h, w = 64, 96
    y = np.linspace(0, 1, h)[:, None, None]
    base = (1 - y) * np.array([0.55, 0.65, 0.85]) + y * np.array([0.45, 0.4, 0.3])
    img = base + rng.normal(0, 0.05, (h, w, 3))
    img[40:56, 10:30] = [0.85, 0.8, 0.7]
    img[5:15, 60:80] = [0.12, 0.12, 0.15]
    return np.clip(img, 0, 1)


def u8(x):
    return np.clip(np.round(x * 255), 0, 255).astype(np.uint8)


def stats(img):
    return color.clip_stats({"hz": 0, "frames": [color.frame_stats(u8(img))]}, 0, 0)


def look(s):
    """What the match evens out: exposure (luma-weighted mean log) and the white balance offsets."""
    return np.concatenate([[color.LUMA @ s[:3]], color._wb(s[3:])])


def apply(img, p):
    return img if p is None else img ** np.array(p)


# ---------- match math: tinted and under-exposed copies of one scene converge on the reference
ref = scene()
warm = np.clip(ref * [1.12, 1.0, 0.86], 0, 1)       # tungsten-ish white balance
dark = ref ** 1.35                                  # a stop or so under in the mids
cool_dark = np.clip(dark * [0.9, 1.0, 1.1], 0, 1)
frames = {1: ref, 2: warm, 3: dark, 4: cool_dark}
st = {k: stats(v) for k, v in frames.items()}
got, capped, used = color.solve(st, ref=1)
assert used == 1 and got[1] is None, got  # the reference itself does not move
for k in (2, 3, 4):
    before = np.abs(look(st[k]) - look(st[1])).max()
    after = np.abs(look(stats(apply(frames[k], got[k]))) - look(st[1])).max()
    assert after < 0.25 * before, (k, before, after, got[k])
assert got[2][0] > 1 > got[2][2], got[2]   # warm: red pulled down (power above 1), blue lifted
assert all(x < 1 for x in got[3]), got[3]   # dark: every channel lifted
# no reference: everything moves toward the median and the spread shrinks
got, _, used = color.solve(st)
assert used is None
spread = lambda ls: np.ptp(np.array([look(x) for x in ls]), 0).max()  # noqa: E731
after = [stats(apply(frames[k], got[k])) for k in frames]
assert spread(after) < 0.4 * spread(list(st.values())), (spread(after), spread(list(st.values())))
# deliberate looks keep their character: a deep orange sunset, and a night shot (far darker, sodium orange)
sunset = np.clip(ref * [1.6, 0.9, 0.45], 0, 1)
night = np.clip(ref ** 2.6 * [1.3, 1.0, 0.6], 0, 1)
for name, img in (("sunset", sunset), ("night", night)):
    s5 = stats(img)
    got, capped, _ = color.solve({1: st[1], 5: s5}, ref=1)
    moved = stats(apply(img, got[5]))
    assert abs(look(moved)[0] - look(st[1])[0]) > 0.7 * abs(look(s5)[0] - look(st[1])[0]) or abs(look(s5)[0] - look(st[1])[0]) < 0.1, name
    assert (moved[0] - moved[2]) > 0.7 * (s5[0] - s5[2]), f"the {name} lost its warmth"
assert capped == [5], capped  # the night shot is reported as left alone
# a scene that is simply blue (sky, sea) has no near-neutral pixels to judge the light by: its white balance stays
blue = np.clip(ref * 0 + [0.3, 0.5, 0.85] + rng.normal(0, 0.03, ref.shape), 0, 1)
sb = stats(blue)
assert np.isnan(sb[3]), sb
p = color.solve({1: st[1], 6: sb}, ref=1)[0][6]
assert p is None or abs(p[0] - p[2]) < 0.05, p
# unmeasured clips stay put; nothing measured means nothing changes; already-matching clips need no filter
assert color.solve({1: st[1], 2: None}, ref=2) == ({1: None, 2: None}, [], None)
assert color.solve({1: None}) == ({1: None}, [], None)
assert color.solve({1: st[1], 2: st[1] + 0.001})[0] == {1: None, 2: None}
assert color.solve({1: st[1], 2: st[2]}, ref=7)[2] is None  # a missing reference falls back to the median
# clip_stats: samples inside the source range, weighted by usable pixels; too little usable -> None
c = {"hz": 2, "frames": [[1.0, -1.0, -1.0, -1.0, 0.5, -1.1, -1.0, -0.9]] * 4 + [[1.0, -2.0, -2.0, -2.0, 0, 0, 0, 0]] * 4}
assert np.allclose(color.clip_stats(c, 0, 1.4)[:3], [-1, -1, -1])
assert np.allclose(color.clip_stats(c, 0, 1.4)[3:], [-1.1, -1.0, -0.9])
assert np.allclose(color.clip_stats(c, 2.5, 3.5)[:3], [-2, -2, -2]) and np.isnan(color.clip_stats(c, 2.5, 3.5)[3])
assert np.allclose(color.clip_stats(c, 9, 10)[:3], [-2, -2, -2])  # past the end: the last sample
assert color.clip_stats({"hz": 2, "frames": [[0.01, -1, -1, -1, 0, 0, 0, 0]]}, 0, 1) is None
assert color.frame_stats(np.zeros((4, 4, 3), np.uint8))[0] == 0  # all black: nothing usable

# ---------- the ffmpeg filter does what the numpy model says, and measure() reads stills and videos
img = T / "warm.png"
from PIL import Image  # noqa: E402
Image.fromarray(u8(np.repeat(np.repeat(warm, 4, 0), 4, 1))).save(img)
p = color.solve({1: st[1], 2: st[2]}, ref=1)[0][2]
out = T / "matched.png"
r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(img), "-vf", color.match_filter(p), str(out)], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
measured = color.clip_stats({"hz": 0, "frames": color.measure(out, False)}, 0, 0)
assert np.abs(look(measured) - look(st[1])).max() < 0.25 * np.abs(look(st[2]) - look(st[1])).max(), (measured, st[1])
vid = T / "v.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=192x108:rate=30:duration=3",
                "-pix_fmt", "yuv420p", str(vid)], check=True)
fr = color.measure(vid, True)
assert len(fr) in (6, 7) and all(f[0] > 0.1 for f in fr), fr  # 2 a second (testsrc2 is mostly saturated)
assert color.match_filter(None) == ""

# ---------- ensure: caches per asset, skips what is not on disk, never raises
media.PROXIES = T / "proxies"
media.PROXIES.mkdir()
(media.PROXIES / "VID.mp4").write_bytes(vid.read_bytes())
media.preview_source = lambda u: img if u == "PH" else (_ for _ in ()).throw(media.NoPreview("none"))
assert color.ensure([("VID", "video"), ("PH", "photo"), ("GONE", "video"), ("NOPRE", "photo")]) == 2
assert color.cached("VID")["hz"] == 2 and color.cached("PH")["hz"] == 0 and color.cached("GONE") is None
assert color.ensure([("VID", "video"), ("PH", "photo")]) == 0  # cached

# ---------- presets
assert color.resolve(None, "16:9") == (None, None)
assert color.resolve({"preset": "none", "strength": 1}, "16:9") == (None, None)
assert color.resolve({"preset": "film", "strength": 0}, "16:9") == (None, None)
f, why = color.resolve({"preset": "film", "strength": 0.5}, "16:9")
assert f == {"grain": 0.25, "vignette": 0.2, "bloom": 0.0, "halation": 0.25, "letterbox": False} and why is None, f
f, why = color.resolve({"preset": "cinematic", "strength": 1}, "9:16")
assert not f["letterbox"] and "16:9" in why, (f, why)
assert color.resolve({"preset": "cinematic", "strength": 1}, "16:9")[0]["letterbox"]
try:
    edit.EditPatch(finish="vhs")
    raise AssertionError("unknown finish accepted")
except ValidationError as e:
    assert "cinematic" in str(e)
# every preset's chain runs in this ffmpeg build, wide and vertical, and the bars are black and the right height
src = T / "src.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=0.5",
                "-pix_fmt", "yuv420p", str(src)], check=True)
for name in color.FINISHES:
    for dims, aspect in (((320, 180), "16:9"), ((180, 320), "9:16")):
        chain = color.finish_filters(color.resolve({"preset": name, "strength": 1}, aspect)[0], dims)
        o = T / f"f-{name}-{dims[0]}.png"
        vf = f"scale={dims[0]}:{dims[1]},{chain or 'null'}"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]",
                            "-frames:v", "1", str(o)], capture_output=True, text=True)
        assert r.returncode == 0, (name, dims, r.stderr)
a = np.asarray(Image.open(T / "f-cinematic-320.png").convert("L"))
bar = color.finish_filters(color.resolve({"preset": "cinematic", "strength": 1}, "16:9")[0], (1920, 1080))
assert "h=138:" in bar, bar  # (1080 - 1920 / 2.39) / 2 = 138
assert a[:10].max() < 20 and a[-10:].max() < 20 and a[90].mean() > 40, (a[:10].max(), a[-10:].max())

# ---------- render: cache key, segment graph, finish graph, bitrate; defaults unchanged
s0 = Seg(1, "VID", "video", 0.0, 2.0, 1.0, 1.0, (0, 1, 1, 0), None, ("center", .5, .5), 60, 0, None)
sm = Seg(**{**s0.__dict__, "match": (1.05, 1.0, 0.95)})
src_info = render.Source(320, 180, 0, 30.0, 3.0, True, False, 8)
k0 = render.seg_key(s0, "sid", "preview", (180, 320), 30)
assert render.seg_key(sm, "sid", "preview", (180, 320), 30) != k0
d = {"v": render.RENDER_V, "mode": "preview", "src": "sid", "in": 0.0, "out": 2.0, "speed": 1.0, "volume": 1.0,
     "adjust": (0, 1, 1, 0), "look": None, "reframe": ("center", .5, .5), "dims": (180, 320), "fps": 30, "kind": "video", "frames": 60}
assert k0 == hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:20]  # unmatched clips keep their old key
g0, gm = render.seg_graph(s0, src_info, (180, 320), 30, "preview"), render.seg_graph(sm, src_info, (180, 320), 30, "preview")
assert "lutrgb" not in g0 and "lutrgb=r='pow(val/maxval,1.05)*maxval'" in gm
assert gm.index("lutrgb") > gm.index("scale=180:320"), gm
# a real segment pass with the match: VideoToolbox decode, lutrgb on yuv frames, back to yuv420p
seg_out = T / "seg.mov"
sv = Seg(**{**s0.__dict__, "match": (1.3, 1.0, 0.8), "frames": 30})
r = subprocess.run(render.seg_command(sv, render.probe_source(vid), vid, seg_out, "preview", (192, 108), 30, T),
                   capture_output=True, text=True)
assert r.returncode == 0, r.stderr
plain = color.clip_stats({"hz": 0, "frames": color.measure(vid, True)[:2]}, 0, 0)
moved = color.clip_stats({"hz": 0, "frames": color.measure(seg_out, True)[:2]}, 0, 0)
assert moved[0] < plain[0] - 0.05 and moved[2] > plain[2] + 0.05, (plain, moved)  # red darker, blue lighter
assert render._graph(2, [30, 30], [None], 30) == render._graph(2, [30, 30], [None], 30, finish="")
for name in ("film", "cinematic"):  # the finish pass runs end to end on real segments (a seam, a window, then the layer)
    fo = T / f"fin-{name}.mp4"
    ch = color.finish_filters(color.resolve({"preset": name, "strength": 1}, "16:9")[0], (192, 108))
    cmd = render.finish_command([seg_out, seg_out], [30, 30], [("fade", 6)], (0.1, 0.9), None, 30, fo, T,
                                render.delivery("preview", "hevc", 1080), None, (192, 108), finish=ch)
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0 and fo.stat().st_size > 0, r.stderr
fin = color.finish_filters(color.resolve({"preset": "film", "strength": 1}, "16:9")[0], (320, 180))
gf = render._graph(2, [30, 30], [None], 30, has_cues=True, finish=fin)
assert gf.index("noise=") < gf.index("overlay="), "the finish must sit under the captions"
assert render.delivery("final", "hevc", 1080) == render.delivery("final", "hevc", 1080, 0.0)
assert "12M" in render.delivery("final", "hevc", 1080) and "16M" in render.delivery("final", "h264", 1080)
assert "13.8M" in render.delivery("final", "hevc", 1080, 0.5), render.delivery("final", "hevc", 1080, 0.5)
assert render.delivery("preview", "hevc", 1080, 1.0) == render.delivery("preview", "hevc", 1080)

# ---------- API and timeline
sys.modules["captions"] = types.SimpleNamespace(start_transcription=lambda cid: None, likely_speech=lambda u: False)
db = core.connect()
for u, kind, dur in [("VID", "video", 3.0), ("PH", "image", 0), ("GONE", "video", 3.0)]:
    db.execute("insert into assets(uuid, kind, duration, meta, created) values (?,?,?,?,?)", (u, kind, dur, "", "2026-11-01"))
db.commit()
app = FastAPI()
app.include_router(library.router)
app.include_router(edit.router)
app.add_exception_handler(library.RequestValidationError, library.validation_error)
cl = TestClient(app)
cid = cl.post("/collections", json={"name": "p", "kind": "project"}).json()["id"]
cl.post(f"/collections/{cid}/items", json={"uuids": ["VID", "PH", "GONE"]})
ids = [i["item_id"] for i in cl.get(f"/collections/{cid}").json()["items"]]
g = cl.get(f"/collections/{cid}/edit").json()
items = library.get_collection(cid)["items"]
spec = g["spec"]
assert "match_color" not in spec and "finish" not in spec and "finishes" in g["options"]
tl = edit.compute_timeline(items, spec)
old_ident = json.dumps([spec, [(i["item_id"], i["uuid"], i["t0"], i["t1"], i["pos"]) for i in items], None],
                       sort_keys=True, separators=(",", ":"))
assert tl.edit_hash == hashlib.sha1(old_ident.encode()).hexdigest()  # an untouched edit keeps its hash
assert tl.finish is None and all(s.match is None for s in tl.segs)
r = cl.patch(f"/collections/{cid}/edit", json={"match_color": True, "match_ref": ids[0], "finish": "film", "finish_strength": 3})
assert r.status_code == 200, r.text
r = r.json()
assert r["spec"]["match_color"] == {"on": True, "ref": ids[0]} and r["spec"]["finish"] == {"preset": "film", "strength": 1}, r
assert r["clamped"] == [{"field": "finish_strength", "from": 3.0, "to": 1}], r["clamped"]
assert cl.patch(f"/collections/{cid}/edit", json={"match_ref": 99999}).status_code == 422
assert cl.patch(f"/collections/{cid}/edit", json={"finish": "vhs"}).status_code == 422
g = cl.get(f"/collections/{cid}/edit").json()
assert any("1 clips are not measured" in w for w in g["warnings"]), g["warnings"]  # GONE has no proxy
assert g["edit_hash"] != tl.edit_hash
tl = edit.compute_timeline(items, g["spec"])
assert tl.finish["grain"] == 0.5 and tl.segs[0].match is None  # the reference clip
assert tl.segs[1].match is not None, tl.segs  # the photo (a warm still) is pulled toward the video
r = cl.patch(f"/collections/{cid}/edit/clips/{ids[1]}", json={"match_color": False}).json()
assert r["spec"]["clips"][str(ids[1])] == {"match": False}
tl = edit.compute_timeline(items, r["spec"])
assert tl.segs[1].match is None
r = cl.patch(f"/collections/{cid}/edit/clips/{ids[1]}", json={"match_color": True}).json()
assert str(ids[1]) not in r["spec"]["clips"]  # back to the default: no entry
assert cl.patch(f"/collections/{cid}/edit/clips/{ids[1]}", json={"match_color": "x"}).status_code == 422
r = cl.patch(f"/collections/{cid}/edit", json={"match_color": False, "finish": "none"}).json()
tl = edit.compute_timeline(items, r["spec"])
assert tl.finish is None and all(s.match is None for s in tl.segs)
print("color ok")
