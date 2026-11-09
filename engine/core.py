"""Shared model, database and hybrid search for the Kioku engine."""
import itertools
import logging
import os
import re
import secrets
import threading
import time
from functools import cache, lru_cache
from pathlib import Path

import numpy as np
from sqlcipher3 import dbapi2 as sqlite3  # SQLCipher 4: every page AES-256 encrypted + HMAC-SHA512 checked

log = logging.getLogger("kioku")
os.umask(0o077)  # everything the engine writes (index, thumbs, renders, logs) is readable by this user only
DATA = Path(os.environ.get("KIOKU_DATA") or Path.home() / "Kioku")
DB_PATH = Path(os.environ.get("CS_DB") or DATA / "index.db")
KEYCHAIN = ("app.kioku.desktop", "index-key")  # login Keychain item: service, account


@cache
def db_key() -> str:
    """The index's 256-bit key as 64 hex chars, from the login Keychain. KIOKU_DB_KEY overrides it (tests only).
    A key is only ever created when no index exists: a new key can't open an existing one, so a missing key on an
    existing index is an error, never a reason to start over."""
    key = os.environ.get("KIOKU_DB_KEY")
    if not key:
        import keyring  # macOS backend: the item's ACL trusts only the Python that created it; others get a prompt
        key = keyring.get_password(*KEYCHAIN)
        if not key:
            if DB_PATH.exists():
                raise RuntimeError(f"{DB_PATH} exists but its key ({KEYCHAIN[0]} / {KEYCHAIN[1]}) is not in the "
                                   "login Keychain. Restore the Keychain item; a new key cannot open this index.")
            # ponytail: two processes creating the very first index at the same instant could race; one engine starts first in practice
            key = secrets.token_hex(32)
            keyring.set_password(*KEYCHAIN, key)
            if keyring.get_password(*KEYCHAIN) != key:
                raise RuntimeError("could not save the index key to the login Keychain")
            log.info("created a new index key in the login Keychain")
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise RuntimeError("index key must be 64 lowercase hex characters")
    return key
AUDIO_DIR = DATA / "audio"
MODEL_ID = "google/embeddinggemma-2"
IMAGE_TOKENS = 280  # allowed 70/140/280/560/1120. 280 matched 560 on search quality at 2.7x the speed (notes/tokens-ab.md)

SCHEMA = """
create table if not exists assets(
  uuid text primary key, kind text, created text, duration real,
  path text, indexed_at real, error text, meta text default '');
create table if not exists segments(
  id integer primary key, uuid text references assets(uuid),
  t0 real, t1 real, source text, vec blob);
create index if not exists segments_uuid on segments(uuid);
create virtual table if not exists assets_fts using fts5(uuid unindexed, meta, tokenize='porter unicode61');

-- From Photos (refreshed by index.py --rebuild-meta). Separate table so the indexer's rows stay stable.
create table if not exists asset_info(
  uuid text primary key, place text, lat real, lon real, albums text, local_original integer);

-- Yours. Photos itself is never written to.
create table if not exists tags(uuid text not null, tag text not null, primary key(uuid, tag));
create table if not exists notes(uuid text primary key, caption text not null);

-- Albums and video projects. Items are pointers (Photos uuid + optional in/out), never copies.
create table if not exists collections(
  id integer primary key, name text not null, kind text not null check(kind in ('album', 'project')),
  brief text not null default '', created real, updated real);
create table if not exists collection_items(
  id integer primary key, collection_id integer not null references collections(id) on delete cascade,
  uuid text not null, pos real not null, t0 real, t1 real);
create index if not exists collection_items_c on collection_items(collection_id, pos);

-- Every Claude edit of a project, kept so the app can show the full history (steps are a JSON list).
create table if not exists ask_runs(
  id integer primary key, collection_id integer not null references collections(id) on delete cascade,
  prompt text not null, model text, status text not null, steps text not null default '[]',
  output text, error text, started real, finished real);
create index if not exists ask_runs_c on ask_runs(collection_id, started);
"""


_wal = set()  # db paths already switched to WAL by this process


_pool: dict = {}  # (path, key) -> idle connections. Never closed while the process runs: see Conn.close
_pool_lock = threading.Lock()


class Conn(sqlite3.Connection):
    """`with core.connect() as c:` commits (or rolls back) AND gives the connection back. Plain sqlite3 only
    commits, which left connections open until garbage collection and ran the engine out of file handles.
    close() returns the connection to a pool instead of closing it: SQLite 3.51 (SQLCipher) deadlocked the
    engine when one thread closed a WAL connection while another opened the same file (2026-11-09), and
    opening per request was the only reason connections ever closed. One holder at a time, so
    check_same_thread is off; the pool is bounded by the number of threads that use the db at once."""
    _path = None
    _out = False

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()

    def close(self):
        if not self._out:  # already back in the pool (double close) or never handed out
            return
        self._out = False
        try:
            self.rollback()  # drop anything uncommitted, exactly as a real close would
        except sqlite3.Error:
            return super().close()  # broken connection: really close it, don't pool it
        with _pool_lock:
            _pool.setdefault(self._path, []).append(self)


def connect(path=DB_PATH):
    key = db_key()  # before connect(): connecting creates the file, and db_key() treats an existing file as an index
    with _pool_lock:
        idle = _pool.get((path, key))
        db = idle.pop() if idle else None
    if db is not None:
        db._out = True
        return db
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # the indexer may be writing; wait instead of failing
    db = sqlite3.connect(path, timeout=30, factory=Conn, check_same_thread=False)
    db._path, db._out = (path, key), True
    # raw hex key (no passphrase KDF needed: it's already 256 random bits). Must be the first statement.
    db.execute(f"pragma key = \"x'{key}'\"")
    db.execute("pragma temp_store = memory")  # sorts and temp tables never touch disk unencrypted
    try:
        db.execute("select count(*) from sqlite_master").fetchone()
    except sqlite3.DatabaseError as e:
        sqlite3.Connection.close(db)  # really close: a connection that can't read the db must never be pooled
        if isinstance(e, sqlite3.OperationalError):  # busy/locked: the caller's usual retry handling applies
            raise
        raise RuntimeError(f"cannot open {path}: wrong key, or an unencrypted index ({e})") from e
    if path not in _wal:
        # WAL lets readers and the indexer's writes overlap. The switch is persistent but needs a brief exclusive
        # lock, so if the db is busy just try again on a later connect; never fail the connection over it.
        try:
            db.execute("pragma journal_mode = wal")
            _wal.add(path)
        except sqlite3.OperationalError as e:
            log.warning("could not enable WAL on %s yet: %s", path, e)
    db.execute("pragma foreign_keys = on")
    db.executescript(SCHEMA)
    return db


def refresh_fts(db, uuid):
    """Search text = Photos metadata + your tags + your caption."""
    meta = (db.execute("select meta from assets where uuid = ?", (uuid,)).fetchone() or [""])[0] or ""
    tags = [r[0] for r in db.execute("select tag from tags where uuid = ? order by tag", (uuid,))]
    cap = (db.execute("select caption from notes where uuid = ?", (uuid,)).fetchone() or [""])[0]
    db.execute("delete from assets_fts where uuid = ?", (uuid,))
    db.execute("insert into assets_fts(uuid, meta) values (?, ?)", (uuid, " ; ".join([meta, *tags, cap])))


@cache
def model():
    from sentence_transformers import SentenceTransformer
    # Vision only: skip the ~300M audio encoder. Don't force float16 (unsupported by this model).
    return SentenceTransformer(MODEL_ID, device="mps", config_kwargs={"audio_config": None})


@cache
def query_model():
    """Text-only copy on the CPU for search queries: ~40 ms, same vectors as the GPU model
    (cosine >= 0.998), and never waits behind an indexer that's saturating the GPU."""
    import torch
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(MODEL_ID, device="cpu", model_kwargs={"dtype": torch.float32},
                               config_kwargs={"audio_config": None, "vision_config": None})


@lru_cache(maxsize=512)
def embed_query(text):
    v = query_model().encode(text, prompt_name="query", normalize_embeddings=True).astype(np.float32)
    v.flags.writeable = False  # cached and shared between requests
    return v


STOP = {"a", "an", "the", "of", "in", "on", "at", "with", "and", "or", "to", "from", "my", "me", "for", "is", "some",
        "photo", "photos", "picture", "pictures", "image", "images", "video", "videos"}


def fts_query(q):
    """User text -> safe FTS5 query: quoted words OR'd together (no FTS syntax injection)."""
    words = [w for w in re.findall(r"\w+", q.lower()) if w not in STOP]
    return " OR ".join(f'"{w}"' for w in words)


class Index:
    """Hybrid search: image vectors (meaning of the pixels) + keyword match on Apple's metadata
    (labels, people, place, albums, date). Ranks are merged with reciprocal rank fusion."""
    # ponytail: brute-force cosine over every vector; fine to ~500k segments, add ANN (sqlite-vec/faiss) past that.
    RRF_K = 60
    FLOOR_MARGIN = 0.08  # vector-only hits must be within this of the best vector score

    def __init__(self, db):
        # Stream rows into one preallocated matrix. fetchall() plus b"".join() held every vector twice (peak 380 MB
        # for a 137 MB matrix), and this runs every 30 s while the indexer writes, with the old index still live.
        n = db.execute("select count(*) from segments").fetchone()[0]
        self.seg_uuid, self.seg_span, ids = [], [], {}
        seg_asset, M = np.empty(n, np.int64), None
        for i, (u, vec, t0, t1, src) in enumerate(db.execute("select uuid, vec, t0, t1, source from segments")):
            if i >= n:  # the indexer added rows since the count: the next reload picks them up
                break
            if M is None:
                M = np.empty((n, len(vec) // 4), np.float32)
            M[i] = np.frombuffer(vec, np.float32)
            self.seg_uuid.append(u)
            self.seg_span.append((t0, t1, src))
            seg_asset[i] = ids.setdefault(u, len(ids))
        k = len(self.seg_uuid)  # fewer than n if rows were deleted meanwhile (analyze rewrites a video's scenes)
        self.seg_asset = seg_asset[:k]
        self.M = M[:k] if M is not None else np.zeros((0, 768), np.float32)
        self.assets = {r[0]: r[1:] for r in db.execute("select uuid, kind, path, duration, meta from assets")}
        self.loaded_at = time.time()

    def vector_hits(self, query):
        """Best segment per asset, highest score first: {uuid: (score, segment index, rank)}.
        Returned rather than stored on self: concurrent searches share this Index."""
        if not len(self.seg_uuid):
            return {}
        s = self.M @ embed_query(query)
        order = np.argsort(-s, kind="stable")
        _, first = np.unique(self.seg_asset[order], return_index=True)  # first hit per asset = its best segment
        segs = order[np.sort(first)]
        return {self.seg_uuid[k]: (v, k, rank) for rank, (k, v) in enumerate(zip(segs.tolist(), s[segs].tolist()))}

    def keyword_hits(self, query):
        fq = fts_query(query)
        if not fq:
            return {}
        # fresh connection per call: the server runs requests on different threads
        db = connect()
        try:
            rows = db.execute(
                "select uuid, bm25(assets_fts) from assets_fts where assets_fts match ? order by 2 limit 500", (fq,)
            ).fetchall()
        finally:
            db.close()
        return {u: -b for u, b in rows}  # bm25 is lower-is-better; flip so higher is better

    def search(self, query, k=20, kind=None):
        vec, kw = self.vector_hits(query), self.keyword_hits(query)
        floor = next(iter(vec.values()), (0,))[0] - self.FLOOR_MARGIN
        kw_rank = {u: i for i, u in enumerate(kw)}
        # weak visual matches with no keyword match are dropped instead of shown as junk; vec is sorted, so stop at the floor
        strong = itertools.takewhile(lambda u: vec[u][0] >= floor, vec)
        out = []
        for u in dict.fromkeys([*strong, *kw]):
            if u not in self.assets:
                continue
            akind, path, duration, meta = self.assets[u]
            if kind and not (akind == kind or (kind == "video" and akind == "poster")):
                continue
            v, seg, vrank = vec.get(u, (0, None, None))
            rrf = sum(1 / (self.RRF_K + r) for r in (vrank, kw_rank.get(u)) if r is not None)
            t0, t1, src = self.seg_span[seg] if seg is not None else (None, None, None)
            out.append(dict(uuid=u, kind=akind, path=path, duration=duration, score=round(rrf * 1000, 2),
                            visual=round(v, 3), keyword=u in kw, meta=meta,
                            moment=dict(t0=t0, t1=t1) if src == "scene" else None))
        out.sort(key=lambda h: -h["score"])
        return out[:k]
