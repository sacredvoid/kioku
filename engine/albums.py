"""Smart albums: describe the photos you want, review a draft, save it as an album.

A short Claude call (no tools) turns the description into search queries; the engine runs them in-process,
fuses the hits and stores them as a draft. Nothing here writes to Photos; a saved album holds pointers.
"""
import datetime
import json
import logging
import re
import subprocess
import threading
import time
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

import core
import projects
import settings

router = APIRouter()
log = logging.getLogger("safelight.albums")

SCHEMA = """
create table if not exists album_drafts(
  id integer primary key, name text not null, description text not null,
  album_id integer references collections(id) on delete set null,
  refining integer not null default 0,
  status text not null default 'open' check(status in ('open', 'saved')),
  round integer not null default 0, created real, updated real);
create table if not exists draft_items(
  draft_id integer not null references album_drafts(id) on delete cascade, uuid text not null,
  state text not null default 'in' check(state in ('in', 'removed')),
  by text not null default 'search' check(by in ('search', 'user', 'claude')),
  query text, why text, note text, round integer not null, score real, hits integer,
  primary key(draft_id, uuid));
create table if not exists draft_runs(
  id integer primary key, draft_id integer not null references album_drafts(id) on delete cascade,
  round integer, prompt text, model text, queries text,
  status text, error text, added integer, started real, finished real);
"""
PER_QUERY, PER_ROUND, PER_DRAFT, MAX_UUIDS = 60, 150, 400, 400
RESTARTED = "Stopped because the engine restarted"
_ready = set()  # db paths that already have the draft tables


def conn():
    db = core.connect()
    if core.DB_PATH not in _ready:
        db.executescript(SCHEMA)
        _ready.add(core.DB_PATH)
    return closing(db)


# ---------- searching and planning (module-level so tests can swap them)

def search(q, k, kind):
    import server  # lazy: server will import this module's router, so a top-level import would be circular
    idx = server.fresh_index()
    if idx is None:
        raise RuntimeError("The search index is still loading. Try again in a few seconds.")
    return idx.search(q, k, kind)


class Q(BaseModel):
    q: str = Field(min_length=1, max_length=200)
    kind: Literal["", "image", "video"] = ""
    why: str = Field("", max_length=80)


class Plan(BaseModel):
    name: str = Field("", max_length=100)
    queries: list[Q] = Field(min_length=1, max_length=20)


def parse_plan(text):
    """Claude's reply -> {"name", "queries": [{q, kind, why}]} (first 8 queries). ValueError if it isn't that."""
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j < i:
        raise ValueError("no JSON object in the reply")
    p = Plan.model_validate_json(text[i:j + 1])  # pydantic's ValidationError is a ValueError
    return {"name": p.name.strip(), "queries": [q.model_dump() for q in p.queries[:8]]}


def claude_text(prompt, model):
    """One tool-less, MCP-less `claude -p` call; returns the reply text."""
    exe = projects.claude_bin()
    if not exe:
        raise RuntimeError("Claude Code CLI not found (expected ~/.local/bin/claude)")
    try:
        r = subprocess.run([exe, "-p", prompt, "--model", model, "--tools", "", "--strict-mcp-config",
                            "--output-format", "json"], capture_output=True, text=True, cwd=str(projects.ENGINE),
                           timeout=settings.get().ask_minutes * 60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude took too long to plan the searches")
    if r.returncode:
        raise RuntimeError(f"claude exited with {r.returncode}: {r.stderr.strip()[-300:]}")
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError("claude printed something unexpected")
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result"))[:300])
    return out.get("result") or ""


PROMPT = """You plan searches over a personal photo library. Reply with JSON only: no prose, no code fences.
{{"name": "short album title", "queries": [{{"q": "...", "kind": "", "why": "short label"}}]}}

Today is {today}. The search matches Apple's labels, people, place names and "Month YYYY" dates, plus what the picture looks like. Example metadata from this library:
{samples}

Rules:
- 4 to 8 queries, one idea per query, concrete words: places, objects, people, activities, and "Month YYYY" when dates are implied (resolve "last summer" from today's date).
- Never use the words photo, photos, picture, image, video: they match everything.
- kind is "" (both), "image" or "video".
- why is a 1 to 3 word label shown on the picture, for example "Cherry blossoms".

{ask}"""


def plan(description, instruction, used, refining_name=None):
    with conn() as c:
        samples = [r[0] for r in c.execute("select meta from assets where meta != '' order by random() limit 3")]
    ask = f"Album request: {description}"
    if refining_name:
        ask += f"\nThis adds to an existing album named {refining_name!r}."
    if instruction:
        ask += f"\nNow the user also asks: {instruction}\nQueries already used, do not repeat them: {used}"
    prompt = PROMPT.format(today=datetime.date.today().isoformat(), ask=ask,
                           samples="\n".join(f"- {m}" for m in samples) or "- (none yet)")
    model, err = settings.get().album_model, None
    for _ in range(2):
        text = claude_text(prompt + (f"\n\nYour last reply was invalid ({err}). Reply with the JSON only." if err else ""), model)
        try:
            return parse_plan(text)
        except ValueError as e:
            err = str(e)[:200]
    raise RuntimeError(f"Claude gave unusable search plans twice ({err})")


def plain_plan(text):
    words = [w for w in re.findall(r"\w+", text) if w.lower() not in core.STOP]
    return {"name": "", "queries": [{"q": " ".join(words) or text, "kind": "", "why": "Search"}]}


def fuse(per_query):
    """[(query dict, hits)] -> candidates best first. A photo several queries agree on outranks a lone strong hit."""
    best = {}
    for qd, hits in per_query:
        for h in hits:
            b = best.setdefault(h["uuid"], dict(uuid=h["uuid"], top=-1.0, n=0, query="", why=""))
            b["n"] += 1
            if h["score"] > b["top"]:
                b.update(top=h["score"], query=qd["q"], why=qd["why"] or qd["q"])
    out = [dict(b, score=b["top"] * (1 + 0.25 * (b["n"] - 1))) for b in best.values()]
    out.sort(key=lambda b: (-b["score"], b["uuid"]))
    return out


# ---------- rounds: plan, search, fuse, store (one background thread per draft)

jobs: dict[int, dict] = {}
jobs_lock = threading.Lock()


def start_round(did, instruction, plain):
    with jobs_lock:
        if jobs.get(did, {}).get("status") == "running":
            raise HTTPException(409, "a search is already running for this draft")
        jobs[did] = {"status": "running", "started": time.time(), "step": "Starting"}

    def run():
        j = jobs[did]
        try:
            added, note = do_round(did, instruction, plain, j)
            j.update(status="done", added=added, note=note, step="")
        except Exception as e:  # report, don't crash the server
            log.error("draft %d round failed: %s", did, e, exc_info=not isinstance(e, RuntimeError))
            msg = str(e) if isinstance(e, RuntimeError) else f"Unexpected error ({type(e).__name__}): {e}"
            j.update(status="failed", error=msg[:1000], step="")
    threading.Thread(target=run, daemon=True).start()


def do_round(did, instruction, plain, job):
    t = time.time()
    with conn() as c:
        d = c.execute("select description, round, refining, name from album_drafts where id = ?", (did,)).fetchone()
        if not d:
            raise RuntimeError("That draft was discarded.")
        desc, rnd, refining, dname = d[0], d[1] + 1, d[2], d[3]
        used = [q["q"] for r in c.execute("select queries from draft_runs where draft_id = ? and queries is not null", (did,))
                for q in json.loads(r[0])]
        run_id = c.execute("insert into draft_runs(draft_id, round, prompt, model, status, started) "
                           "values (?, ?, ?, ?, 'running', ?)", (did, rnd, instruction or desc, settings.get().album_model, t)).lastrowid
        c.execute("update album_drafts set round = ?, updated = ? where id = ?", (rnd, t, did))
        c.commit()
    status, note, added, queries, err = "failed", None, 0, [], None
    try:
        job["step"] = "Planning searches"
        if plain:
            p = plain_plan(instruction or desc)
        else:
            try:
                p = plan(desc, instruction, used, dname if refining else None)
            except Exception as e:
                log.warning("planner failed for draft %d, using plain search: %s", did, e)
                note, p = "Claude unavailable, used plain search", plain_plan(instruction or desc)
        queries = p["queries"]
        per, seen = [], set()
        for qd in queries:
            job["step"] = f"Searching “{qd['q']}” (+{len(seen)})"
            hits = search(qd["q"], PER_QUERY, qd["kind"] or None)
            per.append((qd, hits))
            seen.update(h["uuid"] for h in hits)
        cands = fuse(per)
        with conn() as c:
            have = {r[0] for r in c.execute("select uuid from draft_items where draft_id = ?", (did,))}
            room = max(0, PER_DRAFT - c.execute("select count(*) from draft_items where draft_id = ? and round > 0", (did,)).fetchone()[0])
            new = [x for x in cands if x["uuid"] not in have][:min(PER_ROUND, room)]
            before = c.total_changes
            c.executemany("insert or ignore into draft_items(draft_id, uuid, query, why, round, score, hits) values (?,?,?,?,?,?,?)",
                          [(did, x["uuid"], x["query"], x["why"], rnd, x["score"], x["n"]) for x in new])
            added = c.total_changes - before
            if rnd == 1 and p["name"] and not refining:
                c.execute("update album_drafts set name = ? where id = ?", (p["name"][:100], did))
            c.commit()
        status = "done"
        log.info("draft %d round %d: %d queries, %d new, model %s, %.1fs", did, rnd, len(queries), added,
                 settings.get().album_model, time.time() - t)
        return added, note
    except Exception as e:
        err = str(e)[:500]
        raise
    finally:
        try:
            with conn() as c:
                c.execute("update draft_runs set status = ?, error = ?, queries = ?, added = ?, finished = ? where id = ?",
                          (status, err or note, json.dumps(queries), added, time.time(), run_id))
                c.commit()
        except Exception:
            log.exception("could not close run %d", run_id)


def job_snapshot(c, did):
    """Live job, or (after an engine restart) a stale 'running' run closed as failed."""
    j = jobs.get(did)
    if j:
        return dict(j)
    if c.execute("update draft_runs set status = 'failed', error = ?, finished = ? where draft_id = ? and status = 'running'",
                 (RESTARTED, time.time(), did)).rowcount:
        c.commit()
        return {"status": "failed", "error": RESTARTED}
    return {"status": "idle"}


# ---------- endpoints

DRAFT_COLS = ("id", "name", "description", "album_id", "refining", "status", "round", "created", "updated")


def require_draft(c, did):
    row = c.execute(f"select {', '.join(DRAFT_COLS)} from album_drafts where id = ?", (did,)).fetchone()
    if not row:
        raise HTTPException(404, "no such draft")
    return dict(zip(DRAFT_COLS, row))


def counts(c, did):
    kept, removed = c.execute("select coalesce(sum(state = 'in'), 0), coalesce(sum(state = 'removed'), 0) "
                              "from draft_items where draft_id = ?", (did,)).fetchone()
    return {"kept": kept, "removed": removed}


class NewDraft(BaseModel):
    description: str
    album_id: int | None = None

    @field_validator("description")
    @classmethod
    def clean(cls, v):
        v = v.strip()
        if not 3 <= len(v) <= 2000:
            raise ValueError("describe the photos in 3 to 2000 characters")
        return v


@router.post("/drafts", status_code=202)
def create_draft(body: NewDraft, plain: bool = Query(False)):
    now = time.time()
    with conn() as c:
        name = body.description[:60].strip()
        if body.album_id is not None:
            alb = c.execute("select name, kind from collections where id = ?", (body.album_id,)).fetchone()
            if not alb or alb[1] != "album":
                raise HTTPException(422, "album_id must be an existing album")
            name = alb[0]
        did = c.execute("insert into album_drafts(name, description, album_id, refining, created, updated) values (?,?,?,?,?,?)",
                        (name, body.description, body.album_id, body.album_id is not None, now, now)).lastrowid
        if body.album_id is not None:
            c.execute("insert into draft_items(draft_id, uuid, why, round) "
                      "select ?, uuid, 'Already in album', 0 from collection_items where collection_id = ? group by uuid",
                      (did, body.album_id))
        c.commit()
    start_round(did, "", plain)
    return get_draft(did)


@router.get("/drafts")
def list_drafts():
    with conn() as c:
        rows = c.execute(
            "select d.id, d.name, d.description, d.album_id, d.updated, coalesce(sum(i.state = 'in'), 0) "
            "from album_drafts d left join draft_items i on i.draft_id = d.id where d.status = 'open' "
            "group by d.id order by d.updated desc").fetchall()
        return [dict(zip(("id", "name", "description", "album_id", "updated", "kept"), r),
                     running=jobs.get(r[0], {}).get("status") == "running") for r in rows]


@router.get("/drafts/{did}")
def get_draft(did: int):
    with conn() as c:
        d = require_draft(c, did)
        d.update(counts(c, did))
        d["items"] = [dict(zip(("uuid", "state", "by", "query", "why", "note", "round", "score", "hits", "kind", "duration", "meta", "created"), r))
                      for r in c.execute(
            "select i.uuid, i.state, i.by, i.query, i.why, i.note, i.round, i.score, i.hits, a.kind, a.duration, a.meta, a.created "
            "from draft_items i join assets a using(uuid) where i.draft_id = ? "
            "order by coalesce(i.score, 1e18) desc, i.uuid", (did,))]
        d["job"] = job_snapshot(c, did)
        d["runs"] = [dict(zip(("round", "prompt", "queries", "status", "error", "added"), r)) for r in c.execute(
            "select round, prompt, queries, status, error, added from draft_runs where draft_id = ? order by id desc limit 5", (did,))]
        return d


@router.get("/drafts/{did}/job")
def get_job(did: int):
    with conn() as c:
        require_draft(c, did)
        return job_snapshot(c, did) | counts(c, did)


class Again(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)


@router.post("/drafts/{did}/search", status_code=202)
def search_again(did: int, body: Again):
    with conn() as c:
        if require_draft(c, did)["status"] != "open":
            raise HTTPException(409, "this draft was already saved")
    start_round(did, body.prompt.strip(), False)
    return get_job(did)


class ItemsPatch(BaseModel):
    uuids: list[str] = Field(min_length=1, max_length=MAX_UUIDS)
    state: Literal["in", "removed"]


@router.patch("/drafts/{did}/items")
def patch_items(did: int, body: ItemsPatch):
    uuids = list(dict.fromkeys(body.uuids))
    with conn() as c:
        require_draft(c, did)
        marks = ",".join("?" * len(uuids))
        known = {r[0] for r in c.execute(f"select uuid from draft_items where draft_id = ? and uuid in ({marks})", (did, *uuids))}
        if len(known) != len(uuids):
            raise HTTPException(422, f"not in this draft: {[u for u in uuids if u not in known][:5]}")
        changed = c.execute(f"update draft_items set state = ?, by = 'user' where draft_id = ? and state != ? and uuid in ({marks})",
                            (body.state, did, body.state, *uuids)).rowcount  # a no-op change keeps `by` as it was
        c.execute("update album_drafts set updated = ? where id = ?", (time.time(), did))
        c.commit()
        return {"changed": changed} | counts(c, did)


class Rename(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@router.patch("/drafts/{did}")
def rename_draft(did: int, body: Rename):
    with conn() as c:
        require_draft(c, did)
        c.execute("update album_drafts set name = ?, updated = ? where id = ?", (body.name.strip(), time.time(), did))
        c.commit()
        return require_draft(c, did)


class Save(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    as_new: bool = False  # save as a new album even though the refined one was deleted


@router.post("/drafts/{did}/save")
def save_draft(did: int, body: Save = Save()):
    if jobs.get(did, {}).get("status") == "running":
        raise HTTPException(409, "a search is still running; wait for it to finish")
    gone = HTTPException(409, "that album was deleted, save as a new album instead")
    with conn() as c:
        c.execute("begin immediate")  # one writer at a time: two Saves can't both create an album. Closing without commit rolls back.
        d = require_draft(c, did)
        refine = bool(d["refining"]) and not body.as_new
        if d["status"] == "saved":
            if d["album_id"] is None:
                raise gone
            return {"collection_id": d["album_id"]}
        cid = d["album_id"] if refine else None
        if refine and (cid is None or not c.execute("select 1 from collections where id = ?", (cid,)).fetchone()):
            raise gone
        now = time.time()
        name = (body.name or d["name"]).strip()
        kept = [r[0] for r in c.execute("select i.uuid from draft_items i join assets a using(uuid) "
                                        "where i.draft_id = ? and i.state = 'in' order by a.created, i.uuid", (did,))]
        if refine:
            removed = [(cid, r[0]) for r in c.execute("select uuid from draft_items where draft_id = ? and state = 'removed'", (did,))]
            c.executemany("delete from collection_items where collection_id = ? and uuid = ?", removed)
            have = {r[0] for r in c.execute("select uuid from collection_items where collection_id = ?", (cid,))}
            kept = [u for u in kept if u not in have]
            pos = c.execute("select coalesce(max(pos), 0) from collection_items where collection_id = ?", (cid,)).fetchone()[0]
            c.execute("update collections set name = ?, updated = ? where id = ?", (name, now, cid))
        else:
            cid = c.execute("insert into collections(name, kind, created, updated) values (?, 'album', ?, ?)", (name, now, now)).lastrowid
            pos = 0
        c.executemany("insert into collection_items(collection_id, uuid, pos) values (?, ?, ?)",
                      [(cid, u, pos + i + 1) for i, u in enumerate(kept)])
        c.execute("update album_drafts set status = 'saved', album_id = ?, updated = ? where id = ?", (cid, now, did))
        c.commit()
        return {"collection_id": cid, "added": len(kept)}


@router.delete("/drafts/{did}", status_code=204)
def discard_draft(did: int):
    """Discards the draft in any state. Photos and saved albums are untouched."""
    with conn() as c:
        require_draft(c, did)
        c.execute("delete from album_drafts where id = ?", (did,))
        c.commit()
    jobs.pop(did, None)
