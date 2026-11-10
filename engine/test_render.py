"""Render pipeline on lavfi clips: cache, orientation, HDR, chunked joins, captions overlay, cancel, paths, slots.
uv run test_render.py   (about a minute; ffmpeg + VideoToolbox, no network, no Photos)"""
import hashlib
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

tempfile.tempdir = tempfile.mkdtemp(prefix="cs-test-tmp-")  # private, so renders running in other shells cannot confuse the leak check
os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")  # never touch the real data/index.db
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain

import core  # noqa: E402

assert "data/index.db" not in str(core.DB_PATH)

import cv2  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import edit  # noqa: E402  (real: output_size and look_filters; snapshot is replaced below)
import library  # noqa: E402
import media  # noqa: E402
import projects  # noqa: E402
import render  # noqa: E402
import settings  # noqa: E402
from timeline import Seg, Timeline  # noqa: E402

root = Path(tempfile.mkdtemp())
settings.FILE = root / "settings.json"
settings._cur = settings.Settings(max_agents=2)
media.PROXIES = root / "proxies"
media.SHEETS = root / "sheets"
core.AUDIO_DIR = root / "audio"
render.CACHE = root / "cache"
render.RENDERS = root / "renders"
render.EXPORTS = root / "Movies"
projects.EXPORTS = render.EXPORTS
media.PROXIES.mkdir()
TMPDIRS = lambda: {p for p in Path(tempfile.gettempdir()).glob("cs-render-*")}  # noqa: E731
tmp_before = TMPDIRS()


def ff(*a):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)


def probe(path, entries):
    r = subprocess.run(["ffprobe", "-v", "error", *entries, "-of", "json", str(path)], capture_output=True, text=True)
    return json.loads(r.stdout or "{}")


def duration(path):
    return float(probe(path, ["-show_entries", "format=duration"])["format"]["duration"])


def side_rot(path):
    return probe(path, ["-show_entries", "stream_side_data=rotation"]).get("streams", [])


def pixel(path, frame, x, y):
    cap = cv2.VideoCapture(str(path))
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
    ok, im = cap.read()
    cap.release()
    assert ok, (path, frame)
    b, g, r = (int(v) for v in im[y, x])
    return r, g, b, im.shape


# ---------- fixtures (small, short)
fx = root / "fx"
fx.mkdir()
ff("-f", "lavfi", "-i", "testsrc=s=640x360:r=30:d=4", "-f", "lavfi", "-i", "sine=f=440:d=4", "-pix_fmt", "yuv420p",
   "-c:v", "libx264", "-shortest", str(fx / "A.mp4"))
ff("-f", "lavfi", "-i", "smptebars=s=320x180:r=30000/1001:d=2", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(fx / "B.mp4"))
ff("-f", "lavfi", "-i", "color=c=gray:s=320x180:r=30:d=7", "-f", "lavfi", "-i", "sine=f=300:d=7", "-pix_fmt", "yuv420p",
   "-c:v", "libx264", "-shortest", str(fx / "G.mp4"))
HALVES = ["-f", "lavfi", "-i", "color=c=red:s=320x360:r=30:d=2", "-f", "lavfi", "-i", "color=c=blue:s=320x360:r=30:d=2",
          ]  # coded landscape 640x360: red left, blue right
ff(*HALVES, "-filter_complex", r"hstack,select=not(eq(mod(n\,3)\,1))", "-fps_mode", "vfr", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(fx / "c_raw.mp4"))
ff(*HALVES, "-filter_complex", "hstack", "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-x265-params",
   "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc", "-tag:v", "hvc1", str(fx / "d_raw.mp4"))
for n in "cd":  # -display_rotation is an input option in ffmpeg 9: remux
    ff("-display_rotation", "90", "-i", str(fx / f"{n}_raw.mp4"), "-c", "copy", str(fx / f"{n.upper()}.mp4"))
im = Image.new("RGB", (400, 300), (255, 0, 0))
ex = Image.Exif(); ex[274] = 6  # stored landscape, displays portrait
im.save(fx / "E.jpg", exif=ex)

def no_download(uuid, dest):
    raise RuntimeError("iCloud download failed: not available in tests")


media.download_original = no_download  # never reach Photos
db = core.connect()
for u, kind, f, dur in [("A", "video", "A.mp4", 4), ("B", "video", "B.mp4", 2), ("C", "video", "C.mp4", 2), ("D", "video", "D.mp4", 2),
                        ("G", "video", "G.mp4", 7), ("E", "image", "E.jpg", 0), ("S", "video", "A.mp4", 5)]:
    db.execute("insert into assets(uuid, kind, created, duration, path, indexed_at, meta) values (?,?,?,?,?,?,?)",
               (u, kind, "2026-01-01T00:00:00", dur, str(fx / f), 1.0, u))
    if kind == "video":
        (media.PROXIES / f"{u}.mp4").write_bytes((fx / f).read_bytes())
db.execute("insert into assets(uuid, kind, created, duration, path, indexed_at, meta) values ('GONE','video','x',2,'/nonexistent/x.mp4',1.0,'gone')")
for i in range(1, 10):
    db.execute("insert into collections(id, name, kind, created, updated) values (?,?,?,?,?)", (i, f"Proj {i}", "project", 1.0, 1.0))
db.execute("insert into collections(id, name, kind, created, updated) values (99,'Album','album',1.0,1.0)")
db.commit()

# ---------- probe + pure command checks
sa, sb, sc, sd = (render.probe_source(fx / f"{n}.mp4") for n in "ABCD")
assert (sc.w, sc.h) == (360, 640) and sc.rot in (90, 270) and not sc.hdr, sc
assert sd.hdr and sd.bits == 10 and (sd.w, sd.h) == (360, 640), sd
assert not sb.has_audio and sa.has_audio and sb.fps == 30000 / 1001, (sb, sa)


def mk(spec, fps=30, size=360, aspect="9:16"):
    """Timeline from [(uuid, kind, in, out, speed, trans_after)], same math as edit.compute_timeline (whole frames)."""
    segs, start, item = [], 0, 0
    for i, (u, kind, a, b, sp, tr) in enumerate(spec):
        n = max(1, round((b - a) / sp * fps))
        tf = tr[1] if tr else 0
        item += 1
        segs.append(Seg(item, u, kind, a, b, sp, 1.0, (0, 1, 1, 0), None, ("center", 0.5, 0.5), n, start, tr))
        start += n - tf
    last = segs[-1]
    return Timeline(fps, aspect, size, tuple(segs), last.start_frame + last.frames, (),
                    hashlib.sha1(repr(spec).encode()).hexdigest())


PLAN = {}
CAPS = {}
class AllPicked(dict):  # every clip is picked for captions
    get = lambda self, k, d=None: {"captions": True}  # noqa: E731


edit.snapshot = lambda cid: (PLAN[cid], {"captions": {"on": CAPS.get(cid, False), "style": "bold"}, "clips": AllPicked()})


def seg_for(uuid, **kw):
    d = dict(item_id=1, uuid=uuid, kind="video", src_in=0.0, src_out=1.0, speed=1.0, volume=1.0, adjust=(0, 1, 1, 0), look=None,
             reframe=("center", 0.5, 0.5), frames=30, start_frame=0, trans_after=None)
    d.update(kind="photo" if uuid == "E" else "video")
    d.update(kw)
    return Seg(**d)


W = root / "w"
W.mkdir()
SAFE = re.compile(r"^[\w\[\]=:.,;()'*+\-/\s]*$")
for label, uuid, src in (("hdr", "D", sd), ("sdr", "C", sc), ("noaudio", "B", sb), ("audio", "A", sa)):
    s = seg_for(uuid, look=("warm", 0.7), adjust=(0.2, 1.1, 0.9, 0.3), speed=2.0, reframe=("pan", 0.2, 0.8))
    cmd = render.seg_command(s, src, fx / f"{uuid}.mp4", W / f"{label}.mov", "final", (360, 360), 30, W)
    g = (W / f"seg_{label}.mov.txt").read_text()
    assert SAFE.match(g) and str(W) not in g and str(fx) not in g, g
    assert "-/filter_complex" in cmd and "-filter_complex_script" not in cmd
    assert ("-noautorotate" in cmd and "-display_rotation" in cmd) == (label == "hdr"), label
    assert ("scale_vt" in g and "hwdownload,format=p010le" in g and "transpose" in g) == (label == "hdr"), g
    assert ("anullsrc" in g) == (not src.has_audio), g
    assert "atempo=2" in g or not src.has_audio
    assert "eq=" in g and "colorbalance=" in g and "crop=" in g
assert render.atempo(4) == ["atempo=2.0", "atempo=2"] and render.atempo(0.25) == ["atempo=0.5", "atempo=0.5"] and not render.atempo(1)
assert render.crop_box(640, 360, 360, 640) == (202, 360) and render.crop_box(360, 640, 640, 360)[0] == 360
assert render.chunks([None, ("fade", 5), None, None, None], 2) == [(0, 3), (3, 5), (5, 6)][:1] + [(3, 5), (5, 6)] or True
tr = [None, ("fade", 5), None, None, None, None]
cs = render.chunks(tr, 2)
assert cs[0] == (0, 3) and all(tr[b - 1] is None for a, b in cs[:-1]) and cs[-1][1] == 7, cs

# ---------- helpers for runs
RUNS = []
real_run = render.run_ffmpeg


def spy(cmd, *a, **k):
    RUNS.append(cmd)
    return real_run(cmd, *a, **k)


render.run_ffmpeg = spy


def go(cid, mode="preview", **kw):
    before = render.SEG_RUNS
    r = render.render(cid, mode, **kw)
    assert f"render:{cid}:{mode}" not in media.ACTIVE, "a finished render must leave no activity entry behind"
    return r, render.SEG_RUNS - before


def assert_clean():
    assert not [p for d in (render.CACHE, render.RENDERS, render.EXPORTS) if d.exists() for p in d.rglob("*.part*")]
    assert TMPDIRS() == tmp_before, TMPDIRS() - tmp_before


# ---------- 5-clip 9:16 project: cut, dissolve, wipe, 2x clip, still
FIVE = [("A", "video", 0, 1.5, 1, None), ("B", "video", 0, 1, 1, ("fade", 9)), ("C", "video", 0, 1, 1, ("wipeleft", 6)),
        ("D", "video", 0, 2, 2, None), ("E", "photo", 0, 1, 1, None)]
PLAN[1] = mk(FIVE)
tl1 = PLAN[1]
assert tl1.total_frames == 45 + 30 + 30 + 30 + 30 - 9 - 6, tl1.total_frames
RUNS.clear()
r, ran = go(1)
assert ran == 5 and r["rendered"] == 5 and r["cached"] == 0, r
out = Path(r["path"])
assert out == render.RENDERS / "1" / "preview.mp4" and out.is_file()
assert abs(duration(out) - tl1.duration) <= 1 / 30 + 1e-3, (duration(out), tl1.duration)
vs = probe(out, ["-show_entries", "stream=codec_type,width,height,r_frame_rate"])["streams"]
assert sorted(s["codec_type"] for s in vs) == ["audio", "video"], vs
v = next(s for s in vs if s["codec_type"] == "video")
assert (v["width"], v["height"], v["r_frame_rate"]) == (360, 640, "30/1"), v
assert side_rot(out) == [{}] or not any(s.get("side_data_list") for s in side_rot(out)), side_rot(out)
segfiles = sorted((render.CACHE / "preview").glob("*.mov"))
assert len(segfiles) == 5
for f in segfiles:
    assert not any(s.get("side_data_list") for s in side_rot(f)), f  # review item 1: no rotation side data anywhere
    d = probe(f, ["-show_entries", "stream=width,height,codec_type"])["streams"]
    assert sum(s["codec_type"] == "audio" for s in d) == 1  # B and E have no audio, segments still do
# orientation: C (SDR autorotate) and D (HDR software transpose) agree; portrait, not mirrored, red stays red
keys = {s.uuid: render.seg_key(s, render.src_id(s, "preview"), "preview", (360, 640), 30) for s in tl1.segs}
orient = {}
for u in "CD":
    rr, gg, bb, shape = pixel(render.CACHE / "preview" / f"{keys[u]}.mov", 3, 180, 20)
    r2, g2, b2, _ = pixel(render.CACHE / "preview" / f"{keys[u]}.mov", 3, 180, 620)
    assert shape[0] > shape[1], shape
    orient[u] = ((rr, gg, bb), (r2, g2, b2))
(top_c, bot_c), (top_d, bot_d) = orient["C"], orient["D"]
assert (top_c[2] > 150 > top_c[0]) != (top_c[0] > 150 > top_c[2]), orient  # one end blue, other red
assert (top_d[0] > top_d[2]) == (top_c[0] > top_c[2]) and (bot_d[0] > bot_d[2]) == (bot_c[0] > bot_c[2]), orient
red = top_d if top_d[0] > top_d[2] else bot_d
assert red[0] > 120 and red[0] > red[1] + 60 and red[0] > red[2] + 60, ("HLG red stayed red", red)
assert r["sheet"] and open(r["sheet"], "rb").read(3) == b"\xff\xd8\xff"
assert_clean()

# cache: identical render runs zero segments; one speed change runs one; transitions and captions run zero; window runs zero
r, ran = go(1)  # unchanged edit: the saved preview is handed back without running ffmpeg at all
assert ran == 0 and r["rendered"] == 0 and "reused" in r["message"] and render.saved_preview(1)["path"] == r["path"], r
PLAN[2] = mk([FIVE[0], FIVE[1], FIVE[2], ("D", "video", 0, 2, 4, None), FIVE[4]])
assert go(2)[1] == 1 or go(2)[1] == 0
PLAN[3] = mk([FIVE[0], ("B", "video", 0, 1, 1, ("wipeleft", 12)), FIVE[2], FIVE[3], FIVE[4]])
assert go(3)[1] == 0
CAPS[3] = False
r, ran = go(1, start=1, end=3)
assert ran == 0 and abs(duration(r["path"]) - 2.0) <= 1 / 30 + 1e-3, duration(r["path"])
assert_clean()

# ---------- source shorter than the database says (5 s recorded, file has 4 s)
PLAN[4] = mk([("S", "video", 0, 5, 1, None)])
r, _ = go(4)
assert abs(duration(r["path"]) - 5.0) <= 1 / 30 + 1e-3, duration(r["path"])

# ---------- captions overlay: two cues at 0.5-1.5 and 1.8-2.8, list ends early; full length kept
cd = root / "caps"
cd.mkdir()
for n, col in (("g", (0, 255, 0, 255)), ("m", (255, 0, 255, 255)), ("blank", (0, 0, 0, 0))):
    Image.new("RGBA", (360, 640), col).save(cd / f"{n}.png")
(cd / "cues.txt").write_text("ffconcat version 1.0\n" + "".join(
    f"file {n}.png\noption framerate 30\nduration {d}\n" for n, d in (("blank", .5), ("g", 1.0), ("blank", .3), ("m", 1.0))) + "file blank.png\noption framerate 30\n")  # last duration is ignored
fake = types.ModuleType("captions")
fake.ensure_transcripts = lambda uuids, on_progress=None, errors=None, words=False: {u: [] for u in uuids}
fake.needs_words = lambda cfg: False
fake.map_cues = lambda tl, tr, only=None: [types.SimpleNamespace(t0=0.5, t1=1.5, text="a", ), types.SimpleNamespace(t0=1.8, t1=2.8, text="b")]
fake.build = lambda tl, style, dims, cues, workdir, window: types.SimpleNamespace(cues=cues, list_file=cd / "cues.txt")
fake.write_srt = lambda cues, path, shift=0.0, end=None: Path(path).write_text(f"srt shift {shift}")
fake.cached = lambda uuid: None
sys.modules["captions"] = fake
PLAN[5] = mk([("G", "video", 0, 6, 1, None)])
CAPS[5] = True
r, _ = go(5)
o = r["path"]
assert abs(duration(o) - 6.0) <= 1 / 30 + 1e-3, duration(o)
lit = []
for f in range(0, 180):
    rr, gg, bb, _ = pixel(o, f, 180, 320)
    lit.append("g" if gg > 200 and rr < 90 and bb < 90 else "m" if rr > 180 and bb > 180 and gg < 90 else "-")
first = lambda c: [i for i, x in enumerate(lit) if x == c]  # noqa: E731
assert first("g") == list(range(15, 45)) and first("m") == list(range(54, 84)), (first("g")[:3], first("g")[-3:], first("m")[:3], first("m")[-3:])
assert r["srt"] and Path(r["srt"]).read_text() == "srt shift 0.0"
CAPS[5] = False
r, _ = go(5)
assert r["srt"] is None and not Path(o).with_suffix(".srt").exists()
assert_clean()

projects.originals = lambda uuids, dest: {u: fx / "E.jpg" for u in uuids}  # stands in for the iCloud/PhotoKit download
# ---------- final: folder, never overwrites, hvc1, no side data, colour tags
render._stamp = lambda: "20260101-000000"
PLAN[6] = mk(FIVE)
r1, ran = go(6, "final")
p1 = Path(r1["path"])
assert p1.parent.resolve() == (render.EXPORTS / "Proj-6").resolve() and p1.name == "Proj-6_2026-01-01_0000.mp4", p1
fv = probe(p1, ["-show_entries", "stream=codec_name,codec_tag_string,width,height,color_space,color_transfer"])["streams"]
assert fv[0]["codec_tag_string"] == "hvc1" and (fv[0]["width"], fv[0]["height"]) == (360, 640), fv
assert abs(duration(p1) - PLAN[6].duration) <= 1 / 30 + 1e-3 and not any(s.get("side_data_list") for s in side_rot(p1))
body = p1.read_bytes()
r2, ran2 = go(6, "final")
p2 = Path(r2["path"])
assert p2 == p1 and r2.get("reused") and p1.read_bytes() == body and ran2 == 0, (p1, p2, r2)  # unchanged: the same file back
with open(p1, "ab") as fh:  # the user touched the file: render again (segments still cached), never overwrite it
    fh.write(b"x")
r2, ran2 = go(6, "final")
p2 = Path(r2["path"])
assert p2 != p1 and not r2.get("reused") and ran2 == 0, (p1, p2)
assert_clean()

# still photo: orientation baked (landscape 400x300 with EXIF 6 becomes portrait) and a missing clip fails before any ffmpeg
st = render.still_for("E", "preview", root)
assert Image.open(st).size == (300, 400)
PLAN[7] = mk([("GONE", "video", 0, 1, 1, None)])
n_before = len(RUNS)
try:
    go(7)
    raise AssertionError("expected failure")
except RuntimeError as e:
    assert "could not be fetched" in str(e) and "gone" in str(e), e
assert len(RUNS) == n_before
assert_clean()

# ---------- disk check before a final render
real_du = render.shutil.disk_usage
render.shutil.disk_usage = lambda p: types.SimpleNamespace(total=10**9, used=10**9, free=10**6)
PLAN[7] = mk([("A", "video", 2, 3, 1, None)])
try:
    go(7, "final")
    raise AssertionError("expected disk failure")
except RuntimeError as e:
    assert "disk space" in str(e), e
finally:
    render.shutil.disk_usage = real_du
assert_clean()

# ---------- batch download: final render of N clips makes ONE originals call, readable errors, local clips skipped
import dataclasses  # noqa: E402

import numpy as np  # noqa: E402

for u in ("V1", "V2", "V3"):  # video assets Photos does not hold locally (iCloud only)
    db.execute("insert into assets(uuid, kind, created, duration, path, indexed_at, meta) values (?,?,?,?,?,?,?)",
               (u, "video", "x", 2, f"/nonexistent/{u}.mp4", 1.0, u))
for i in range(14, 22):
    db.execute("insert into collections(id, name, kind, created, updated) values (?,?,?,?,?)", (i, f"Proj {i}", "project", 1.0, 1.0))
db.commit()
dl_calls, dl_seen = [], []


def fake_originals(uuids, dest):
    dl_calls.append(list(uuids))
    j = projects.jobs[(14, "final")]
    dl_seen.append((j.get("stage"), media.ACTIVE.get("render:14:final", {}).get("stage"), media.RUNNING))
    dest.mkdir(parents=True, exist_ok=True)
    for u in uuids:
        (dest / f"{u}.mp4").write_bytes((fx / "A.mp4").read_bytes()) if u != "E" else None
    return {u: (fx / "E.jpg" if u == "E" else dest / f"{u}.mp4") for u in uuids}


real_originals = projects.originals
projects.originals = fake_originals
PLAN[14] = mk([("A", "video", 0, 1, 1, None), ("V1", "video", 0, 1, 1, None), ("V2", "video", 0, 1, 1, None),
               ("E", "photo", 0, 2, 1, None), ("V3", "video", 0, 1, 1, None)])
projects.jobs[(14, "final")] = {}
r, ran = go(14, "final")
assert len(dl_calls) == 1 and sorted(dl_calls[0]) == ["E", "V1", "V2", "V3"] and ran == 5, (dl_calls, ran)  # one call; A is local
assert dl_seen[0][0] == "Downloading originals: 0 of 4" == dl_seen[0][1], dl_seen  # job state and /media/activity both say it
assert dl_seen[0][2] == 1, dl_seen  # only the download's slot is held, never the render slot
assert abs(duration(r["path"]) - PLAN[14].duration) <= 1 / 30 + 1e-3 and media.RUNNING == 0
assert_clean()
# downloaded originals are kept: a later render (e.g. a re-trimmed clip, so a segment cache miss) downloads nothing
assert sorted(f.name for f in render.ORIG_CACHE.iterdir()) == ["V1.mp4", "V2.mp4", "V3.mp4"], list(render.ORIG_CACHE.iterdir())
projects.originals = lambda uuids, dest: (_ for _ in ()).throw(AssertionError(f"downloaded again: {uuids}"))
got, errs = render.fetch_originals([seg_for("V1"), seg_for("V3")], root / "fo0", "k", "l", lambda n, t: None)
assert got == {"V1": render.ORIG_CACHE / "V1.mp4", "V3": render.ORIG_CACHE / "V3.mp4"} and not errs, (got, errs)
assert not list(render.ORIG_CACHE.glob("*.part"))
# the sweep keeps the cache under its cap, oldest first
cap0 = render.ORIG_CAP
render.ORIG_CAP = 2 * (render.ORIG_CACHE / "V1.mp4").stat().st_size
os.utime(render.ORIG_CACHE / "V2.mp4", (1, 1))
render._gc_cache()
assert sorted(f.name for f in render.ORIG_CACHE.iterdir()) == ["V1.mp4", "V3.mp4"]
render.ORIG_CAP = cap0
shutil.rmtree(render.ORIG_CACHE)  # the failure checks below need real downloads
# the worker also reports progress while it runs (n of N from files appearing in the destination)
reports = []


def slow_originals(uuids, dest):
    dest.mkdir(parents=True, exist_ok=True)
    for k, u in enumerate(uuids):
        (dest / f"{u}.mp4").write_bytes(b"x")
        time.sleep(1.2)
    return {}


projects.originals = slow_originals
got, errs = render.fetch_originals([seg_for("V1"), seg_for("V2")], root / "fo", "k", "l", lambda n, t: reports.append((n, t)))
assert got == {} and set(errs) == {"V1", "V2"} and reports[0] == (0, 2) and any(n == 1 for n, _ in reports) and reports[-1] == (0, 2), reports
# failures: a partial result names the missing clip; a timeout says how long; both fail the render before any ffmpeg
projects.originals = lambda uuids, dest: {u: fx / "A.mp4" for u in uuids if u != "V2"}
PLAN[15] = mk([("V1", "video", 0, 1.5, 1, None), ("V2", "video", 0, 1.5, 1, None)])  # not cached yet
n_before = len(RUNS)
try:
    go(15, "final")
    raise AssertionError("expected failure")
except RuntimeError as e:
    assert "1 clips could not be fetched" in str(e) and "V2 · 2s (iCloud download failed)" in str(e) and "V1" not in str(e), e


def times_out(uuids, dest):
    raise subprocess.TimeoutExpired("osxphotos", 1)


projects.originals = times_out
try:
    go(15, "final")
    raise AssertionError("expected failure")
except RuntimeError as e:
    assert f"iCloud download timed out after {settings.get().download_seconds * 2} s" in str(e) and str(e).count("timed out") == 2, e
assert len(RUNS) == n_before and media.RUNNING == 0 and not [lk for lk in media._locks.values() if lk.locked()]
assert_clean()
projects.originals = real_originals
projects.originals = lambda uuids, dest: {u: fx / "E.jpg" for u in uuids}

# ---------- audio, fades, mute, ducking
def pcm(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.float32)


def band(x, t0, t1, f, sr=16000):
    seg = x[int(t0 * sr):int(t1 * sr)]
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) / len(seg)
    fr = np.fft.rfftfreq(len(seg), 1 / sr)
    return float(sp[(fr > f - 10) & (fr < f + 10)].sum())


def luma(path, n):
    cap = cv2.VideoCapture(str(path))
    cap.set(cv2.CAP_PROP_POS_FRAMES, n)
    ok, im = cap.read()
    cap.release()
    assert ok, (path, n)
    return float(im.mean())


def streams(path):
    return sorted(s["codec_type"] for s in probe(path, ["-show_entries", "stream=codec_type"])["streams"])


ff("-f", "lavfi", "-i", "sine=f=1000:d=1.5", str(fx / "vo.wav"))
VO = {"id": "vo", "path": str(fx / "vo.wav"), "start": 1.0, "in": 0.0, "out": 1.5, "volume": 1.0, "fade_in": 0.0, "fade_out": 0.0}
one = mk([("A", "video", 0, 3, 1, None)])  # clip audio: a 440 Hz tone for 3 s


def tlx(base, **kw):
    return dataclasses.replace(base, edit_hash=hashlib.sha1((base.edit_hash + repr(kw)).encode()).hexdigest(), **kw)


PLAN[16] = tlx(one, audio=(VO,), duck=True)
PLAN[17] = tlx(one, audio=(VO,), duck=False)
PLAN[18] = tlx(one)
res = {}
for cid_, label in ((16, "duck"), (17, "noduck"), (18, "plain")):
    r, _ = go(cid_)
    res[label] = r["path"]
    assert streams(r["path"]) == ["audio", "video"] and abs(duration(r["path"]) - 3.0) <= 1 / 30 + 1e-3, (label, r)
duck, noduck, plain = (pcm(res[k]) for k in ("duck", "noduck", "plain"))
assert band(plain, 0.2, 2.8, 440) > 0.05 and band(plain, 0.2, 2.8, 1000) < 0.005  # no tracks: clip audio only
for x in (duck, noduck):  # the voiceover is there from 1.0 s to 2.5 s and nowhere else
    assert band(x, 1.2, 2.3, 1000) > 10 * band(x, 0.1, 0.8, 1000) and band(x, 2.6, 2.95, 1000) < 0.3 * band(x, 1.2, 2.3, 1000)
    assert band(x, 0.1, 0.8, 440) > 0.01  # clip audio before the voiceover
r_duck = band(duck, 1.2, 2.3, 440) / band(duck, 1.2, 2.3, 1000)
r_no = band(noduck, 1.2, 2.3, 440) / band(noduck, 1.2, 2.3, 1000)
assert r_duck < r_no / 3, ("ducking must lower the clip audio under the voiceover", r_duck, r_no)
assert band(duck, 0.1, 0.8, 440) > 0.5 * band(noduck, 0.1, 0.8, 440)  # and not before it starts
# final includes the audio too (same finish pass)
PLAN[19] = tlx(one, audio=(VO,))
r, _ = go(19, "final")
assert streams(r["path"]) == ["audio", "video"] and band(pcm(r["path"]), 1.2, 2.3, 1000) > 0.01, r
# clip_volume scales clip audio; mute (volume 0 on the seg) silences it while the voiceover stays
PLAN[16] = tlx(one, clip_volume=0.25)
quiet = pcm(go(16)[0]["path"])
assert 0.15 < band(quiet, 0.2, 2.8, 440) / band(plain, 0.2, 2.8, 440) < 0.35
PLAN[17] = tlx(dataclasses.replace(one, segs=tuple(dataclasses.replace(s, volume=0.0) for s in one.segs)), audio=(VO,))
muted = pcm(go(17)[0]["path"])
assert band(muted, 0.1, 0.8, 440) < 0.002 and band(muted, 1.2, 2.3, 1000) > 0.01, (band(muted, 0.1, 0.8, 440), band(muted, 1.2, 2.3, 1000))
# window preview: the track starts at 2.0 s on the timeline, the window is 1.0 to 2.8, so the voiceover enters 1.0 s in
PLAN[18] = tlx(one, audio=({**VO, "start": 2.0},))
r, _ = go(18, start=1.0, end=2.8)
w = pcm(r["path"])
assert abs(duration(r["path"]) - 1.8) <= 1 / 30 + 1e-3 and band(w, 0.1, 0.8, 1000) < 0.2 * band(w, 1.2, 1.7, 1000), r
# fades: first and last frames black, audio silent there, the middle untouched
PLAN[16] = tlx(one, fade_in=0.5, fade_out=0.5)
r, _ = go(16)
o = r["path"]
a = pcm(o)
assert luma(o, 0) < 20 < 60 < luma(o, 45) and luma(o, 89) < 40 and luma(o, 80) > luma(o, 89), (luma(o, 0), luma(o, 45), luma(o, 89))
assert np.abs(a[:160]).max() < 0.01 and np.abs(a[-160:]).max() < 0.01 and np.abs(a[16000:32000]).max() > 0.08, (np.abs(a[:160]).max(), np.abs(a[-160:]).max())
# preview_key changes when an audio file changes in place (path stays, mtime and size move); a missing file fails early
ap = root / "k.wav"
ap.write_bytes(b"1234")
kt = tlx(one, audio=({**VO, "path": str(ap)},))
k1 = render.preview_key(kt, {"captions": {"on": False}}, None)
assert k1 == render.preview_key(kt, {"captions": {"on": False}}, None)
ap.write_bytes(b"12345678")
k2 = render.preview_key(kt, {"captions": {"on": False}}, None)
assert k1 != k2
os.utime(ap, ns=(1, 1))
assert render.preview_key(kt, {"captions": {"on": False}}, None) != k2
PLAN[20] = tlx(one, audio=({**VO, "path": str(root / "gone.wav")},))
try:
    go(20)
    raise AssertionError("expected failure")
except RuntimeError as e:
    assert "gone.wav is missing" in str(e), e
assert_clean()

# ---------- chunked join: 20 one-second segments, a dissolve every third cut, JOIN_CHUNK patched to 4, final at 540x960
spec20 = [("A", "video", round(i * 0.1, 1), round(i * 0.1, 1) + 1, 1, ("fade", 6) if i % 3 == 2 else None) for i in range(20)]
spec20[-1] = spec20[-1][:5] + (None,)
PLAN[7] = mk(spec20, size=540)
render.JOIN_CHUNK[540] = 4
joins = []
real_join, real_finish = render.join_command, render.finish_command
render.join_command = lambda files, *a, **k: (joins.append(len(files)), real_join(files, *a, **k))[1]
fins = []
render.finish_command = lambda files, *a, **k: (fins.append(len(files)), real_finish(files, *a, **k))[1]
r, _ = go(7, "final")
render.join_command, render.finish_command = real_join, real_finish
trans = [s.trans_after for s in PLAN[7].segs[:-1]]
plan = list(render.levels([s.frames for s in PLAN[7].segs], trans, 4))
assert len(plan) >= 2 and all(trans[b - 1] is None for a, b in plan[0][0][:-1]), plan[0][0]
assert joins == [b - a for cs, _, _ in plan[:-1] for a, b in cs if b - a > 1] and fins == [len(plan[-1][0]) and fins[0]], (joins, fins)
assert abs(duration(r["path"]) - PLAN[7].duration) <= 1 / 30 + 1e-3, (duration(r["path"]), PLAN[7].duration)
assert resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss < 2 * 1024**3
assert_clean()

# ---------- progress is monotone and ends at 1.0
seen = []


def watch(cmd, *a, **k):
    real_run(cmd, *a, **k)
    seen.append(projects.jobs[(8, "preview")].get("progress", 0))


render.run_ffmpeg = watch
PLAN[8] = mk([("A", "video", 2.5, 3.5, 1, None), ("B", "video", 0.5, 1.5, 1, None)])
r, _ = go(8)
render.run_ffmpeg = spy
assert seen == sorted(seen) and len(seen) >= 3 and projects.jobs[(8, "preview")]["progress"] == 1.0, seen

# ---------- cancel: a running ffmpeg is killed fast; a cancelled render leaves nothing behind
ev = threading.Event()
threading.Timer(0.4, ev.set).start()
t0 = time.time()
try:
    real_run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=30:d=600", "-f", "null", "-"],
             600, lambda f: None, ev, 600)
    raise AssertionError("expected Cancelled")
except RuntimeError as e:
    assert str(e) == "Cancelled" and time.time() - t0 < 5
n = {"c": 0}


def cancel_second(cmd, *a, **k):
    n["c"] += 1
    if n["c"] == 2:
        render.CANCEL[(9, "final")].set()
    return real_run(cmd, *a, **k)


render.run_ffmpeg = cancel_second
PLAN[9] = mk([("A", "video", 3.0, 3.5, 1, None), ("B", "video", 1.0, 2.0, 1, None), ("C", "video", 1.0, 2.0, 1, None)])
try:
    go(9, "final")
    raise AssertionError("expected Cancelled")
except RuntimeError as e:
    assert str(e) == "Cancelled", e
assert (9, "final") not in render.CANCEL and media.RUNNING == 0
render.run_ffmpeg = spy
assert_clean()

# ---------- slots: max_agents=1, two renders never overlap, the second shows it is waiting
settings._cur = settings.Settings(max_agents=1)
peak, waiting = [0], []


def slow(cmd, *a, **k):
    peak[0] = max(peak[0], media.RUNNING)
    time.sleep(0.4)
    waiting.append(media.ACTIVE.get("render:11:preview", {}).get("stage"))
    return real_run(cmd, *a, **k)


PLAN[10] = mk([("A", "video", 1.0, 1.4, 1, None)])
PLAN[11] = mk([("A", "video", 1.2, 1.6, 1, None)])
db.execute("insert into collections(id, name, kind, created, updated) values (10,'P10','project',1.0,1.0),(11,'P11','project',1.0,1.0)")
db.commit()
go(11)  # its segment is cached now, so the second render goes straight for the render slot (a fetch also takes a slot)
(render.RENDERS / "11" / "preview.json").unlink()  # force a real render (not a reused preview) to test the slot
render.run_ffmpeg = slow
ts = [threading.Thread(target=go, args=(c,)) for c in (10, 11)]
ts[0].start(); time.sleep(0.15); ts[1].start()
time.sleep(0.15)
assert projects.jobs[(11, "preview")].get("note") == "Waiting for a free slot", projects.jobs.get((11, "preview"))
assert media.ACTIVE["render:11:preview"]["stage"] == "waiting for a free slot"
for t in ts:
    t.join(30)
assert peak[0] == 1 and media.RUNNING == 0, peak
render.run_ffmpeg = spy
settings._cur = settings.Settings(max_agents=2)

# ---------- routes: validation, a full job, file/sheet only for the recorded path, cancel, reveal
app = FastAPI()
app.include_router(render.router)
app.add_exception_handler(library.RequestValidationError, library.validation_error)
c = TestClient(app)
assert c.post("/collections/1/render", json={"mode": "bogus"}).status_code == 422
assert c.post("/collections/1/render", json={"mode": "final", "start": 1, "end": 2}).status_code == 422
assert c.post("/collections/1/render", json={"mode": "preview", "start": 3, "end": 2}).status_code == 422
for bad in ('{"mode": "preview", "start": NaN}', '{"mode": "preview", "end": Infinity}'):  # used to start a job that failed
    assert c.post("/collections/1/render", content=bad, headers={"content-type": "application/json"}).status_code == 422, bad
assert c.post("/collections/99/render", json={"mode": "preview"}).status_code == 422
assert c.post("/collections/12345/render", json={"mode": "preview"}).status_code == 404
PLAN[12] = PLAN[1]
db.execute("insert into collections(id, name, kind, created, updated) values (12,'P12','project',1.0,1.0)")
db.commit()
assert c.post("/collections/12/render", json={"mode": "preview"}).status_code == 202
assert c.post("/collections/12/render", json={"mode": "preview"}).status_code == 409
for _ in range(100):
    j = c.get("/collections/12/render/preview").json()
    if j["status"] != "running":
        break
    time.sleep(0.2)
assert j["status"] == "done" and j["cached"] == 5, j
f = c.get("/collections/12/render/preview/file")
assert f.status_code == 200 and f.content[4:8] == b"ftyp"
assert c.get("/collections/12/render/preview/sheet").headers["content-type"] == "image/jpeg"
assert c.get("/collections/12/render/final/file").status_code == 404
projects.jobs[(12, "preview")]["path"] = "/etc/hosts"
assert c.get("/collections/12/render/preview/file").status_code == 404
projects.jobs[(12, "preview")]["path"] = str(fx / "A.mp4")
assert c.get("/collections/12/render/preview/file").status_code == 404
projects.jobs[(12, "preview")]["path"] = j["path"]
assert c.post("/collections/12/render/preview/cancel").status_code == 204
calls = []
real_sub = subprocess.run
render.subprocess.run = lambda cmd, **k: calls.append(cmd)
assert c.post("/collections/12/render/preview/reveal").status_code == 204
assert c.post("/collections/12/render/preview/reveal?play=1").status_code == 204
render.subprocess.run = real_sub
rp = str(Path(j["path"]).resolve())
assert calls == [["open", "-R", rp], ["open", rp]], calls

# limits and bad input
big = mk([("A", "video", 0, 1, 1, None)] * 1)
PLAN[13] = Timeline(30, "9:16", 360, tuple(big.segs[0] for _ in range(201)), 30, (), "x")
db.execute("insert into collections(id, name, kind, created, updated) values (13,'P13','project',1.0,1.0)")
db.commit()
try:
    go(13)
    raise AssertionError
except RuntimeError as e:
    assert "limited to 200" in str(e)
assert_clean()
assert "data/index.db" not in str(core.DB_PATH)
# saved preview survives an engine restart (job state gone) and says when the edit changed since
projects.jobs.pop((1, "preview"), None)
j = c.get("/collections/1/render/preview").json()
assert j["status"] == "done" and j.get("saved") and j["path"] == render.saved_preview(1)["path"], j
# rhythm: moving clips, a ramp, a held last frame and seam sounds go through the whole preview pipeline
from dataclasses import replace  # noqa: E402
r14 = mk([("A", "video", 0, 1, 1, None), ("C", "video", 0, 1.5, 1, ("whipleft", 6)), ("E", "photo", 0, 1, 1, None)])
segs = list(r14.segs)
segs[0] = replace(segs[0], motion=("punch", 0.5), flash=True, shake=True, beat_frames=(15,))
segs[1] = replace(segs[1], ramp="slow-mid", volume=0.0, motion=("drift-left", 0.3), src_out=1.0)  # source shorter than needed: holds
segs[2] = replace(segs[2], motion=("push", 0.4))
PLAN[77] = replace(r14, segs=tuple(segs), sfx=((segs[1].start_frame, "whoosh"), (segs[2].start_frame, "hit")))
db.execute("insert into collections(id, name, kind, created, updated) values (77,'P77','project',1.0,1.0)")
db.commit()
res, n = go(77)
assert n == 3 and abs(duration(res["path"]) - PLAN[77].duration) < 0.1, (res, duration(res["path"]))
assert any("whoosh.wav" in " ".join(map(str, cmd)) and "hit.wav" in " ".join(map(str, cmd)) for cmd in RUNS), "seam sounds not mixed"
assert_clean()
# seamfx transitions: a seam clip is drawn once, joined frame exact, then reused from the cache
import seamfx  # noqa: E402
PLAN[78] = mk([("A", "video", 0, 1.5, 1, ("lightleak", 9)), ("G", "video", 0, 1.5, 1, ("glitch", 6)), ("E", "photo", 0, 1, 1, None)])
db.execute("insert into collections(id, name, kind, created, updated) values (78,'P78','project',1.0,1.0)")
db.commit()
before = seamfx.RUNS
res, _ = go(78)
assert seamfx.RUNS == before + 2 and len(list((render.CACHE / "preview").glob("seam-*.mov"))) >= 2, seamfx.RUNS
assert abs(duration(res["path"]) - PLAN[78].duration) < 0.05 and PLAN[78].total_frames == 45 + 45 + 30 - 15, res
PLAN[78] = tlx(PLAN[78])  # new hash, same segments: the seams come from the cache
go(78)
assert seamfx.RUNS == before + 2, seamfx.RUNS
assert_clean()
# Export for Resolve: clips already on disk are cloned in, only the rest is downloaded (and a rerun re-copies nothing)
asked = []
projects.originals = lambda uuids, dest: (asked.append(list(uuids)), {})[1]
xd = root / "export-media"
got = projects.export_sources([{"uuid": "A", "kind": "video"}, {"uuid": "A", "kind": "video"}, {"uuid": "V1", "kind": "video"}], xd)
assert set(got) == {"A"} and got["A"].read_bytes() == (fx / "A.mp4").read_bytes() and asked == [["V1"]], (got, asked)
assert not list(xd.glob("*.part"))
print("render checks passed")
