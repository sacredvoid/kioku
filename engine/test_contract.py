"""media.fetch lock order + label plumbing; no downloads, no ffmpeg.  uv run test_contract.py"""
import testenv  # noqa: F401  (first: temp data folder, never ~/Kioku)
import os
import tempfile
from pathlib import Path

os.environ["CS_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain

import core  # noqa: E402
import media  # noqa: E402
import settings  # noqa: E402
import timeline  # noqa: E402

testenv.check()
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
# a proxy that already exists is returned without queueing for a video slot
order.clear()
media.PROXIES.mkdir(parents=True, exist_ok=True)
(media.PROXIES / "u2.mp4").write_bytes(b"x")
assert media.fetch("u2", "preview", Path(".")) == media.PROXIES / "u2.mp4" and order == [], order
print("ok")

# connections from core.connect() go back to the pool when their `with` block ends (they used to leak until the
# engine ran out of files), are reused instead of reopened, and a double close never hands one out twice
with core.connect() as _c:
    _c.execute("create table if not exists _t(x)")
    _c.execute("insert into _t values (1)")
assert not _c._out
_d = core.connect()
assert _d is _c and _d._out  # reused, not reopened
_d.execute("insert into _t values (2)")
_d.close(); _d.close()  # double close: pooled once
assert core.connect() is _c and core.connect() is not _c  # second checkout gets a fresh connection
assert core.connect().execute("select count(*) from _t").fetchone()[0] == 1  # close without commit rolled back
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
