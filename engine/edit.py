"""Edit spec for video projects: storage, validation, timeline math, looks, routes (notes/editing-design.md).

The spec is plain JSON (enums and numbers only, never ffmpeg strings or paths) in the lazily created
edit_specs table. compute_timeline is pure and works in whole frames; render and captions only read Timeline.
"""
import hashlib
import json
import math
import time
from contextlib import closing
from dataclasses import dataclass, fields
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

import core
import library
from projects import STILL_SECONDS
from timeline import Seg, Timeline

router = APIRouter()

EDIT_SCHEMA = """create table if not exists edit_specs(
  collection_id integer primary key references collections(id) on delete cascade,
  spec text not null, rev integer not null default 0, updated real)"""
MAX_SPEC_BYTES = 100_000
ASPECTS = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:5": (4, 5)}
SIZES = (720, 1080, 2160)
FPS = (24, 25, 30, 60)
CAPTION_STYLES = ("bold", "box")
XFADE = {"dissolve": "fade", "fade-black": "fadeblack", "wipe-left": "wipeleft",
         "wipe-right": "wiperight", "slide-left": "slideleft"}
MIN_TRANS_FRAMES = 3
SHORT_CLIP = 0.2


# ---------- looks: parametric presets at strength 1; strength k scales every parameter's distance from identity

@dataclass(frozen=True)
class Look:
    contrast: float = 1.0
    saturation: float = 1.0
    brightness: float = 0.0
    rs: float = 0.0   # colorbalance shadows / midtones, red and blue
    bs: float = 0.0
    rm: float = 0.0
    bm: float = 0.0


# ponytail: no curves=preset (it cannot scale with strength); lut3d is the upgrade for real film LUTs.
LOOKS = {
    "warm": Look(saturation=1.08, rs=0.06, bs=-0.06, rm=0.04, bm=-0.04),
    "cool": Look(saturation=0.98, rs=-0.05, bs=0.06, rm=-0.03, bm=0.04),
    "film": Look(contrast=1.08, saturation=0.88, rs=0.05, bs=0.03, rm=0.02, bm=-0.02),
    "mono": Look(contrast=1.1, saturation=0.0),
    "punch": Look(contrast=1.15, saturation=1.3),
}
_IDENT = Look()


def _num(x):
    return format(round(x, 3) + 0.0, "g")  # +0.0 turns -0.0 into 0.0


def look_filters(look):
    """(name, strength) -> ffmpeg filter text, '' for none or identity. Numbers only, safe to inline."""
    if not look or not look[0]:
        return ""
    name, k = look
    p = LOOKS[name]
    v = {f.name: getattr(_IDENT, f.name) + (getattr(p, f.name) - getattr(_IDENT, f.name)) * k for f in fields(Look)}
    parts = []
    for flt, keys in (("eq", ("contrast", "saturation", "brightness")), ("colorbalance", ("rs", "bs", "rm", "bm"))):
        args = [f"{x}={_num(v[x])}" for x in keys if _num(v[x]) != _num(getattr(_IDENT, x))]
        if args:
            parts.append(f"{flt}=" + ":".join(args))
    return ",".join(parts)


# ---------- spec

def default_spec():
    return {"version": 1, "format": {"aspect": "16:9", "size": 1080, "fps": 30},
            "look": {"name": None, "strength": 1.0}, "clips": {}, "transitions": {},
            "captions": {"on": False, "style": "bold"}}


def output_size(aspect, size, preview):
    """(width, height), both even. Preview short side is at most 540."""
    if aspect not in ASPECTS:
        raise ValueError(f"aspect must be one of {list(ASPECTS)}")
    short = min(size, 540) if preview else size
    w, h = ASPECTS[aspect]
    even = lambda x: 2 * round(x / 2)  # noqa: E731
    return (even(short * w / h), short) if w >= h else (short, even(short * h / w))


def conn():
    c = core.connect()
    c.execute(EDIT_SCHEMA)
    return closing(c)


def _prune(spec, col):
    ids = {str(i["item_id"]) for i in col["items"]}
    for k in ("clips", "transitions"):
        spec[k] = {i: v for i, v in spec[k].items() if i in ids}


def _read(c, col):
    row = c.execute("select spec, rev from edit_specs where collection_id = ?", (col["id"],)).fetchone()
    spec, rev = (json.loads(row[0]), row[1]) if row else (default_spec(), 0)
    _prune(spec, col)
    return spec, rev


def _require_project(col):
    if col["kind"] != "project":
        raise HTTPException(422, "only projects have an edit")


def load(cid):
    """(spec with orphans pruned, rev). No row means the default spec."""
    col = library.get_collection(cid)
    _require_project(col)
    with conn() as c:
        return _read(c, col)


def mutate(cid, fn):
    """Atomic read-modify-write. fn(spec, collection) edits spec in place and returns a list of clamp notes
    ({"field", "from", "to"}); it may raise HTTPException(422), which rolls everything back."""
    with conn() as c:
        c.execute("begin immediate")  # writers queue here, so parallel tool calls cannot lose updates
        try:
            col = library.get_collection(cid)
            _require_project(col)
            spec, rev = _read(c, col)
            notes = fn(spec, col) or []
            _prune(spec, col)
            text = json.dumps(spec, separators=(",", ":"))
            if len(text) > MAX_SPEC_BYTES:
                raise HTTPException(422, f"edit is too large ({len(text)} bytes, limit {MAX_SPEC_BYTES})")
            c.execute("insert or replace into edit_specs(collection_id, spec, rev, updated) values (?,?,?,?)",
                      (cid, text, rev + 1, time.time()))
            library.touch(c, cid)
            c.commit()
        except BaseException:
            c.rollback()
            raise
    return {"spec": spec, "rev": rev + 1, "clamped": notes}


def _clamp(notes, field, v, lo, hi):
    if math.isnan(v):
        raise HTTPException(422, f"{field} must be a number")
    nv = min(max(v, lo), hi)
    if nv != v:
        notes.append({"field": field, "from": v, "to": nv})
    return nv


# ---------- timeline (pure)

def compute_timeline(items, spec):
    fps = spec["format"]["fps"]
    warnings, raw = [], []
    for it in items:
        e = spec["clips"].get(str(it["item_id"]), {})
        photo = it["kind"] == "image"
        t0 = it["t0"] or 0.0
        if photo:
            speed, a, b = 1.0, 0.0, (it["t1"] - t0 if it["t1"] is not None else STILL_SECONDS)
        else:
            speed = e.get("speed", 1.0)
            a = t0
            b = it["t1"] if it["t1"] is not None else (it["duration"] or STILL_SECONDS)
        n = max(1, round((b - a) / speed * fps))
        adj, rf = e.get("adjust", {}), e.get("reframe", {})
        pos, pf, pt = rf.get("pos", .5), rf.get("pos_from", .5), rf.get("pos_to", .5)
        reframe = {"center": ("center", .5, .5), "manual": ("manual", pos, pos),
                   "pan": ("pan", pf, pt)}[rf.get("mode", "center")]
        lk = e["look"] if "look" in e else spec["look"]
        raw.append(dict(item_id=it["item_id"], uuid=it["uuid"], kind="photo" if photo else "video",
                        src_in=a, src_out=b, speed=speed, volume=e.get("volume", 1.0),
                        adjust=(adj.get("exposure", 0.0), adj.get("contrast", 1.0), adj.get("saturation", 1.0),
                                adj.get("warmth", 0.0)),
                        look=(lk["name"], lk.get("strength", 1.0)) if lk["name"] else None,
                        reframe=reframe, frames=n))
        if n / fps < SHORT_CLIP:
            warnings.append(f"clip {len(raw)} is only {n / fps:.2f}s long")
    tf = [0] * len(raw)  # transition frames after each clip
    for i in range(len(raw) - 1):
        t = spec["transitions"].get(str(raw[i]["item_id"]))
        if not t or t["type"] == "cut":
            continue
        want = round(t["duration"] * fps)
        got = min(want, raw[i]["frames"] // 2, raw[i + 1]["frames"] // 2, raw[i]["frames"] - (tf[i - 1] if i else 0))
        if got < MIN_TRANS_FRAMES:
            warnings.append(f"transition after clip {i + 1} is too short for its neighbours and became a cut")
            got = 0
        elif got < want:
            warnings.append(f"transition after clip {i + 1} shortened from {want} to {got} frames "
                            "(at most half of each neighbouring clip)")
        tf[i] = got
    segs, start = [], 0
    for i, r in enumerate(raw):
        t = spec["transitions"].get(str(r["item_id"]))
        segs.append(Seg(start_frame=start, trans_after=(XFADE[t["type"]], tf[i]) if tf[i] else None, **r))
        start += r["frames"] - tf[i]
    total = segs[-1].start_frame + segs[-1].frames if segs else 0
    ident = json.dumps([spec, [(i["item_id"], i["uuid"], i["t0"], i["t1"], i["pos"]) for i in items]],
                       sort_keys=True, separators=(",", ":"))
    return Timeline(fps=fps, aspect=spec["format"]["aspect"], size=spec["format"]["size"], segs=tuple(segs),
                    total_frames=total, warnings=tuple(warnings), edit_hash=hashlib.sha1(ident.encode()).hexdigest())


def snapshot(cid):
    """(Timeline, Spec) from one read; later edits do not affect a running render."""
    col = library.get_collection(cid)
    _require_project(col)
    with conn() as c:
        spec, _ = _read(c, col)
    return compute_timeline(col["items"], spec), spec


# ---------- routes

F = float


class EditPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aspect: Literal["16:9", "9:16", "1:1", "4:5"] = "16:9"
    size: Literal[720, 1080, 2160] = 1080
    fps: Literal[24, 25, 30, 60] = 30
    look: str | None = None            # null or "none" clears the project look
    look_strength: F = 1.0
    captions: bool = False
    caption_style: Literal["bold", "box"] = "bold"


class ClipPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speed: F | None = None
    volume: F | None = None
    exposure: F | None = None
    contrast: F | None = None
    saturation: F | None = None
    warmth: F | None = None
    look: str | None = None            # null inherits the project look, "none" means no look
    look_strength: F | None = None
    reframe_mode: Literal["center", "manual", "pan"] | None = None
    reframe_pos: F | None = None
    reframe_from: F | None = None
    reframe_to: F | None = None


class TransitionPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["cut", "dissolve", "fade-black", "wipe-left", "wipe-right", "slide-left"]
    duration: F = 0.5


# field -> (path in the clip entry, default, lo, hi); lo is None for enums
CLIP_FIELDS = {
    "speed": (("speed",), 1.0, .25, 4), "volume": (("volume",), 1.0, 0, 2),
    "exposure": (("adjust", "exposure"), 0.0, -1, 1), "contrast": (("adjust", "contrast"), 1.0, .8, 1.3),
    "saturation": (("adjust", "saturation"), 1.0, 0, 1.6), "warmth": (("adjust", "warmth"), 0.0, -1, 1),
    "reframe_mode": (("reframe", "mode"), "center", None, None),
    "reframe_pos": (("reframe", "pos"), .5, 0, 1), "reframe_from": (("reframe", "pos_from"), .5, 0, 1),
    "reframe_to": (("reframe", "pos_to"), .5, 0, 1),
}


def _set_look(holder, key, name):
    """Store {"name", "strength"} at holder[key]; keeps the old strength when only the name changes."""
    if name not in (None, "none") and name not in LOOKS:
        raise HTTPException(422, f"unknown look {name!r}; allowed: {sorted(LOOKS)} or 'none'")
    holder[key] = {"name": None if name == "none" else name, "strength": (holder.get(key) or {}).get("strength", 1.0)}


@router.get("/collections/{cid}/edit")
def get_edit(cid: int):
    col = library.get_collection(cid)
    _require_project(col)
    with conn() as c:
        spec, rev = _read(c, col)
    tl = compute_timeline(col["items"], spec)
    return {"spec": spec, "rev": rev, "edit_hash": tl.edit_hash, "duration": tl.duration,
            "fps": tl.fps, "total_frames": tl.total_frames,
            "clips": [{"item_id": s.item_id, "uuid": s.uuid, "start": s.start_frame / tl.fps,
                       "end": (s.start_frame + s.frames) / tl.fps, "frames": s.frames} for s in tl.segs],
            "warnings": list(tl.warnings),
            "options": {"looks": sorted(LOOKS), "aspects": list(ASPECTS), "sizes": list(SIZES), "fps": list(FPS),
                        "transitions": ["cut", *XFADE], "caption_styles": list(CAPTION_STYLES)}}


@router.patch("/collections/{cid}/edit")
def patch_edit(cid: int, body: EditPatch):
    given, flipped = body.model_fields_set, []

    def fn(spec, col):
        notes = []
        for k in ("aspect", "size", "fps"):
            if k in given:
                spec["format"][k] = getattr(body, k)
        if "look" in given:
            _set_look(spec, "look", body.look)
        if "look_strength" in given:
            spec["look"]["strength"] = _clamp(notes, "look_strength", body.look_strength, 0, 1)
        if "captions" in given:
            flipped.append(body.captions and not spec["captions"]["on"])
            spec["captions"]["on"] = body.captions
        if "caption_style" in given:
            spec["captions"]["style"] = body.caption_style
        return notes

    out = mutate(cid, fn)
    if flipped and flipped[0]:
        import captions  # lazy: C's module. The edit is already saved if this raises.
        captions.start_transcription(cid)
    return out


@router.patch("/collections/{cid}/edit/clips/{item_id}")
def patch_clip(cid: int, item_id: int, body: ClipPatch):
    given = body.model_fields_set

    def fn(spec, col):
        item = next((i for i in col["items"] if i["item_id"] == item_id), None)
        if not item:
            raise HTTPException(404, "no such item")
        if item["kind"] == "image" and body.speed not in (None, 1.0):
            raise HTTPException(422, "photos cannot change speed")
        notes, e = [], spec["clips"].setdefault(str(item_id), {})
        for k in given & CLIP_FIELDS.keys():
            (*parents, leaf), default, lo, hi = CLIP_FIELDS[k]
            d = e
            for p in parents:
                d = d.setdefault(p, {})
            v = getattr(body, k)
            if v is not None and lo is not None:
                v = _clamp(notes, k, v, lo, hi)
            if v is None or v == default:
                d.pop(leaf, None)
            else:
                d[leaf] = v
        if "look" in given:
            if body.look is None:
                e.pop("look", None)
            else:
                _set_look(e, "look", body.look)
        if "look_strength" in given:
            if body.look_strength is None:
                if "look" in e:
                    e["look"]["strength"] = 1.0
            else:
                if "look" not in e:  # strength alone adjusts the project look for this clip
                    if not spec["look"]["name"]:
                        raise HTTPException(422, "pick a look first; there is no project look to adjust")
                    _set_look(e, "look", spec["look"]["name"])
                e["look"]["strength"] = _clamp(notes, "look_strength", body.look_strength, 0, 1)
        for p in ("adjust", "reframe"):
            if not e.get(p):
                e.pop(p, None)
        if not e:
            del spec["clips"][str(item_id)]
        return notes

    return mutate(cid, fn)


@router.put("/collections/{cid}/edit/transitions/{item_id}")
def put_transition(cid: int, item_id: int, body: TransitionPut):
    def fn(spec, col):
        if not any(i["item_id"] == item_id for i in col["items"]):
            raise HTTPException(404, "no such item")
        if body.type == "cut":
            spec["transitions"].pop(str(item_id), None)
            return []
        notes = []
        spec["transitions"][str(item_id)] = {"type": body.type,
                                             "duration": _clamp(notes, "duration", body.duration, .2, 2)}
        return notes

    return mutate(cid, fn)


@router.delete("/collections/{cid}/edit/transitions/{item_id}")
def delete_transition(cid: int, item_id: int):
    return put_transition(cid, item_id, TransitionPut(type="cut"))
