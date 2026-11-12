"""Curated CC0 seam sounds, text sounds and the audio declick at hard cuts. No network.  uv run test_seam_sounds.py  (needs ffmpeg)"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["KIOKU_DATA"] = str(tmp / "data")
os.environ["CS_DB"] = str(tmp / "t.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64

import numpy as np  # noqa: E402
from scipy.io import wavfile  # noqa: E402

import edit  # noqa: E402
import render  # noqa: E402
from timeline import Timeline  # noqa: E402

# ---------- the bundled files and their manifest: every selectable sound exists, is small, and is CC0 with a source
man = json.loads((Path(render.SFX_SRC) / "MANIFEST.json").read_text())
assert set(man) == set(edit.SFX) == set(render.SFX_META), (set(man), edit.SFX)
for name, m in man.items():
    f = render.sfx_file(name)
    assert f.is_file() and f.stat().st_size < 60_000, (name, f)
    d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)],
                             capture_output=True, text=True, check=True).stdout)
    assert abs(d - m["dur"]) < 0.1, (name, d, m["dur"])
    assert m["license"] == "CC0-1.0" and m["license_url"] == "https://creativecommons.org/publicdomain/zero/1.0/", name
    assert m["source_url"].startswith("https://freesound.org/people/") and m["author"] and m["title"], name
    assert 0 <= m["before"] <= m["dur"] and 0 < m["volume"] <= 1, name

# ---------- auto mapping only names real sounds and real transitions
assert set(edit.AUTO_SFX.values()) <= set(edit.SFX) and set(edit.AUTO_SFX) <= set(edit.XFADE), edit.AUTO_SFX
assert edit.AUTO_SFX["whip-left"] == "whoosh" and "dissolve" not in edit.AUTO_SFX

# ---------- seam and title sounds land on their frame, earlier by the file's lead-in
tl = Timeline(30, "9:16", 360, (), 300, (), "x", sfx=((90, "whoosh"), (150, "hit")),
              texts=({"start_frame": 60, "sfx": "pop"}, {"start_frame": 70}, {"start_frame": 80, "sfx": "nope"}))
got = {t["id"]: t for t in render.sfx_tracks(tl)}
assert set(got) == {"sfx90whoosh", "sfx150hit", "sfx60pop"}, set(got)  # a text without a sound, or an unknown one, adds none
assert abs(got["sfx90whoosh"]["start"] - (3.0 - man["whoosh"]["before"])) < 1e-9
assert abs(got["sfx60pop"]["start"] - (2.0 - man["pop"]["before"])) < 1e-9 and got["sfx60pop"]["volume"] == man["pop"]["volume"]
early = {t["id"]: t for t in render.sfx_tracks(Timeline(30, "9:16", 360, (), 300, (), "x", sfx=((3, "riser"),)))}["sfx3riser"]
assert early["start"] == 0 and abs(early["in"] - (man["riser"]["before"] - 0.1)) < 1e-9  # a riser at the very start loses its lead-in

# ---------- declick: a hard cut between two waveforms that disagree must not jump
W = tmp / "w"
W.mkdir()
for n, v in (("a", 0.8), ("b", -0.8)):  # constant levels: the worst case, a full-scale step at the join
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=30:d=1",
                    "-f", "lavfi", "-i", f"aevalsrc={v}:s=48000:d=1:c=stereo", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "pcm_s16le", str(W / f"{n}.mov")], check=True)


def worst_step(declick):
    render.DECLICK = declick
    g = render._graph(2, [30, 30], [None], 30)
    out = W / f"o{declick}.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(W / "a.mov"), "-i", str(W / "b.mov"), "-filter_complex", g,
                    "-map", "[aout]", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(out),
                    "-map", "[vout]", "-an", "-f", "null", "-"], check=True)
    x = wavfile.read(out)[1].astype(float) / 32768
    assert abs(len(x) - 96000) < 2000, len(x)  # still one second per clip: no audio was dropped or added
    return np.abs(np.diff(x[47000:49000])).max()


SAVED = render.DECLICK
step_off = worst_step(0)
step_on = worst_step(SAVED)
render.DECLICK = SAVED
assert SAVED > 0 and step_off > 1.0, (SAVED, step_off)  # the test clips really do click without it
assert step_on < 0.01, step_on   # with it the largest sample-to-sample jump is a tenth of a percent of full scale
print("seam sound checks passed")
