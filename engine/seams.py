"""suggest_seams: per-seam facts and ONE suggested transition (notes/editing-power-research.md, X4).

Read-only and fast: it only reads caches (beat grid, music map, moments signals, scene vectors, asset metadata) and, when a
scene vector is missing, grabs one frame from an existing proxy for a colour histogram. It never analyses anything and never
writes to the edit; a field that cannot be known from the caches is null ("unknown").

  facts(...)      impure: collects the facts for every seam of a project
  suggest(f)      pure: facts of one seam -> suggestion
  plan(seams)     pure: applies the density caps (whips, share of seams with a transition)
"""
import json
import math
from datetime import datetime

from fastapi import APIRouter

import core
import edit
import library

router = APIRouter()

BEAT_TOL = 0.08        # seconds: a cut this close to a beat counts as on it (synced cuts lead the beat by 1 frame)
WINDOW = 1.0           # output seconds of shot looked at on each side of the cut
# ponytail: thresholds below are uncalibrated guesses (YDIF at 160 px, 10 fps); tune them against real clips.
MOVING = 1.5           # mean frame difference: below this a shot is "still"
FAST = 5.0
DRIFT = 0.12           # saliency centre travel per second (0 to 1 of the frame) that counts as a direction
HIGH = 6               # music energy digit of a loud bar
LOW = 4
DROP_BY = 3            # energy fall across the cut that earns a dissolve
SIM_MATCH = {"embedding": 0.85, "histogram": 0.9}  # same scene / very similar framing
JUMP_HOURS = 3.0       # time between two shots that reads as a new moment
PLACE_KM = 5.0
MAX_SHARE = 0.3        # at most this share of seams carries a transition
OPPOSITE = {"left": "right", "right": "left", "up": "down", "down": "up"}
RANK = {"whip": 3, "zoom": 2, "dissolve": 1}  # by type prefix; flash-white and the rest are never suggested


# ---------- pure: the rules

def suggest(f):
    """One suggestion {type, duration, sfx, reason} from a seam's facts (keys as built by facts())."""
    sim, by = f.get("similarity"), f.get("similarity_by")
    if sim is not None and sim >= SIM_MATCH[by]:
        return _s("cut", 0, None, f"very similar shots (similarity {sim:.2f}): hard cut, line the subject up with reframe_pos for a match cut")
    e_out, e_in = f.get("energy_out"), f.get("energy_in")
    loud = e_out is not None and e_in is not None and max(e_out, e_in) >= HIGH
    quiet = e_out is not None and e_in is not None and max(e_out, e_in) <= LOW
    strong = f.get("beat") == "downbeat"
    mo, mi = f.get("motion_out") or {}, f.get("motion_in") or {}
    d_out = mo.get("direction")
    if (strong and loud and d_out and mo.get("amount") is not None and mo["amount"] >= MOVING
            and mi.get("direction") != OPPOSITE[d_out]):
        return _s(f"whip-{d_out}", 0.25, "whoosh",
                  f"downbeat in a loud bar and the picture is moving {d_out} at the end of the outgoing shot")
    gap, place = f.get("gap_hours"), f.get("place_change")
    if place or (gap is not None and gap >= JUMP_HOURS):
        why = "new place" if place else f"{gap:.0f} h later"
        if strong and loud:
            return _s("zoom-in", 0.3, "whoosh", f"{why} on a downbeat in a loud bar")
        if quiet or e_out is None:
            return _s("dissolve", 0.6, None, f"{why}, " + ("slow music" if quiet else "no music map"))
        return _s("cut", 0, None, f"{why}, but the music is moving: hard cut on the beat")
    if e_out is not None and e_in is not None and e_out - e_in >= DROP_BY:
        return _s("dissolve", 0.5, None, f"energy drops from {e_out} to {e_in}")
    return _s("cut", 0, None, "no reason for a transition (hard cut is the default)")


def _s(type_, duration, sfx, reason):
    return {"type": type_, "duration": duration, "sfx": sfx, "reason": reason}


def _rank(s):
    return RANK.get(s["type"].split("-")[0], 0)


def plan(seams):
    """Cap the suggestions in place: whips at least edit.WHIP_EVERY seconds apart, transitions on at most MAX_SHARE of the
    seams (highest rank first, earlier seams win ties). A dropped suggestion becomes a cut and says why."""
    def drop(sg, why):
        sg["suggestion"] = _s("cut", 0, None, f"{sg['suggestion']['type']} dropped ({why})")

    last = -1e9
    for sg in seams:
        if sg["suggestion"]["type"].startswith("whip"):
            if sg["at"] - last < edit.WHIP_EVERY:
                drop(sg, f"another whip within {edit.WHIP_EVERY:.0f} s")
            else:
                last = sg["at"]
    room = max(1, math.floor(MAX_SHARE * len(seams)))
    live = [sg for sg in seams if sg["suggestion"]["type"] != "cut"]
    if len(live) > room:
        keep = {id(sg) for sg in sorted(live, key=lambda g: (-_rank(g["suggestion"]), g["at"]))[:room]}
        for sg in live:
            if id(sg) not in keep:
                drop(sg, f"at most {MAX_SHARE:.0%} of seams get a transition")
    return seams


# ---------- facts from caches

def _beat_facts(an, s):
    """(beat kind, position in bar) for source time s: 'downbeat' / 'beat' / 'off' and 1-based beat in the bar."""
    nb = min(an["beats"], key=lambda t: abs(t - s), default=None)
    if nb is None or abs(nb - s) > BEAT_TOL:
        return "off", None
    downs = [t for t in an["downbeats"] if t <= nb + 1e-6]
    if downs and abs(nb - downs[-1]) < 1e-3:
        return "downbeat", 1
    if not downs:
        return "beat", None
    return "beat", 1 + sum(1 for t in an["beats"] if downs[-1] - 1e-6 <= t < nb - 1e-6)


def _music(spec, at):
    """Music facts at output second `at`, or {} when the sync track has no cached beats or map."""
    import beats
    import music
    t, m = edit._sync_music(spec)
    an = beats.cached(t["path"]) if t else None
    s = (t["in"] + at - t["start"]) if t else None
    if not t or not an or s is None or at < t["start"] or (t["out"] is not None and s > t["out"]):
        return {}
    kind, pos = _beat_facts(an, s)
    out = {"beat": kind, "bar_pos": pos}
    if m:
        out.update(energy_out=music.level_at(m, s - 0.05), energy_in=music.level_at(m, s + 0.05),
                   section_change=any(abs(x["start"] - s) <= 0.15 and x["bar"] > 0 for x in m["sections"]))
    return out


def _signals(uuid, cache):
    if uuid not in cache:
        import moments
        try:
            got = json.loads(moments.cache_path(uuid).read_text())
            cache[uuid] = got if got.get("version") == moments.VERSION else None
        except (OSError, ValueError):
            cache[uuid] = None
    return cache[uuid]


def _motion(seg, sig, end):
    """{amount, label, direction} of the picture over the last (end=True) or first WINDOW of a shot. Photos are still.
    direction is where the PICTURE moves (same naming as whip-*), from the drift of the saliency centre."""
    import numpy as np
    if seg.kind == "photo":
        return {"amount": 0.0, "label": "still", "direction": None}
    if not sig:
        return {"amount": None, "label": "unknown", "direction": None}
    w = WINDOW * seg.speed
    a, b = (seg.src_out - w, seg.src_out) if end else (seg.src_in, seg.src_in + w)
    hz, ser = sig["hz"], sig["series"]
    i0, i1 = max(0, int(a * hz)), max(1, int(math.ceil(b * hz)))
    mot = ser["motion"][i0:i1]
    if not mot:
        return {"amount": None, "label": "unknown", "direction": None}
    amount = float(np.mean(mot))
    label = "still" if amount < MOVING else "fast" if amount >= FAST else "moderate"
    drift = {}
    xs, ys = ser.get("sal_x", [])[i0:i1], ser.get("sal_y", [])[i0:i1]
    if len(xs) >= 3 and any(xs) and any(ys):
        t = np.arange(len(xs)) / hz
        dx, dy = (float(np.polyfit(t, v, 1)[0]) for v in (xs, ys))
        if max(abs(dx), abs(dy)) >= DRIFT:
            drift = {"direction": (("right" if dx > 0 else "left") if abs(dx) >= abs(dy) else ("down" if dy > 0 else "up"))}
    return {"amount": round(amount, 2), "label": label, "direction": drift.get("direction")}


def _vectors(c, uuid):
    out = []
    for t0, t1, blob in c.execute("select t0, t1, vec from segments where uuid = ?", (uuid,)):
        if blob:
            out.append((t0 or 0.0, t1 if t1 is not None else (t0 or 0.0), blob))
    return out


def _vec_at(rows, t):
    import numpy as np
    if not rows:
        return None
    inside = [r for r in rows if r[0] <= t <= r[1]]
    r = min(inside or rows, key=lambda r: abs((r[0] + r[1]) / 2 - t))
    return np.frombuffer(r[2], np.float32)


def _hist(uuid, kind, t):
    """HSV histogram of a frame from an existing proxy (or kept preview); None when there is none. Never creates a proxy."""
    import cv2
    import media
    frame = None
    if kind == "photo":
        p = next(iter((media.PREVIEWS / uuid[:2]).glob(f"{uuid}.*")), None) if (media.PREVIEWS / uuid[:2]).is_dir() else None
        frame = cv2.imread(str(p)) if p else None
    elif (media.PROXIES / f"{uuid}.mp4").is_file():
        cap = cv2.VideoCapture(str(media.PROXIES / f"{uuid}.mp4"))
        try:
            frame = media.frame_at(cap, max(t, 0.0))
        finally:
            cap.release()
    if frame is None:
        return None
    h = cv2.calcHist([cv2.cvtColor(cv2.resize(frame, (96, 54)), cv2.COLOR_BGR2HSV)], [0, 1], None, [12, 8], [0, 180, 0, 256])
    return cv2.normalize(h, h).flatten()


def _similarity(c, a, b):
    """(similarity, 'embedding' | 'histogram') of the outgoing seg a's last frames and incoming seg b's first, or (None, None)."""
    import numpy as np
    ta, tb = max(a.src_out - 0.1, 0.0), b.src_in + 0.1
    va, vb = _vec_at(_vectors(c, a.uuid), ta), _vec_at(_vectors(c, b.uuid), tb)
    if va is not None and vb is not None and va.shape == vb.shape:
        return round(float(va @ vb), 3), "embedding"
    ha, hb = _hist(a.uuid, a.kind, ta), _hist(b.uuid, b.kind, tb)
    if ha is not None and hb is not None:
        import cv2
        return round(float(cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL)), 3), "histogram"
    return None, None


def _when(created):
    try:
        d = datetime.fromisoformat(created)
        return d.replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def _place(c, ua, ub, ca, cb):
    """(place_change, gap_hours): None where the metadata cannot say."""
    q = "select place, lat, lon from asset_info where uuid = ?"
    ia, ib = c.execute(q, (ua,)).fetchone(), c.execute(q, (ub,)).fetchone()
    change = None
    if ia and ib:
        if None not in (ia[1], ia[2], ib[1], ib[2]):  # coordinates first: names differ in detail ("Salem" vs a street in Salem)
            change = _km(ia[1:], ib[1:]) >= PLACE_KM
        elif ia[0] and ib[0]:
            change = ia[0].strip().lower() != ib[0].strip().lower()
    da, db = _when(ca), _when(cb)
    return change, (round(abs((db - da).total_seconds()) / 3600, 1) if da and db else None)


def facts(cid):
    """(seams, notes) for project cid. Raises HTTPException 404/422 like the other edit routes."""
    col = library.get_collection(cid)
    edit._require_project(col)
    spec, _ = edit.load(cid)
    notes, grid = [], None
    t, _m = edit._sync_music(spec)
    sid = (spec.get("sync") or {}).get("track")
    if sid:
        import beats
        tr = next((x for x in spec.get("audio", ()) if x["id"] == sid), None)
        an = beats.cached(tr["path"]) if tr else None
        if an:
            grid = beats.grid(tr, an, spec["sync"]["grid"], edit.MAX_SECONDS)
        else:
            notes.append("the sync track's beats are not cached yet (set_edit(sync_track=...) analyses them); timings and music facts are unknown")
    if sid and grid and not _m:
        notes.append("no music map cached: call analyze_music for energy and section facts")
    tl = edit.compute_timeline(col["items"], spec, grid)
    items = {i["item_id"]: i for i in col["items"]}
    cache, seams = {}, []
    with core.connect() as c:
        for k, (a, b) in enumerate(zip(tl.segs, tl.segs[1:])):
            at = round((b.start_frame + (a.trans_after[1] // 2 if a.trans_after else 0)) / tl.fps, 3)
            cur = spec["transitions"].get(str(a.item_id), {"type": "cut", "duration": 0})
            f = {"seam": k + 1, "after_item_id": a.item_id, "at": at,
                 "current": {"type": cur["type"], "duration": cur.get("duration", 0), "sfx": spec.get("sfx", {}).get(str(a.item_id))},
                 **(_music(spec, at) if grid else {}),
                 "motion_out": _motion(a, _signals(a.uuid, cache) if a.kind == "video" else None, True),
                 "motion_in": _motion(b, _signals(b.uuid, cache) if b.kind == "video" else None, False)}
            f["similarity"], f["similarity_by"] = _similarity(c, a, b)
            f["place_change"], f["gap_hours"] = _place(c, a.uuid, b.uuid, items[a.item_id]["created"], items[b.item_id]["created"])
            seams.append(f)
    for f in seams:
        f["suggestion"] = suggest(f)
    plan(seams)
    for f in seams:
        f["change"] = f["suggestion"]["type"] != f["current"]["type"]
        for key in ("beat", "bar_pos", "energy_out", "energy_in", "section_change", "similarity", "place_change", "gap_hours"):
            f.setdefault(key, None)
    share = sum(1 for f in seams if f["current"]["type"] != "cut") / len(seams) if seams else 0
    if share > MAX_SHARE:
        notes.append(f"{share:.0%} of the seams already carry a transition; more than {MAX_SHARE:.0%} reads as a slideshow")
    return seams, notes


@router.get("/collections/{cid}/edit/seams")
def seams_route(cid: int):
    """Read-only. Per seam: where it falls on the music, the motion on both sides, how alike the two shots are, the place or
    time change, the current transition and one suggestion. Unknown facts are null."""
    seams, notes = facts(cid)
    return {"seams": seams, "notes": notes,
            "summary": {"seams": len(seams), "transitions_now": sum(1 for f in seams if f["current"]["type"] != "cut"),
                        "transitions_suggested": sum(1 for f in seams if f["suggestion"]["type"] != "cut")}}
