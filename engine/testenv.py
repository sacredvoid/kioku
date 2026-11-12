"""Import first in every test: engine data, the db and its key go to a temp folder, never ~/Kioku or the Keychain.
A test may still point KIOKU_DATA / CS_DB at its own temp paths afterwards, before it imports core."""
import os
import tempfile
from pathlib import Path

REAL = (Path.home() / "Kioku").resolve()
_tmp = Path(tempfile.mkdtemp(prefix="kioku-test-"))


def _real(p: str | Path) -> bool:
    return Path(p).expanduser().resolve().is_relative_to(REAL)


if not os.environ.get("KIOKU_DATA") or _real(os.environ["KIOKU_DATA"]):
    os.environ["KIOKU_DATA"] = str(_tmp / "data")
    (_tmp / "data").mkdir(mode=0o700)  # the engine expects its data folder to exist (the app creates ~/Kioku)
if not os.environ.get("CS_DB") or _real(os.environ["CS_DB"]):
    os.environ["CS_DB"] = str(_tmp / "t.db")
os.environ.setdefault("KIOKU_DB_KEY", "0" * 64)


def check():
    """Fail fast if core ended up on the real data folder (call after importing core)."""
    import core
    assert not _real(core.DATA) and not _real(core.DB_PATH), (core.DATA, core.DB_PATH)
