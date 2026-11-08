"""PoC: Reinhard (Lab mean/std) transfer baked into a .cube LUT. Usage: look_lut.py target.mp4 profile.json out.cube [strength]"""
import sys, json, numpy as np, cv2
tgt, prof, out = sys.argv[1:4]; k = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0

def lab_of(bgr_u8):  # -> L 0..100, a/b centered, float
    lab = cv2.cvtColor(bgr_u8, cv2.COLOR_BGR2LAB).astype(np.float32); lab[..., 0] *= 100 / 255; lab[..., 1:] -= 128; return lab

cap = cv2.VideoCapture(tgt); samples = []; i = 0
while True:
    ok, f = cap.read()
    if not ok: break
    if i % 5 == 0: samples.append(lab_of(cv2.resize(f, (320, 180))).reshape(-1, 3))
    i += 1
t = np.concatenate(samples); mt, st = t.mean(0), t.std(0) + 1e-6
p = json.load(open(prof))["look"]; mr, sr = np.array(p["lab_mean"]), np.array(p["lab_std"])

N = 33
g = np.linspace(0, 1, N)
b, gg, r = np.meshgrid(g, g, g, indexing="ij")          # .cube order: red fastest
rgb = np.stack([r, gg, b], -1).reshape(-1, 1, 3).astype(np.float32)
lab = cv2.cvtColor(rgb[..., ::-1].copy(), cv2.COLOR_BGR2LAB)  # float input: L 0..100, a/b centered
moved = (lab - mt) / st * sr + mr
lab = lab + k * (moved - lab)                             # strength blend
bgr = np.clip(cv2.cvtColor(lab.astype(np.float32), cv2.COLOR_LAB2BGR), 0, 1)
with open(out, "w") as f:
    f.write(f"TITLE \"safelight reinhard\"\nLUT_3D_SIZE {N}\n")
    for px in bgr.reshape(-1, 3)[:, ::-1]: f.write(f"{px[0]:.6f} {px[1]:.6f} {px[2]:.6f}\n")
print("target mean", mt.round(1), "-> ref mean", mr.round(1))
