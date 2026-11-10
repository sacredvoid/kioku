"""The Ask runner's live trace: tool calls open entries, tool results close them as done or error.  uv run test_ask_trace.py"""
import json
import os
import stat
import tempfile
from pathlib import Path

os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")  # never touch the real data/index.db
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain

import projects  # noqa: E402
import settings  # noqa: E402

settings.FILE = Path(tempfile.mkdtemp()) / "settings.json"  # never touch the real data/settings.json
settings._cur = None

events = [
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "a", "name": "mcp__kioku__search_media", "input": {"query": "campus"}},
        {"type": "tool_use", "id": "b", "name": "mcp__kioku__analyze_reference", "input": {"path": "https://example.com/v"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "a", "content": [{"type": "text", "text": "[]"}]},
        {"type": "tool_result", "tool_use_id": "b", "content": '{"error": "422 bad link"}'}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Picking moments now."},
        {"type": "tool_use", "id": "c", "name": "mcp__kioku__find_moments", "input": {"uuids": []}}]}},
    {"type": "result", "result": "done"},
]
fake = Path(tempfile.mkdtemp()) / "claude"
fake.write_text("#!/bin/sh\ncat <<'EOF'\n" + "\n".join(json.dumps(e) for e in events) + "\nEOF\n")
fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

state = {"steps": [], "trace": []}
out = projects.run_claude(str(fake), 1, {"name": "t", "brief": "", "items": []}, "make it", state)
assert out == {"output": "done"}, out
t = state["trace"]
assert [e["tool"] for e in t] == ["search_media", "analyze_reference", "find_moments"], t
assert t[0]["state"] == "done" and t[0]["t1"] >= t[0]["t0"]
assert t[1]["state"] == "error" and "422" in t[1]["error"] and t[1]["detail"] == "https://example.com/v"
assert t[2]["state"] == "running"  # no result came back for it
assert state["steps"][0] == "Searching “campus”" and state["note"] == "Picking moments now."
snap_src = {(9, "ask"): {"status": "running", **state}}
projects.jobs.update(snap_src)
snap = projects.job(9, "ask")
snap["trace"][0]["state"] = "x"
assert state["trace"][0]["state"] == "done"  # the snapshot is a copy
print("ask trace ok")

# an edit is recorded as running when it starts; a restart turns it into a visible failure with Retry text
import core  # noqa: E402
import library  # noqa: E402
with library.conn() as c:
    cid = c.execute("insert into collections(name, kind) values ('t', 'project')").lastrowid
    c.commit()
rid = projects.open_run(cid, "make it", 1.0)
projects.mark_interrupted()
row = core.connect().execute("select status, error from ask_runs where id = ?", (rid,)).fetchone()
assert row[0] == "failed" and "restarted" in row[1], row
rid2 = projects.open_run(cid, "again", 2.0)
projects.save_run(cid, "again", {"run_id": rid2, "status": "done", "steps": ["a"], "output": "ok", "model": "opus"})
row = core.connect().execute("select status, output, steps from ask_runs where id = ?", (rid2,)).fetchone()
assert row == ("done", "ok", '["a"]'), row
assert core.connect().execute("select count(*) from ask_runs where collection_id = ?", (cid,)).fetchone()[0] == 2
print("run history ok")
