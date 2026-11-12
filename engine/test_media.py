"""Scene detection pipeline check on a synthetic 3-shot video.  uv run test_media.py"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="cs-media-"))
os.environ["CS_DB"] = str(tmp / "t.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain

import core  # noqa: E402

testenv.check()

core.DATA = tmp  # keep proxies/sheets out of the real data folder
import media  # noqa: E402

media.PROXIES, media.SHEETS, core.AUDIO_DIR = tmp / "proxies", tmp / "sheets", tmp / "audio"

cat = tmp / "cat.mp4"
urllib.request.urlretrieve("https://github.com/bebechien/gemma/raw/refs/heads/main/videos/cat_dozing.mp4", cat)
clip = tmp / "three_shots.mp4"
# shot 1: test pattern 3s, shot 2: 6s of the cat video, shot 3: colour bars 3s (hard cuts at 3s and 9s)
subprocess.run(["ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=3",
                "-ss", "2", "-t", "6", "-i", str(cat),
                "-f", "lavfi", "-i", "smptebars=size=1280x720:rate=30:duration=3",
                "-filter_complex", "[1:v]scale=1280:720,setsar=1,fps=30[c];[0:v][c][2:v]concat=n=3:v=1:a=0[v]",
                "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)

db = core.connect()
db.execute("insert into assets(uuid, kind, path, duration, meta) values ('V', 'video', ?, 12, 'test')", (str(clip),))
db.commit()

r = media.analyze("V", embed=True)
cuts = [s["t0"] for s in r["scenes"]][1:]
print("scenes:", [(s["t0"], s["t1"], s["sharpness"], s["motion"]) for s in r["scenes"]])
assert any(abs(c - 3) < 0.5 for c in cuts), f"missed cut at 3s: {cuts}"
assert any(abs(c - 9) < 0.5 for c in cuts), f"missed cut at 9s: {cuts}"
assert (media.PROXIES / "V.mp4").is_file() and (media.SHEETS / "V.jpg").is_file()
assert media.probe_duration(media.PROXIES / "V.mp4") > 11
n = core.connect().execute("select count(*) from segments where uuid='V' and source='scene'").fetchone()[0]
assert n == len(r["scenes"]), (n, len(r["scenes"]))
assert media.analyze("V")["scenes"] == r["scenes"]  # second call is cached
print("media checks passed; contact sheet:", media.SHEETS / "V.jpg")
