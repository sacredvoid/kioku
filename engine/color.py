"""Colour match across clips (X7) and the finish layer (X8), notes/editing-power-research.md.

Match: each clip's colour is measured once from its proxy (or preview image for photos), 2 samples a second, cached per
uuid in DATA/color: the mean log of R, G and B over pixels that are neither black nor clipped (exposure), and the same
over near-neutral pixels only (white balance: a blue sky or a red wall says nothing about the light, a grey wall
does). A clip's stats are the samples inside its source range. solve() moves every clip toward a reference (one
chosen clip, or the median of the edit) with a per-channel power curve out = in ** p: a power curve maps the mean
log exactly (log-mean of x**p is p times the log-mean of x), keeps black and white where they are, and never clips. The move fades out for big
differences: camera drift (a few tenths of a stop, a slightly warm white balance) is corrected almost fully, while a
night shot or a sunset is far enough from the rest to read as intended and barely moves. Applied in the segment pass
before the look, so the look grades footage that already agrees.

Finish: grain, vignette, bloom or halation, and letterbox bars on the whole edit, applied once in the finish pass
(render._graph) before titles and captions, so text stays clean.
"""
import json
import logging
import math
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

import core

log = logging.getLogger("kioku.color")
VERSION = 1             # stats format; bump to re-measure everything
STATS = core.DATA / "color"
HZ = 2                  # samples per second of video
SIDE = (96, 64)         # sample size; plenty for a mean
MAX_SECONDS = 600       # only the first 10 minutes of a long video are measured
LUMA = np.array([0.2126, 0.7152, 0.0722])
EXP_CAP = 0.5           # exposure difference (mean-log units, 0.3 is about a stop at mid grey) where the move halves
WB_CAP = 0.3            # the same for each channel's white balance offset
MIN_PIXELS = 0.05       # a clip needs this share of usable pixels (one frame's worth) to be matched
MIN_GREY = 0.02         # and this share of near-neutral ones for its white balance to move
GREY_RATIO = 1.5        # near-neutral: brightest channel at most this times the darkest (a cast still fits)

# ---------- measuring


def _path(uuid):
    return STATS / f"{uuid}.json"


def cached(uuid):
    """Measured samples for an asset, or None (never measures)."""
    try:
        got = json.loads(_path(uuid).read_text())
    except (OSError, ValueError):
        return None
    return got if got.get("v") == VERSION and isinstance(got.get("frames"), list) else None


def frame_stats(rgb):
    """rgb: (h, w, 3) uint8 -> [usable share, mean log R, G, B, near-neutral share, their mean log R, G, B].
    Usable = not near black, no channel clipped. Shares are of all pixels; means are 0 when the share is 0."""
    x = rgb.reshape(-1, 3).astype(np.float64) / 255.0
    ok = (x.max(1) < 0.97) & (x @ LUMA > 0.03)
    lx = np.log(np.maximum(x, 1 / 255))
    cast = np.zeros(3)
    for _ in range(3):  # pick near-neutral pixels as they would be without this frame's cast, so a cast cannot hide itself
        y = lx - cast
        grey = ok & (y.max(1) - y.min(1) <= math.log(GREY_RATIO))
        if not grey.any():
            break
        cast = _wb(lx[grey].mean(0))
    out = []
    for m in (ok, grey):
        n = int(m.sum())
        lg = lx[m].mean(0) if n else np.zeros(3)
        out += [round(n / len(x), 4), *(round(float(v), 5) for v in lg)]
    return out


def measure(src, video):
    """Samples of one file: a list of frame_stats, one per 1/HZ s (one for a still). Raises RuntimeError."""
    w, h = SIDE
    vf = (f"fps={HZ}," if video else "") + f"scale={w}:{h}:flags=area,format=rgb24"
    # a full decode of a 540p proxy runs at about 100x real time; -skip_frame nokey is faster but starves fps= (no frames)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-vf", vf, "-an"]
    cmd += ["-t", str(MAX_SECONDS)] if video else ["-frames:v", "1"]
    try:
        r = subprocess.run(cmd + ["-f", "rawvideo", "-"], capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("measuring took too long") from None
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-200:])
    k = len(r.stdout) // (w * h * 3)
    if not k:
        raise RuntimeError("no frames")
    a = np.frombuffer(r.stdout[: k * w * h * 3], np.uint8).reshape(k, h, w, 3)
    return [frame_stats(f) for f in a]


def ensure(assets, build=False):
    """Measure every (uuid, "video" | "photo") that is not cached yet. build: make missing video proxies first (what a
    preview render does anyway). Best effort: an asset that cannot be measured is skipped (logged). Returns how many
    were measured."""
    import media
    done = 0
    for uuid, kind in dict.fromkeys(assets):
        if cached(uuid):
            continue
        try:
            if kind == "photo":
                src = media.preview_source(uuid)
            else:
                src = media.PROXIES / f"{uuid}.mp4"
                if not src.is_file():
                    if not build:
                        continue
                    with tempfile.TemporaryDirectory(prefix="cs-color-") as tmp:
                        src = media.fetch(uuid, "preview", Path(tmp))
            frames = measure(src, kind != "photo")
        except Exception as e:  # noqa: BLE001 - a clip we cannot measure is just not matched
            log.warning("colour match: could not measure %s: %s", uuid, e)
            continue
        STATS.mkdir(parents=True, exist_ok=True)
        tmp = _path(uuid).with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"v": VERSION, "hz": HZ if kind != "photo" else 0, "frames": frames}))
        tmp.replace(_path(uuid))
        done += 1
    return done


def clip_stats(c, t0, t1):
    """np array [mean log R, G, B; the same over near-neutral pixels] of the samples inside source seconds t0..t1 (the
    nearest one when none falls inside, all of them for a still), weighted by pixel counts. The near-neutral part is
    NaN when too few pixels are near neutral. None when too little of the clip is usable."""
    fr = c["frames"]
    if not fr:
        return None
    hz = c.get("hz") or 0
    if hz:
        i0, i1 = max(0, math.floor(t0 * hz)), min(len(fr) - 1, math.ceil(t1 * hz))
        pick = fr[i0:i1 + 1] if i0 <= i1 else [fr[min(max(i0, 0), len(fr) - 1)]]
    else:
        pick = fr
    a = np.array(pick, float)
    w, gw = a[:, 0], a[:, 4]
    if w.sum() < MIN_PIXELS:
        return None
    l = (a[:, 1:4] * w[:, None]).sum(0) / w.sum()
    if gw.sum() < MIN_GREY:
        return np.concatenate([l, [np.nan] * 3])
    return np.concatenate([l, (a[:, 5:8] * gw[:, None]).sum(0) / gw.sum()])


# ---------- solving


def _keep(d, cap):
    """The share of a difference to correct (d: its size): nearly all while small, half at cap, little beyond (the
    largest move is 0.57 * cap, at d = 0.76 * cap). Big differences are taken as intended."""
    return 1 / (1 + (d / cap) ** 4)


def _wb(g):
    """White balance of near-neutral mean logs: each channel's offset from their luma-weighted mean."""
    return g - LUMA @ g


def _centre(group):
    """Reference (exposure, overall colour, near-neutral white balance or None) as the median of clip_stats arrays."""
    wbs = [_wb(v[3:]) for v in group if not np.isnan(v[3])]
    return (float(np.median([LUMA @ v[:3] for v in group])), np.median([_wb(v[:3]) for v in group], 0),
            np.median(wbs, 0) if wbs else None)


def _gap(s, ref):
    """(exposure difference, near-neutral white balance difference, how far the colour looks) from clip to reference.
    Far counts the overall colour too: a blue sky under orange light can pass for grey, the whole frame still says orange."""
    ry, ra, rc = ref
    dc = np.zeros(3) if rc is None or np.isnan(s[3]) else rc - _wb(s[3:])
    return ry - float(LUMA @ s[:3]), dc, max(np.abs(dc).max(), np.abs(ra - _wb(s[:3])).max())


def solve(stats, ref=None):
    """stats: {item_id: clip_stats or None}; ref: item_id of the reference clip or None for the edit median.
    -> ({item_id: (pr, pg, pb) or None}, [item_ids left mostly as they are], reference used: item_id or None).
    None means no change (unmeasured, or already within 1%). Exposure is one power for all channels that puts the
    clip's luma on the reference's; white balance then bends each channel so the clip's near-neutral pixels carry the
    reference's offsets. A clip without near-neutral pixels keeps its white balance (a reference without them borrows
    the median's). The median is taken twice: the second time without the clips far from the first (night shots,
    sunsets), so a few of them cannot drag the rest."""
    got = {k: v for k, v in stats.items() if v is not None}
    if not got:
        return {k: None for k in stats}, [], None
    t = _centre(list(got.values()))
    if ref in got:
        r = got[ref]
        t = (float(LUMA @ r[:3]), _wb(r[:3]), t[2] if np.isnan(r[3]) else _wb(r[3:]))
    else:
        ref = None
        gaps = [(v, _gap(v, t)) for v in got.values()]
        near = [v for v, (dy, _, far) in gaps if abs(dy) <= EXP_CAP and far <= WB_CAP]
        if near and len(near) < len(got):
            t = _centre(near)
    out, capped = {}, []
    for k, s in stats.items():
        if s is None:
            out[k] = None
            continue
        dy, dc, far = _gap(s, t)
        if abs(dy) > EXP_CAP or far > WB_CAP:
            capped.append(k)
        y, g = float(LUMA @ s[:3]), s[3:]
        dc = dc * _keep(far, WB_CAP)  # one share for all channels, so a strong cast is kept whole
        pe = (y + dy * _keep(dy, EXP_CAP)) / y if y < -1e-3 else 1.0
        if np.isnan(g[0]):
            p = [pe] * 3
        else:  # near-neutral logs after the exposure power are pe * g; shift each by its share of dc
            p = [(pe * v + d) / v if v < -1e-3 else pe for v, d in zip(g, dc)]
        p = tuple(round(min(max(x, 0.6), 1.6), 3) for x in p)
        out[k] = None if all(abs(x - 1) < 0.01 for x in p) else p
    return out, capped, ref


def match_filter(p):
    """ffmpeg filter for a solved (pr, pg, pb); '' for None. Numbers only, safe to inline."""
    if not p:
        return ""
    e = lambda x: f"'pow(val/maxval,{x:g})*maxval'"  # noqa: E731 - quoted: the comma must not split the filter chain
    return f"lutrgb=r={e(p[0])}:g={e(p[1])}:b={e(p[2])}"


# ---------- finish layer

# per preset at strength 1: grain, vignette, bloom (white glow), halation (warm glow round highlights), letterbox
FINISHES = {
    "none": {},
    "clean": {"vignette": 0.25, "bloom": 0.2},
    "film": {"grain": 0.5, "vignette": 0.4, "halation": 0.5},
    "cinematic": {"grain": 0.3, "vignette": 0.35, "bloom": 0.3, "letterbox": True},
}
LETTERBOX = 2.39
GRAIN_MBPS = 0.3        # final bitrate grows by this share at grain 1, so the grain is not smeared away


def resolve(finish, aspect):
    """Spec finish {"preset", "strength"} -> ({grain, vignette, bloom, halation, letterbox} or None, warning or None)."""
    if not finish or finish.get("preset", "none") == "none":
        return None, None
    k = finish.get("strength", 1.0)
    if k <= 0:
        return None, None
    p = FINISHES[finish["preset"]]
    out = {x: round(p.get(x, 0.0) * k, 3) for x in ("grain", "vignette", "bloom", "halation")}
    out["letterbox"] = bool(p.get("letterbox"))
    why = None
    if out["letterbox"] and aspect != "16:9":
        out["letterbox"] = False
        why = f"letterbox bars only go on 16:9 edits; this {aspect} edit gets the rest of the {finish['preset']} finish"
    return out, why


def _glow(W, H, amount, warm):
    """Bloom (white) or halation (a red-orange rim): luma above a threshold, blurred at quarter size, screened back onto
    luma. Halation uses only the rim (blurred minus sharp highlights) and pushes it toward red through grainmerge on
    chroma (adds the offset from neutral), so a clipped sun keeps a white core instead of turning pink. All in 8-bit
    YUV: an RGB round trip at full size costs about as much as the rest of the finish pass."""
    hi = "max(val-200,0)*4.6"  # luma over 200 (of 235) stretched to about 160
    sigma = max(W, H) / 4 * (0.012 if warm else 0.008)
    op = min(amount * (0.9 if warm else 0.7), 1.0)
    n = "h" if warm else "b"  # labels differ, so halation and bloom can share one graph
    small = f"scale={max(W // 4, 2)}:{max(H // 4, 2)}:flags=area,format=yuv444p,shuffleplanes=0:0:0,lutyuv=y='{hi}':u='{hi}':v='{hi}'"
    if warm:  # every plane holds the rim strength, then chroma becomes a red-orange offset from neutral
        g = (f"{small},split[f{n}3][f{n}4];[f{n}3]gblur=sigma={sigma:.2f}[f{n}5];[f{n}5][f{n}4]blend=all_mode=subtract,"
             f"lutyuv=y='val*1.6':u='128-0.25*val':v='128+0.7*val'")
    else:
        g = f"{small},lutyuv=u=128:v=128,gblur=sigma={sigma:.2f}"
    return (f"format=yuv420p,split[f{n}0][f{n}1];[f{n}1]{g},scale={W}:{H}:flags=bilinear,format=yuv420p[f{n}2];"
            f"[f{n}0][f{n}2]blend=c0_mode=screen:c1_mode=grainmerge:c2_mode=grainmerge:all_opacity={op:.3f}")


def finish_filters(f, dims):
    """Resolved finish -> one filter chain (may contain labelled branches, so it is pasted between [in] and [out]).
    '' for None. Order: glow, grain, vignette, bars (bars stay pure black)."""
    if not f:
        return ""
    W, H = dims
    parts = []
    if f["halation"] > 0:
        parts.append(_glow(W, H, f["halation"], True))
    if f["bloom"] > 0:
        parts.append(_glow(W, H, f["bloom"], False))
    if f["grain"] > 0:
        parts.append(f"noise=c0s={max(1, round(f['grain'] * 16))}:c0f=t+u")
    if f["vignette"] > 0:
        parts.append(f"vignette=angle={min(f['vignette'], 1.0) * 0.9:.3f}")
    if f["letterbox"] and W / H < LETTERBOX:
        b = round((H - W / LETTERBOX) / 4) * 2  # even
        parts.append(f"drawbox=x=0:y=0:w=iw:h={b}:color=black:t=fill,drawbox=x=0:y=ih-{b}:w=iw:h={b}:color=black:t=fill")
    return ",".join(parts)
