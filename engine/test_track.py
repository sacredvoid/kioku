"""find_track route with a fake yt-dlp: no network.  uv run test_track.py  (needs ffmpeg)"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import os
import sys
import tempfile
import types
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["KIOKU_DATA"] = str(tmp / "data")
os.environ["CS_DB"] = str(tmp / "t.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.modules["captions"] = types.SimpleNamespace(start_transcription=lambda cid: None, likely_speech=lambda u: False)
import reference  # noqa: E402
import sounds  # noqa: E402

# fake yt-dlp: search prints a flat playlist; a download writes a real 2 s m4a where -o points
fake = tmp / "yt-dlp"
fake.write_text(f"""#!{sys.executable}
import json, subprocess, sys
a = sys.argv[1:]
if "-J" in a:
    w = "https://www.youtube.com/watch?v="
    print(json.dumps({{"entries": [
        {{"id": "UC1", "url": "https://www.youtube.com/channel/UC1", "title": "Ben", "duration": None}},
        {{"id": "set", "url": w + "set", "title": "Live set", "channel": "X", "duration": 5000}},
        {{"id": "a1", "url": w + "a1", "title": "Beyond Beliefs", "channel": "Ben Böhmer - Topic", "duration": 330}}]}}))
elif "bad" in a[-1]:
    sys.exit(0)  # match-filter rejected it: exit 0, no file
else:
    out = a[a.index("-o") + 1].replace("%(ext)s", "m4a")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=2", "-c:a", "aac", out], check=True)
    print("Beyond Beliefs\\tBen Böhmer - Topic\\t" + a[-1])
""")
fake.chmod(0o755)
reference.ytdlp_bin = lambda: str(fake)

app = FastAPI()
app.include_router(sounds.router)
c = TestClient(app)

r = c.post("/sounds/track", json={"q": "ben bohmer"}).json()
assert [x["url"][-2:] for x in r["candidates"]] == ["a1"], r  # channel and 83 min set filtered out
assert r["candidates"][0]["official_audio"] is True

link = "https://www.youtube.com/watch?v=a1"
r = c.post("/sounds/track", json={"q": link}).json()
assert Path(r["path"]).is_file() and r["title"] == "Beyond Beliefs" and 1.5 < r["seconds"] < 2.5, r
assert r == c.post("/sounds/track", json={"q": link}).json()  # cached
assert not list(sounds.YT.glob("*.part-dl.*"))

r = c.post("/sounds/track", json={"q": "https://www.youtube.com/watch?v=bad"})
assert r.status_code == 422 and not list(sounds.YT.glob("*.part-dl.*")), r.text
assert c.post("/sounds/track", json={"q": ""}).status_code == 422

reference.ytdlp_bin = lambda: None
assert "brew install yt-dlp" in c.post("/sounds/track", json={"q": "x"}).json()["detail"]
print("ok")
