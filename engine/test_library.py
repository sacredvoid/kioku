"""Checks for tags, captions and collections against a throwaway db.  uv run test_library.py"""
import os
import tempfile

os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import core  # noqa: E402
import library  # noqa: E402

db = core.connect()
for u, kind, dur in [("A", "image", 0), ("B", "video", 12.0), ("C", "poster", 30.0)]:
    db.execute("insert into assets(uuid, kind, duration, meta, created) values (?,?,?,?,?)",
               (u, kind, dur, "Beach ; Dog", "2026-11-01"))
    core.refresh_fts(db, u)
db.commit()

app = FastAPI()
app.include_router(library.router)
c = TestClient(app)


def fts(word):
    return {r[0] for r in core.connect().execute("select uuid from assets_fts where assets_fts match ?", (f'"{word}"',))}


# tags: cleaned, deduped, searchable, validated
r = c.put("/asset/A/tags", json={"tags": ["  Grandma  ", "grandma", "lake house"]})
assert r.json()["tags"] == ["Grandma", "lake house"], r.json()
assert fts("grandma") == {"A"}
assert c.put("/asset/A/tags", json={"tags": ["x" * 41]}).status_code == 422
assert c.put("/asset/NOPE/tags", json={"tags": ["a"]}).status_code == 404

# caption: searchable, empty clears it
c.put("/asset/B/caption", json={"caption": "first swim of the summer"})
assert fts("swim") == {"B"}
c.put("/asset/B/caption", json={"caption": "  "})
assert fts("swim") == set()
assert fts("beach") == {"A", "B", "C"}  # Photos labels survive tag/caption edits

# albums: pointers, deduped, unknown uuids rejected
alb = c.post("/collections", json={"name": "Summer", "kind": "album"}).json()
assert c.post(f"/collections/{alb['id']}/items", json={"uuids": ["A", "B", "A"]}).json() == {"added": 2, "skipped": 1}
assert c.post(f"/collections/{alb['id']}/items", json={"uuids": ["A"]}).json()["added"] == 0
assert c.post(f"/collections/{alb['id']}/items", json={"uuids": ["ZZZ"]}).status_code == 422
assert c.post("/collections", json={"name": "x", "kind": "folder"}).status_code == 422

# projects: same clip twice allowed, in/out validated against clip length
prj = c.post("/collections", json={"name": "Japan cut", "kind": "project"}).json()
c.post(f"/collections/{prj['id']}/items", json={"uuids": ["B", "B"]})
items = c.get(f"/collections/{prj['id']}").json()["items"]
assert len(items) == 2
iid = items[0]["item_id"]
assert c.patch(f"/collections/{prj['id']}/items/{iid}", json={"t0": 2, "t1": 5}).json()["t1"] == 5
assert c.patch(f"/collections/{prj['id']}/items/{iid}", json={"t0": 6}).status_code == 422  # in after out
assert c.patch(f"/collections/{prj['id']}/items/{iid}", json={"t1": 99}).status_code == 422  # past the end
assert c.patch(f"/collections/{prj['id']}/items/{iid}", json={"t0": -1}).status_code == 422

# asset detail lists its collections
a = c.get("/asset/B").json()
assert {x["name"] for x in a["collections"]} == {"Summer", "Japan cut"}, a["collections"]

# listing, cover, delete cascades pointers only
cols = {x["name"]: x for x in c.get("/collections").json()}
assert cols["Summer"]["count"] == 2 and cols["Summer"]["cover"] == "A"
assert c.delete(f"/collections/{alb['id']}").status_code == 204
assert core.connect().execute("select count(*) from collection_items where collection_id = ?", (alb["id"],)).fetchone()[0] == 0
assert core.connect().execute("select count(*) from assets").fetchone()[0] == 3  # photos untouched
# search: best segment per asset, scene moment, weak visual-only hits dropped, keyword hits kept
import numpy as np  # noqa: E402
e = np.eye(768, dtype=np.float32)
db = core.connect()
for u, t0, t1, src, v in [("A", None, None, "image", 0.95 * e[0]), ("B", 0, 4, "scene", 0.3 * e[0]),
                          ("B", 4, 8, "scene", e[0]), ("C", None, None, "poster", e[1])]:
    db.execute("insert into segments(uuid, t0, t1, source, vec) values (?,?,?,?,?)", (u, t0, t1, src, v.tobytes()))
db.commit()
core.embed_query = lambda text: e[0]
idx = core.Index(db)
hits = idx.search("zebra")  # no keyword match: vector only, C (score 0) is below the floor
assert [h["uuid"] for h in hits] == ["B", "A"], hits
assert hits[0]["moment"] == {"t0": 4, "t1": 8} and hits[0]["visual"] == 1.0 and hits[1]["moment"] is None
assert [h["uuid"] for h in idx.search("zebra", kind="image")] == ["A"]
c.put("/asset/C/tags", json={"tags": ["kite"]})
hits = idx.search("kite")  # keyword match keeps C despite its weak picture score, and ranks it first
assert [h["uuid"] for h in hits] == ["C", "B", "A"] and hits[0]["keyword"] and hits[0]["visual"] == 0, hits
print("library checks passed")
