"""Style reference checks (notes/pro-editing-design.md phase 4) on a synthetic 120 BPM video with known cuts, one crossfade,
one zoom-in, one punch-in, one white flash and one shot of text.
uv run test_reference.py   (ffmpeg, TransNetV2 and beat_this on CPU; about 30 s)"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

T = Path(tempfile.mkdtemp())
os.environ["CS_DB"] = str(T / "t.db")
os.environ["KIOKU_DATA"] = str(T / "data")
os.environ["KIOKU_DB_KEY"] = "0" * 64

import core  # noqa: E402

testenv.check()
assert str(core.DATA).startswith(str(T)), core.DATA
import edit  # noqa: E402
import reference  # noqa: E402

S = "640x360"


def ff(*a):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *a], check=True)


def make_reference(out):
    """11.5 s. Hard cuts at 2, 4, 6 and 9.5 (all beats at 120 BPM), crossfade 7.5 to 8.0, zoom-in 4 to 6, punch-in at 3,
    white flash of 3 frames at 8.5, text from 9.5 to 11.5."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGBA", (640, 360), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((60, 230), "HELLO WORLD", font=ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 70), fill=(255, 255, 255, 255))
    im.save(T / "text.png")
    import numpy as np
    for n in range(1, 6):  # a static texture per shot: the only camera motion is what the filters add
        Image.fromarray(np.random.default_rng(n).integers(0, 256, (18, 32, 3), dtype=np.uint8)).resize((640, 360), Image.BICUBIC).save(T / f"tex{n}.png")

    def shot(n, vf, name):
        ff("-loop", "1", "-framerate", "30", "-i", str(T / f"tex{n}.png"), "-t", "2", "-vf", vf, "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "12", str(T / name))
    shot(1, "null", "s1.mp4")
    shot(2, "scale=w='640*if(gte(t,1),1.3,1)':h='360*if(gte(t,1),1.3,1)':eval=frame,crop=640:360", "s2.mp4")
    shot(3, "scale=w='640*(1+0.15*t)':h='360*(1+0.15*t)':eval=frame,crop=640:360", "s3.mp4")
    shot(4, "null", "s4.mp4")
    shot(5, "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,1.0,1.1)'", "s5.mp4")
    ff("-f", "lavfi", "-i", f"color=c=0x203040:s={S}:r=30:d=2", "-i", str(T / "text.png"), "-filter_complex", "overlay", "-pix_fmt", "yuv420p",
       "-c:v", "libx264", "-crf", "12", str(T / "s6.mp4"))
    ff("-f", "lavfi", "-i", "aevalsrc='0.9*sin(2*PI*55*t)*exp(-30*mod(t,0.5))+0.1*sin(2*PI*220*t)':s=44100:d=12", str(T / "song.wav"))
    ins = [x for n in ("s1", "s2", "s3", "s4", "s5", "s6") for x in ("-i", str(T / f"{n}.mp4"))] + ["-i", str(T / "song.wav")]
    ff(*ins, "-filter_complex",
       "[0][1][2][3]concat=n=4[x];[4][5]concat=n=2[y];[x][y]xfade=transition=fade:duration=0.5:offset=7.5[v]", "-map", "[v]", "-map", "6:a",
       "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "12", "-c:a", "aac", "-shortest", str(out))


def near(xs, t, tol=0.12):
    return any(abs(x - t) <= tol for x in xs)


# ---------- input validation
for bad in ("", "https://example.com/a.mp4", str(T / "nope.mp4"), str(T), str(T / "text.png")):
    try:
        reference.validate(bad)
        raise AssertionError(bad)
    except ValueError:
        pass
(T / "fake.mp4").write_text("not a video")
try:
    reference.validate(str(T / "fake.mp4"))
    raise AssertionError("fake")
except ValueError:
    pass

# ---------- analysis
REF = T / "ref.mp4"
make_reference(REF)
t0 = time.time()
p = reference.analyze_reference(str(REF))
print(f"analysis of {p['duration']}s took {time.time() - t0:.1f}s")
assert p["detector"] == "transnetv2", p["detector"]
assert p["aspect"] == "16:9" and abs(p["fps"] - 30) < 0.1, (p["aspect"], p["fps"])
starts = [s["start"] for s in p["shots"]][1:]
tr = p["transitions"]
assert tr["cut"] == 4 and tr["gradual"] == 1, (tr, [(x["start"], x["length"], x["camera"]) for x in p["shots"]], p["motion"])
for t in (2.0, 4.0, 6.0, 9.5):
    assert near(starts, t), (t, starts)
assert near(starts, 7.75, 0.3) and 0.3 <= tr["gradual_median_s"] <= 0.9, (starts, tr)
assert len(p["shots"]) == 6, [(s["start"], s["length"]) for s in p["shots"]]
assert abs(p["music"]["tempo"] - 120) < 3, p["music"]
assert p["music"]["cut_on_beat"] >= 0.75, p["music"]               # 4 hard cuts on beats
assert p["pacing"]["beats"]["p50"] >= 3.0, p["pacing"]               # about 4 beats per shot
cams = {round(s["start"]): s["camera"] for s in p["shots"]}
assert cams[4] == "push", cams                                        # the zoom-in shot
assert cams[0] == "static", cams
assert near(p["motion"]["punches"], 3.0, 0.2), p["motion"]            # the scale jump inside shot 2
assert not any(4.2 < x < 5.9 for x in p["motion"]["punches"]), p["motion"]  # a slow zoom is not a punch
assert len(p["motion"]["flashes"]) == 1 and near(p["motion"]["flashes"], 8.5, 0.1), p["motion"]
assert p["motion"]["flashes"] and len(p["shots"]) == 6              # the flash did not split a shot
tx = p["text"]
assert tx and 0.1 < tx["share_time"] < 0.4 and tx["band"] == "bottom" and tx["words_per_text"] == 2, tx
assert tx["first_s"] is not None and 9 <= tx["first_s"] <= 10, tx
assert p["color"]["saturation"] > 0 and len(p["color"]["lab_mean"]) == 3
assert Path(p["sheet"]).is_file() and Path(p["cube"]).is_file()
cube = Path(p["cube"]).read_text().splitlines()
assert cube[1] == "LUT_3D_SIZE 33" and len(cube) == 2 + 33 ** 3, len(cube)
assert all(0 <= float(v) <= 1 for ln in cube[2:50] for v in ln.split())
# identity-ish at strength 0, capped at 0.6 above that
reference.bake_cube(p["color"], T / "a.cube", strength=0)
reference.bake_cube(p["color"], T / "b.cube", strength=5)
reference.bake_cube(p["color"], T / "c.cube", strength=0.6)
assert (T / "b.cube").read_text() == (T / "c.cube").read_text() != (T / "a.cube").read_text()
# cached: second call reads the JSON
t0 = time.time()
assert reference.analyze_reference(str(REF)) == p and time.time() - t0 < 2

# ---------- fallback detector finds the hard cuts too and says so
f = reference.analyze_reference(str(REF), sheet=False, cube=False, detector="scenedetect")
fs = [s["start"] for s in f["shots"]][1:]
assert f["detector"] == "scenedetect" and sum(near(fs, t) for t in (2.0, 4.0, 6.0, 9.5)) >= 3, fs
assert f["transitions"]["cut"] >= 3, f["transitions"]

# ---------- plan: only values edit.py knows
def check_plan(prof, n, grid_len=0):
    pl = reference.plan_from_profile(prof, n, grid_len)
    assert pl["sync"]["grid"] in ("beat", "half", "downbeat")
    assert len(pl["beats_pattern"]) == n and all(1 <= b <= edit.MAX_BEATS for b in pl["beats_pattern"]), pl["beats_pattern"]
    b = pl["budgets"]
    assert set(b["motion_types"]) <= set(edit.MOTIONS) - {"none"} and b["shake"] <= n // 2 and b["motion_clips"] <= n
    assert abs(sum(t["share"] for t in pl["transitions"]) - 1) < 0.05, pl["transitions"]
    for t in pl["transitions"]:
        assert t["type"] == "cut" or t["type"] in edit.XFADE, t
        assert t["sfx"] == "none" or t["sfx"] in edit.SFX, t
        edit.TransitionPut(type=t["type"], duration=t["duration"] or 0.5, sfx=t["sfx"])  # the real validator accepts it
    if pl["captions"]:
        c = pl["captions"]
        assert c["position"] in edit.CAPTION_POSITIONS and c["size"] in edit.CAPTION_SIZES and c["style"] in edit.CAPTION_STYLES
    assert pl["look"]["name"] in (None, *edit.LOOKS) and pl["look"]["strength"] <= 0.6
    assert pl["aspect"] in edit.ASPECTS
    reference.summarize(prof, pl)
    return pl


pl = check_plan(p, 12)
assert pl["sync"]["grid"] == "half" and pl["captions"] and pl["captions"]["position"] == "bottom", pl
assert {t["type"] for t in pl["transitions"]} == {"cut", "dissolve"}, pl["transitions"]
check_plan(p, 1)
assert not check_plan(p, 40, grid_len=30)["fits_music"]       # 40 clips cannot fit 30 beats: scaled to the minimum
assert check_plan(p, 6, grid_len=200)["fits_music"]
import copy  # noqa: E402
fast = copy.deepcopy(p)
for s in fast["shots"]:
    s["beats"] = 0.5
fast["pacing"]["beats"] = {"p10": 0.5, "p50": 0.5, "p90": 0.5}
fast["pacing"]["seconds"] = {"p10": 0.25, "p50": 0.25, "p90": 0.25}
fast["transitions"].update(gradual=3, gradual_share=0.5, gradual_median_s=0.1)
pf = check_plan(fast, 20)
assert pf["sync"]["grid"] == "beat" and {t["type"] for t in pf["transitions"]} == {"cut", "whip-left", "whip-right"}, pf
assert all(t["duration"] >= edit.MIN_TRANS_FRAMES / 30 for t in pf["transitions"] if t["type"] != "cut")
slow = copy.deepcopy(p)
for s in slow["shots"]:
    s["beats"] = 16.0
slow["transitions"].update(gradual_share=1.0, gradual_median_s=2.5)
ps = check_plan(slow, 5)
assert ps["sync"]["grid"] == "downbeat" and ps["transitions"][-1]["type"] == "fade-black" and ps["transitions"][-1]["duration"] <= 1.5, ps
nomusic = copy.deepcopy(p)
for s in nomusic["shots"]:
    s["beats"] = None
nomusic["music"]["cut_on_beat"] = None
check_plan(nomusic, 8)

# ---------- gaps: identical profile has none, a different one lists the biggest first
assert reference.gaps(p, p) == []
g = reference.gaps(p, fast)
assert g and g[0].startswith("Pacing"), g
assert len(g) <= 4 and all(len(x) < 220 for x in g), g

# ---------- routes
from fastapi.testclient import TestClient  # noqa: E402
from fastapi import FastAPI  # noqa: E402

app = FastAPI()
app.include_router(reference.router)
c = TestClient(app)
assert c.post("/references/analyze", json={"path": "https://x.y/z.mp4"}).status_code == 422
assert c.post("/references/analyze", json={"path": str(T / "text.png")}).status_code == 422
r = c.post("/references/analyze", json={"path": str(REF)})
assert r.status_code == 200 and r.json()["id"] == p["id"] and "PLAN" in r.json()["summary"], r.text[:300]
assert c.get(f"/references/{p['id']}").json()["detector"] == "transnetv2"
assert c.get("/references/../../etc/passwd").status_code in (404, 405)
assert c.get("/references/xyz").status_code == 404
im = c.get(r.json()["sheet"])
assert im.status_code == 200 and im.headers["content-type"] == "image/jpeg"
print("reference checks passed")
