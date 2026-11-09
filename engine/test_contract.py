"""media.fetch lock order + label plumbing; no downloads, no ffmpeg.  uv run test_contract.py"""
import os
import tempfile
from pathlib import Path

os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")  # never touch the real data/index.db
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain

import core  # noqa: E402
import media  # noqa: E402
import settings  # noqa: E402
import timeline  # noqa: E402

assert "data/index.db" not in str(core.DB_PATH)
assert core.fts_query("photos of the beach") == '"beach"'
timeline.Timeline(30, "9:16", 1080, (), 0, (), "h").duration == 0

media.stage("k", "working", "Render: X")
assert media.ACTIVE["k"]["label"] == "Render: X"
media.stage("k", None)

order, f = [], Path("/x/p.mp4")
media.ensure_proxy = lambda u: order.append("proxy") or f
media.local_original = lambda u: order.append("local") or None
_vs, _rs = media.video_slot, media.release_video_slot
media.video_slot = lambda k, label=None: order.append(("slot", media.lock_for(k).locked())) or _vs(k, label)
media.release_video_slot = lambda: order.append("release") or _rs()
assert media.fetch("u1", "preview", Path(tempfile.mkdtemp())) == f
assert order == [("slot", True), "proxy", "release"], order  # lock held when the slot is taken
assert not media.lock_for("u1").locked() and media.RUNNING == 0 and "u1" not in media.ACTIVE
try:
    media.fetch("u1", "bogus", Path("."))
    raise SystemExit("bad mode accepted")
except ValueError:
    pass
assert media.RUNNING == 0
print("ok")

# connections from core.connect() close when their `with` block ends (they used to leak until the engine ran out of files)
from core import sqlite3 as _sq  # noqa: E402
with core.connect() as _c:
    _c.execute("select 1")
try:
    _c.execute("select 1")
    raise AssertionError("connection still open after with-block")
except _sq.ProgrammingError:
    pass
print("connection close ok")

# the search index streams vectors into one preallocated matrix (fetchall + join used to hold every vector twice)
import numpy as _np  # noqa: E402
with core.connect() as _c:
    _c.execute("insert into assets(uuid, kind) values ('a', 'image'), ('b', 'image')")
    _c.executemany("insert into segments(uuid, t0, t1, source, vec) values (?, 0, 1, 'image', ?)",
                   [(u, _np.full(768, v, _np.float32).tobytes()) for u, v in (("a", 1.0), ("b", 2.0), ("a", 3.0))])
    _idx = core.Index(_c)
assert _idx.M.shape == (3, 768) and _idx.M[2, 0] == 3.0 and _idx.M.dtype == _np.float32, _idx.M.shape
assert _idx.seg_uuid == ["a", "b", "a"] and _idx.seg_asset.tolist() == [0, 1, 0] and _idx.seg_span[1] == (0, 1, "image")
with core.connect(Path(tempfile.mkdtemp()) / "empty.db") as _c:
    assert core.Index(_c).M.shape == (0, 768)
print("index build ok")
