"""Captions: cue mapping, srt, PNG cue track, fake whisper-cli, cache, routes.  uv run test_captions.py
No real whisper, no network. edit.* is stubbed (it belongs to the spec branch)."""
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")  # before importing core

import core  # noqa: E402

assert "data/index.db" not in str(core.DB_PATH)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import captions  # noqa: E402
import library  # noqa: E402
import media  # noqa: E402
import projects  # noqa: E402
from captions import Cue, Line  # noqa: E402
from timeline import Seg, Timeline  # noqa: E402

tmp = Path(tempfile.mkdtemp())
core.AUDIO_DIR = tmp / "audio"
media.PROXIES = tmp / "proxies"
captions.MODELS = tmp / "models"


def seg(uuid, a, b, speed, start_frame, frames, kind="video"):
    return Seg(1, uuid, kind, a, b, speed, 1.0, (0, 1, 1, 0), None, ("center", .5, .5), frames, start_frame, None)


def tl_of(*segs, fps=30):
    total = max(s.start_frame + s.frames for s in segs)
    return Timeline(fps, "9:16", 1080, tuple(segs), total, (), "h")


# ---------- map_cues: the worked example from the design (in 4, out 10, speed 2, starts at 12.0)
tl = tl_of(seg("u1", 4, 10, 2.0, 360, 90))
got = captions.map_cues(tl, {"u1": [Line(6, 8, "a"), Line(3, 5, "b"), Line(9.5, 12, "c")]})
assert [(c.t0, c.t1, c.text) for c in got] == [(12.0, 12.5, "b"), (13.0, 14.0, "a")], got
# speed 0.5 stretches
got = captions.map_cues(tl_of(seg("u1", 0, 4, 0.5, 0, 240)), {"u1": [Line(1, 2, "x")]})
assert [(c.t0, c.t1) for c in got] == [(2.0, 4.0)], got
# overlapping tails are cut at the next cue; under 0.25 s dropped; photos ignored
tl2 = tl_of(seg("a", 0, 4, 1.0, 0, 120), seg("b", 0, 4, 1.0, 90, 120))
got = captions.map_cues(tl2, {"a": [Line(2.5, 4, "tail")], "b": [Line(0, 1, "next")]})
assert [(c.t0, c.t1, c.text) for c in got] == [(2.5, 3.0, "tail"), (3.0, 4.0, "next")], got
assert captions.map_cues(tl_of(seg("a", 0, 4, 1.0, 0, 120), seg("p", 0, 3, 1.0, 120, 90, "photo")), {"a": [Line(0, .2, "t")], "p": [Line(0, 3, "no")]}) == []
# the same uuid twice with different ranges: both get their lines
tl3 = tl_of(seg("u", 0, 2, 1.0, 0, 60), seg("u", 5, 7, 1.0, 60, 60))
got = captions.map_cues(tl3, {"u": [Line(0.5, 1.5, "one"), Line(5.5, 6.5, "two")]})
assert [(c.t0, c.text) for c in got] == [(0.5, "one"), (2.5, "two")], got

# ---------- srt
srt = tmp / "x.srt"
captions.write_srt([Cue(0.5, 1.5, "hello"), Cue(3725.25, 3726.0, "late")], srt, shift=0.5)
assert srt.read_text() == "1\n00:00:00,000 --> 00:00:01,000\nhello\n\n2\n01:02:04,750 --> 01:02:05,500\nlate\n", repr(srt.read_text())
captions.write_srt([Cue(0.0, 1.0, "gone"), Cue(2.0, 3.0, "kept")], srt, shift=1.5)  # ends before the window: skipped
assert srt.read_text().startswith("1\n00:00:00,500 --> 00:00:01,500\nkept")
captions.write_srt([Cue(1.0, 2.0, "in"), Cue(2.5, 4.0, "clipped"), Cue(5.0, 6.0, "after")], srt, shift=1.0, end=2.0)
assert srt.read_text() == "1\n00:00:00,000 --> 00:00:01,000\nin\n\n2\n00:00:01,500 --> 00:00:02,000\nclipped\n", repr(srt.read_text())

# ---------- clean
L = lambda *t: [Line(i, i + 1, x) for i, x in enumerate(t)]  # noqa: E731
assert [l.text for l in captions.clean(L(" hi ", "[MUSIC]", "(applause)", "*laughs*", "♪ ♪", "", "ok"))] == ["hi", "ok"]
assert [l.text for l in captions.clean(L("a", "a", "a", "a", "b", "b"))] == ["a", "b", "b"]

# ---------- build: PNG track
W, H = 540, 960
t4 = tl_of(seg("u", 0, 4, 1.0, 0, 120))
long = "This is a rather long caption that needs to be wrapped over several lines to fit the frame width nicely " * 2
cues = [Cue(0.5, 1.5, "hello world"), Cue(1.8, 2.8, "你好世界"), Cue(3.0, 3.8, long)]
work = tmp / "w"
cap = captions.build(t4, "bold", (W, H), cues, work, None)
lines = cap.list_file.read_text().splitlines()
assert lines[0] == "ffconcat version 1.0"
files = [l for l in lines if l.startswith("file ")]
assert len(lines) == 1 + 3 * (len(files) - 1) + 2, "every file but the last has option framerate + duration; last has framerate only"
assert all(lines[i + 1] == "option framerate 30" for i, l in enumerate(lines) if l.startswith("file "))
durs = [float(l.split()[1]) for l in lines if l.startswith("duration ")]
assert abs(sum(durs) - 4.0) < 1e-3, sum(durs)
assert len(cap.cues) >= 4, "the long cue was split into pages"  # hello, nihao, 2+ pages
png = work / files[1].split("'")[1]  # first cue (after the leading blank)
im = Image.open(png)
assert im.size == (W, H) and im.mode == "RGBA"
box = im.getchannel("A").getbbox()
assert box and box[1] > H * 0.6 and box[3] <= H * 0.81, box  # text sits in the lower part, above 0.80 H
assert Image.open(work / files[0].split("'")[1]).getchannel("A").getbbox() is None, "blank is transparent"
# every cue has at most 2 lines
for c in cap.cues:
    assert c.text.count("\n") <= 1, c.text
# CJK picks the unicode font; latin the bold one
assert captions._font("abc", 20).path == captions.FONT and captions._font("你好", 20).path == captions.FONT_UNI
# 16:9 uses the lower position (0.92 H)
cap169 = captions.build(t4, "box", (960, 540), [Cue(0.5, 1.5, "wide")], tmp / "w2", None)
f169 = [l for l in cap169.list_file.read_text().splitlines() if l.startswith("file ")]
b = Image.open(tmp / "w2" / f169[1].split("'")[1]).getchannel("A").getbbox()
assert b[3] > 540 * 0.85, b
# window: times shift, duration is the window length
capw = captions.build(t4, "bold", (W, H), cues, tmp / "w3", (1.0, 3.0))
assert abs(sum(float(l.split()[1]) for l in capw.list_file.read_text().splitlines() if l.startswith("duration ")) - 2.0) < 1e-3
assert capw.cues[0].t0 == 0.0 and abs(capw.cues[1].t0 - 0.8) < 0.04, capw.cues
assert captions.build(t4, "bold", (W, H), [Cue(0, 0.1, "short")], tmp / "w4", None).list_file is None

# ---------- fake whisper-cli
ARGV = tmp / "argv.txt"
fake = tmp / "whisper-cli"
fake.write_text(f"""#!{sys.executable}
import json, os, sys
a = sys.argv[1:]
open({str(ARGV)!r}, "a").write(json.dumps(a) + "\\n")
if os.environ.get("FAKE_METAL_FAIL") and "-ng" not in a:
    sys.stderr.write("ggml_metal_init: failed\\n"); sys.exit(1)
assert a[a.index("-f") + 1].endswith(".wav") and os.path.isfile(a[a.index("-f") + 1])
rows = [("Hello there", 500, 2500), ("[MUSIC]", 2500, 3000), ("Bye", 3000, 3500), ("Bye", 3500, 4000), ("Bye", 4000, 4500)]
json.dump({{"result": {{"language": "en"}}, "transcription": [{{"text": " " + t, "offsets": {{"from": f, "to": e}}}} for t, f, e in rows]}},
          open(a[a.index("-of") + 1] + ".json", "w"))
""")
fake.chmod(0o755)

# setup_status: missing binary and model, with the install commands
captions.whisper_bin = lambda: None
s = captions.setup_status()
assert s["whisper"] is None and not s["model"] and "brew install whisper-cpp" in s["hint"] and captions.MODEL in s["hint"]
try:
    captions.require_setup()
    raise AssertionError("expected RuntimeError")
except RuntimeError as e:
    assert "brew install" in str(e)
captions.whisper_bin = lambda: str(fake)
assert captions.setup_status()["hint"].startswith("mkdir -p data/models")
captions.MODELS.mkdir()
captions.model_path().write_bytes(b"x")
assert captions.setup_status() == {"whisper": str(fake), "model": True, "hint": ""}

# media: a loud sine (speech stand-in), a near-silent one, a video with no audio
db = core.connect()
for u, kind in [("LOUD", "video"), ("QUIET", "video"), ("MUTE", "video"), ("PIC", "image")]:
    db.execute("insert into assets(uuid, kind, duration, meta) values (?,?,?,?)", (u, kind, 4.0, "Beach"))
db.commit()
core.AUDIO_DIR.mkdir()
media.PROXIES.mkdir()


def ff(*a):
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *a], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


ff("-f", "lavfi", "-i", "sine=f=300:d=4", "-c:a", "libopus", str(core.AUDIO_DIR / "LOUD.opus"))
ff("-f", "lavfi", "-i", "sine=f=300:d=4", "-af", "volume=0.0005", "-c:a", "libopus", str(core.AUDIO_DIR / "QUIET.opus"))
ff("-f", "lavfi", "-i", "color=c=gray:s=64x64:d=1", "-pix_fmt", "yuv420p", str(media.PROXIES / "MUTE.mp4"))

assert captions.cached("LOUD") is None
errors = {}
prog = []
res = captions.ensure_transcripts(["LOUD", "QUIET", "MUTE", "LOUD"], lambda n, t: prog.append((n, t)), errors)
assert not errors, errors
assert prog == [(1, 3), (2, 3), (3, 3)], prog
assert [(l.t0, l.t1, l.text) for l in res["LOUD"]] == [(0.5, 2.5, "Hello there"), (3.0, 3.5, "Bye")], res["LOUD"]
assert res["QUIET"] == [] and res["MUTE"] == []
assert captions.cached("QUIET")[0] == "silent" and captions.cached("MUTE")[0] == "no_audio"
calls = [__import__("json").loads(x) for x in ARGV.read_text().splitlines()]
assert len(calls) == 1, "silent and no-audio clips never reach whisper"
a = calls[0]
assert a[:2] == ["-m", str(captions.model_path())] and a[a.index("-l") + 1] == "auto" and "-oj" in a and "-sns" in a
assert a[a.index("-t") + 1] == "4" and "-np" in a and "-ng" not in a and a[a.index("-f") + 1].endswith(".wav")
with core.connect() as c:
    assert c.execute("select count(*) from transcripts").fetchone()[0] == 3
    assert c.execute("select count(*) from transcript_lines").fetchone()[0] == 2
captions.ensure_transcripts(["LOUD"])
assert len(ARGV.read_text().splitlines()) == 1, "second call is served from the cache"
assert media.RUNNING == 0 and not media.ACTIVE, "slot and stage released"

# metal failure retries once with -ng
with core.connect() as c:
    c.execute("delete from transcripts"); c.execute("delete from transcript_lines"); c.commit()
os.environ["FAKE_METAL_FAIL"] = "1"
ARGV.unlink()
captions.transcribe("LOUD")
calls = [__import__("json").loads(x) for x in ARGV.read_text().splitlines()]
assert len(calls) == 2 and "-ng" not in calls[0] and calls[1][-1] == "-ng", calls
del os.environ["FAKE_METAL_FAIL"]

# a photo is reported, not cached; a failing clip does not stop the others
with core.connect() as c:
    c.execute("delete from transcripts"); c.execute("delete from transcript_lines"); c.commit()
errors = {}
res = captions.ensure_transcripts(["PIC", "LOUD"], errors=errors)
assert "PIC" in errors and len(res["LOUD"]) == 2 and captions.cached("PIC") is None, errors

# missing whisper with uncached clips raises the hint up front
with core.connect() as c:
    c.execute("delete from transcripts"); c.commit()
captions.whisper_bin = lambda: None
try:
    captions.ensure_transcripts(["LOUD"])
    raise AssertionError("expected RuntimeError")
except RuntimeError as e:
    assert "brew install whisper-cpp" in str(e)
captions.whisper_bin = lambda: str(fake)

# ---------- routes + job (edit stubbed)
fake_spec = {"captions": {"on": True, "style": "box"}}
sys.modules["edit"] = types.SimpleNamespace(load=lambda cid: (fake_spec, 1))
app = FastAPI()
app.include_router(library.router)
app.include_router(captions.router)
c = TestClient(app)
cid = c.post("/collections", json={"name": "P", "kind": "project"}).json()["id"]
for u in ("LOUD", "QUIET", "PIC"):
    c.post(f"/collections/{cid}/items", json={"uuids": [u]})
with core.connect() as cx:
    cx.execute("delete from transcripts"); cx.execute("delete from transcript_lines"); cx.commit()
st = c.get(f"/collections/{cid}/captions").json()
assert st["on"] and st["style"] == "box" and st["pending"] == 2 and st["ready"] == 0 and st["setup"]["hint"] == "", st
assert c.post(f"/collections/{cid}/captions/transcribe").status_code == 202
for _ in range(100):
    j = c.get(f"/collections/{cid}/captions/job").json()
    if j["status"] != "running":
        break
    time.sleep(0.1)
assert j["status"] == "done" and j["progress"] == 1.0 and "2 of 2" in j["message"] and not j["warnings"], j
st = c.get(f"/collections/{cid}/captions").json()
assert (st["ready"], st["pending"], st["no_speech"]) == (1, 0, 1), st
captions.start_transcription(cid)  # nothing pending: finishes at once
time.sleep(0.5)
assert c.get(f"/collections/{cid}/captions/job").json()["status"] == "done"
tr = c.get("/media/LOUD/transcript").json()
assert tr["status"] == "ok" and len(tr["lines"]) == 2 and c.get("/media/NOPE/transcript").status_code == 404
captions.whisper_bin = lambda: None
assert c.post(f"/collections/{cid}/captions/transcribe").status_code == 422

print("test_captions: ok")
