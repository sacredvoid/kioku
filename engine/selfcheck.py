"""End-to-end check: download 5 public samples, index them into a throwaway db, assert each query ranks its match first.

  uv run selfcheck.py
"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/google-gemma/cookbook/main/apps/sample-data/"
SAMPLES = {
    "goldengate.png": (BASE + "GoldenGate.png", "golden gate bridge"),
    "kitchen.jpg": (BASE + "kitchen_painting.jpg", "painting of a kitchen"),
    "namsan.png": (BASE + "namsan.png", "tower on a hill in a city"),
    "surprise.png": (BASE + "surprise.png", None),
    "cat.mp4": ("https://github.com/bebechien/gemma/raw/refs/heads/main/videos/cat_dozing.mp4", "a sleeping cat"),
}

tmp = Path(tempfile.mkdtemp(prefix="cs-selfcheck-"))
media = tmp / "media"
media.mkdir()
for name, (url, _) in SAMPLES.items():
    urllib.request.urlretrieve(url, media / name)

os.environ["CS_DB"] = str(tmp / "test.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain
here = Path(__file__).parent
subprocess.run([sys.executable, here / "index.py", "--folder", media], check=True)

import core  # noqa: E402  (after CS_DB is set)

testenv.check()

idx = core.Index(core.connect())
for name, (_, query) in SAMPLES.items():
    if query:
        top = Path(idx.search(query, 1)[0]["path"]).name
        assert top == name, f"{query!r}: expected {name}, got {top}"
        print(f"ok  {query!r} -> {name}")
print("selfcheck passed")
