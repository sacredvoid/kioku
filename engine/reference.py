"""Style reference (notes/pro-editing-design.md, phase 4).

analyze_reference(path) -> profile dict, cached in data/references/<sha1>.json (+ .jpg contact sheet, .cube look).
plan_from_profile(profile, n_clips, music_grid_len) -> recommendations in Kioku's own vocabulary (edit.XFADE, MOTIONS, SFX ...).
gaps(reference_profile, my_profile) -> short actionable lines for compare_to_reference.

The user supplies a local video file; nothing is downloaded. Shots come from TransNetV2 (transnetv2-pytorch), with PySceneDetect's
AdaptiveDetector as the fallback; the profile says which one ran. Beats come from beats.analyze on the reference's own audio.
"""
import hashlib
import json
import os
import re
import statistics
import subprocess
import tempfile
import threading
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

import beats
import core
import edit

router = APIRouter()
REFS = core.DATA / "references"
REF_V = 1
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
MAX_SECONDS = 15 * 60
CUT_TOL_FRAMES = 2
LUT_STRENGTH = 0.6  # a LUT baked from stats must not overpower the footage
AH = 90             # analysis height; width follows the aspect
MIN_TRANS = edit.MIN_TRANS_FRAMES / 30
_lock = threading.Lock()  # one analysis at a time: TransNetV2 and the beat model are heavy and not thread safe
_transnet = None


# ---------- input

def _probe(p: Path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,avg_frame_rate,r_frame_rate:format=duration",
                        "-of", "json", str(p)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise ValueError(f"{p.name} is not a readable video")
    j = json.loads(r.stdout or "{}")
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v or not v.get("width"):
        raise ValueError(f"{p.name} has no video stream")

    def rate(s):
        a, _, b = s.partition("/")
        return float(a) / float(b or 1) if float(b or 1) else 0.0
    fps = rate(v.get("avg_frame_rate", "0/1")) or rate(v.get("r_frame_rate", "0/1"))
    dur = float(j.get("format", {}).get("duration") or 0)
    if not (1 <= fps <= 240) or dur <= 0:
        raise ValueError(f"{p.name} has no usable frame rate or length")
    return {"w": int(v["width"]), "h": int(v["height"]), "fps": fps, "duration": dur,
            "audio": any(s.get("codec_type") == "audio" for s in j["streams"])}


def validate(path):
    """(resolved Path, probe info) of a local video file, or ValueError. No URLs, no folders, no other extensions, at most 15 minutes."""
    if not isinstance(path, str) or not path.strip() or "://" in path:
        raise ValueError("give a path to a video file on this Mac; links are not downloaded")
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"no such file: {p}")
    if p.suffix.lower() not in VIDEO_EXT:
        raise ValueError(f"reference must be a video ({', '.join(sorted(VIDEO_EXT))}), not {p.suffix or 'a file without extension'}")
    p = p.resolve()
    info = _probe(p)
    if info["duration"] > MAX_SECONDS:
        raise ValueError(f"reference is {info['duration'] / 60:.0f} min; the limit is {MAX_SECONDS // 60}")
    return p, info


def _key(p: Path):
    st = p.stat()
    return hashlib.sha1(f"{REF_V}|{p}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()


# ---------- measurements

def _pass(p: Path, info, afps):
    """One decode at low resolution: per-frame luma, frame difference, camera motion (similarity transform) and Lab/HSV sums."""
    import cv2
    w = max(16, round(AH * info["w"] / info["h"] / 2) * 2)
    fsz = w * AH * 3
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(p), "-an", "-vf", f"fps={afps},scale={w}:{AH}:flags=area", "-pix_fmt", "rgb24",
           "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    luma, diff, tx, ty, ls, fit = [], [], [], [], [], []
    acc = np.zeros(3), np.zeros(3), 0  # Lab sum, sum of squares, pixel count
    sat = []
    hist = np.zeros(256)
    prev = None
    try:
        while True:
            buf = proc.stdout.read(fsz)
            if len(buf) < fsz:
                break
            rgb = np.frombuffer(buf, np.uint8).reshape(AH, w, 3)
            g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            luma.append(float(g.mean()))
            dx = dy = s = 0.0
            q = 1.0
            if prev is None:
                diff.append(0.0)
            else:
                diff.append(float(np.abs(g.astype(np.int16) - prev).mean()))
                p0 = cv2.goodFeaturesToTrack(prev, 150, 0.01, 4)
                if p0 is not None and len(p0) >= 8:
                    p1, st, _ = cv2.calcOpticalFlowPyrLK(prev, g, p0, None)
                    ok = st.ravel() == 1
                    if ok.sum() >= 8:
                        M, _ = cv2.estimateAffinePartial2D(p0[ok], p1[ok])
                        if M is not None:
                            dx, dy, s = float(M[0, 2]), float(M[1, 2]), float(np.log(max(np.hypot(M[0, 0], M[1, 0]), 1e-3)))
                            if s > 0.06:  # big jump: does warping the last frame by it explain this one? (a cut does not)
                                c = (slice(AH // 6, -AH // 6), slice(w // 6, -w // 6))
                                e1 = np.abs(g[c].astype(np.int16) - prev[c]).mean()
                                e2 = np.abs(g[c].astype(np.int16) - cv2.warpAffine(prev, M, (w, AH))[c]).mean()
                                q = float(e2 / (e1 + 1e-6))
            tx.append(dx), ty.append(dy), ls.append(s), fit.append(q)
            prev = g
            if len(luma) % 5 == 1:
                lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).reshape(-1, 3).astype(np.float64)
                lab[:, 0] *= 100 / 255
                lab[:, 1:] -= 128
                acc = acc[0] + lab.sum(0), acc[1] + (lab ** 2).sum(0), acc[2] + len(lab)
                sat.append(float(cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)[..., 1].mean() / 255))
                hist += np.bincount(np.clip(lab[:, 0] * 2.55, 0, 255).astype(np.uint8), minlength=256)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
    n = max(acc[2], 1)
    mean = acc[0] / n
    std = np.sqrt(np.maximum(acc[1] / n - mean ** 2, 0))
    cum = np.cumsum(hist) / max(hist.sum(), 1)
    color = {"lab_mean": [round(float(x), 2) for x in mean], "lab_std": [round(float(x), 2) for x in std],
             "saturation": round(float(np.mean(sat)) if sat else 0.0, 3), "contrast": round(float(std[0]), 2),
             "warmth": round(float(mean[2]), 2), "black_level": round(float(np.searchsorted(cum, 0.02)) / 2.55, 2)}
    return {"w": w, "luma": np.array(luma), "diff": np.array(diff), "tx": np.array(tx), "ty": np.array(ty), "ls": np.array(ls), "fit": np.array(fit)}, color


def _transnet_shots(p: Path, fps):
    """[(time, 'cut'|'gradual', duration)] from TransNetV2: the single-frame head marks the boundary, the many-hot head its extent."""
    global _transnet
    import torch
    if _transnet is None:
        from transnetv2_pytorch import TransNetV2
        _transnet = TransNetV2(device="cpu")
        _transnet.eval()
    with torch.no_grad():
        _, single, many = _transnet.predict_video(str(p), quiet=True)
    single, many = np.asarray(single).ravel(), np.asarray(many).ravel()
    hot = many > 0.5
    out, skip_to = [], -1
    idx = np.where(single > 0.5)[0]
    groups = np.split(idx, np.where(np.diff(idx) > 1)[0] + 1) if len(idx) else []
    for g in groups:
        f = int(g[np.argmax(single[g])])
        if f <= skip_to:
            continue
        a = b = f
        while a > 0 and hot[a - 1]:
            a -= 1
        while b + 1 < len(hot) and hot[b + 1]:
            b += 1
        n = b - a + 1
        if n > CUT_TOL_FRAMES and hot[f]:
            out.append(((a + b + 1) / 2 / fps, "gradual", round(n / fps, 3)))
            skip_to = b
        else:
            out.append(((f + 1) / fps, "cut", 0.0))
    return out


def _fallback_shots(p: Path, fps, diff, afps):
    """PySceneDetect AdaptiveDetector for hard cuts; gradual transitions are runs of raised frame difference without a spike."""
    from scenedetect import AdaptiveDetector, detect
    cuts = [s[0].get_seconds() for s in detect(str(p), AdaptiveDetector())[1:]]
    out = [(t, "cut", 0.0) for t in cuts]
    if len(diff) > 12:
        base = float(np.median(diff)) + 1.0
        hi = diff > 2.5 * base
        i = 0
        while i < len(hi):
            if hi[i]:
                j = i
                while j + 1 < len(hi) and hi[j + 1]:
                    j += 1
                mid = (i + j) / 2 / afps
                n = j - i + 1
                if 4 <= n <= 60 and diff[i:j + 1].max() < 6 * base and not any(abs(mid - c) < (n / afps) / 2 + 0.1 for c in cuts):
                    out.append((mid, "gradual", round(n / afps, 3)))
                i = j + 1
            else:
                i += 1
    return sorted(out)


def _is_punch(m, t, afps):
    i = round(t * afps)
    for j in (i - 1, i, i + 1):
        if 0 < j < len(m["ls"]) and m["ls"][j] > 0.06 and m["fit"][j] < 0.6 and abs(m["tx"][j]) < 0.1 * m["w"]:
            return True
    return False


def _flashes(luma, afps):
    """Luma spikes of 1 to 4 frames that return to where they started (a cut changes the level for good, so it is not one)."""
    out, i, n = [], 1, len(luma)
    while i < n:
        if luma[i] - luma[i - 1] > 30:
            for j in range(i, min(i + 4, n - 1)):
                if luma[j] - luma[j + 1] > 30 and abs(luma[i - 1] - luma[j + 1]) < 20:
                    out.append((i, j))
                    i = j
                    break
        i += 1
    return [(a / afps, (b + 1) / afps) for a, b in out]


def _camera(m, a, b, skip, afps):
    """(label, punch times, shake) for analysis frames a..b of one shot. skip: frame ranges to ignore (transitions)."""
    ok = np.ones(len(m["ls"]), bool)
    for s, e in skip:
        ok[max(s, 0):e + 1] = False
    ok[:a + 3] = False
    ok[b - 1:] = False
    ls, tx = m["ls"], m["tx"]
    s3 = np.convolve(ls, np.ones(3), "full")[:len(ls)]
    punches, last = [], -99
    for i in np.where((s3 > 0.06) & ok)[0]:
        if i - last > 0.4 * afps:
            punches.append(round(float(i) / afps, 2))
        last = i
    mask = ok & (np.abs(ls) < 0.03)  # a punch is not a push
    if mask.sum() < 0.4 * afps:
        return "unknown", punches, False
    zoom, pan = float(ls[mask].sum()), float(tx[mask].sum() / m["w"])
    shake = bool(np.std(tx[mask] - np.convolve(tx, np.ones(5) / 5, "same")[mask]) > 1.5)
    if max(abs(zoom) / 0.05, abs(pan) / 0.06) < 1:
        return "static", punches, shake
    if abs(zoom) / 0.05 >= abs(pan) / 0.06:
        return ("push" if zoom > 0 else "pull"), punches, shake
    return ("drift-right" if pan < 0 else "drift-left"), punches, shake  # content moving left = camera panning right


def _text(p: Path):
    """Apple Vision OCR on 1 frame per second. None when Vision is unavailable."""
    try:
        import objc
        import Vision
        from Foundation import NSURL
    except ImportError:
        return None
    seen, times, first = [], 0, None
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(p), "-an", "-vf", "fps=1,scale=960:-2", "-q:v", "4", f"{d}/%05d.jpg"],
                       check=True, timeout=600)
        files = sorted(Path(d).glob("*.jpg"))
        for k, f in enumerate(files):
            with objc.autorelease_pool():
                req = Vision.VNRecognizeTextRequest.alloc().init()
                req.setRecognitionLevel_(0)
                with core.VISION_SLOTS:  # shared with moments: unbounded parallel Vision calls deadlock
                    Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(f)), None).performRequests_error_([req], None)
                hits = []
                for o in req.results() or []:
                    c = o.topCandidates_(1)[0]
                    bb = o.boundingBox()
                    if c.confidence() >= 0.5 and bb.size.height >= 0.02 and len(str(c.string()).strip()) >= 2:
                        hits.append((bb.origin.y + bb.size.height / 2, bb.size.height, len(str(c.string()).split())))
            if hits:
                times += 1
                first = k if first is None else first
                seen += hits
    if not files:
        return None
    band = None
    if seen:
        ys = [("top" if y > 0.66 else "bottom" if y < 0.34 else "middle") for y, _, _ in seen]
        band = max(set(ys), key=ys.count)
    return {"share_time": round(times / len(files), 3), "band": band, "rel_height": round(statistics.median(h for _, h, _ in seen), 3) if seen else None,
            "words_per_text": statistics.median(n for _, _, n in seen) if seen else None, "first_s": first, "sampled": len(files)}


def _sections(dur, wav, shots, bt):
    """4 to 8 sections split where the audio energy changes most (equal parts when there is no audio)."""
    n = min(max(round(dur / 15), 4), 8)
    gap = dur / (2 * n)
    cuts = []
    try:
        import soundfile as sf
        x, sr = sf.read(wav, dtype="float32")
        hop = int(sr * 0.5)
        e = np.array([np.sqrt(np.mean(x[i:i + hop] ** 2)) for i in range(0, len(x) - hop, hop)])
        w = 6
        nov = np.array([abs(e[i:i + w].mean() - e[max(i - w, 0):i].mean()) if i else 0 for i in range(len(e))]) / (e.mean() + 1e-9)
        for i in np.argsort(-nov):
            t = i * 0.5
            if len(cuts) >= n - 1:
                break
            if gap < t < dur - gap and all(abs(t - c) > gap for c in cuts):
                cuts.append(t)
    except Exception:  # noqa: BLE001 - no audio or unreadable: equal parts
        pass
    if len(cuts) < n - 1:
        cuts = [dur * k / n for k in range(1, n)]
    edges = [0.0] + sorted(cuts) + [dur]
    out = []
    for a, b in zip(edges, edges[1:]):
        ss = [s for s in shots if a <= s["start"] < b]
        out.append({"start": round(a, 2), "end": round(b, 2), "shots": len(ss),
                    "median_seconds": round(statistics.median(s["length"] for s in ss), 2) if ss else None,
                    "median_beats": round(statistics.median(s["beats"] for s in ss), 2) if ss and bt else None})
    return out


def _dist(xs):
    if not xs:
        return None
    q = np.percentile(xs, [10, 50, 90])
    return {"p10": round(float(q[0]), 2), "p50": round(float(q[1]), 2), "p90": round(float(q[2]), 2)}


def _sheet(p: Path, shots, out):
    import cv2
    import media
    pick = shots if len(shots) <= 24 else [shots[round(i * (len(shots) - 1) / 23)] for i in range(24)]
    cap, frames, spans = cv2.VideoCapture(str(p)), [], []
    for s in pick:
        cap.set(cv2.CAP_PROP_POS_MSEC, (s["start"] + s["length"] / 2) * 1000)
        ok, f = cap.read()
        frames.append(f if ok else None)
        spans.append((s["start"], s["start"] + s["length"]))
    cap.release()
    return media.contact_sheet(frames, spans, out)


def bake_cube(color, out, src=None, strength=LUT_STRENGTH):
    """33^3 .cube that moves Lab mean/std from src stats (default: typical camera footage) toward the reference's (Reinhard)."""
    import cv2
    src = src or {"lab_mean": [48.0, 2.0, 8.0], "lab_std": [22.0, 9.0, 13.0]}
    k = min(max(strength, 0.0), LUT_STRENGTH)
    ms, ss = np.array(src["lab_mean"], np.float32), np.array(src["lab_std"], np.float32) + 1e-6
    mr, sr = np.array(color["lab_mean"], np.float32), np.array(color["lab_std"], np.float32)
    ratio = np.clip(sr / ss, 0.7, 1.4)  # a flat or crushed reference must not blow the footage up
    n = 33
    g = np.linspace(0, 1, n, dtype=np.float32)
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")  # .cube order: red varies fastest
    rgb = np.stack([r, gg, b], -1).reshape(-1, 1, 3)
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)       # float input: L 0..100, a/b centred
    moved = (lab - ms) * ratio + mr
    lab = lab + k * (moved - lab)
    res = np.clip(cv2.cvtColor(lab.astype(np.float32), cv2.COLOR_LAB2RGB), 0, 1).reshape(-1, 3)
    lines = ['TITLE "kioku reference"', f"LUT_3D_SIZE {n}"] + [f"{a:.6f} {b_:.6f} {c:.6f}" for a, b_, c in res]
    Path(out).write_text("\n".join(lines) + "\n")
    return str(out)


# ---------- the profile

def analyze_reference(path, sheet=True, cube=True, detector=None):
    """Profile of a local video; cached by path, mtime and size. Raises ValueError for bad input.
    detector="scenedetect" forces the fallback shot detector (tests)."""
    p, info = validate(path)
    key = _key(p)
    jf = REFS / f"{key}.json"
    if detector is None:
        try:
            got = json.loads(jf.read_text())
            if (not sheet or got.get("sheet")) and (not cube or got.get("cube")):
                return got
        except (OSError, ValueError):
            pass
    with _lock:
        REFS.mkdir(parents=True, exist_ok=True)
        fps, dur = info["fps"], info["duration"]
        afps = min(fps, 30.0)
        m, color = _pass(p, info, afps)
        flashes = _flashes(m["luma"], afps)
        used = "transnetv2"
        try:
            if detector == "scenedetect":
                raise RuntimeError("forced")
            marks = _transnet_shots(p, fps)
        except Exception:  # noqa: BLE001 - torch or weights unavailable: fall back, and say so
            used = "scenedetect"
            marks = _fallback_shots(p, fps, m["diff"], afps)
        marks = [t for t in marks if t[1] != "cut" or not _is_punch(m, t[0], afps)]  # a scale jump that tracks cleanly is a punch-in, not a cut
        marks = [t for t in marks if 0.05 < t[0] < dur - 0.05 and not any(a - 0.1 <= t[0] <= b + 0.1 for a, b in flashes)]
        bounds = [0.0] + [t[0] for t in marks] + [dur]
        bt, wav = None, None
        with tempfile.TemporaryDirectory() as td:
            if info["audio"]:  # beats on the reference's own audio
                wav = f"{td}/a.wav"
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(p), "-vn", "-ac", "1", "-ar", "22050", wav],
                               check=True, timeout=600)
                try:
                    bt = beats.analyze(wav)
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    bt = None
                if bt and len(bt["beats"]) < 4:
                    bt = None
            period = 60 / bt["tempo"] if bt and bt["tempo"] else None
            skip = [(int((t - d / 2) * afps) - 2, int((t + d / 2) * afps) + 2) for t, k, d in marks if k == "gradual"]
            skip += [(round(x * afps) - 2, round(y * afps) + 2) for x, y in flashes]
            shots = []
            for i in range(len(bounds) - 1):
                a, b = round(bounds[i] * afps), min(round(bounds[i + 1] * afps), len(m["ls"]) - 1)
                cam, punches, shake = _camera(m, a, b, skip, afps) if b - a > 5 else ("unknown", [], False)
                ln = bounds[i + 1] - bounds[i]
                shots.append({"start": round(bounds[i], 3), "length": round(ln, 3), "beats": round(ln / period, 2) if period else None,
                              "camera": cam, "punches": punches, "shake": shake})
            sections = _sections(dur, wav, shots, bt)
        hard = [t for t, k, _ in marks if k == "cut"]
        grad = [d for _, k, d in marks if k == "gradual"]
        offs = [float(np.min(np.abs(np.array(bt["beats"]) - t))) for t in hard] if bt and hard else []
        bl = [s["beats"] for s in shots if s["beats"] is not None]
        moving = [s for s in shots if s["camera"] in ("push", "pull", "drift-left", "drift-right")]
        by_type = {k: sum(s["camera"] == k for s in moving) for k in ("push", "pull", "drift-left", "drift-right")}
        hist = {}
        for x in bl:
            k = str(max(1, round(x)))
            hist[k] = hist.get(k, 0) + 1
        ns = max(len(shots), 1)
        prof = {
            "v": REF_V, "id": key, "source": p.name, "duration": round(dur, 2), "fps": round(fps, 3), "width": info["w"], "height": info["h"],
            "aspect": min(edit.ASPECTS, key=lambda k: abs(edit.ASPECTS[k][0] / edit.ASPECTS[k][1] - info["w"] / info["h"])),
            "detector": used,
            "pacing": {"shots": len(shots), "cuts_per_min": round(len(marks) / dur * 60, 1), "seconds": _dist([s["length"] for s in shots]),
                       "beats": _dist(bl), "beat_hist": dict(sorted(hist.items(), key=lambda kv: int(kv[0]))), "sections": sections},
            "music": {"tempo": bt["tempo"] if bt else None,
                      "cut_on_beat": round(float(np.mean([o <= CUT_TOL_FRAMES / fps + 1e-6 for o in offs])), 3) if offs else None,
                      "median_offset_s": round(float(np.median(offs)), 3) if offs else None},
            "transitions": {"cut": len(hard), "gradual": len(grad), "gradual_share": round(len(grad) / max(len(marks), 1), 3),
                            "gradual_median_s": round(float(np.median(grad)), 2) if grad else None},
            "motion": {"moving_share": round(len(moving) / ns, 3), "by_type": by_type,
                       "shake_share": round(sum(s["shake"] for s in shots) / ns, 3),
                       "punch_shots": sum(bool(s["punches"]) for s in shots), "punches": [t for s in shots for t in s["punches"]],
                       "flashes": [round(a, 2) for a, _ in flashes]},
            "text": _text(p), "color": color, "shots": shots,
        }
        if cube:
            prof["cube"] = bake_cube(color, REFS / f"{key}.cube")
        if sheet:
            try:
                prof["sheet"] = str(_sheet(p, shots, REFS / f"{key}.jpg") or "")
            except Exception:  # noqa: BLE001 - the numbers still help without the picture
                prof["sheet"] = ""
        if detector is None:  # a forced detector is a test; keep it out of the cache
            tmp = REFS / f"{key}.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(prof))
            tmp.replace(jf)
        return prof


# ---------- planner

def _look_name(c):
    """Nearest edit.LOOKS preset by saturation, contrast and warmth; None when the reference is close to neutral."""
    if c["saturation"] < 0.04:
        return "mono"
    if c["saturation"] > 0.42:
        return "punch"
    if c["saturation"] < 0.2 and c["contrast"] > 24:
        return "film"
    if c["warmth"] > 14:
        return "warm"
    if c["warmth"] < 2:
        return "cool"
    return None


def plan_from_profile(profile, n_clips, music_grid_len=0):
    """Concrete values for set_edit / set_clip_fx / set_transition. music_grid_len: beats in the music, 0 when unknown.
    Every enum it returns exists in edit.py (checked by test_reference.py)."""
    n = max(int(n_clips), 1)
    shots = profile["shots"]
    use_beats = bool(shots) and all(s["beats"] is not None for s in shots)
    raw = [s["beats"] for s in shots] if use_beats else [s["length"] / 0.5 for s in shots]  # unknown tempo: assume 120 BPM
    med = statistics.median(raw) if raw else 2.0
    grid, unit = ("downbeat", 4) if med >= 6 else ("half", 2) if med >= 3 else ("beat", 1)
    # the reference's own rhythm, stretched over n clips so its fast and slow sections keep their place
    pat = [min(max(round(raw[round(i * (len(raw) - 1) / max(n - 1, 1))] / unit), 1), edit.MAX_BEATS) for i in range(n)] if raw else [1] * n
    fits = True
    if music_grid_len and sum(pat) * unit > music_grid_len:
        fits = False
        k = music_grid_len / (sum(pat) * unit)
        pat = [max(1, int(x * k)) for x in pat]
    mo = profile["motion"]
    types = [(k, v) for k, v in mo["by_type"].items() if v and k in edit.MOTIONS]
    moving = round(n * mo["moving_share"])
    tot = sum(v for _, v in types) or 1
    ns = max(len(shots), 1)
    budgets = {"motion_clips": moving, "motion_types": {k: round(moving * v / tot) for k, v in types},
               "punch": min(round(n * mo["punch_shots"] / ns), n), "flash": min(round(n * len(mo["flashes"]) / ns), n),
               "shake": min(round(n * mo["shake_share"]), n // 2)}  # edit.py refuses shake on more than half the clips
    tr = profile["transitions"]
    g = tr["gradual_share"]
    mix = [{"type": "cut", "share": round(1 - g, 2), "duration": 0.0, "sfx": "none"}]
    if g > 0:
        d = tr["gradual_median_s"] or 0.5
        if d <= 0.3:
            kinds = [("whip-left", "whoosh"), ("whip-right", "whoosh")]
        elif d <= 1.2:
            kinds = [("dissolve", "none")]
        else:
            kinds, d = [("fade-black", "none")], min(d, 1.5)
        mix += [{"type": t, "share": round(g / len(kinds), 2), "duration": round(min(max(d, MIN_TRANS), 2.0), 2), "sfx": s} for t, s in kinds]
    tx = profile.get("text")
    captions = None
    if tx and tx["share_time"] >= 0.15:
        h = tx["rel_height"] or 0.05
        captions = {"on": True, "position": tx["band"] if tx["band"] in edit.CAPTION_POSITIONS else "bottom",
                    "size": "S" if h < 0.04 else "M" if h < 0.08 else "L", "style": "pop" if (tx["words_per_text"] or 5) <= 3 else "bold"}
    title = bool(tx and tx["first_s"] is not None and tx["first_s"] <= 3 and tx["share_time"] > 0.02)
    look = {"name": _look_name(profile["color"]), "strength": 0.6, "cube": profile.get("cube")}
    return {"sync": {"grid": grid, "unit_beats": unit, "cut_on_beat": (profile["music"]["cut_on_beat"] or 0) >= 0.5},
            "beats_pattern": pat, "fits_music": fits, "motion_share": mo["moving_share"], "budgets": budgets, "transitions": mix,
            "captions": captions, "title_card": title, "look": look, "aspect": profile["aspect"]}


# ---------- reading it back

def summarize(p, plan=None):
    pc, mo, tr, mu, c, tx = p["pacing"], p["motion"], p["transitions"], p["music"], p["color"], p.get("text")
    L = [f"{p['source']}: {p['duration']:.0f}s {p['width']}x{p['height']} {p['fps']:.0f}fps ({p['aspect']}), shots by {p['detector']}",
         f"Pacing: {pc['shots']} shots, {pc['cuts_per_min']} cuts/min, median {pc['seconds']['p50']}s"
         + (f" = {pc['beats']['p50']} beats at {mu['tempo']} BPM" if pc["beats"] else " (no beat found)")
         + (f", {round(mu['cut_on_beat'] * 100)}% of hard cuts on a beat" if mu["cut_on_beat"] is not None else ""),
         "Sections (median beats/shot): " + ", ".join(f"{s['start']:.0f}s:{s['median_beats'] if s['median_beats'] is not None else s['median_seconds']}" for s in pc["sections"]),
         f"Transitions: {tr['cut']} hard cuts, {tr['gradual']} gradual" + (f" (about {tr['gradual_median_s']}s)" if tr["gradual"] else ""),
         f"Motion: {round(mo['moving_share'] * 100)}% of shots move {mo['by_type']}, {len(mo['punches'])} punch-ins, {len(mo['flashes'])} flashes",
         "Text: " + (f"{round(tx['share_time'] * 100)}% of the time, {tx['band']}, height {tx['rel_height']}, {tx['words_per_text']} words" if tx and tx["share_time"] else "none seen"),
         f"Color: saturation {c['saturation']}, contrast {c['contrast']}, warmth b {c['warmth']}"]
    out = "\n".join(L)
    if plan:
        b = plan["budgets"]
        out += ("\n\nPLAN\n" + f"sync grid={plan['sync']['grid']} (1 unit = {plan['sync']['unit_beats']} beat)\n"
                f"beats per clip: {plan['beats_pattern']}" + ("" if plan["fits_music"] else " (scaled down to fit the music)") + "\n"
                f"motion: {b['motion_clips']} clips {b['motion_types']}, punch {b['punch']}, flash {b['flash']}, shake {b['shake']}\n"
                "transitions: " + ", ".join(f"{t['type']} {round(t['share'] * 100)}%" + (f" {t['duration']}s sfx {t['sfx']}" if t["type"] != "cut" else "") for t in plan["transitions"]) + "\n"
                f"captions: {plan['captions'] or 'off'}; title card: {plan['title_card']}\n"
                f"look: {plan['look']['name']} at {plan['look']['strength']}")
    return out


def gaps(ref, mine, top=4):
    """Biggest differences between the reference profile and a render's profile, worst first, as short actions."""
    out = []

    def add(score, line):
        if score > 0.15:
            out.append((score, line))
    rb, mb = ref["pacing"]["beats"], mine["pacing"]["beats"]
    rs, ms = ref["pacing"]["seconds"]["p50"], mine["pacing"]["seconds"]["p50"]
    if rb and mb:
        add(abs(mb["p50"] - rb["p50"]) / max(rb["p50"], 0.5),
            f"Pacing: median {mb['p50']} beats per shot, reference {rb['p50']}. Set clip beats to about {max(1, round(rb['p50']))} (set_clip_fx beats).")
    else:
        add(abs(ms - rs) / max(rs, 0.3), f"Pacing: median shot {ms}s, reference {rs}s. Shorten or lengthen clips toward {rs}s.")
    rc, mc = ref["music"]["cut_on_beat"], mine["music"]["cut_on_beat"]
    if rc is not None and mc is not None:
        add((rc - mc) * 0.8, f"Sync: {round(mc * 100)}% of cuts on a beat, reference {round(rc * 100)}%. Turn sync on and set beats on every clip.")
    rm, mm = ref["motion"]["moving_share"], mine["motion"]["moving_share"]
    add(abs(rm - mm) * 1.2, f"Motion: {round(mm * 100)}% of shots move, reference {round(rm * 100)}%. "
        + ("Add push/drift motion to more clips." if mm < rm else "Drop motion from some clips."))
    rp = len(ref["motion"]["punches"]) / ref["duration"] * 60
    mp = len(mine["motion"]["punches"]) / mine["duration"] * 60
    add(abs(rp - mp) / max(rp, 3) * 0.8, f"Punch-ins: {mp:.0f}/min, reference {rp:.0f}/min. " + ("Add motion punch on beats." if mp < rp else "Use fewer punch clips."))
    rf = len(ref["motion"]["flashes"]) / ref["duration"] * 60
    mf = len(mine["motion"]["flashes"]) / mine["duration"] * 60
    add(abs(rf - mf) / max(rf, 3) * 0.6, f"Flashes: {mf:.0f}/min, reference {rf:.0f}/min. " + ("Set flash on a few clips at peaks." if mf < rf else "Remove some flash."))
    rg, mg = ref["transitions"]["gradual_share"], mine["transitions"]["gradual_share"]
    add(abs(rg - mg) * 1.2, f"Transitions: {round(mg * 100)}% gradual, reference {round(rg * 100)}% (typical {ref['transitions']['gradual_median_s']}s). "
        + ("Use set_transition dissolve or whip on more seams." if mg < rg else "Switch some seams back to cut."))
    rt = (ref.get("text") or {}).get("share_time") or 0
    mt = (mine.get("text") or {}).get("share_time") or 0
    add(abs(rt - mt) * 1.5, f"Text: on screen {round(mt * 100)}% of the time, reference {round(rt * 100)}%. "
        + ("Turn captions on or add titles." if mt < rt else "Show less text."))
    rc_, mc_ = ref["color"], mine["color"]
    d = max(abs(rc_["saturation"] - mc_["saturation"]) / max(rc_["saturation"], 0.1), abs(rc_["contrast"] - mc_["contrast"]) / max(rc_["contrast"], 5))
    add(d * 0.7, f"Color: saturation {mc_['saturation']} vs {rc_['saturation']}, contrast {mc_['contrast']} vs {rc_['contrast']}. "
        f"Try look '{_look_name(rc_) or 'warm'}' (set_edit look) or the reference .cube.")
    return [line for _, line in sorted(out, key=lambda t: -t[0])[:top]]


# ---------- links (YouTube and anything else yt-dlp reads)

YTDLP_PATHS = ("/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp", str(Path.home() / ".local/bin/yt-dlp"))


def ytdlp_bin():
    import shutil
    import glob
    # the app starts the engine with a bare PATH, so also look where Homebrew, pipx and python.org installs put it
    extra = sorted(glob.glob("/Library/Frameworks/Python.framework/Versions/*/bin/yt-dlp"), reverse=True)
    return shutil.which("yt-dlp") or next((p for p in (*YTDLP_PATHS, *extra) if Path(p).is_file()), None)


def tool_env():
    """The app's bare PATH hides Homebrew: yt-dlp then can't find deno (YouTube needs a JS runtime) or ffmpeg to merge."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "/usr/bin:/bin")])
    return env


def is_link(s):
    from urllib.parse import urlparse
    u = urlparse(s.strip())
    return u.scheme in ("http", "https") and bool(u.netloc)


def fetch_link(url):
    """Download a reference link once (at most 1080p, 15 min) into DATA/references/src and return the local path.
    The user asked for this video as a style reference; it never leaves this Mac."""
    url = url.strip()
    if not is_link(url):
        raise ValueError("a reference link must start with http:// or https://")
    exe = ytdlp_bin()
    if not exe:
        raise ValueError("links need yt-dlp: run `brew install yt-dlp`, or save the video and give its file instead")
    out = REFS / "src" / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}.mp4"
    if out.is_file():
        return str(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmpl = str(out.with_suffix("")) + ".part-dl.%(ext)s"
    try:
        r = subprocess.run([exe, "--no-playlist", "--no-progress", "--max-filesize", "800M",
                            "--match-filter", f"duration <= {MAX_SECONDS}",
                            "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b[height<=1080]/b",
                            "--merge-output-format", "mp4", "-o", tmpl, "--", url],
                           capture_output=True, text=True, timeout=900, env=tool_env())
    except subprocess.TimeoutExpired:
        raise RuntimeError("downloading the reference took over 15 minutes")
    got = sorted(out.parent.glob(out.stem + ".part-dl.*"), key=lambda f: f.stat().st_size, reverse=True)
    vid = next((f for f in got if f.suffix.lower() in VIDEO_EXT), None)
    if r.returncode != 0 or not vid:
        for f in got:
            f.unlink(missing_ok=True)
        why = (r.stderr.strip().splitlines() or ["no video was downloaded (longer than 15 min, or not a video page?)"])[-1]
        if "403" in r.stderr or "JavaScript runtime" in r.stderr:  # YouTube changes often; old yt-dlp builds get blocked
            why = "the site refused this yt-dlp (it is out of date or has no JavaScript runtime). Run `brew install yt-dlp deno`, then try again"
        raise ValueError(f"could not download the reference: {why[:300]}")
    vid.replace(out)
    for f in got:
        f.unlink(missing_ok=True)
    return str(out)


def local(path_or_link):
    return fetch_link(path_or_link) if is_link(path_or_link) else path_or_link


# ---------- routes

class AnalyzeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class CompareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    collection_id: int


def _run(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        raise HTTPException(500, f"could not analyze: {str(e)[:300]}")


@router.post("/references/analyze")
def analyze_route(body: AnalyzeBody):
    prof = _run(lambda: analyze_reference(local(body.path)))
    plan = plan_from_profile(prof, max(prof["pacing"]["shots"], 1))
    return {"id": prof["id"], "profile": prof, "plan": plan, "summary": summarize(prof, plan), "sheet": f"/references/{prof['id']}/sheet"}


@router.get("/references/{rid}")
def get_reference(rid: str):
    if not re.fullmatch(r"[0-9a-f]{40}", rid):
        raise HTTPException(404, "no such reference")
    try:
        return json.loads((REFS / f"{rid}.json").read_text())
    except (OSError, ValueError):
        raise HTTPException(404, "no such reference")


@router.get("/references/{rid}/sheet")
def get_sheet(rid: str):
    f = REFS / f"{rid}.jpg"
    if not re.fullmatch(r"[0-9a-f]{40}", rid) or not f.is_file():
        raise HTTPException(404, "no frame grid")
    return FileResponse(f, media_type="image/jpeg")


@router.post("/references/compare")
def compare_route(body: CompareBody):
    import render
    j = render.render_status(body.collection_id, "preview")
    if j.get("status") != "done" or not j.get("path"):
        raise HTTPException(409, "no finished preview render for this project; call render_preview first")
    ref = _run(lambda: analyze_reference(local(body.path), sheet=False))
    mine = _run(analyze_reference, j["path"], sheet=False, cube=False)
    return {"gaps": gaps(ref, mine), "reference_id": ref["id"], "stale": bool(j.get("stale")),
            "mine": {"shots": mine["pacing"]["shots"], "median_seconds": mine["pacing"]["seconds"]["p50"],
                     "median_beats": (mine["pacing"]["beats"] or {}).get("p50")}}
