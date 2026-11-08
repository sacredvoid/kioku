"""Settings round-trip, validation, the Claude agent queue, and the quiet access-log filter.  uv run test_settings.py"""
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")  # never touch the real data/index.db

from fastapi.testclient import TestClient  # noqa: E402

import projects  # noqa: E402
import server  # noqa: E402
import settings  # noqa: E402

settings.FILE = Path(tempfile.mkdtemp()) / "settings.json"  # never touch the real data/settings.json
settings._cur = None
API = "http://127.0.0.1:8765"
c = TestClient(server.app, base_url=API)  # no `with`: skips lifespan, so no model load

assert c.get("/settings").json()["values"]["max_agents"] == 2
assert c.put("/settings", json={"max_agents": 0, "ask_minutes": 30, "download_seconds": 180, "proxy_height": 540}).status_code == 422
assert c.put("/settings", json={"max_agents": 2, "ask_minutes": 30, "download_seconds": 180, "proxy_height": 480}).status_code == 422
r = c.put("/settings", json={"max_agents": 1, "ask_minutes": 10, "download_seconds": 60, "proxy_height": 360})
assert r.status_code == 200 and settings.FILE.is_file()
settings._cur = None  # reload from disk
assert settings.get().max_agents == 1 and settings.get().proxy_height == 360
settings.FILE.write_text("{not json")
settings._cur = None
assert settings.get().max_agents == 2, "corrupt file falls back to defaults"

# queue: with max_agents=1 the second edit waits until the first releases its slot
settings._cur = settings.Settings(max_agents=1)
a, b = {}, {}
projects.agent_slot(a)
t = threading.Thread(target=projects.agent_slot, args=(b,))
t.start()
time.sleep(0.3)
assert t.is_alive() and "Waiting for a free Claude slot" in b["note"]
projects.release_slot()
t.join(3)
assert not t.is_alive() and "note" not in b and projects.RUNNING_AGENTS == 1
projects.release_slot()
assert projects.RUNNING_AGENTS == 0

# videos share the same cap: with max_agents=1 a second analyze waits ("waiting for a free slot")
import media
settings._cur = settings.Settings(max_agents=1)
media.video_slot("A")
t = threading.Thread(target=media.video_slot, args=("B",))
t.start()
time.sleep(0.3)
assert t.is_alive() and media.ACTIVE["B"]["stage"] == "waiting for a free slot"
media.release_video_slot()
t.join(3)
assert not t.is_alive() and media.RUNNING == 1
media.release_video_slot(); media.ACTIVE.clear()
assert media.RUNNING == 0

# job status is a snapshot, so serializing it can't race the job thread mutating the live dict
projects.jobs[(9, "ask")] = {"status": "running", "steps": ["a"]}
snap = projects.job(9, "ask")
snap["steps"].append("b"); snap["note"] = "x"
assert projects.jobs[(9, "ask")] == {"status": "running", "steps": ["a"]}
assert projects.job(8, "ask") == {"status": "idle"}

# access log: successful polls hidden, failures and real requests kept
f = server.QuietPolls()
rec = lambda m, p, s: logging.LogRecord("uvicorn.access", 20, "", 0, '%s - "%s %s HTTP/%s" %d', ("1", m, p, "1.1", s), None)
assert not f.filter(rec("GET", "/collections/2/ask", 200))
assert not f.filter(rec("GET", "/thumb/ABC", 200))
assert f.filter(rec("GET", "/media/activity", 404))
assert f.filter(rec("POST", "/collections/2/ask", 202))
assert f.filter(rec("GET", "/search?q=dog", 200))

# unexpected exceptions come back as readable JSON, not a bare 500
@server.app.get("/__boom")
def boom():
    raise ValueError("kaboom")
r = TestClient(server.app, base_url=API, raise_server_exceptions=False).get("/__boom")
assert r.status_code == 500 and "kaboom" in r.json()["detail"], r.text

# only the app's own origins and localhost Host names get through (CSRF from web pages, DNS rebinding)
assert c.get("/settings", headers={"Origin": "https://evil.example"}).status_code == 403
assert c.post("/reindex", headers={"Origin": "null"}).status_code == 403
assert c.get("/settings", headers={"Origin": "tauri://localhost"}).status_code == 200
assert c.get("/settings", headers={"Origin": "http://localhost:1420"}).headers["access-control-allow-origin"] == "http://localhost:1420"
assert TestClient(server.app, base_url="http://rebound.example:8765").get("/settings").status_code == 400
assert TestClient(server.app, base_url="http://localhost:8765").get("/settings").status_code == 200
# export keeps the exact 30000/1001 rate: FCP XML says timebase 30 + ntsc TRUE (Resolve reads 29.97); true 30 stays FALSE
import opentimelineio as otio  # noqa: E402
import core  # noqa: E402
import sqlite3  # noqa: E402
ntsc = 30000 / 1001
for rate, flag in [(ntsc, "TRUE"), (30, "FALSE")]:
    xml = otio.adapters.write_to_string(projects.build_timeline("t", [(Path("/x/a.mov"), True, rate, 0, 2, 5)], rate), "fcp_xml")
    assert f"<timebase>30</timebase>" in xml and f"<ntsc>{flag}</ntsc>" in xml and (flag == "TRUE") == ("<ntsc>FALSE" not in xml), flag
vid = Path(tempfile.mkdtemp()) / "n.mp4"
import subprocess  # noqa: E402
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:r=30000/1001:d=1", str(vid)], check=True)
assert projects.probe(vid)[0] == ntsc, projects.probe(vid)
# variable frame rate (phone-style timestamps): nominal 30 fps even though the average is lower
vfr = vid.with_name("vfr.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:r=30:d=2", "-vf",
                r"select=not(eq(mod(n\,3)\,1))", "-fps_mode", "vfr", str(vfr)], check=True)
assert projects.probe(vfr)[0] == 30, projects.probe(vfr)

# core.connect: WAL is switched on once per process; a busy db must not break connect()
core.DB_PATH = Path(tempfile.mkdtemp()) / "w.db"
core._wal.clear()
assert core.connect(core.DB_PATH).execute("pragma journal_mode").fetchone()[0] == "wal" and core.DB_PATH in core._wal
busy = Path(tempfile.mkdtemp()) / "b.db"
holder = sqlite3.connect(busy)
holder.execute("create table t(x)"); holder.commit()
holder.execute("begin exclusive")
orig = sqlite3.connect
sqlite3.connect = lambda p, timeout=30, **kw: orig(p, timeout=0.1, **kw)
try:
    core.connect(busy)  # locked: logs and carries on... schema script also needs the lock, so only the pragma is checked
except sqlite3.OperationalError:
    pass
finally:
    sqlite3.connect = orig
assert busy not in core._wal
holder.rollback()

# index info + "fetch new photos": counts and span come from the db; newer=true starts from the newest item's day
db = core.connect()
for u, kind, created, err in [("i1", "image", "2026-11-05T10:00:00-05:00", None), ("v1", "video", "2015-11-02T09:00:00-05:00", None),
                              ("p1", "poster", "2026-11-07T21:30:00-05:00", "bad file")]:
    db.execute("insert into assets(uuid, kind, created, indexed_at, error) values (?,?,?,?,?)", (u, kind, created, 1.0, err))
db.commit()
info = c.get("/index/info").json()
assert info["assets"] == 3 and info["kinds"] == {"image": 1, "video": 1, "poster": 1} and info["failed"] == 1, info
assert info["newest"].startswith("2026-11-07") and info["oldest"].startswith("2015-11-02") and info["model"] == core.MODEL_ID, info
started = []
def fake_run(since):
    started.append(since); server.lock.release()
server.run_reindex = fake_run
assert c.post("/reindex?newer=true").status_code == 202
time.sleep(0.2)
assert started == ["2026-11-07"], started
print("settings checks passed")
