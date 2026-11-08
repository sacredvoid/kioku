"""Settings round-trip, validation, the Claude agent queue, and the quiet access-log filter.  uv run test_settings.py"""
import logging
import tempfile
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

import projects
import server
import settings

settings.FILE = Path(tempfile.mkdtemp()) / "settings.json"  # never touch the real data/settings.json
settings._cur = None
c = TestClient(server.app)  # no `with`: skips lifespan, so no model load

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
r = TestClient(server.app, raise_server_exceptions=False).get("/__boom")
assert r.status_code == 500 and "kaboom" in r.json()["detail"], r.text
print("settings checks passed")
