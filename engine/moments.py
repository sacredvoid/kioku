"""Moments v2: find the best 1-4 s windows of a clip from cheap local signals (notes/pro-editing-design.md, Phase 2).

signals(uuid, proxy)  ffmpeg stats + Apple Vision + Apple SoundAnalysis on the proxy, at 4 Hz, cached in data/moments/<uuid>.json
fuse(sig, ...)        pure: z-scored signals + a weight profile -> ranked windows (unit-tested on synthetic signals)
routes                GET /media/{uuid}/moments and /moments/sheet
"""
import json
import logging
import re
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

import core
import media

router = APIRouter()
log = logging.getLogger("kioku.moments")
MOMENTS = core.DATA / "moments"
VERSION = 1  # bump when a signal's meaning changes; stale caches are recomputed
HZ = 4  # signal grid
VISION_FPS = 2
MIN_LEN, MAX_LEN = 1.0, 4.0
PROFILES = ("auto", "action", "people", "scenic", "talk")
# per-signal weights; the feature names are the keys of features()
WEIGHTS = {
    "auto":   dict(motion=.5, loud=.4, aesth=.6, face=.6, people=.3, laugh=1, cheer=1, shout=.6, speech=.2, brief=1.5),
    "action": dict(motion=1.2, loud=.6, aesth=.2, face=.1, people=.3, laugh=.3, cheer=.8, shout=.8, brief=1.5),
    "people": dict(motion=.1, loud=.2, aesth=.4, face=1.2, people=.8, laugh=1, cheer=.4, shout=.3, speech=.3, brief=1.5),
    "scenic": dict(motion=.1, sharp=.6, aesth=1.4, music=.2, brief=1.5),
    "talk":   dict(loud=.2, aesth=.2, face=.8, laugh=.4, speech=1.2, brief=1.5),
}
SHAKE_W, UTILITY_W, DARK_W = 1.5, 1.0, 0.8
# SoundAnalysis v1 labels (checked against knownClassifications) grouped into our signals
SOUND = {"laugh": ("laughter", "belly_laugh", "chuckle_chortle", "baby_laughter"),
         "cheer": ("cheering", "applause", "clapping", "crowd"),
         "shout": ("shout", "yell", "screaming", "children_shouting"),
         "speech": ("speech",), "music": ("music",)}
SERIES = ("motion", "blur", "yavg", "loud", "aesth", "utility", "faces", "face_q", "humans", "sal_x", "sal_y", *SOUND)


# ---------- ffmpeg

def parse_meta(text):
    """ffmpeg `metadata=print` output -> [(pts_time, {key: float})]."""
    out = []
    for line in text.splitlines():
        if line.startswith("frame:"):
            m = re.search(r"pts_time:([\d.eE+-]+)", line)
            out.append((float(m.group(1)) if m else 0.0, {}))
        elif "=" in line and out:
            k, v = line.split("=", 1)
            try:
                x = float(v)
                if x == x and abs(x) != float("inf"):  # blurdetect prints nan on black frames
                    out[-1][1][k] = x
            except ValueError:
                pass
    return out


def binned(rows, key, n, how=np.mean):
    """Reduce (t, dict) rows onto the HZ grid; empty bins repeat their left neighbour."""
    cells = [[] for _ in range(n)]
    for t, d in rows:
        if key in d and 0 <= int(t * HZ) < n:
            cells[int(t * HZ)].append(d[key])
    out, last = [], 0.0
    for c in cells:
        last = float(how(c)) if c else last
        out.append(last)
    return out


def ffmpeg_signals(proxy, dur):
    """One video pass at 10 fps/160 px and one audio pass; ~0.25 s each for a 22 s clip."""
    n = max(1, int(np.ceil(dur * HZ)))
    with tempfile.TemporaryDirectory(prefix="cs-mom-") as d:
        vf, af = Path(d) / "v.txt", Path(d) / "a.txt"
        run = lambda *a: subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(proxy), *a, "-f", "null", "-"],
                                        capture_output=True, text=True)
        v = run("-an", "-vf", f"scale=160:-2,fps=10,signalstats,scdet=t=8,blurdetect,metadata=print:file={vf}")
        a = run("-vn", "-af", f"ebur128=metadata=1,ametadata=print:file={af}")
        if v.returncode != 0:
            raise RuntimeError(f"ffmpeg: {v.stderr.strip()[-300:]}")
        vrows = parse_meta(vf.read_text()) if vf.is_file() else []
        arows = parse_meta(af.read_text()) if a.returncode == 0 and af.is_file() else []  # no audio track: loudness stays 0
    sig = {"motion": binned(vrows, "lavfi.signalstats.YDIF", n), "blur": binned(vrows, "lavfi.blur", n),
           "yavg": binned(vrows, "lavfi.signalstats.YAVG", n),
           "loud": [max(x, -70.0) + 70 for x in binned(arows, "lavfi.r128.M", n, max)] if arows else [0.0] * n}  # 0 = silent
    cuts = [d["lavfi.scd.time"] for _, d in vrows if "lavfi.scd.time" in d]
    return sig, cuts


# ---------- Apple Vision

def vision_signals(proxy, dur, n):
    """Aesthetics, faces, humans, saliency on VISION_FPS frames at 384 px; {} if Vision is unavailable."""
    try:
        import Vision
        from Foundation import NSURL
    except ImportError:
        log.warning("pyobjc Vision missing: skipping aesthetics/faces")
        return {}
    with tempfile.TemporaryDirectory(prefix="cs-vis-") as d:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(proxy), "-vf", f"fps={VISION_FPS},scale=384:-2",
                            "-q:v", "4", str(Path(d) / "%05d.jpg")], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg: {r.stderr.strip()[-300:]}")
        files = sorted(Path(d).glob("*.jpg"))

        def one(f):
            reqs = [Vision.VNCalculateImageAestheticsScoresRequest.alloc().init(), Vision.VNDetectFaceCaptureQualityRequest.alloc().init(),
                    Vision.VNDetectHumanRectanglesRequest.alloc().init(), Vision.VNGenerateAttentionBasedSaliencyImageRequest.alloc().init()]
            h = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(f)), {})
            ok, _ = h.performRequests_error_(reqs, None)
            if not ok:
                return None
            aes, face, hum, sal = ([r.results() or [] for r in reqs])
            boxes = sal[0].salientObjects() if sal and sal[0].salientObjects() else []
            cx, cy = (0.5, 0.5)
            if boxes:
                b = max(boxes, key=lambda o: o.confidence()).boundingBox()
                cx, cy = b.origin.x + b.size.width / 2, 1 - (b.origin.y + b.size.height / 2)  # Vision's y is bottom-up
            return dict(aesth=float(aes[0].overallScore()) if aes else 0.0, utility=float(bool(aes and aes[0].isUtility())),
                        faces=float(len(face)), face_q=max([float(o.faceCaptureQuality() or 0) for o in face], default=0.0),
                        humans=float(len(hum)), sal_x=float(cx), sal_y=float(cy))

        with ThreadPoolExecutor(8) as ex:  # ponytail: 8 threads, Vision releases the GIL; 1.9 s per minute of video, lower VISION_FPS if that is too slow
            rows = list(ex.map(one, files))
    out = {k: [] for k in ("aesth", "utility", "faces", "face_q", "humans", "sal_x", "sal_y")}
    for i in range(n):
        j = min(int(i / HZ * VISION_FPS), len(rows) - 1)  # nearest sampled frame
        r = next((x for x in rows[j::-1] if x), None) if rows else None
        for k in out:
            out[k].append(r[k] if r else 0.0)
    return out


# ---------- Apple SoundAnalysis

@cache
def observer_class():
    """The SoundAnalysis results observer; built once because an ObjC class can only be defined once per process."""
    import objc
    from Foundation import NSObject

    class Observer(NSObject, protocols=[objc.protocolNamed("SNResultsObserving")]):
        def init(self):
            self = objc.super(Observer, self).init()
            self.rows = []
            return self

        def request_didProduceResult_(self, request, result):
            try:  # an exception inside a callback would abort the whole engine process
                (v, scale, *_), (dv, *_) = result.timeRange()
                self.rows.append((v / scale, dv / scale, {c.identifier(): float(c.confidence()) for c in result.classifications()}))
            except Exception:
                log.exception("sound result")

        def request_didFailWithError_(self, request, error):
            log.warning("sound analysis failed: %s", error)

        def requestDidComplete_(self, request):
            pass
    return Observer


def sound_signals(proxy, n):
    """Per-bin laughter/cheering/shout/speech/music probabilities from Apple's built-in classifier (3 s windows,
    so events smear over ~1.5 s each side; ponytail: fine for 1-4 s windows); {} on failure or no audio."""
    try:
        import SoundAnalysis as S
        from Foundation import NSURL
    except ImportError:
        log.warning("pyobjc SoundAnalysis missing: skipping audio events")
        return {}

    Observer = observer_class()

    with tempfile.TemporaryDirectory(prefix="cs-snd-") as d:
        wav = Path(d) / "a.wav"
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(proxy), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not wav.is_file():
            return {}  # no audio track
        analyzer, err = S.SNAudioFileAnalyzer.alloc().initWithURL_error_(NSURL.fileURLWithPath_(str(wav)), None)
        req, err2 = S.SNClassifySoundRequest.alloc().initWithClassifierIdentifier_error_(S.SNClassifierIdentifierVersion1, None)
        obs = Observer.alloc().init()
        if analyzer is None or req is None or not analyzer.addRequest_withObserver_error_(req, obs, None)[0]:
            log.warning("sound analysis unavailable: %s", err or err2)
            return {}
        analyzer.analyze()
    out = {k: [0.0] * n for k in SOUND}
    for t, dur, probs in obs.rows:
        for k, labels in SOUND.items():
            p = max((probs.get(x, 0.0) for x in labels), default=0.0)
            for i in range(max(0, int(t * HZ)), min(n, int(np.ceil((t + dur) * HZ)))):
                out[k][i] = max(out[k][i], p)
    return out


# ---------- cache

def cache_path(uuid):
    return MOMENTS / f"{uuid}.json"


def signals(uuid, proxy):
    """Cached signals for the proxy: recomputed when VERSION or the proxy's mtime changes."""
    f, stamp = cache_path(uuid), proxy.stat().st_mtime
    if f.is_file():
        try:
            got = json.loads(f.read_text())
            if got.get("version") == VERSION and got.get("mtime") == stamp:
                return got
        except ValueError:
            pass  # torn or corrupt cache: rebuild
    dur = media.probe_duration(proxy)
    if not dur > 0:
        raise RuntimeError("the playable copy has no length")
    n = max(1, int(np.ceil(dur * HZ)))
    with ThreadPoolExecutor(3) as ex:  # the three analyses are independent and mostly wait on ffmpeg / Apple frameworks
        ff, fv, fs = ex.submit(ffmpeg_signals, proxy, dur), ex.submit(vision_signals, proxy, dur, n), ex.submit(sound_signals, proxy, n)
        sig, cuts = ff.result()
        sig.update(fv.result())
        sig.update(fs.result())
    got = {"version": VERSION, "mtime": stamp, "hz": HZ, "duration": dur, "cuts": cuts,
           "series": {k: [round(float(x), 3) for x in sig.get(k, [0.0] * n)] for k in SERIES}}
    MOMENTS.mkdir(parents=True, exist_ok=True)
    part = f.with_suffix(f".{threading.get_ident()}.part")
    part.write_text(json.dumps(got))
    part.replace(f)
    return got


# ---------- fusion (pure)

def z(x):
    x = np.asarray(x, float)
    s = x.std()
    return (x - x.mean()) / s if s > 1e-9 else np.zeros_like(x)


def features(series, brief=None):
    """Per-bin features in a comparable scale: continuous signals z-scored per clip, classifier probabilities raw."""
    g = lambda k: np.asarray(series.get(k) or [0.0] * len(series["motion"]), float)
    f = {"motion": np.clip(z(g("motion")), -1, 3), "loud": np.clip(z(g("loud")), -1, 3) if g("loud").any() else g("loud"),
         "aesth": np.clip(z(g("aesth")), -2, 3), "sharp": np.clip(-z(g("blur")), -2, 2),
         "face": g("face_q") * (g("faces") > 0), "people": (g("humans") > 0) * 1.0,
         **{k: g(k) for k in SOUND}}
    if brief is not None:
        f["brief"] = np.clip(z(brief), -1, 3)
    return f


def window(sm, p, floor, n):
    """Grow around bin p while the smoothed score stays above floor, within MIN_LEN..MAX_LEN seconds; returns bin range [a, b)."""
    a, b = p, p + 1
    longest, shortest = int(MAX_LEN * HZ), int(MIN_LEN * HZ)
    while b - a < longest:
        left = sm[a - 1] if a > 0 else -1e9
        right = sm[b] if b < n else -1e9
        if max(left, right) < floor and b - a >= shortest:
            break
        if max(left, right) <= -1e9:
            break
        if right >= left:
            b += 1
        else:
            a -= 1
    return a, b


def fuse(series, dur, cuts=(), profile="auto", brief=None, top=5):
    """Ranked moments [{t0, t1, peak, score, reasons}] from per-bin series at HZ; all times inside [0, dur]."""
    if profile not in WEIGHTS:
        raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
    n = len(series["motion"])
    if n == 0 or dur <= 0:
        return []
    feat = features(series, brief)
    w = WEIGHTS[profile]
    raw = sum(w[k] * feat[k] for k in w if k in feat)
    g = lambda k: np.asarray(series.get(k) or [0.0] * n, float)
    shake = np.clip(z(g("motion")), 0, 3) * np.clip(z(g("blur")), 0, 3) if g("blur").any() else np.zeros(n)  # lots of motion, soft frames
    util, dark = g("utility") > 0.5, (g("yavg") < 25) & (g("yavg") > 0)
    score = raw - SHAKE_W * shake - UTILITY_W * util - DARK_W * dark
    sm = np.convolve(np.pad(score, 2, mode="edge"), np.ones(5) / 5, mode="valid")  # ~1 s moving average
    floor_ = sm.mean() + 0.25 * sm.std()
    peaks = [i for i in range(n) if sm[i] >= sm[max(0, i - 1)] and sm[i] >= sm[min(n - 1, i + 1)] and sm[i] > floor_]
    wins = []
    for p in sorted(peaks, key=lambda i: -sm[i]):
        if any(a <= p < b for a, b, _ in wins):
            continue  # inside an already-claimed window: merged
        a, b = window(sm, p, sm[p] * 0.6 if sm[p] > 0 else floor_, n)
        wins.append((a, b, p))
        wins = merge(wins, sm)
    out = []
    for a, b, p in wins:
        t0, t1 = snap(a / HZ, min(b / HZ, dur), p / HZ, cuts, dur)
        pk = a + int(np.argmax(score[a:b]))
        out.append({"t0": round(t0, 2), "t1": round(t1, 2), "peak": round(min(max(pk / HZ + 0.5 / HZ, t0), t1), 2),
                    "score": round(float(sm[pk]), 2), "reasons": reasons(w, feat, pk, shake, util, dark, brief is not None)})
    out.sort(key=lambda m: -m["score"])
    return out[:top]


def merge(wins, sm):
    """Merge overlapping windows (the higher peak stays the anchor); a merged window longer than MAX_LEN is trimmed around the anchor."""
    wins = sorted(wins)
    out = [wins[0]]
    for a, b, p in wins[1:]:
        A, B, P = out[-1]
        if a < B:
            keep = P if sm[P] >= sm[p] else p
            A, B = min(A, a), max(B, b)
            cap = int(MAX_LEN * HZ)
            if B - A > cap:
                A = min(max(A, keep + 1 - cap // 2), B - cap)
                B = A + cap
            out[-1] = (A, B, keep)
        else:
            out.append((a, b, p))
    return out


def snap(t0, t1, peak, cuts, dur, edge=0.4):
    """Keep the window inside one shot: a cut strictly inside trims the side away from the peak; a cut just outside pulls the edge to it."""
    for c in sorted(cuts):
        if t0 + 0.05 < c < t1 - 0.05:
            t0, t1 = (c, t1) if c <= peak else (t0, c)
        elif t0 - edge <= c <= t0 + 0.05:
            t0 = c
        elif t1 - 0.05 <= c <= t1 + edge:
            t1 = c
    return max(0.0, t0), min(dur, t1)


NAMES = {"laugh": "laugh", "cheer": "cheering", "shout": "shout", "speech": "speech", "music": "music", "face": "face",
         "aesth": "pretty", "sharp": "sharp", "people": "people", "motion": "motion peak", "loud": "loud peak"}


def reasons(w, feat, i, shake, util, dark, has_brief):
    """Top contributors at the peak bin, as short labels with the raw value where it means something."""
    parts = sorted(((w[k] * feat[k][i], k) for k in w if k in feat and k != "brief"), reverse=True)
    out = []
    for c, k in parts[:3]:
        if c < 0.3:
            break
        v = feat[k][i]
        out.append(f"{NAMES[k]} {v:.2f}" if k in SOUND or k == "face" else NAMES[k])
    if has_brief and "brief" in feat and feat["brief"][i] > 0.5:
        out.insert(0, "matches query")
    if shake[i] > 0.5:
        out.append("shaky")
    if util[i]:
        out.append("utility shot")
    if dark[i]:
        out.append("dark")
    return out


def snippet(lines, t0, t1, limit=160):
    """Transcript text overlapping [t0, t1], or None."""
    text = " ".join(l.text.strip() for l in lines if l.t1 > t0 and l.t0 < t1).strip()
    return (text[:limit] + "...") if len(text) > limit else (text or None)


# ---------- brief match

def brief_series(proxy, dur, n, query):
    """Cosine of the query text with frame embeddings at 1 frame/s, spread onto the grid; None when the model is unavailable.
    ponytail: re-embeds on every query (cached text vector, not frames); cache frame vectors per clip if queries repeat."""
    import cv2
    cap = cv2.VideoCapture(str(proxy))
    try:
        ts = [min(k + 0.5, dur - 0.05) for k in range(max(1, int(dur)))]
        frames = [media.frame_at(cap, t) for t in ts]
    finally:
        cap.release()
    vecs = media.embed_frames(frames)
    q = core.embed_query(query)
    sims = [float(v @ q) if v is not None else 0.0 for v in vecs]
    return [sims[min(int(i / HZ), len(sims) - 1)] for i in range(n)]


# ---------- service + routes

def find(uuid, profile="auto", q="", top=5):
    """Moments for one video (fetches the proxy under media's slot rules); raises HTTPException."""
    if profile not in PROFILES:
        raise HTTPException(422, f"profile must be one of {', '.join(PROFILES)}")
    top = max(1, min(top, 20))
    try:
        proxy = media.fetch(uuid, "preview", "")
        sig = signals(uuid, proxy)
        brief = brief_series(proxy, sig["duration"], len(sig["series"]["motion"]), q) if q.strip() else None
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    moms = fuse(sig["series"], sig["duration"], sig["cuts"], profile, brief, top)
    got = None
    try:
        import captions
        got = captions.cached(uuid)  # never starts a transcription
    except Exception:
        log.exception("transcript lookup")
    for m in moms:
        if got and got[0] == "ok":
            m["transcript"] = snippet(got[1], m["t0"], m["t1"])
            if m["transcript"] is None:
                del m["transcript"]
    return {"uuid": uuid, "profile": profile, "duration": sig["duration"], "moments": moms}


@router.get("/media/{uuid}/moments")
def moments_route(uuid: str, profile: str = "auto", q: str = "", top: int = 5):
    return find(uuid, profile, q, top)


def sheet_jpeg(proxy, moms, prefix=""):
    """One row per moment: before (t0), peak, after (t1), each labelled."""
    import cv2
    from PIL import Image, ImageDraw
    W = 260
    cap = cv2.VideoCapture(str(proxy))
    try:
        rows = []
        for i, m in enumerate(moms, 1):
            tiles = []
            for name, t in (("before", m["t0"]), ("peak", m["peak"]), ("after", max(m["t0"], m["t1"] - 0.05))):
                f = media.frame_at(cap, t)
                if f is not None:
                    im = Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))
                    im.thumbnail((W, W))
                    tiles.append((im, f"{prefix}{i} {name} {t:.1f}s"))
            rows.append(tiles)
    finally:
        cap.release()
    rows = [r for r in rows if r]
    if not rows:
        return None
    h = max(t[0].height for r in rows for t in r) + 20
    sheet = Image.new("RGB", (3 * (W + 6) + 6, len(rows) * (h + 6) + 6), (12, 11, 11))
    d = ImageDraw.Draw(sheet)
    for y, r in enumerate(rows):
        for x, (im, label) in enumerate(r):
            ox, oy = 6 + x * (W + 6), 6 + y * (h + 6)
            sheet.paste(im, (ox, oy))
            d.text((ox + 4, oy + h - 17), label, fill=(255, 140, 90))
    import io
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=82)
    return buf.getvalue()


@router.get("/media/{uuid}/moments/sheet")
def sheet_route(uuid: str, profile: str = "auto", q: str = "", top: int = 5, prefix: str = ""):
    res = find(uuid, profile, q, top)
    jpg = sheet_jpeg(media.PROXIES / f"{uuid}.mp4", res["moments"], prefix[:4])
    if not jpg:
        raise HTTPException(404, "no moments found")
    return Response(jpg, media_type="image/jpeg")
