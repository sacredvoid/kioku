"""Beat grid of an audio file (notes/pro-editing-design.md, phase 1a).

analyze(path) -> {"tempo", "beats", "downbeats"} in source seconds, from beat_this, cached in data/beats by path, mtime and size.
grid(track, kind, horizon) turns that into output-timeline times for a spec audio track, extended past both ends of the music.
"""
import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path

import core

CACHE = core.DATA / "beats"
BEATS_V = 1
SR = 22050
GRIDS = ("beat", "half", "downbeat")
_model = None
_lock = threading.Lock()  # one analysis at a time: the model is not thread safe and a song takes a few seconds


def _key(p: Path):
    st = p.stat()
    return hashlib.sha1(f"{BEATS_V}|{p}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:20]


def cached(path):
    """The cached analysis or None; never computes."""
    p = Path(path)
    try:
        return json.loads((CACHE / f"{_key(p)}.json").read_text())
    except (OSError, ValueError):
        return None


def _decode(p: Path):
    import numpy as np
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(p), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                       capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"{p.name} has no readable audio")
    x = np.frombuffer(r.stdout[: len(r.stdout) // 4 * 4], dtype="<f4")
    if x.size < SR:
        raise RuntimeError(f"{p.name} is shorter than a second")
    return x


def analyze(path):
    global _model
    p = Path(path)
    got = cached(p)
    if got:
        return got
    with _lock:
        got = cached(p)
        if got:
            return got
        x = _decode(p)
        if _model is None:
            from beat_this.inference import Audio2Beats
            _model = Audio2Beats(checkpoint_path="final0", device="cpu", dbn=False)
        b, d = _model(x, SR)
        b, d = [round(float(t), 4) for t in b], [round(float(t), 4) for t in d]
        out = {"tempo": round(60 / _median_period(b), 2) if len(b) > 1 else 0.0, "beats": b, "downbeats": d,
               "duration": round(x.size / SR, 3)}
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = CACHE / f"{_key(p)}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(out))
        tmp.replace(CACHE / f"{_key(p)}.json")
        return out


def _median_period(ts):
    """Least-squares slope of time against beat index: robust to the model's 20 ms frame steps, unlike a median of gaps."""
    import numpy as np
    if len(ts) < 2:
        return 0.5
    d = np.diff(ts)
    keep = np.abs(d - np.median(d)) < 0.25 * np.median(d)  # drop gaps from missed or doubled beats
    if keep.sum() < 2:
        return float(np.median(d))
    idx = np.concatenate([[0], np.cumsum(np.rint(d / np.median(d)))])
    return float(np.polyfit(idx, ts, 1)[0])


def grid(track, analysis, kind="beat", horizon=900.0):
    """Output-timeline seconds of the grid points for an audio track {start, in, out}: the analysed points inside [in, out]
    shifted by start - in, then extended with the median period back to 0 and forward to horizon, so clips before or after
    the music still land on a steady pulse. Sorted, unique, all >= 0."""
    if kind not in GRIDS:
        raise ValueError(f"grid must be one of {GRIDS}")
    pts = analysis["downbeats"] if kind == "downbeat" else analysis["beats"]
    if kind == "half":
        pts = pts[::2]
    a, b = track["in"], track["out"] if track.get("out") is not None else analysis.get("duration", 1e9)
    off = track["start"] - a
    inside = [t + off for t in pts if a - 1e-6 <= t <= b + 1e-6]
    if len(inside) < 2:  # no usable rhythm (silence, speech): fall back to the analysed tempo, or 120 BPM
        per = _median_period(pts) if len(pts) > 1 else 0.5
        first = inside[0] if inside else track["start"]
        inside = [first, first + per]
    per = _median_period(inside)
    out = list(inside)
    t = out[0] - per
    while t >= -1e-6:
        out.insert(0, max(t, 0.0))
        t -= per
    t = out[-1] + per
    while t <= horizon:
        out.append(t)
        t += per
    res = []
    for t in out:
        if not res or t - res[-1] > 1e-3:
            res.append(round(t, 4))
    return res


if __name__ == "__main__":
    tr = {"start": 2.0, "in": 1.0, "out": 3.0}
    an = {"beats": [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5], "downbeats": [1.0, 3.0], "duration": 4.0}
    g = grid(tr, an, "beat", 6.0)
    assert g[:3] == [0.0, 0.5, 1.0] and 2.0 in g and 4.0 in g and g[-1] <= 6.0, g
    assert all(abs((y - x) - 0.5) < 1e-3 for x, y in zip(g, g[1:])), g
    assert grid(tr, an, "half", 6.0) == sorted(set(grid(tr, an, "half", 6.0)))
    print("ok")
