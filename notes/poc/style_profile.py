"""PoC: reference video -> style profile JSON (+ look stats). Usage: style_profile.py ref.mp4 out.json"""
import sys, json, subprocess, tempfile, pathlib, numpy as np, cv2, torch, librosa
from transnetv2_pytorch import TransNetV2

path, out = sys.argv[1], sys.argv[2]
cap = cv2.VideoCapture(path); fps = cap.get(cv2.CAP_PROP_FPS)
frames = []
while True:
    ok, f = cap.read()
    if not ok: break
    frames.append(cv2.resize(f, (320, 180)))
dur = len(frames) / fps

# shots + transition types (TransNetV2: single-frame head = boundary, many-hot head = transition extent)
m = TransNetV2(device="cpu"); m.eval()
with torch.no_grad():
    _, single, many = m.predict_video(path, quiet=True)
single, many = np.asarray(single).ravel(), np.asarray(many).ravel()
hot = many > 0.5
transitions = []
for f in np.where(single > 0.5)[0]:
    a = b = f
    while a > 0 and hot[a - 1]: a -= 1
    while b + 1 < len(hot) and hot[b + 1]: b += 1
    span = (b - a + 1) / fps
    transitions.append({"t": round((f + 1) / fps, 3), "type": "cut" if span <= 2 / fps else "gradual",
                        "duration": round(span, 2) if span > 2 / fps else 0})
bounds = [0.0] + [t["t"] for t in transitions] + [dur]
lens = np.diff(bounds)

# beats and how cuts sit on them
wav = tempfile.mktemp(suffix=".wav")
subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "22050", wav], check=True)
y, sr = librosa.load(wav, sr=None)
tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
tempo = float(np.atleast_1d(tempo)[0]); period = 60 / tempo
offs = np.array([np.min(np.abs(beats - t["t"])) for t in transitions])

# camera motion per shot (similarity transform between consecutive frames)
def motion(a, b):
    tx = ty = sc = 0.0; k = 0
    for f in range(a, b):
        g0, g1 = (cv2.cvtColor(frames[i], cv2.COLOR_BGR2GRAY) for i in (f, f + 1))
        p0 = cv2.goodFeaturesToTrack(g0, 200, 0.01, 5)
        if p0 is None: continue
        p1, st, _ = cv2.calcOpticalFlowPyrLK(g0, g1, p0, None)
        M, _ = cv2.estimateAffinePartial2D(p0[st == 1], p1[st == 1])
        if M is None: continue
        tx += M[0, 2]; ty += M[1, 2]; sc += np.hypot(M[0, 0], M[1, 0]) - 1; k += 1
    if not k: return "unknown"
    tx, ty, sc = tx / k * fps / 320, ty / k * fps / 180, sc / k * fps
    if sc > 0.05: return "push-in"
    if sc < -0.05: return "pull-out"
    if abs(tx) > 0.05 or abs(ty) > 0.05: return "pan-right" if tx < 0 else "pan-left" if abs(tx) >= abs(ty) else "tilt"
    return "static"
shots = []
for i in range(len(bounds) - 1):
    a, b = int(bounds[i] * fps) + 3, int(bounds[i + 1] * fps) - 3
    shots.append({"start": round(bounds[i], 3), "length": round(float(lens[i]), 3),
                  "beats": round(float(lens[i] / period), 2), "camera": motion(a, b) if b - a > 4 else "too-short"})

# look: Lab mean/std over sampled frames (Reinhard statistics) + readable summary
lab = np.concatenate([cv2.cvtColor(f, cv2.COLOR_BGR2LAB).astype(np.float32).reshape(-1, 3) for f in frames[::5]])
lab[:, 0] *= 100 / 255; lab[:, 1:] -= 128
hsv = np.concatenate([cv2.cvtColor(f, cv2.COLOR_BGR2HSV).reshape(-1, 3) for f in frames[::5]])
look = {"lab_mean": lab.mean(0).round(2).tolist(), "lab_std": lab.std(0).round(2).tolist(),
        "warmth_b": round(float(lab[:, 2].mean()), 2), "saturation": round(float(hsv[:, 1].mean() / 255), 3),
        "contrast_L_std": round(float(lab[:, 0].std()), 2), "black_level_L_p2": round(float(np.percentile(lab[:, 0], 2)), 2)}

# on-screen text: Apple Vision OCR on frames sampled at 2 fps
text = []
try:
    import Vision
    from Foundation import NSURL
    tmp = pathlib.Path(tempfile.mkdtemp())
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-vf", "fps=2", str(tmp / "%04d.png")], check=True)
    for p in sorted(tmp.glob("*.png")):
        req = Vision.VNRecognizeTextRequest.alloc().init(); req.setRecognitionLevel_(0)
        h = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(p)), None)
        h.performRequests_error_([req], None)
        for o in req.results() or []:
            c = o.topCandidates_(1)[0]
            bb = o.boundingBox()
            text.append({"t": round((int(p.stem) - 1) / 2, 1), "text": str(c.string()), "conf": round(float(c.confidence()), 2),
                         "y": round(float(bb.origin.y), 2), "height": round(float(bb.size.height), 3)})
except ImportError:
    text = "pyobjc-framework-Vision not installed"

profile = {"source": pathlib.Path(path).name, "duration": round(dur, 2), "fps": round(fps, 3),
           "pacing": {"shots": len(shots), "cuts_per_min": round(len(transitions) / dur * 60, 1),
                      "median_shot": round(float(np.median(lens)), 2), "shot_length_cv": round(float(lens.std() / lens.mean()), 2)},
           "transitions": {"cut": sum(t["type"] == "cut" for t in transitions),
                           "gradual": sum(t["type"] == "gradual" for t in transitions), "list": transitions},
           "music": {"tempo": round(tempo, 1), "cuts_on_beat_pct": round(float((offs < 0.06).mean() * 100), 0),
                     "median_offset_s": round(float(np.median(offs)), 3)},
           "shots": shots, "look": look, "text": text}
json.dump(profile, open(out, "w"), indent=1)
print(json.dumps({k: profile[k] for k in ("pacing", "transitions", "music")}, indent=None))
print("shots:", [(s["length"], s["beats"], s["camera"]) for s in shots])
print("look:", look); print("text:", text)
