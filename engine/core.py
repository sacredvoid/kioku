"""Shared model, database and hybrid search for the Safelight engine."""
import os
import re
import sqlite3
import time
from functools import cache, lru_cache
from pathlib import Path

import numpy as np

DATA = Path(__file__).resolve().parent.parent / "data"
DB_PATH = Path(os.environ.get("CS_DB") or DATA / "index.db")
AUDIO_DIR = DATA / "audio"
MODEL_ID = "google/embeddinggemma-2"
IMAGE_TOKENS = 560  # model default is 280; allowed 70/140/280/560/1120

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
"""


def connect(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)  # the indexer may be writing; wait instead of failing
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


STOP = {"a", "an", "the", "of", "in", "on", "at", "with", "and", "or", "to", "from", "my", "me", "for", "is", "some"}


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
        rows = db.execute("select uuid, vec, t0, t1, source from segments").fetchall()
        self.seg_uuid = [r[0] for r in rows]
        self.seg_span = [(r[2], r[3], r[4]) for r in rows]
        self.M = (np.frombuffer(b"".join(r[1] for r in rows), np.float32).reshape(len(rows), -1)
                  if rows else np.zeros((0, 768), np.float32))
        self.assets = {r[0]: r[1:] for r in db.execute("select uuid, kind, path, duration, meta from assets")}
        self.loaded_at = time.time()

    def vector_hits(self, query):
        """Best segment score per asset, highest first. self.moment[uuid] = (t0, t1) of that segment
        when it's a detected scene, so callers know *where* in a video the match is."""
        self.moment = {}
        if not len(self.seg_uuid):
            return {}
        s = self.M @ embed_query(query)
        best = {}
        for k, (u, v) in enumerate(zip(self.seg_uuid, s.tolist())):
            if v > best.get(u, -2):
                best[u] = v
                t0, t1, src = self.seg_span[k]
                self.moment[u] = (t0, t1) if src == "scene" else None
        return dict(sorted(best.items(), key=lambda kv: -kv[1]))

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
        top_vec = next(iter(vec.values()), 0)
        floor = top_vec - self.FLOOR_MARGIN
        vec_rank = {u: i for i, u in enumerate(vec)}
        kw_rank = {u: i for i, u in enumerate(kw)}
        out = []
        for u in set(vec) | set(kw):
            if u not in self.assets:
                continue
            akind, path, duration, meta = self.assets[u]
            if kind and not (akind == kind or (kind == "video" and akind == "poster")):
                continue
            if u not in kw and vec.get(u, -2) < floor:
                continue  # weak visual match and no keyword match: drop it instead of showing junk
            rrf = sum(1 / (self.RRF_K + r[u]) for r in (vec_rank, kw_rank) if u in r)
            m = self.moment.get(u)
            out.append(dict(uuid=u, kind=akind, path=path, duration=duration, score=round(rrf * 1000, 2),
                            visual=round(vec.get(u, 0), 3), keyword=u in kw, meta=meta,
                            moment=dict(t0=m[0], t1=m[1]) if m else None))
        out.sort(key=lambda h: -h["score"])
        return out[:k]
