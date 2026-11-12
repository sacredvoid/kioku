"""Captions: cue mapping, srt, PNG cue track, fake whisper-cli, cache, routes.  uv run test_captions.py
No real whisper, no network. edit.* is stubbed (it belongs to the spec branch)."""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import math
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")  # before importing core
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain

import core  # noqa: E402

testenv.check()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import captions  # noqa: E402
import edit  # noqa: E402  (the real one, only for the shared constants; stubbed again below)
import library  # noqa: E402
import media  # noqa: E402
import projects  # noqa: E402
import settings  # noqa: E402
from captions import Cue, Line  # noqa: E402
from timeline import Seg, Timeline  # noqa: E402

tmp = Path(tempfile.mkdtemp())
core.AUDIO_DIR = tmp / "audio"
media.PROXIES = tmp / "proxies"
captions.MODELS = tmp / "models"


def seg(uuid, a, b, speed, start_frame, frames, kind="video", item_id=1):
    return Seg(item_id, uuid, kind, a, b, speed, 1.0, (0, 1, 1, 0), None, ("center", .5, .5), frames, start_frame, None)


def tl_of(*segs, fps=30):
    total = max(s.start_frame + s.frames for s in segs)
    return Timeline(fps, "9:16", 1080, tuple(segs), total, (), "h")


# ---------- additive migration: tables made before word timing keep their rows
with core.connect() as _c:
    _c.executescript("""create table transcripts(uuid text not null, model text not null, lang text, status text not null
        check(status in ('ok','no_audio','silent')), created real, primary key(uuid, model));
      create table transcript_lines(uuid text not null, model text not null, idx integer not null, t0 real not null,
        t1 real not null, text text not null, primary key(uuid, model, idx));
      insert into transcripts values ('OLD', ?, 'nn', 'ok', 1.0);
      insert into transcript_lines values ('OLD', ?, 0, 0, 1, 'old line');""".replace("?", f"'{captions.MODEL}'"))
assert captions.cached("OLD") == ("ok", [Line(0, 1, "old line")]), captions.cached("OLD")
assert captions.fresh("OLD", "auto") is not None and captions.fresh("OLD", "en") is None, "old rows count as auto"
assert captions.speech_status("OLD") == "speech" and captions.speech_status("NOPE") == "not transcribed"
captions.store("NEW", "ok", "en", [Line(0, 1, "hi", ((0.0, 0.5, "hi"),))], "en")
captions.store("NEW2", "ok", "en", [Line(0, 1, "hi")], "en")
assert captions.cached("NEW")[1][0].words == ((0.0, 0.5, "hi"),)
assert captions.fresh("NEW", "en", words=True) and captions.fresh("NEW2", "en") and captions.fresh("NEW2", "en", words=True) is None
captions._migrated = False
captions.cached("OLD")  # second run of the migration is a no-op
assert settings.get().caption_language == "en"
assert set(captions.FONTS) == set(edit.CAPTION_FONTS) and all(Path(f).is_file() for f in captions.FONTS.values())
assert captions.DEFAULT == {k: v for k, v in edit.CAPTIONS_DEFAULT.items() if k != "on"}

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

# word-level cues map like lines (in 4, out 10, speed 2, starts at 12.0), and only picked clips get cues
W3 = ((6.0, 6.5, "a"), (6.5, 7.9, "b"))
got = captions.map_cues(tl, {"u1": [Line(6, 8, "a b", W3)]})
assert [(round(a, 3), round(b, 3), t) for a, b, t in got[0].words] == [(13.0, 13.25, "a"), (13.25, 13.95, "b")], got
twin = tl_of(seg("u", 0, 2, 1.0, 0, 60, item_id=1), seg("u", 0, 2, 1.0, 60, 60, item_id=2))
both = {"u": [Line(0.5, 1.5, "x")]}
assert [c.t0 for c in captions.map_cues(twin, both)] == [0.5, 2.5]
assert [c.t0 for c in captions.map_cues(twin, both, {2})] == [2.5], "only the picked clip"
assert captions.map_cues(twin, both, set()) == []

# ---------- clean
L = lambda *t: [Line(i, i + 1, x) for i, x in enumerate(t)]  # noqa: E731
assert [l.text for l in captions.clean(L(" hi ", "[MUSIC]", "(applause)", "*laughs*", "♪ ♪", "", "ok"))] == ["hi", "ok"]
assert [l.text for l in captions.clean(L("a", "a", "a", "a", "b", "b"))] == ["a", "b", "b"]

# ---------- parse (whisper -ojf) and hallucination signals
def tok(text, a, b, p=0.9):
    return {"text": text, "offsets": {"from": a, "to": b}, "p": p}


data = {"result": {"language": "en"}, "transcription": [{"text": " Hi Caption!", "offsets": {"from": 0, "to": 3000}, "tokens": [
    tok("[_BEG_]", 0, 0), tok(" Hi", 40, 400), tok(" Capt", 500, 800), tok("ion", 800, 1000), tok("!", 1900, 1900), tok("[_TT_150]", 3000, 3000)]}]}
lang, raw, lp = captions.parse(data)
assert lang == "en" and raw == [Line(0, 3.0, " Hi Caption!", ((0.04, 0.4, "Hi"), (0.5, 1.0, "Caption!")))], raw
assert abs(lp - math.log(0.9)) < 1e-9
assert captions.parse({"transcription": []}) == (None, [], None)
R = lambda *segs: [Line(a, b, t) for a, b, t in segs]  # noqa: E731
real = R((0, 1.7, "We went to the lake"), (1.7, 4.2, "and it was cold"), (4.2, 5, "so cold"))
assert not captions.hallucinated(real, "en", "en", -0.3)
assert not captions.hallucinated([], "en", "en", None) and not captions.hallucinated(R((0, 2, " ")), "en", "en", None)
assert captions.hallucinated(R((0, 2.0, "Some words"), (2.0, 4.0, "More words"), (4.0, 6.0, "Even more")), "en", "en", -0.2), "exact 2.0 s windows"
assert captions.hallucinated(R((0, 2.0, "One real line")), "en", "en", -0.2), "a single 2.0 s window is also invented"
assert captions.hallucinated(real, "zh", "en", -0.3), "language other than the setting"
assert not captions.hallucinated(real, "pt", "auto", -0.3), "auto accepts any"
assert captions.hallucinated(real, "en", "en", -1.4), "low logprob"
assert captions.hallucinated([Line(0, 7.9, "Thank you.", ((0.0, 3.6, "Thank"), (3.6, 5.8, "you.")))], "en", "en", -0.1), "two words smeared over seconds"
assert not captions.hallucinated([Line(0, 7.9, "ok then", ((0.0, 0.4, "ok"), (0.5, 0.9, "then")))], "en", "en", -0.1)
assert captions.hallucinated(R((0, 1.3, "Thank you for watching."), (1.3, 3.1, "Please subscribe")), "en", "en", -0.2)
assert captions.hallucinated(R((0, 1.3, "la"), (1.3, 3.1, "la"), (3.1, 4.4, "la"), (5, 6.1, "ok")), "en", "en", -0.2), "repeated lines"

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

# ---------- styles: position, fonts, uppercase, karaoke and pop timing (30 fps, frame exact)
def files_of(cap):
    return [l.split("'")[1] for l in cap.list_file.read_text().splitlines() if l.startswith("file ")]


def durs_of(cap):
    return [round(float(l.split()[1]) * 30) for l in cap.list_file.read_text().splitlines() if l.startswith("duration ")]


def ink(path, rgb):
    im = Image.open(path).convert("RGBA")
    return sum(1 for px in (im.get_flattened_data() if hasattr(im, 'get_flattened_data') else im.getdata()) if px[3] > 200 and px[:3] == rgb)  # noqa: E501


one = [Cue(1.0, 3.0, "hi", ((1.0, 1.5, "hi"),))]
for pos, lo, hi in (("top", 0.1, 0.3), ("middle", 0.4, 0.6), ("bottom", 0.7, 0.81)):
    cp = captions.build(t4, {"style": "bold", "position": pos}, (W, H), one, tmp / f"pos-{pos}", None)
    bb = Image.open(tmp / f"pos-{pos}" / files_of(cp)[1]).getchannel("A").getbbox()
    assert lo * H <= (bb[1] + bb[3]) / 2 <= hi * H, (pos, bb)
cpl = captions.build(tl_of(seg("u", 0, 4, 1.0, 0, 120)), {"style": "bold", "position": "top"}, (960, 540), one, tmp / "pos-land", None)
assert Image.open(tmp / "pos-land" / files_of(cpl)[1]).getchannel("A").getbbox()[1] >= 0.07 * 540
sizes = {}
for sz in "SML":
    cp = captions.build(t4, {"style": "bold", "size": sz}, (W, H), one, tmp / f"sz-{sz}", None)
    b_ = Image.open(tmp / f"sz-{sz}" / files_of(cp)[1]).getchannel("A").getbbox()
    sizes[sz] = b_[3] - b_[1]
assert sizes["S"] < sizes["M"] < sizes["L"], sizes
for key in captions.FONTS:  # every listed font loads and draws
    cp = captions.build(t4, {"style": "box", "font": key, "uppercase": True}, (W, H), one, tmp / f"f-{key}", None)
    assert Image.open(tmp / f"f-{key}" / files_of(cp)[1]).getchannel("A").getbbox()

# karaoke: words at 1.0 / 1.5 / 2.4 s, cue ends at 3.0; one state per word, each until the next word starts
KW = ((1.0, 1.5, "one"), (1.5, 2.2, "two"), (2.4, 3.0, "three"))
kcue = [Cue(1.0, 3.0, "one two three", KW)]
kcfg = {"style": "bold", "highlight": True, "color": "#FFFFFF", "highlight_color": "#FFD400"}
kc = captions.build(t4, kcfg, (W, H), kcue, tmp / "k1", None)
assert [(c.t0, c.t1) for c in kc.cues] == [(1.0, 1.5), (1.5, 2.4), (2.4, 3.0)], kc.cues
assert durs_of(kc) == [30, 15, 27, 18, 30], durs_of(kc)  # blank, three words, blank to 4.0 s
ff_ = files_of(kc)
assert len(set(ff_[1:4])) == 3 and ff_[0] == ff_[-1] and all(l == "option framerate 30" for l in
    [x for x in kc.list_file.read_text().splitlines() if x.startswith("option")])
assert all(ink(tmp / "k1" / f, (255, 212, 0)) > 0 for f in ff_[1:4]), "each state has the highlighted word"
assert all(ink(tmp / "k1" / f, (255, 255, 255)) > 0 for f in ff_[1:3]), "the others stay in the base colour"
off = captions.build(t4, {"style": "bold", "highlight": False}, (W, H), kcue, tmp / "k0", None)
assert len(off.cues) == 1 and ink(tmp / "k0" / files_of(off)[1], (255, 212, 0)) == 0, "no highlight, one page"
# a cue without word times falls back to the plain page even with highlight on
nw = captions.build(t4, kcfg, (W, H), [Cue(1.0, 3.0, "no words")], tmp / "k2", None)
assert len(nw.cues) == 1
# window shifts word states by whole frames
kw = captions.build(t4, kcfg, (W, H), kcue, tmp / "k3", (0.5, 3.5))
assert [(c.t0, c.t1) for c in kw.cues] == [(0.5, 1.0), (1.0, 1.9), (1.9, 2.5)], kw.cues
# pop: 3 words at a time, 1 word at a time with max_words 1, pages last from first word to last word of the group
PW = tuple((1.0 + .5 * i, 1.0 + .5 * i + .4, w) for i, w in enumerate("a b c d".split()))
pcue = [Cue(1.0, 3.5, "a b c d", PW)]
p3 = captions.build(t4, {"style": "pop"}, (W, H), pcue, tmp / "p3", None)
assert [(c.t0, c.t1, c.text) for c in p3.cues] == [(1.0, 2.4, "a b c"), (2.5, 2.9, "d")], p3.cues
p1 = captions.build(t4, {"style": "pop", "max_words": 1}, (W, H), pcue, tmp / "p1", None)
assert [(c.t0, c.t1) for c in p1.cues] == [(1.0, 1.4), (1.5, 1.9), (2.0, 2.4), (2.5, 2.9)], p1.cues
assert sum(durs_of(p1)) == 120 and sum(durs_of(p3)) == 120
ph = captions.build(t4, {"style": "pop", "highlight": True}, (W, H), pcue, tmp / "ph", None)
assert [(c.t0, c.t1) for c in ph.cues][:3] == [(1.0, 1.5), (1.5, 2.0), (2.0, 2.4)] and len(ph.cues) == 4, ph.cues
# uppercase reaches the drawing
up = captions.build(t4, {"style": "pop", "uppercase": True}, (W, H), pcue, tmp / "pu", None)
assert up.cues[0].text == "A B C"

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
kind = os.environ.get("FAKE_KIND", "words")
def tk(t, f, e, p=0.95):
    return {{"text": t, "offsets": {{"from": f, "to": e}}, "p": p}}
def seg(text, f, e, words=None):
    toks = [tk("[_BEG_]", f, f)] + [tk(" " + w, a, b, 0.95) for w, a, b in (words or [])] + [tk("[_TT_9]", e, e)]
    return {{"text": " " + text, "offsets": {{"from": f, "to": e}}, "tokens": toks}}
lang = os.environ.get("FAKE_LANG", "en")
if kind == "words":  # real-looking speech with word timing; a music tag and a one-word line
    segs = [seg("Hello there", 500, 2500, [("Hello", 500, 1100), ("there", 1300, 2400)]), seg("[MUSIC]", 2500, 3000),
            seg("Bye", 3000, 3500, [("Bye", 3000, 3400)])]
elif kind == "hallu":  # invented text on noise: every window exactly 2.0 s
    segs = [seg("Thank you for watching", 0, 2000, [("Thank", 0, 500)]), seg("Please subscribe", 2000, 4000, [("Please", 2000, 2500)])]
else:  # "speech": uneven windows, confident tokens
    segs = [seg("We went to the lake", 300, 1900, [("We", 300, 500), ("went", 500, 800), ("to", 800, 900), ("the", 900, 1000), ("lake", 1000, 1800)]),
            seg("and it was cold", 2100, 3700, [("and", 2100, 2300), ("it", 2300, 2500), ("was", 2500, 2800), ("cold", 2800, 3600)])]
json.dump({{"result": {{"language": lang}}, "transcription": segs}}, open(a[a.index("-of") + 1] + ".json", "w"))
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
assert captions.setup_status()["hint"].startswith("mkdir -p ~/Kioku/models")
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

with core.connect() as c:
    c.execute("delete from transcripts"); c.execute("delete from transcript_lines"); c.commit()
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
assert a[:2] == ["-m", str(captions.model_path())] and a[a.index("-l") + 1] == "en" and "-ojf" in a and "-sns" in a
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

# ---------- routes + job (edit stubbed): only picked clips are transcribed
fake_spec = {"captions": {**edit.CAPTIONS_DEFAULT, "on": True, "style": "box"}, "clips": {}}
sys.modules["edit"] = types.SimpleNamespace(load=lambda cid: (fake_spec, 1))
app = FastAPI()
app.include_router(library.router)
app.include_router(captions.router)
c = TestClient(app)
cid = c.post("/collections", json={"name": "P", "kind": "project"}).json()["id"]
for u in ("LOUD", "QUIET", "PIC"):
    c.post(f"/collections/{cid}/items", json={"uuids": [u]})
iid = {i["uuid"]: i["item_id"] for i in library.get_collection(cid)["items"]}
with core.connect() as cx:
    cx.execute("delete from transcripts"); cx.execute("delete from transcript_lines"); cx.commit()
ARGV.unlink()


def wait_job():
    for _ in range(100):
        j = c.get(f"/collections/{cid}/captions/job").json()
        if j["status"] != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


st = c.get(f"/collections/{cid}/captions").json()
assert st["on"] and st["style"] == "box" and st["config"]["position"] == "bottom" and st["language"] == "en", st
assert (st["pending"], st["ready"], st["no_speech"]) == (0, 0, 0), "nothing picked, nothing to do"
assert [(x["uuid"], x["selected"], x["speech"], x["lang"]) for x in st["clips"]] == [("LOUD", False, "not transcribed", None), ("QUIET", False, "not transcribed", None)], st["clips"]
assert c.post(f"/collections/{cid}/captions/transcribe").status_code == 202
j = wait_job()
assert j["status"] == "done" and "0 of 0" in j["message"] and not ARGV.exists()
fake_spec["clips"][str(iid["LOUD"])] = {"captions": True}
st = c.get(f"/collections/{cid}/captions").json()
assert (st["pending"], st["ready"]) == (1, 0) and st["clips"][0]["selected"] and not st["clips"][1]["selected"], st
assert c.post(f"/collections/{cid}/captions/transcribe").status_code == 202
j = wait_job()
assert j["status"] == "done" and j["progress"] == 1.0 and "1 of 1" in j["message"] and not j["warnings"], j
assert captions.cached("QUIET") is None, "an unpicked clip is never transcribed"
fake_spec["clips"][str(iid["QUIET"])] = {"captions": True}
c.post(f"/collections/{cid}/captions/transcribe"); wait_job()
st = c.get(f"/collections/{cid}/captions").json()
assert (st["ready"], st["pending"], st["no_speech"]) == (1, 0, 1), st
assert [(x["speech"], x["lang"]) for x in st["clips"]] == [("speech", "en"), ("no speech", None)], st["clips"]
captions.start_transcription(cid)  # nothing pending: finishes at once
time.sleep(0.5)
assert c.get(f"/collections/{cid}/captions/job").json()["status"] == "done"
tr = c.get("/media/LOUD/transcript").json()
assert tr["status"] == "ok" and tr["lang"] == "en" and tr["requested_lang"] == "en" and len(tr["lines"]) == 2 and c.get("/media/NOPE/transcript").status_code == 404
assert tr["lines"][0]["words"] == [{"t0": 0.5, "t1": 1.1, "text": "Hello"}, {"t0": 1.3, "t1": 2.4, "text": "there"}], tr["lines"][0]

# ---------- whisper kinds, language, hallucination, lazy word times
def forget(*uu):
    with core.connect() as cx:
        for u in uu:
            cx.execute("delete from transcripts where uuid = ?", (u,)); cx.execute("delete from transcript_lines where uuid = ?", (u,))
        cx.commit()


def calls():
    return [__import__("json").loads(x) for x in ARGV.read_text().splitlines()] if ARGV.exists() else []


forget("LOUD")
os.environ["FAKE_KIND"] = "hallu"
got = captions.transcribe("LOUD")
assert got == ("silent", []) and captions.speech_status("LOUD") == "no speech", got
assert c.get("/media/LOUD/transcript").json()["lines"] == [] and c.get("/media/LOUD/transcript").json()["lang"] == "en"
os.environ["FAKE_KIND"] = "speech"
forget("LOUD")
got = captions.transcribe("LOUD")
assert got[0] == "ok" and [l.text for l in got[1]] == ["We went to the lake", "and it was cold"], got
assert got[1][0].words == ((0.3, 0.5, "We"), (0.5, 0.8, "went"), (0.8, 0.9, "to"), (0.9, 1.0, "the"), (1.0, 1.8, "lake"))
os.environ["FAKE_LANG"] = "zh"  # whisper reports another language than the setting: invented
forget("LOUD")
assert captions.transcribe("LOUD") == ("silent", [])
del os.environ["FAKE_LANG"]

# lazy: word times are only fetched when asked for; old lines without them are redone once
forget("LOUD")
captions.store("LOUD", "ok", "en", [Line(0.5, 2.5, "Hello there")], "en")
ARGV.unlink()
os.environ["FAKE_KIND"] = "words"
assert captions.ensure_transcripts(["LOUD"])["LOUD"][0].words == () and calls() == [], "no word timing asked: cache is used"
res = captions.ensure_transcripts(["LOUD"], words=True)
assert res["LOUD"][0].words and len(calls()) == 1
captions.ensure_transcripts(["LOUD"], words=True)
assert len(calls()) == 1, "word times are cached too"

# language setting: -l follows it, a different language redoes the clip, auto gets a longer timeout
seen = []
real_run = subprocess.run
subprocess.run = lambda *a_, **k: (seen.append(k.get("timeout")), real_run(*a_, **k))[1]
try:
    captions.ensure_transcripts(["LOUD"], words=True)
    assert len(calls()) == 1
    settings._cur = settings.Settings(caption_language="auto")
    assert captions.fresh("LOUD", "auto") is None, "transcribed for en"
    ARGV.unlink()
    captions.ensure_transcripts(["LOUD"])
    assert calls()[0][calls()[0].index("-l") + 1] == "auto"
    auto_timeout = [t for t in seen if t][-1]
    settings._cur = settings.Settings(caption_language="en")
    forget("LOUD")
    captions.ensure_transcripts(["LOUD"])
    en_timeout = [t for t in seen if t][-1]
finally:
    subprocess.run = real_run
    settings._cur = None
assert en_timeout >= 300 and auto_timeout >= 2 * en_timeout and auto_timeout >= 600, (en_timeout, auto_timeout)
for bad in ("xx", ""):
    try:
        settings.Settings(caption_language=bad)
        raise AssertionError("expected a validation error")
    except ValueError:
        pass

# the rendering side: unpicked clips get no cues, a picked clip's words drive the karaoke states
captions.whisper_bin = lambda: str(fake)
trs = captions.ensure_transcripts(["LOUD"], words=True)
two = tl_of(seg("LOUD", 0, 4, 1.0, 0, 120, item_id=7), seg("LOUD", 0, 4, 1.0, 120, 120, item_id=8))
cues_ = captions.map_cues(two, trs, {8})
assert cues_ and all(cc.t0 >= 4.0 for cc in cues_), cues_
kk = captions.build(two, {"style": "bold", "highlight": True}, (W, H), cues_, tmp / "e2e", None)
assert [round(x.t0 * 30) for x in kk.cues][:2] == [120 + 15, 120 + 39], kk.cues  # Hello at 0.5 s, there at 1.3 s into clip 2
assert captions.build(two, {"style": "bold"}, (W, H), captions.map_cues(two, trs, set()), tmp / "e3", None).list_file is None

captions.whisper_bin = lambda: None
assert c.post(f"/collections/{cid}/captions/transcribe").status_code == 422

print("test_captions: ok")
