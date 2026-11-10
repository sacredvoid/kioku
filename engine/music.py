"""Music map and auto fit (notes/editing-power-research.md, M1 and M2).

analyze(path) -> bar-level picture of a track from beats.analyze plus the decoded signal: energy per bar (0 to 9),
sections, the drop. Cached in data/music by path, mtime and size. fit(m, target, drop_at) picks the part of the track to
use as audio-spec fields (in, out, start, fades) so no render path changes. Vocal presence is not detected (needs the
SoundAnalysis classifier, a separate job); "vocals" is always null.
"""
import json
import os
import threading
from pathlib import Path

import beats
import core

CACHE = core.DATA / "music"
MUSIC_V = 1
HOP = 512
NPERSEG = 2048
LOW_HZ = 150
MIN_BARS = 3
MIN_SECTION_BARS = 4
_lock = threading.Lock()


def _bar_edges(downbeats, duration):
    """Bar start times plus the end of the last bar, or None when the track has no usable bar structure."""
    import numpy as np
    d = [float(t) for t in downbeats]
    if len(d) < 2:
        return None
    per = float(np.median(np.diff(d)))
    if per <= 0.2:
        return None
    end = d[-1] + per
    e = d + ([end] if end <= duration + 0.05 else ([duration] if duration - d[-1] >= 0.75 * per else []))
    return e if len(e) - 1 >= MIN_BARS else None


def build(x, analysis, sr=beats.SR):
    """The music map from mono signal x and a beats.analyze result. Raises RuntimeError without a bar structure."""
    import numpy as np
    from scipy import signal as sg
    duration = x.size / sr
    E = _bar_edges(analysis["downbeats"], duration)
    if E is None:
        raise RuntimeError("no clear bar structure (needs at least 3 bars with downbeats)")
    f, t, Z = sg.stft(x, fs=sr, nperseg=NPERSEG, noverlap=NPERSEG - HOP, padded=False, boundary=None)
    P = np.abs(Z) ** 2
    full = np.log10(P.sum(0) + 1e-9)
    low = np.log10(P[f < LOW_HZ].sum(0) + 1e-9)
    t = t.astype(float)

    def mean_in(v, a, b):
        m = (t >= a) & (t < b)
        return float(v[m].mean()) if m.any() else float(v.min())

    def norm(v):  # 0..1 between the 5th and 95th percentile, so one click cannot flatten the scale
        lo, hi = np.percentile(v, [5, 95])
        return np.clip((v - lo) / max(hi - lo, 1e-6), 0, 1)

    nf, nl = norm(full), norm(low)
    n = len(E) - 1
    bar = np.array([0.6 * mean_in(nf, E[i], E[i + 1]) + 0.4 * mean_in(nl, E[i], E[i + 1]) for i in range(n)])
    bar = (bar - bar.min()) / max(bar.max() - bar.min(), 1e-6)
    level = [int(round(9 * v)) for v in bar]
    per = float(np.median(np.diff(E)))

    # sections: bar starts where the next 2 bars differ most from the previous 2; at least `gap` bars apart
    cuts, gap = [], max(MIN_SECTION_BARS, round(8 / per))  # sections last at least 4 bars and about 8 s
    nov = [(abs(bar[i:i + 2].mean() - bar[max(i - 2, 0):i].mean()), i) for i in range(1, n)]
    for s, i in sorted(nov, reverse=True):
        if s < 0.15:
            break
        if all(abs(i - j) >= gap for j in cuts):
            cuts.append(i)
    b = [0, *sorted(cuts), n]
    b = [v for k, v in enumerate(b) if k == 0 or v == n or n - v >= MIN_SECTION_BARS]  # no tiny tail section
    sections = [{"start": round(E[a], 3), "end": round(E[c], 3), "bar": a, "bars": c - a,
                 "level": round(float(np.mean(level[a:c])), 1)} for a, c in zip(b, b[1:])]

    # drop: the bar whose first beat has the biggest low-band rise over the previous bar, and stays loud
    beat_len = per / 4
    drop_bar, best = None, 0.25
    for i in range(1, n):
        jump = mean_in(nl, E[i], E[i] + beat_len) - mean_in(nl, E[i - 1], E[i])
        if jump > best and bar[min(i + 1, n - 1)] >= bar[i - 1]:
            drop_bar, best = i, jump
    return {"v": MUSIC_V, "tempo": analysis.get("tempo", 0.0), "duration": round(duration, 3), "bar_s": round(per, 3),
            "bars": [round(e, 3) for e in E[:-1]], "end": round(E[-1], 3),
            "energy": "".join(map(str, level)), "sections": sections,
            "drop_s": round(E[drop_bar], 3) if drop_bar is not None else None, "drop_bar": drop_bar, "vocals": None}


def _key(p: Path):
    return f"{MUSIC_V}-{beats._key(p)}"


def cached(path):
    """The cached map or None; never computes."""
    try:
        return json.loads((CACHE / f"{_key(Path(path))}.json").read_text())
    except (OSError, ValueError):
        return None


def analyze(path):
    p = Path(path)
    got = cached(p)
    if got:
        return got
    with _lock:
        got = cached(p)
        if got:
            return got
        out = build(beats._decode(p), beats.analyze(p))
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = CACHE / f"{_key(p)}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(out))
        tmp.replace(CACHE / f"{_key(p)}.json")
        return out


def level_at(m, t):
    """0 to 9 energy of the bar containing source time t, or None outside the mapped bars."""
    bars = m["bars"]
    if not bars or t < bars[0] or t >= m["end"]:
        return None
    i = max(j for j, b in enumerate(bars) if b <= t)
    return int(m["energy"][i])


def fit(m, target, drop_at=None):
    """Audio-spec fields (in, out, start, fade_in, fade_out) that fit the track to `target` output seconds, plus notes.
    Cuts sit on bar edges. out - in never exceeds target (the render cuts a longer track before its fade plays), so the
    music can end up to one bar early; `music_end_s` says where. drop_at (output seconds; default 35% of target when the
    track has a drop) puts the drop that far into the video, to the nearest bar. No drop: the track opens at its first bar.
    Tracks shorter than the target play once, whole; looping is not done."""
    E = [*m["bars"], m["end"]]
    per, n, notes = m["bar_s"], len(m["bars"]), []
    drop = m.get("drop_s")
    if drop is None:
        if drop_at is not None:
            notes.append("no drop found in this track; it opens at its first bar and drop_at was ignored")
        drop_at = None
    elif drop_at is None:
        drop_at = 0.35 * target
    L = max(int((target + 0.15) / per), 1)  # whole bars that fit in the target (0.15 s of slack)
    if L >= n:
        a, e = 0, n
        if L > n:
            notes.append(f"the track has {E[n] - E[0]:.1f}s of bars but the edit is {target:.1f}s; it plays once and the rest is silent")
    else:
        a = 0
        if drop_at is not None:
            a = min(range(n - L + 1), key=lambda i: abs(E[i] - (drop - drop_at)))
        e = a + L
    res = {"in": round(E[a], 3), "out": round(E[e], 3), "start": 0.0, "fade_in": 0.1 if a else 0.0,
           "fade_out": round(min(per, 4.0), 2), "music_end_s": round(E[e] - E[a], 3)}
    if drop is not None and a <= m["drop_bar"] < e:
        res["drop_out_s"] = round(drop - E[a], 3)
        if abs(res["drop_out_s"] - drop_at) > per / 2 + 1e-6:
            notes.append(f"the drop lands at {res['drop_out_s']:.1f}s, not {drop_at:.1f}s (the track is too short to shift it)")
    elif drop is not None:
        notes.append("the chosen part of the track does not contain its drop")
    return {**res, "notes": notes}


def _curve(lv, k=8):
    """Mean level per eighth of the track as digits (the whole string when the track has fewer than k bars)."""
    if len(lv) < k:
        return "".join(map(str, lv))
    return "".join(str(round(sum(c) / len(c))) for c in (lv[i * len(lv) // k:(i + 1) * len(lv) // k] for i in range(k)))


def summary(m):
    """Shortlist view of a music map: tempo, a short energy curve (0 to 9), its shape, drop, length, one text line."""
    lv = [int(c) for c in m["energy"]]
    third = max(len(lv) // 3, 1)
    a, z = sum(lv[:third]) / third, sum(lv[-third:]) / third
    shape = "builds" if z - a >= 2 else "fades" if a - z >= 2 else "flat" if max(lv) - min(lv) <= 2 else "varies"
    out = {"tempo": round(m["tempo"], 1), "length_s": round(m["duration"], 1), "bars": len(lv), "curve": _curve(lv),
           "shape": shape, "avg_level": round(sum(lv) / len(lv), 1), "drop_s": m["drop_s"], "vocals": m.get("vocals")}
    out["summary"] = (f"{out['tempo']:g} bpm, energy {shape} (avg {out['avg_level']}/9), "
                      + (f"drop at {m['drop_s']:.0f}s" if m["drop_s"] is not None else "no clear drop") + f", {m['duration']:.0f}s")
    return out


def quick_features(x, sr=beats.SR):
    """Cheap feel of a snippet of mono audio, no beat model: tempo (onset autocorrelation, 70 to 170 bpm, a guess that can
    be off by a factor of two), energy 0 to 9 (loudness plus onset density), brightness. A heuristic for searching a library."""
    import numpy as np
    from scipy import signal as sg
    f, _, Z = sg.stft(x, fs=sr, nperseg=NPERSEG, noverlap=NPERSEG - HOP, padded=False, boundary=None)
    A = np.abs(Z)
    flux = np.maximum(np.diff(np.log1p(A * 50), axis=1), 0).sum(0)
    fps = sr / HOP
    rms_db = 20 * np.log10(float(np.sqrt(np.mean(x.astype("float64") ** 2))) + 1e-9)
    spec = A.mean(1)
    tempo = 0.0
    if flux.size > fps * 4 and flux.std() > 0:
        o = flux - flux.mean()
        ac = np.correlate(o, o, "full")[o.size - 1:]
        lo, hi = int(fps * 60 / 170), int(fps * 60 / 70)
        lags = np.arange(lo, hi + 1)
        w = ac[lo:hi + 1] * (1 - 0.15 * np.abs(np.log2(60 * fps / lags / 115)))  # mild pull toward 115 bpm
        tempo = round(float(60 * fps / lags[int(np.argmax(w))]), 1)
    onset = float((flux > flux.mean() + flux.std()).sum() / (flux.size / fps))  # strong onsets per second
    energy = int(round(9 * min(1.0, max(0.0, 0.5 * (rms_db + 38) / 26 + 0.5 * onset / 4))))
    return {"tempo": tempo, "energy": energy, "rms_db": round(float(rms_db), 1),
            "brightness_hz": round(float((spec * f).sum() / (spec.sum() + 1e-9)))}


QUICK_V = 1
QUICK_S = 45  # seconds of audio read per file, from 30% in (past the intro)


def _quick_path(p: Path):
    return CACHE / f"qf{QUICK_V}-{beats._key(p)}.json"


def cached_quick(path):
    """Cached quick_features of a library file, or None; never computes. {"error": true} marks an unreadable file."""
    try:
        return json.loads(_quick_path(Path(path)).read_text())
    except (OSError, ValueError):
        return None


def library_features(path, duration_s):
    """quick_features of one file (cached by path, mtime and size). Decodes at most QUICK_S seconds, so a whole library
    is affordable; a file that cannot be decoded is cached as {"error": true} and not retried until it changes."""
    import subprocess

    import numpy as np
    p = Path(path)
    got = cached_quick(p)
    if got:
        return got
    start = max(0.0, min(30.0, duration_s * 0.3))
    span = min(float(QUICK_S), duration_s - start)
    out = {"error": True}
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{span:.2f}", "-i", str(p),
                            "-vn", "-ac", "1", "-ar", str(beats.SR), "-f", "f32le", "-"], capture_output=True, timeout=60)
        x = np.frombuffer(r.stdout[: len(r.stdout) // 4 * 4], dtype="<f4")
        if r.returncode == 0 and x.size >= 5 * beats.SR:
            out = quick_features(x)
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE / f"qf{QUICK_V}-{os.getpid()}-{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(out))
    tmp.replace(_quick_path(p))
    return out


if __name__ == "__main__":
    import numpy as np
    sr = beats.SR
    t = np.arange(int(16 * sr)) / sr  # 120 BPM, 2 s bars: 4 quiet bars, then 4 loud bars with a kick
    x = 0.02 * np.sin(2 * np.pi * 440 * t)
    kick = 0.9 * np.sin(2 * np.pi * 55 * t) * np.exp(-8 * (t % 0.5))
    x = x + np.where(t >= 8, kick + 0.2 * np.sin(2 * np.pi * 1200 * t), 0)
    an = {"tempo": 120.0, "beats": [i * 0.5 for i in range(32)], "downbeats": [i * 2.0 for i in range(8)]}
    m = build(x.astype("float32"), an)
    assert m["drop_s"] == 8.0 and m["energy"][:4] < m["energy"][4:] and len(m["bars"]) == 8, m
    assert any(s["start"] == 8.0 for s in m["sections"]), m["sections"]
    assert level_at(m, 9.0) == int(m["energy"][4]) and level_at(m, 99) is None
    f = fit(m, 8.0, 4.0)  # 4 bars, drop 4 s in: starts at bar 2
    assert (f["in"], f["out"], f["drop_out_s"], f["music_end_s"]) == (4.0, 12.0, 4.0, 8.0), f
    assert fit(m, 30.0)["notes"] and fit(m, 30.0)["in"] == 0.0
    print("ok")
