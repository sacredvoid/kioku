"""Preview cache: /full and project warm read the Photos library once, then only Kioku's copy.
uv run test_previews.py   (no Photos, no network)"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import os
import tempfile
import time
from pathlib import Path

os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import core  # noqa: E402

testenv.check()
import media  # noqa: E402
import server  # noqa: E402

tmp = Path(tempfile.mkdtemp())
lib = tmp / "Photos Library.photoslibrary"  # stands in for the real library
lib.mkdir()
media.PREVIEWS, server.THUMBS = tmp / "previews", tmp / "thumbs"
for u in ("A", "B", "C"):
    Image.new("RGB", (1024, 768), "red").save(lib / f"{u}.jpeg")
db = core.connect()
for u, kind, path in [("A", "image", lib / "A.jpeg"), ("B", "image", lib / "B.jpeg"), ("C", "image", lib / "C.jpeg"),
                      ("V", "video", None)]:
    db.execute("insert into assets(uuid, kind, path, duration, meta, created) values (?,?,?,?,?,?)",
               (u, kind, str(path) if path else None, 0, "", "2026-11-01"))
db.commit()
c = TestClient(server.app, base_url="http://127.0.0.1:8765")  # no `with`: skips lifespan, so no model load

# /full copies once, then serves the copy even if the library file can't be read any more
assert c.get("/full/A").status_code == 200
copy = media.PREVIEWS / "A" / "A.jpeg"
assert copy.is_file() and copy.read_bytes() == (lib / "A.jpeg").read_bytes()
os.chmod(lib / "A.jpeg", 0)  # what a macOS denial looks like to Python: PermissionError on open
assert c.get("/full/A").status_code == 200
assert c.get("/thumb/A").status_code == 200  # tile built from the copy, not the library

# a denied read is a readable 403, not a 500, and leaves no partial file behind
os.chmod(lib / "B.jpeg", 0)
r = c.get("/full/B")
assert r.status_code == 403 and "Photos library" in r.json()["detail"], r.text
assert not any((media.PREVIEWS / "B").glob("*")) if (media.PREVIEWS / "B").exists() else True
os.chmod(lib / "B.jpeg", 0o644)

# grid tiles never copy the full preview (browsing shouldn't duplicate the library)
assert c.get("/thumb/C").status_code == 200 and not (media.PREVIEWS / "C").exists()
assert c.get("/thumb/..").status_code == 404 and c.get("/thumb/nope").status_code == 404
assert c.get("/full/nope").status_code == 404 and c.get("/full/V").status_code == 404  # unknown id, no preview

# project warm: copies every item's preview and tile in one background pass
cid = db.execute("insert into collections(name, kind, created, updated) values ('p', 'project', '', '')").lastrowid
db.executemany("insert into collection_items(collection_id, uuid, pos) values (?, ?, ?)",
               [(cid, u, i) for i, u in enumerate(("B", "C", "V"))])
db.commit()
r = c.post(f"/collections/{cid}/warm").json()
assert r == {"items": 3, "started": True}, r
for _ in range(100):
    if cid not in server.warming:
        break
    time.sleep(0.05)
assert (media.PREVIEWS / "B" / "B.jpeg").is_file() and (media.PREVIEWS / "C" / "C.jpeg").is_file()
assert (server.THUMBS / "B" / "B.jpg").is_file() and cid not in server.warming
print("ok")
