"""Checks for smart-album drafts against a throwaway db, fake search and fake planner.  uv run test_albums.py"""
import os
import tempfile
import threading
import time
from pathlib import Path

os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import albums  # noqa: E402
import core  # noqa: E402
import library  # noqa: E402
import settings  # noqa: E402

assert "data/index.db" not in str(core.DB_PATH), core.DB_PATH
settings.FILE = Path(tempfile.mkdtemp()) / "settings.json"
settings._cur = settings.Settings()

db = core.connect()
for i in range(1, 31):  # P01..P30, created in uuid order; the library is also tested through its own routes
    db.execute("insert into assets(uuid, kind, duration, meta, created) values (?,?,?,?,?)",
               (f"P{i:02d}", "image", 0, "Beach", f"2026-01-{i:02d}"))
db.commit()
n_assets = db.execute("select count(*) from assets").fetchone()[0]

app = FastAPI()
app.include_router(library.router)
app.include_router(albums.router)
c = TestClient(app)


def hit(u, score):
    return dict(uuid=u, score=score)


HITS = {  # query -> hits, best first
    "kyoto": [hit("P01", 10), hit("P02", 9), hit("P03", 8)],
    "temple": [hit("P02", 7), hit("P04", 6)],
    "osaka": [hit("P05", 5), hit("P01", 4)],
    "plain words": [hit("P06", 3)],
}
gate = threading.Event()
gate.set()
calls = []


def fake_search(q, k, kind):
    calls.append((q, kind))
    gate.wait(5)
    return HITS.get(q, [])


def fake_plan(description, instruction, used, refining_name=None):
    qs = ["osaka"] if instruction else ["kyoto", "temple"]
    return {"name": "Japan trip", "queries": [{"q": q, "kind": "", "why": q.title()} for q in qs]}


real_plan = albums.plan
albums.search, albums.plan = fake_search, fake_plan


def wait(did):
    for _ in range(100):
        j = c.get(f"/drafts/{did}/job").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job still running")


def items(did):
    return {i["uuid"]: i for i in c.get(f"/drafts/{did}").json()["items"]}


# parse_plan: fences and prose tolerated, junk rejected
ok = albums.parse_plan('Sure!\n```json\n{"name": "X", "queries": [{"q": "a"}]}\n```')
assert ok["queries"] == [{"q": "a", "kind": "", "why": ""}]
for bad in ["", "no json", '{"queries": []}', '{"queries": [{"q": "a", "kind": "audio"}]}', '{"queries": [{"q": ""}]}']:
    try:
        albums.parse_plan(bad)
        raise AssertionError(f"accepted {bad!r}")
    except ValueError:
        pass

# 1. create: fused, deduped, labelled; a photo two queries agree on beats a lone hit; name from the plan
r = c.post("/drafts", json={"description": "kyoto trip"})
assert r.status_code == 202, r.text
did = r.json()["id"]
assert wait(did)["status"] == "done"
it = items(did)
assert list(it) == ["P02", "P01", "P03", "P04"], list(it)  # P02 = 9 * 1.25 beats P01 = 10
assert it["P02"]["hits"] == 2 and it["P02"]["query"] == "kyoto" and it["P02"]["why"] == "Kyoto"
assert all(i["state"] == "in" and i["round"] == 1 for i in it.values())
d = c.get(f"/drafts/{did}").json()
assert d["name"] == "Japan trip" and d["kept"] == 4 and d["runs"][0]["status"] == "done"
assert [x["id"] for x in c.get("/drafts").json()] == [did]

# 2. remove, then search again: the tombstone holds, new items are round 2
assert c.patch(f"/drafts/{did}/items", json={"uuids": ["P01", "P03"], "state": "removed"}).json()["removed"] == 2
HITS["osaka"] = [hit("P05", 5), hit("P01", 4), hit("P03", 3)]
assert c.post(f"/drafts/{did}/search", json={"prompt": "also osaka"}).status_code == 202
wait(did)
it = items(did)
assert it["P01"]["state"] == "removed" and it["P01"]["by"] == "user" and it["P01"]["round"] == 1
assert it["P05"]["round"] == 2 and it["P05"]["state"] == "in" and len(it) == 5

# 3. restore, and a no-op change leaves `by` alone
assert c.patch(f"/drafts/{did}/items", json={"uuids": ["P01"], "state": "in"}).json()["changed"] == 1
assert c.patch(f"/drafts/{did}/items", json={"uuids": ["P02"], "state": "in"}).json()["changed"] == 0
assert items(did)["P02"]["by"] == "search" and c.get(f"/drafts/{did}").json()["kept"] == 4

# 4. a running job blocks a second one (409) and Save
gate.clear()
assert c.post(f"/drafts/{did}/search", json={"prompt": "again please"}).status_code == 202
assert c.post(f"/drafts/{did}/search", json={"prompt": "and again"}).status_code == 409
assert c.post(f"/drafts/{did}/save").status_code == 409
gate.set()
wait(did)

# 5. planner failure falls back to plain search (generic words stripped) with a note
def broken_plan(*a, **k):
    raise RuntimeError("no claude")


albums.plan = broken_plan
did2 = c.post("/drafts", json={"description": "plain words photos"}).json()["id"]
assert wait(did2)["note"] == "Claude unavailable, used plain search"
assert ("plain words", None) in calls and list(items(did2)) == ["P06"]
albums.plan = fake_plan

# planner output: bad JSON is retried once with the error, then gives up; a good second reply works
replies = iter(["not json", '{"queries": [{"q": "ok"}]}'])
albums.claude_text = lambda prompt, model: next(replies)
assert real_plan("x y z", "", [])["queries"][0]["q"] == "ok"
albums.claude_text = lambda prompt, model: "still not json"
try:
    real_plan("x y z", "", [])
    raise AssertionError("should give up")
except RuntimeError as e:
    assert "twice" in str(e)

# ?plain=1 skips the planner entirely
albums.plan = broken_plan
did3 = c.post("/drafts?plain=1", json={"description": "kyoto"}).json()["id"]
assert wait(did3).get("note") is None and set(items(did3)) == {"P01", "P02", "P03"}
albums.plan = fake_plan

# 6. caps: 150 per round, 400 per draft
many = [hit(f"M{i:03d}", 1000 - i) for i in range(500)]
db = core.connect()
db.executemany("insert into assets(uuid, kind, duration, meta, created) values (?,?,?,?,?)",
               [(h["uuid"], "image", 0, "Many", "2026-02-01") for h in many])
db.commit()
n_assets += 500
HITS["bulk"] = many
fake_plan_bulk = lambda *a, **k: {"name": "", "queries": [{"q": "bulk", "kind": "", "why": ""}]}
albums.plan = fake_plan_bulk
did4 = c.post("/drafts", json={"description": "lots of them"}).json()["id"]
wait(did4)
assert len(items(did4)) == 150
HITS["bulk"] = many[:] + [hit(f"N{i:03d}", 1) for i in range(400)]
db.executemany("insert into assets(uuid, kind, duration, meta, created) values (?,?,?,?,?)",
               [(f"N{i:03d}", "image", 0, "Many", "2026-02-02") for i in range(400)])
db.commit()
n_assets += 400
for _ in range(4):
    c.post(f"/drafts/{did4}/search", json={"prompt": "more"})
    wait(did4)
assert len(items(did4)) == 400
albums.plan = fake_plan

# 7. save: kept items only, date ascending, idempotent, photos untouched
r = c.post(f"/drafts/{did}/save", json={"name": "Japan"})
assert r.status_code == 200, r.text
cid = r.json()["collection_id"]
col = c.get(f"/collections/{cid}").json()
kept = sorted(u for u, i in items(did).items() if i["state"] == "in")  # created ascending == uuid order here
assert col["name"] == "Japan" and [i["uuid"] for i in col["items"]] == kept and len(kept) == 4, col["items"]
assert c.post(f"/drafts/{did}/save").json()["collection_id"] == cid
assert len(c.get(f"/collections/{cid}").json()["items"]) == len(col["items"])
assert did not in [x["id"] for x in c.get("/drafts").json()]
assert c.get(f"/drafts/{did}").json()["status"] == "saved"
assert core.connect().execute("select count(*) from assets").fetchone()[0] == n_assets

# 8. refine an existing album: seeded, diff applied on save, others keep their pos
alb = c.post("/collections", json={"name": "Trip", "kind": "album"}).json()["id"]
c.post(f"/collections/{alb}/items", json={"uuids": ["P10", "P11", "P12"]})
did5 = c.post("/drafts", json={"description": "kyoto too", "album_id": alb}).json()["id"]
wait(did5)
it = items(did5)
assert it["P10"]["why"] == "Already in album" and it["P10"]["round"] == 0 and "P02" in it
c.patch(f"/drafts/{did5}/items", json={"uuids": ["P11", "P03"], "state": "removed"})
assert c.post(f"/drafts/{did5}/save").json()["collection_id"] == alb
got = {i["uuid"]: i["pos"] for i in c.get(f"/collections/{alb}").json()["items"]}
assert "P11" not in got and "P03" not in got and {"P10", "P12", "P01", "P02"} <= set(got), got
assert got["P10"] == 1 and got["P12"] == 3

# refined album deleted before Save: 409, then Save as new works
did6 = c.post("/drafts", json={"description": "kyoto again", "album_id": alb}).json()["id"]
wait(did6)
c.delete(f"/collections/{alb}")
assert c.post(f"/drafts/{did6}/save").status_code == 409
new = c.post(f"/drafts/{did6}/save", json={"as_new": True})
assert new.status_code == 200 and c.get(f"/collections/{new.json()['collection_id']}").json()["items"]  # (sqlite may reuse the id)

# 9. restart: a run left 'running' with no live job is reported failed and closed
did7 = c.post("/drafts", json={"description": "kyoto"}).json()["id"]
wait(did7)
core.connect().execute("insert into draft_runs(draft_id, round, status) values (?, 9, 'running')", (did7,)).connection.commit()
albums.jobs.clear()
j = c.get(f"/drafts/{did7}/job").json()
assert j["status"] == "failed" and j["error"] == albums.RESTARTED
assert core.connect().execute("select status from draft_runs where draft_id = ? and round = 9", (did7,)).fetchone()[0] == "failed"
assert c.get(f"/drafts/{did7}/job").json()["status"] == "idle"

# 10. validation and discard
assert c.post("/drafts", json={"description": "  "}).status_code == 422
assert c.post("/drafts", json={"description": "x" * 2001}).status_code == 422
assert c.post("/drafts", json={"description": "ok then", "album_id": 9999}).status_code == 422
proj = c.post("/collections", json={"name": "P", "kind": "project"}).json()["id"]
assert c.post("/drafts", json={"description": "ok then", "album_id": proj}).status_code == 422
assert c.get("/drafts/9999").status_code == 404
assert c.patch(f"/drafts/{did5}/items", json={"uuids": ["NOPE"], "state": "in"}).status_code == 422
assert c.patch(f"/drafts/{did5}/items", json={"uuids": ["P10"], "state": "gone"}).status_code == 422
assert c.patch(f"/drafts/{did5}/items", json={"uuids": [f"u{i}" for i in range(401)], "state": "in"}).status_code == 422
assert c.patch(f"/drafts/{did5}/items", json={"uuids": [], "state": "in"}).status_code == 422
assert c.patch(f"/drafts/{did5}", json={"name": ""}).status_code == 422
assert c.patch(f"/drafts/{did5}", json={"name": " Renamed "}).json()["name"] == "Renamed"
assert c.delete(f"/drafts/{did5}").status_code == 204 and c.get(f"/drafts/{did5}").status_code == 404
assert c.delete(f"/drafts/{did}").status_code == 204  # saved drafts can be discarded too
assert c.get(f"/collections/{cid}").status_code == 200  # the album outlives its draft

# the real settings file was never touched; album_model is a setting
assert settings.get().album_model == "sonnet" and not settings.FILE.exists()
print("albums: all checks passed")
