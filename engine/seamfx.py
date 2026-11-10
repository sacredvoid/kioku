"""Seam clips: transitions xfade cannot do well, drawn in numpy and cv2 (notes/editing-power-research.md X3).

A seam clip is the tf frames where clip A hands over to clip B, built from the last tf frames of A's cached segment and
the first tf frames of B's. The join lays it over a plain xfade of the same length (render._graph), so the timeline math
is xfade's: the overlap shortens the edit by tf frames and audio keeps its acrossfade. Seam clips are cached next to the
segments under a key of both segment keys, the effect and its length, so ordinary seams and re-renders cost nothing.
Frames go through ffmpeg rawvideo pipes as RGB: each segment is read with its own tagged matrix and range, and the seam
is written back with segment A's matrix and range and tagged like it, so its pixels match the frames around it and no
filter in the join converts it.
"""
import hashlib
import json
import math
import os
import subprocess
import tempfile

import cv2
import numpy as np

SEAM_V = 1  # bump to invalidate every cached seam clip
RUNS = 0  # seam clips generated since start (tests count it)


def _smooth(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def _f(img):
    return img.astype(np.float32) * (1 / 255)


def _u8(x):
    return np.clip(x * 255 + 0.5, 0, 255).astype(np.uint8)


# ---------- effects: fn(a, b, p, ctx) -> uint8 RGB frame. a, b: uint8 HxWx3; p: 0..1 through the seam (cut at 0.5)

def _zoomed(img, s, blur):
    """img scaled by s about its centre, with a radial (zoom) blur: the mean of copies scaled s .. s*(1+blur).
    The blur is drawn at half size (it is soft anyway) and only the scaled copy at s is full resolution."""
    H, W = img.shape[:2]

    def warp(src, sc, w, h):
        M = np.float32([[sc, 0, (1 - sc) * w / 2], [0, sc, (1 - sc) * h / 2]])
        return cv2.warpAffine(src, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    sharp = warp(img, s, W, H)
    if blur < 0.004:
        return sharp
    small = cv2.resize(img, (W // 2, H // 2), interpolation=cv2.INTER_AREA)
    k = min(12, 3 + int(blur * 60))
    acc = np.zeros((H // 2, W // 2, 3), np.float32)
    for i in range(k):
        acc += warp(small, s * (1 + blur * i / (k - 1)), W // 2, H // 2)
    soft = cv2.resize(acc * (1 / k), (W, H), interpolation=cv2.INTER_LINEAR)
    # the centre of a zoom blur stays sharp: blend by distance from the centre
    return _u8(_f(sharp) * (1 - _radial(W, H)) + soft * (1 / 255) * _radial(W, H))


_RADIAL = {}


def _radial(W, H):
    if (W, H) not in _RADIAL:
        y, x = np.mgrid[0:H, 0:W].astype(np.float32)
        r = np.sqrt(((x - W / 2) / (W / 2)) ** 2 + ((y - H / 2) / (H / 2)) ** 2) / math.sqrt(2)
        _RADIAL[(W, H)] = np.clip(r * 2.2, 0, 1)[..., None]
    return _RADIAL[(W, H)]


def zoom_through(a, b, p, ctx):
    """Push into A, accelerating, then emerge from B settling from 1.6x; radial blur follows the speed, and a 2 to 3
    frame mix around the cut hides the switch inside the blur."""
    def side_a(u):  # u 0..1 over the first half
        return _zoomed(a, 1 + 1.2 * u ** 2.5, 0.3 * u ** 1.5)

    def side_b(u):  # u 0..1 over the second half
        return _zoomed(b, 1 + 0.6 * (1 - u) ** 3, 0.3 * (1 - u) ** 2)
    w = _smooth((p - 0.42) / 0.16)
    if w <= 0:
        return side_a(p / 0.5)
    if w >= 1:
        return side_b((p - 0.5) / 0.5)
    fa, fb = side_a(min(p / 0.5, 1.0)), side_b(max((p - 0.5) / 0.5, 0.0))
    return cv2.addWeighted(fa, 1 - w, fb, w, 0)


LEAK_COLORS = np.float32([(1.0, 0.42, 0.10), (1.0, 0.68, 0.30), (0.92, 0.22, 0.12), (1.0, 0.86, 0.66)])


def light_leak(a, b, p, ctx):
    """A warm film leak blooms in from one edge, peaks on the cut and drifts off; A dissolves to B under its peak.
    Procedural (soft gaussian blobs drawn small and scaled up), screen blended, so there is nothing to licence."""
    H, W = a.shape[:2]
    if "blobs" not in ctx:  # per seam: an edge, then 4 blobs (3 colours and a hot core) along it
        rng = ctx["rng"]
        edge = rng.integers(4)
        blobs = []
        for c in range(4):
            along = rng.uniform(0.15, 0.85)
            depth = rng.uniform(-0.05, 0.25)
            x, y = {0: (depth, along), 1: (1 - depth, along), 2: (along, depth), 3: (along, 1 - depth)}[edge]
            drift = rng.uniform(-0.25, 0.25, 2)
            r = rng.uniform(0.35, 0.6) if c < 3 else rng.uniform(0.18, 0.28)
            blobs.append((x, y, drift, r, LEAK_COLORS[c], 0.7 if c < 3 else 0.45))
        ctx["blobs"] = blobs
        sw, sh = 96, max(2, round(96 * H / W))
        ys, xs = np.mgrid[0:sh, 0:sw].astype(np.float32)
        ctx["grid"] = (xs / sw, ys / sh * H / W, sw, sh)  # y in units of width, so blobs stay round
    xs, ys, sw, sh = ctx["grid"]
    env = math.sin(math.pi * p) ** 1.4
    leak = np.zeros((sh, sw, 3), np.float32)
    for x, y, drift, r, col, k in ctx["blobs"]:
        cx, cy = x + drift[0] * (p - 0.5), (y + drift[1] * (p - 0.5)) * H / W
        g = np.exp(-((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * (r * (0.8 + 0.4 * p)) ** 2))
        leak += g[..., None] * col * k
    leak = cv2.resize(np.clip(leak * env, 0, 1), (W, H), interpolation=cv2.INTER_CUBIC)
    w = _smooth((p - 0.3) / 0.4)
    base = cv2.addWeighted(a, 1 - w, b, w, 0)
    base = _f(base) * (1 + 0.08 * env)  # the leak lifts the exposure a little, like light on the film
    return _u8(1 - (1 - np.clip(base, 0, 1)) * (1 - np.clip(leak, 0, 1)))


def luma_fade(a, b, p, ctx):
    """B arrives through its own brightness: highlights (sky, windows, lamps) first, shadows last. The key is B's luma,
    softened and stretched to its own range so dim footage still reveals in steps."""
    H, W = b.shape[:2]
    sw, sh = max(2, W // 8), max(2, H // 8)
    lum = cv2.cvtColor(cv2.resize(b, (sw, sh), interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY).astype(np.float32)
    lum = cv2.GaussianBlur(lum, (0, 0), max(sw, sh) / 60)
    lo, hi = np.percentile(lum, (2, 98))
    lum = np.clip((lum - lo) / max(hi - lo, 1.0), 0, 1)
    soft = 0.3
    e = (1 + soft) - (1 + 2 * soft) * _smooth(p)
    m = np.clip((lum - e) / soft + 0.5, 0, 1)
    m = m * m * (3 - 2 * m)
    m = np.ascontiguousarray(cv2.resize(m, (W, H), interpolation=cv2.INTER_LINEAR), np.float32)
    return cv2.blendLinear(a, b, 1 - m, m)


def glitch(a, b, p, ctx):
    """A short digital break on the cut: horizontal block shifts (some showing the other clip), RGB split and fine
    scanlines, strongest on the cut, plus a one frame stutter just before it. Deterministic per seam."""
    H, W = a.shape[:2]
    rng = ctx["rng"]
    n, j = ctx["n"], ctx["j"]
    env = (1 - abs(2 * p - 1)) ** 0.7
    stutter = n >= 6 and j == n // 2 - 1
    if stutter and "prev" in ctx:
        src = ctx["prev"]
    else:
        src = a if p < 0.5 else b
    other = b if p < 0.5 else a
    out = src.copy()
    for _ in range(int(2 + 6 * env)):  # block shifts
        h = max(2, int(H * rng.uniform(0.01, 0.06)))
        y = int(rng.integers(0, max(1, H - h)))
        dx = int(W * rng.uniform(0.02, 0.09) * env) * (1 if rng.random() < 0.5 else -1)
        band = (other if rng.random() < 0.35 * env else src)[y:y + h]
        out[y:y + h] = np.roll(band, dx, axis=1)
    d = int(W * 0.012 * env)
    if d:  # RGB split, edges clamped (no wrap)
        r, bl = out[..., 0].copy(), out[..., 2].copy()
        out[:, d:, 0], out[:, :d, 0] = r[:, :-d], r[:, :1]
        out[:, :-d, 2], out[:, -d:, 2] = bl[:, d:], bl[:, -1:]
    if env > 0.3:  # scanlines, period scaled to the frame so they survive the encode
        k = max(1, H // 540)
        rows = (np.arange(H) // k) % 2 == 1
        out[rows] = (out[rows].astype(np.uint16) * int(256 * (1 - 0.18 * env)) >> 8).astype(np.uint8)
    ctx["prev"] = src
    return out


FX = {"zoomthrough": zoom_through, "lightleak": light_leak, "lumafade": luma_fade, "glitch": glitch}


def frame_p(j, n):
    """Progress of seam frame j of n: centred in each frame, so the cut (0.5) sits on the overlap's middle (the beat when
    synced, see edit.compute_timeline)."""
    return (j + 0.5) / n


# ---------- files

def key(name, tf, key_a, key_b, mode, dims, fps):
    d = {"v": SEAM_V, "fx": name, "tf": tf, "a": key_a, "b": key_b, "mode": mode, "dims": list(dims), "fps": fps}
    return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:20]


# ffprobe color_space -> swscale matrix name; anything else is read as unspecified, which swscale treats as bt601
MATRIX = {"bt709": "bt709", "smpte170m": "smpte170m", "bt470bg": "bt470", "fcc": "fcc", "smpte240m": "smpte240m",
          "bt2020nc": "bt2020", "bt2020c": "bt2020"}


def colour(path):
    """(color_space, color_range) tags of the first video stream ('' when unset)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=color_space,color_range",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
    return st.get("color_space", ""), st.get("color_range", "")


def _read_cmd(path, start_frame, n, fps, dims):
    W, H = dims
    ss = max(0.0, (start_frame - 0.5) / fps)  # half a frame early: accurate seek keeps frame start_frame, drops the one before
    return ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{ss:.6f}", "-i", str(path), "-frames:v", str(n), "-an",
            "-vf", f"scale={W}:{H},format=rgb24", "-f", "rawvideo", "-"]


def _read_all(path, start_frame, n, fps, dims, timeout):
    """n RGB frames from start_frame on. A short read repeats the last frame (a segment one frame short must not fail
    the render); nothing at all is an error."""
    W, H = dims
    r = subprocess.run(_read_cmd(path, start_frame, n, fps, dims), capture_output=True, timeout=timeout)
    size = W * H * 3
    got = len(r.stdout) // size
    if r.returncode != 0 or got == 0:
        raise RuntimeError(f"could not read frames for a transition from {os.path.basename(path)}: "
                           + r.stderr.decode(errors="replace")[-300:])
    frames = [np.frombuffer(r.stdout, np.uint8, size, i * size).reshape(H, W, 3) for i in range(min(got, n))]
    return frames + [frames[-1]] * (n - len(frames))


def build(name, tf, file_a, frames_a, file_b, out, mode, dims, fps, seed, codec, cancel=None):
    """Write the tf-frame seam clip for A (frames_a long) into B to out (no audio). codec: the segment codec args."""
    global RUNS
    if name not in FX:
        raise ValueError(f"unknown seam effect {name!r}")
    if tf < 1 or tf > frames_a:
        raise ValueError(f"seam of {tf} frames does not fit a {frames_a} frame clip")
    W, H = dims
    t = max(60, tf * 4)
    A = _read_all(file_a, frames_a - tf, tf, fps, dims, t)
    B = _read_all(file_b, 0, tf, fps, dims, t)
    ctx = {"rng": np.random.default_rng(seed), "n": tf}
    space, rng_ = colour(file_a)
    rng_ = "pc" if rng_ == "pc" else "tv"
    tag = ["-colorspace", space] if space in MATRIX else []
    enc = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-framerate", str(fps), "-i", "-",
           "-vf", f"scale=out_color_matrix={MATRIX.get(space, 'bt601')}:out_range={rng_},format=yuv420p,setsar=1",
           *codec, *tag, "-color_range", rng_, "-an", "-frames:v", str(tf), "-f", "mov", str(out)]
    RUNS += 1
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(enc, stdin=subprocess.PIPE, stderr=err)
        try:
            for j in range(tf):
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("Cancelled")
                ctx["j"] = j
                fr = FX[name](A[j], B[j], frame_p(j, tf), ctx)
                p.stdin.write(np.ascontiguousarray(fr, np.uint8).tobytes())
            p.stdin.close()
            p.wait(timeout=t)
        except BrokenPipeError:
            p.wait(timeout=t)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
        if p.returncode != 0:
            err.seek(0)
            raise RuntimeError("seam encode failed: " + err.read().decode(errors="replace")[-1000:])
    return out


def ensure(name, tf, file_a, frames_a, file_b, k, cdir, mode, dims, fps, codec, parts, cancel=None):
    """The cached seam clip for key k in cdir, built when missing (via .part + rename; the .part goes in parts so a
    failed render removes it)."""
    f = cdir / f"seam-{k}.mov"
    if f.is_file():
        os.utime(f)  # LRU, swept with the segments
        return f
    part = cdir / f"seam-{k}.part.mov"
    parts.append(part)
    build(name, tf, file_a, frames_a, file_b, part, mode, dims, fps, int(k[:8], 16), codec, cancel)
    part.replace(f)
    return f
