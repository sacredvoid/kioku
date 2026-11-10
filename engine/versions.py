"""Saved versions of a project's edit: the clip list plus the edit spec, so any earlier cut can be loaded back.

Saved automatically after every Claude edit, and before one when you changed the edit by hand since the last save.
Loading a version first saves the current edit (if it isn't saved already), so loading never loses work.
Stored id-free: items as [uuid, t0, t1] in order, spec clip keys as list positions. Loading inserts fresh items and
maps the positions back onto their new ids."""
import hashlib
import json
import logging
import time

from fastapi import APIRouter, HTTPException

import edit
import library

log = logging.getLogger("kioku")
router = APIRouter()

SCHEMA = """create table if not exists edit_versions(
  id integer primary key, collection_id integer not null references collections(id) on delete cascade,
  n integer not null, label text not null, state text not null, hash text not null, created real not null)"""
KEEP = 50          # per project; the oldest go first
KEYED = ("clips", "transitions", "sfx")  # spec maps keyed by str(item_id)


def _current(c, cid):
    """(state dict, hash) of the project's edit as it is now, read on connection c."""
    row = c.execute("select kind from collections where id = ?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "collection not found")
    if row[0] != "project":
        raise HTTPException(422, "only projects have versions")
    rows = c.execute("select id, uuid, t0, t1 from collection_items where collection_id = ? order by pos", (cid,)).fetchall()
    col = {"id": cid, "kind": "project", "items": [{"item_id": r[0]} for r in rows]}
    spec, _ = edit._read(c, col)
    pos = {str(r[0]): str(i) for i, r in enumerate(rows)}
    for k in KEYED:
        spec[k] = {pos[i]: v for i, v in spec[k].items() if i in pos}
    state = {"items": [[r[1], r[2], r[3]] for r in rows], "spec": spec}
    text = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return state, hashlib.sha1(text.encode()).hexdigest()


def _save(c, cid, label):
    """Save the current edit unless it is empty or identical to a saved version. Returns the new id or None."""
    state, h = _current(c, cid)
    if not state["items"] or c.execute("select 1 from edit_versions where collection_id = ? and hash = ?", (cid, h)).fetchone():
        return None
    n = c.execute("select coalesce(max(n), 0) + 1 from edit_versions where collection_id = ?", (cid,)).fetchone()[0]  # stable: never reused
    vid = c.execute("insert into edit_versions(collection_id, n, label, state, hash, created) values (?,?,?,?,?,?)",
                    (cid, n, label[:200], json.dumps(state, separators=(",", ":")), h, time.time())).lastrowid
    c.execute("delete from edit_versions where collection_id = ? and id not in "
              "(select id from edit_versions where collection_id = ? order by id desc limit ?)", (cid, cid, KEEP))
    return vid


def number(cid, label):
    """The version number of the edit as it is now, saving it under `label` if it isn't saved yet (renders name their
    file after it). None if that fails or the edit is empty: a render must never fail over its file name."""
    try:
        with edit.conn() as c:
            c.execute(SCHEMA)
            c.execute("begin immediate")
            try:
                _save(c, cid, label)
                _, h = _current(c, cid)
                row = c.execute("select n from edit_versions where collection_id = ? and hash = ?", (cid, h)).fetchone()
                c.commit()
                return row[0] if row else None
            except BaseException:
                c.rollback()
                raise
    except Exception:
        log.exception("could not find the version of collection %d", cid)
        return None


def save(cid, label):
    """For the ask job: never let a version save break the edit itself."""
    try:
        with edit.conn() as c:
            c.execute(SCHEMA)
            c.execute("begin immediate")
            try:
                vid = _save(c, cid, label)
                c.commit()
                return vid
            except BaseException:
                c.rollback()
                raise
    except Exception:
        log.exception("could not save a version of collection %d", cid)
        return None


@router.get("/collections/{cid}/versions")
def list_versions(cid: int):
    """Newest first. current: this version is exactly what the edit is now."""
    with edit.conn() as c:
        c.execute(SCHEMA)
        _, h = _current(c, cid)
        rows = c.execute("select id, n, label, hash, created, state from edit_versions where collection_id = ? order by n desc",
                         (cid,)).fetchall()
    return [{"id": r[0], "n": r[1], "label": r[2], "created": r[4], "current": r[3] == h, "clips": len(json.loads(r[5])["items"])}
            for r in rows]


def _ask_running(cid):
    import projects  # lazy: projects imports this module
    return projects.jobs.get((cid, "ask"), {}).get("status") == "running"


@router.post("/collections/{cid}/versions/{vid}/load")
def load_version(cid: int, vid: int):
    if _ask_running(cid):
        raise HTTPException(409, "Claude is editing this project; load a version when it finishes")
    with edit.conn() as c:
        c.execute(SCHEMA)
        c.execute("begin immediate")
        try:
            row = c.execute("select label, state from edit_versions where id = ? and collection_id = ?", (vid, cid)).fetchone()
            if not row:
                raise HTTPException(404, "version not found")
            _save(c, cid, "Your changes")  # data-loss guard: the edit being replaced stays loadable
            state = json.loads(row[1])
            have = {u for (u,) in c.execute("select uuid from assets where uuid in (%s)" % ",".join("?" * len(state["items"])),
                                            [i[0] for i in state["items"]])} if state["items"] else set()
            c.execute("delete from collection_items where collection_id = ?", (cid,))
            ids, skipped = {}, 0
            for k, (uuid, t0, t1) in enumerate(state["items"]):
                if uuid not in have:  # removed from Photos since: skip it, keep the rest
                    skipped += 1
                    continue
                ids[str(k)] = str(c.execute("insert into collection_items(collection_id, uuid, pos, t0, t1) values (?,?,?,?,?)",
                                            (cid, uuid, float(len(ids) + 1), t0, t1)).lastrowid)
            spec = state["spec"]
            for key in KEYED:
                spec[key] = {ids[p]: v for p, v in spec.get(key, {}).items() if p in ids}
            rev = c.execute("select rev from edit_specs where collection_id = ?", (cid,)).fetchone()
            c.execute("insert or replace into edit_specs(collection_id, spec, rev, updated) values (?,?,?,?)",
                      (cid, json.dumps(spec, separators=(",", ":")), (rev[0] if rev else 0) + 1, time.time()))
            library.touch(c, cid)
            c.commit()
        except BaseException:
            c.rollback()
            raise
    return {"loaded": vid, "label": row[0], "skipped": skipped}
