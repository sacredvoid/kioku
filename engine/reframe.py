"""Auto reframe: follow the subject with the crop window, from the 4 Hz analysis cached by moments.py.

track(...)   pure: cached series -> subject centre (cx, cy), normalised 0..1 over the source frame, sampled evenly across a clip.
             Faces first (face_x / face_y), then attention saliency; samples with neither hold the last good value.
path(...)    pure: subject track + crop fraction -> crop positions (0..1 along the crop slack) with a dead zone and a
             max pan speed, so the frame never jitters.
Stored on Seg.track and turned into an ffmpeg expression by render.seg_graph. Bump VERSION when anything below changes
what a given track produces (it is part of the segment cache key)."""
import json
import logging

import numpy as np

log = logging.getLogger("kioku.reframe")
VERSION = 1
SMOOTH_S = 1.0       # moving average over the subject track, seconds
DEAD = 0.18          # subject may drift this far from the window centre (fraction of the crop width) before the window moves
MAX_SPEED = 0.2      # fastest pan, source widths (or heights) per second
MAX_KEYS = 24        # keyframes in the ffmpeg expression
FLAT = 0.02          # a path that moves less than this (in crop positions) becomes a still crop


def load(uuid):
    """Cached series for a clip, or None when it was never analysed (this never triggers an analysis)."""
    import moments
    f = moments.cache_path(uuid)
    try:
        got = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    return got if got.get("version") == moments.VERSION and got.get("series") and got.get("hz") else None


def _smooth(x, k):
    if k <= 1 or len(x) < 3:
        return x
    p = np.pad(x, k // 2, mode="edge")
    return np.convolve(p, np.ones(k) / k, mode="valid")[:len(x)]


def track(cached, t0, t1, n):
    """n >= 2 subject centres evenly spaced over source seconds t0..t1, as ((cx, cy), ...); None without a usable subject."""
    ser, hz = cached["series"], cached["hz"]
    sx, sy = ser.get("sal_x"), ser.get("sal_y")
    if not sx or not sy or t1 <= t0:
        return None
    fx, fy, fc = ser.get("face_x"), ser.get("face_y"), ser.get("faces")
    m = len(sx)
    pts, ok = [], []
    for t in np.linspace(t0, t1, n):
        i = min(max(int(t * hz), 0), m - 1)
        if fx and fy and fc and fc[i] > 0:  # a face: trust it
            pts.append((fx[i], fy[i])); ok.append(True)
        elif (sx[i], sy[i]) != (0.5, 0.5):  # exactly centred = Vision found nothing salient (see moments.vision_signals)
            pts.append((sx[i], sy[i])); ok.append(True)
        else:
            pts.append(None); ok.append(False)
    if not any(ok):
        return None
    last = next(p for p in pts if p)  # leading gap takes the first good value
    for i, p in enumerate(pts):
        if p: last = p
        else: pts[i] = last
    a = np.array(pts, float)
    k = max(1, round(SMOOTH_S * (n - 1) / (t1 - t0)))
    a = np.stack([_smooth(a[:, 0], k), _smooth(a[:, 1], k)], 1)
    return tuple((round(float(x), 3), round(float(y), 3)) for x, y in a)


def path(centres, f, L):
    """centres: 1D subject positions evenly spaced over L output seconds; f: crop size as a fraction of the frame (< 1).
    -> [(t, pos)] crop positions, 0 = start edge, 1 = far edge; a single (0, pos) when the crop should stay still."""
    c = np.asarray(centres, float)
    dt = L / max(len(c) - 1, 1)
    w, out = float(np.clip(c[0], f / 2, 1 - f / 2)), []
    step = MAX_SPEED * dt
    for x in c:
        d = x - w
        if abs(d) > DEAD * f:  # outside the dead zone: move just far enough to bring the subject back to its edge
            want = d - np.sign(d) * DEAD * f
            w += float(np.clip(want, -step, step))
        w = float(np.clip(w, f / 2, 1 - f / 2))
        out.append(w)
    pos = _smooth(np.array(out), max(1, round(SMOOTH_S / dt)))
    pos = np.clip((pos - f / 2) / (1 - f), 0, 1)
    if pos.max() - pos.min() < FLAT:
        return [(0.0, round(float(np.median(pos)), 4))]
    idx = np.unique(np.linspace(0, len(pos) - 1, min(len(pos), MAX_KEYS)).round().astype(int))
    return [(round(float(i * dt), 4), round(float(pos[i]), 4)) for i in idx]
