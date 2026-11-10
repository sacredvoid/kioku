"""Your layer on top of Photos: tags, captions, albums and video projects.

Everything here lives in data/index.db. The Photos library is never written to, and
albums/projects hold pointers (Photos uuid + optional in/out seconds), never copies.
"""
import time
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

import core

router = APIRouter()
ASSET_COLS = ("uuid", "kind", "path", "duration", "meta", "created")


def validation_error(request, exc: RequestValidationError):
    """422 body without the echoed input: FastAPI's default includes it, and a NaN or inf input cannot be
    serialized, which turned a bad request into a 500. The app reads only loc and msg."""
    return JSONResponse(status_code=422, content={"detail": [{k: e.get(k) for k in ("loc", "msg", "type")} for e in exc.errors()]})


def conn():
    return closing(core.connect())


def require_asset(c, uuid):
    row = c.execute(f"select {', '.join(ASSET_COLS)} from assets where uuid = ?", (uuid,)).fetchone()
    if not row:
        raise HTTPException(404, "not indexed")
    return dict(zip(ASSET_COLS, row))


def require_collection(c, cid):
    row = c.execute("select id, name, kind, brief, created, updated from collections where id = ?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "no such collection")
    return dict(zip(("id", "name", "kind", "brief", "created", "updated"), row))


def touch(c, cid):
    c.execute("update collections set updated = ? where id = ?", (time.time(), cid))


def renumber(c, cid):
    """Positions back to 1..n in order. Midpoints halve the gap on every move and run out of float precision after ~50."""
    c.execute("update collection_items set pos = r from (select id, row_number() over (order by pos, id) r "
              "from collection_items where collection_id = ?) n where collection_items.id = n.id", (cid,))


# ---------- assets: details, tags, caption

@router.get("/asset/{uuid}")
def asset(uuid: str):
    with conn() as c:
        a = require_asset(c, uuid)
        info = c.execute("select place, lat, lon, albums, local_original from asset_info where uuid = ?", (uuid,)).fetchone()
        a.update(dict(zip(("place", "lat", "lon", "photos_albums", "local_original"), info or (None,) * 5)))
        a["photos_albums"] = [x for x in (a["photos_albums"] or "").split(" ; ") if x]
        a["tags"] = [r[0] for r in c.execute("select tag from tags where uuid = ? order by tag", (uuid,))]
        a["caption"] = (c.execute("select caption from notes where uuid = ?", (uuid,)).fetchone() or [""])[0]
        a["collections"] = [dict(zip(("id", "name", "kind"), r)) for r in c.execute(
            "select distinct c.id, c.name, c.kind from collections c join collection_items i on i.collection_id = c.id "
            "where i.uuid = ? order by c.name", (uuid,))]
        return a


class Tags(BaseModel):
    tags: list[str] = Field(max_length=50)

    @field_validator("tags")
    @classmethod
    def clean(cls, v):
        out, seen = [], set()
        for t in v:
            t = " ".join(t.split())
            if not 1 <= len(t) <= 40:
                raise ValueError("each tag must be 1 to 40 characters")
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out


@router.put("/asset/{uuid}/tags")
def set_tags(uuid: str, body: Tags):
    with conn() as c:
        require_asset(c, uuid)
        c.execute("delete from tags where uuid = ?", (uuid,))
        c.executemany("insert into tags(uuid, tag) values (?, ?)", [(uuid, t) for t in body.tags])
        core.refresh_fts(c, uuid)
        c.commit()
    return {"tags": body.tags}


class Caption(BaseModel):
    caption: str = Field(max_length=2000)


@router.put("/asset/{uuid}/caption")
def set_caption(uuid: str, body: Caption):
    cap = body.caption.strip()
    with conn() as c:
        require_asset(c, uuid)
        if cap:
            c.execute("insert or replace into notes(uuid, caption) values (?, ?)", (uuid, cap))
        else:
            c.execute("delete from notes where uuid = ?", (uuid,))
        core.refresh_fts(c, uuid)
        c.commit()
    return {"caption": cap}


@router.get("/tags")
def all_tags():
    """Your tags with counts, for autocomplete."""
    with conn() as c:
        return [{"tag": t, "count": n} for t, n in c.execute(
            "select tag, count(*) from tags group by tag order by count(*) desc, tag limit 500")]


# ---------- collections: albums and projects (pointers only)

class NewCollection(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["album", "project"]


class CollectionPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    brief: str | None = Field(None, max_length=10000)


@router.get("/collections")
def list_collections(kind: Literal["album", "project"] | None = None):
    with conn() as c:
        rows = c.execute(
            "select c.id, c.name, c.kind, c.updated, count(i.id), "
            "(select uuid from collection_items where collection_id = c.id order by pos limit 1) "
            "from collections c left join collection_items i on i.collection_id = c.id "
            + ("where c.kind = ? " if kind else "") + "group by c.id order by c.updated desc",
            (kind,) if kind else ()).fetchall()
        # collage covers: first, middle and last clip, so one dull opening frame doesn't stand for the project
        covers: dict[int, list[str]] = {}
        for cid, uuid in c.execute(
                "select collection_id, uuid from (select collection_id, uuid, pos, "
                "row_number() over (partition by collection_id order by pos) rn, "
                "count(*) over (partition by collection_id) n from collection_items) "
                "where rn = 1 or rn = (n + 1) / 2 or rn = n order by collection_id, pos"):
            if uuid not in covers.setdefault(cid, []):
                covers[cid].append(uuid)
        return [dict(zip(("id", "name", "kind", "updated", "count", "cover"), r), covers=covers.get(r[0], []))
                for r in rows]


@router.post("/collections", status_code=201)
def create_collection(body: NewCollection):
    now = time.time()
    with conn() as c:
        cid = c.execute("insert into collections(name, kind, created, updated) values (?, ?, ?, ?)",
                        (body.name.strip(), body.kind, now, now)).lastrowid
        c.commit()
        return require_collection(c, cid)


@router.get("/collections/{cid}")
def get_collection(cid: int):
    with conn() as c:
        col = require_collection(c, cid)
        col["items"] = [dict(zip(("item_id", "uuid", "pos", "t0", "t1", "kind", "duration", "meta", "created"), r))
                        for r in c.execute(
            "select i.id, i.uuid, i.pos, i.t0, i.t1, a.kind, a.duration, a.meta, a.created "
            "from collection_items i join assets a using(uuid) where i.collection_id = ? order by i.pos", (cid,))]
        return col


@router.patch("/collections/{cid}")
def update_collection(cid: int, body: CollectionPatch):
    with conn() as c:
        require_collection(c, cid)
        if body.name is not None:
            c.execute("update collections set name = ? where id = ?", (body.name.strip(), cid))
        if body.brief is not None:
            c.execute("update collections set brief = ? where id = ?", (body.brief, cid))
        touch(c, cid)
        c.commit()
        return require_collection(c, cid)


@router.delete("/collections/{cid}", status_code=204)
def delete_collection(cid: int):
    """Deletes the collection and its pointers. Your photos are untouched."""
    with conn() as c:
        require_collection(c, cid)
        c.execute("delete from collections where id = ?", (cid,))  # items cascade
        c.commit()


class AddItems(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    uuids: list[str] = Field(min_length=1, max_length=5000)
    pos: float | None = None  # insert here (same scale as PATCH pos) instead of appending; ties go after existing items


@router.post("/collections/{cid}/items")
def add_items(cid: int, body: AddItems):
    with conn() as c:
        col = require_collection(c, cid)
        known = {r[0] for r in c.execute(
            f"select uuid from assets where uuid in ({','.join('?' * len(body.uuids))})", body.uuids)}
        missing = [u for u in body.uuids if u not in known]
        if missing:
            raise HTTPException(422, f"not indexed: {missing[:5]}")
        have = {r[0] for r in c.execute("select uuid from collection_items where collection_id = ?", (cid,))}
        # albums hold each item once; projects may reuse a clip with different in/out points
        new = list(body.uuids) if col["kind"] == "project" else [u for u in dict.fromkeys(body.uuids) if u not in have]
        if body.pos is None:
            pos = (c.execute("select max(pos) from collection_items where collection_id = ?", (cid,)).fetchone()[0] or 0)
            c.executemany("insert into collection_items(collection_id, uuid, pos) values (?, ?, ?)",
                          [(cid, u, pos + i + 1) for i, u in enumerate(new)])
        else:
            c.executemany("insert into collection_items(collection_id, uuid, pos) values (?, ?, ?)",
                          [(cid, u, body.pos + i * 1e-6) for i, u in enumerate(new)])
            renumber(c, cid)
        touch(c, cid)
        c.commit()
        return {"added": len(new), "skipped": len(body.uuids) - len(new)}


class ItemPatch(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)  # NaN passed ge=0 and then broke the JSON reply (500)
    t0: float | None = Field(None, ge=0)
    t1: float | None = Field(None, ge=0)
    pos: float | None = None


@router.patch("/collections/{cid}/items/{iid}")
def update_item(cid: int, iid: int, body: ItemPatch):
    with conn() as c:
        require_collection(c, cid)
        row = c.execute("select i.t0, i.t1, a.duration from collection_items i join assets a using(uuid) "
                        "where i.id = ? and i.collection_id = ?", (iid, cid)).fetchone()
        if not row:
            raise HTTPException(404, "no such item")
        # an explicit null clears the trim (back to the clip's start/end); a missing field keeps it
        t0 = body.t0 if "t0" in body.model_fields_set else row[0]
        t1 = body.t1 if "t1" in body.model_fields_set else row[1]
        dur = row[2] or 0
        if t0 is not None and t1 is not None and t1 <= t0:
            raise HTTPException(422, "out point must be after in point")
        if dur and any(t is not None and t > dur + 0.05 for t in (t0, t1)):
            raise HTTPException(422, f"clip is only {dur:.1f}s long")
        c.execute("update collection_items set t0 = ?, t1 = ?, pos = coalesce(?, pos) where id = ?",
                  (t0, t1, body.pos, iid))
        if body.pos is not None:
            renumber(c, cid)
        touch(c, cid)
        c.commit()
        return {"item_id": iid, "t0": t0, "t1": t1}


class ItemReplace(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    uuid: str | None = None
    t0: float | None = Field(None, ge=0)
    t1: float | None = Field(None, ge=0)


@router.put("/collections/{cid}/items/{iid}")
def replace_item(cid: int, iid: int, body: ItemReplace):
    """Swap the source of a clip in place: item_id and pos stay, so its edit settings (speed, look, transition...) carry over.
    A new uuid starts untrimmed unless t0/t1 are given; without uuid this trims like PATCH."""
    with conn() as c:
        require_collection(c, cid)
        row = c.execute("select i.uuid, i.t0, i.t1, a.kind from collection_items i join assets a using(uuid) "
                        "where i.id = ? and i.collection_id = ?", (iid, cid)).fetchone()
        if not row:
            raise HTTPException(404, "no such item")
        uuid, t0, t1 = row[0], row[1], row[2]
        new = row[0]
        if body.uuid is not None and body.uuid != uuid:
            a = c.execute("select kind, duration from assets where uuid = ?", (body.uuid,)).fetchone()
            if not a:
                raise HTTPException(422, f"not indexed: {body.uuid}")
            if (a[0] == "image") != (row[3] == "image"):
                raise HTTPException(422, "a photo can only be swapped for a photo, and a video for a video")
            new, t0, t1 = body.uuid, None, None
        dur = (c.execute("select duration from assets where uuid = ?", (new,)).fetchone() or [0])[0] or 0
        if "t0" in body.model_fields_set:
            t0 = body.t0
        if "t1" in body.model_fields_set:
            t1 = body.t1
        if t0 is not None and t1 is not None and t1 <= t0:
            raise HTTPException(422, "out point must be after in point")
        if dur and any(t is not None and t > dur + 0.05 for t in (t0, t1)):
            raise HTTPException(422, f"clip is only {dur:.1f}s long")
        c.execute("update collection_items set uuid = ?, t0 = ?, t1 = ? where id = ?", (new, t0, t1, iid))
        touch(c, cid)
        c.commit()
        return {"item_id": iid, "uuid": new, "t0": t0, "t1": t1}


@router.delete("/collections/{cid}/items/{iid}", status_code=204)
def remove_item(cid: int, iid: int):
    with conn() as c:
        require_collection(c, cid)
        if not c.execute("delete from collection_items where id = ? and collection_id = ?", (iid, cid)).rowcount:
            raise HTTPException(404, "no such item")
        touch(c, cid)
        c.commit()
