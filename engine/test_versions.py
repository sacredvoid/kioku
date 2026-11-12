"""Checks for saved edit versions: save, dedupe, load (with fresh item ids), the data-loss guard.  uv run test_versions.py"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import os
import sys
import tempfile
import types

os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import core  # noqa: E402
testenv.check()
sys.modules["captions"] = types.SimpleNamespace(start_transcription=lambda cid: None, likely_speech=lambda u: False)
import edit  # noqa: E402
import library  # noqa: E402
import projects  # noqa: E402
import versions  # noqa: E402

db = core.connect()
for u in ("A", "B", "C"):
    db.execute("insert into assets(uuid, kind, duration, meta, created) values (?,?,?,?,?)", (u, "video", 10.0, "", "2026-11-01"))
db.commit()
app = FastAPI()
for r in (library.router, edit.router, versions.router):
    app.include_router(r)
c = TestClient(app)

cid = c.post("/collections", json={"name": "p", "kind": "project"}).json()["id"]
assert versions.save(cid, "empty") is None  # nothing to keep yet
c.post(f"/collections/{cid}/items", json={"uuids": ["A", "B"]})
ids = [i["item_id"] for i in c.get(f"/collections/{cid}").json()["items"]]
assert c.patch(f"/collections/{cid}/edit/clips/{ids[1]}", json={"speed": 2}).status_code == 200
v1 = versions.save(cid, "first cut")
assert v1 and versions.save(cid, "same again") is None  # identical edit: not saved twice

# change it (Claude's second prompt): new clip, B slower, A trimmed
c.post(f"/collections/{cid}/items", json={"uuids": ["C"]})
assert c.patch(f"/collections/{cid}/edit/clips/{ids[1]}", json={"speed": 0.5}).status_code == 200
assert c.patch(f"/collections/{cid}/items/{ids[0]}", json={"t0": 1, "t1": 3}).status_code == 200
v2 = versions.save(cid, "longer")
vs = c.get(f"/collections/{cid}/versions").json()
assert [(v["n"], v["label"], v["current"], v["clips"]) for v in vs] == [(2, "longer", True, 3), (1, "first cut", False, 2)], vs

# a hand edit after v2, then load v1: the hand edit is saved first, v1 comes back with its settings on new ids
c.post(f"/collections/{cid}/items", json={"uuids": ["A"]})
r = c.post(f"/collections/{cid}/versions/{v1}/load").json()
assert r["loaded"] == v1 and r["skipped"] == 0, r
items = c.get(f"/collections/{cid}").json()["items"]
assert [i["uuid"] for i in items] == ["A", "B"] and items[0]["t0"] is None, items
spec = c.get(f"/collections/{cid}/edit").json()["spec"]
assert spec["clips"] == {str(items[1]["item_id"]): {"speed": 2.0}}, spec["clips"]
vs = c.get(f"/collections/{cid}/versions").json()
assert [(v["label"], v["current"]) for v in vs] == [("Your changes", False), ("longer", False), ("first cut", True)], vs

# load v2 back: everything it had, and loading the same state again saves nothing new
c.post(f"/collections/{cid}/versions/{v2}/load")
items = c.get(f"/collections/{cid}").json()["items"]
assert [(i["uuid"], i["t0"], i["t1"]) for i in items] == [("A", 1, 3), ("B", None, None), ("C", None, None)], items
assert c.get(f"/collections/{cid}/edit").json()["spec"]["clips"] == {str(items[1]["item_id"]): {"speed": 0.5}}
assert len(c.get(f"/collections/{cid}/versions").json()) == 3

# guards: unknown version, another project's version, Claude mid-edit, albums
assert c.post(f"/collections/{cid}/versions/999/load").status_code == 404
other = c.post("/collections", json={"name": "q", "kind": "project"}).json()["id"]
assert c.post(f"/collections/{other}/versions/{v1}/load").status_code == 404
projects.jobs[(cid, "ask")] = {"status": "running"}
assert c.post(f"/collections/{cid}/versions/{v1}/load").status_code == 409
projects.jobs.pop((cid, "ask"))
alb = c.post("/collections", json={"name": "a", "kind": "album"}).json()["id"]
assert c.get(f"/collections/{alb}/versions").status_code == 422

# a photo deleted from the library since: skipped, the rest loads
db.execute("delete from collection_items where uuid = 'C'")
db.execute("delete from assets where uuid = 'C'")
db.commit()
assert c.post(f"/collections/{cid}/versions/{v2}/load").json()["skipped"] == 1
assert [i["uuid"] for i in c.get(f"/collections/{cid}").json()["items"]] == ["A", "B"]

# renders: the current edit's number, saved as "Rendered" when it is new; numbers are never reused
n_now = versions.number(cid, "Rendered")
assert n_now == c.get(f"/collections/{cid}/versions").json()[0]["n"] and versions.number(cid, "Rendered") == n_now
c.post(f"/collections/{cid}/items", json={"uuids": ["B"]})
assert versions.number(cid, "Rendered") == n_now + 1
assert c.get(f"/collections/{cid}/versions").json()[0]["label"] == "Rendered"

# capped per project
for k in range(versions.KEEP + 5):
    db.execute("update collection_items set t1 = ? where collection_id = ? and uuid = 'A'", (k + 4.0, cid))
    db.commit()
    versions.save(cid, f"v{k}")
vs = c.get(f"/collections/{cid}/versions").json()
assert len(vs) == versions.KEEP and vs[0]["n"] == n_now + 1 + versions.KEEP + 5, vs[0]  # oldest dropped, numbers kept
print("versions checks passed")
