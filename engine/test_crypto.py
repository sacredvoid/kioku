"""The index is encrypted at rest and the key logic never silently starts over. Never touches the real Keychain."""
import os
import sys
import tempfile
import types
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["KIOKU_DATA"] = str(tmp)
os.environ["CS_DB"] = str(tmp / "t.db")
os.environ.pop("KIOKU_DB_KEY", None)
store = {}
sys.modules["keyring"] = types.SimpleNamespace(  # fake Keychain
    get_password=lambda s, a: store.get((s, a)), set_password=lambda s, a, k: store.__setitem__((s, a), k))

import core  # noqa: E402

# first run: no index, no key -> a fresh 256-bit key is created and saved
with core.connect() as c:
    c.execute("insert into assets(uuid, kind, meta) values ('u1', 'image', 'PLAINTEXT-CANARY')")
key = store[core.KEYCHAIN]
assert len(key) == 64
raw = core.DB_PATH.read_bytes()
assert not raw.startswith(b"SQLite format 3"), "header must be encrypted"
assert b"PLAINTEXT-CANARY" not in raw, "row data must be encrypted"
assert oct(tmp.stat().st_mode & 0o777) and (core.DB_PATH.stat().st_mode & 0o077) == 0, "index must be owner-only"

# wrong key -> clear error, not a silent empty index
core.db_key.cache_clear()
store[core.KEYCHAIN] = "f" * 64
try:
    core.connect()
    raise AssertionError("wrong key opened the index")
except RuntimeError as e:
    assert "wrong key" in str(e)

# key missing while an index exists -> refuse; never mint a new key over existing data
core.db_key.cache_clear()
store.clear()
try:
    core.db_key()
    raise AssertionError("minted a new key over an existing index")
except RuntimeError as e:
    assert "not in the" in str(e)
assert not store, "must not have saved a new key"

# right key reads the data back
core.db_key.cache_clear()
store[core.KEYCHAIN] = key
with core.connect() as c:
    assert c.execute("select meta from assets where uuid = 'u1'").fetchone()[0] == "PLAINTEXT-CANARY"
print("ok")
