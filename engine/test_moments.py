"""Moments fusion on synthetic signals + the ffmpeg signal parse on a generated clip.  uv run test_moments.py"""
import os
import subprocess
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="cs-moments-"))
os.environ["CS_DB"] = str(tmp / "t.db")
os.environ["KIOKU_DB_KEY"] = "0" * 64  # never touch the Keychain

import core  # noqa: E402

core.DATA = tmp
import moments  # noqa: E402

HZ, N = moments.HZ, 80  # a 20 s clip
DUR = N / HZ


def flat(**over):
    """Quiet, sharp, steady clip with a few overrides (name -> {bin: value})."""
    s = {k: [0.0] * N for k in moments.SERIES}
    s["blur"], s["yavg"] = [5.0] * N, [100.0] * N
    for k, spots in over.items():
        for i, v in spots.items():
            s[k][i] = v
    return s


def at(t):
    return int(t * HZ)


def near(m, t):
    return m["t0"] - 0.01 <= t <= m["t1"] + 0.01


# 1. a laugh at 10 s is the top moment; its peak and reasons say so
sig = flat(laugh={i: 0.8 for i in range(at(9.5), at(11))}, motion={i: 1.0 for i in range(at(3), at(3.5))})
top = moments.fuse(sig, DUR, [], "auto")
assert near(top[0], 10) and top[0]["t0"] <= top[0]["peak"] <= top[0]["t1"], top
assert any(r.startswith("laugh") for r in top[0]["reasons"]), top[0]

# 2. shake (lots of motion, soft frames) ranks below the same motion on sharp frames
shaky = flat(motion={i: 9.0 for i in list(range(at(5), at(6))) + list(range(at(15), at(16)))},
             blur={i: 30.0 for i in range(at(5), at(6))})
a = moments.fuse(shaky, DUR, [], "action", top=2)
assert near(a[0], 15), f"sharp motion should beat shaky motion: {a}"
assert all("shaky" in m["reasons"] for m in a if near(m, 5)), a

# 3. isUtility is penalised (a pretty receipt shot loses to a pretty real one)
pretty = flat(aesth={i: 0.8 for i in range(at(4), at(5))}, utility={i: 1.0 for i in range(at(4), at(5))})
pretty["aesth"][at(12):at(13)] = [0.8] * (at(13) - at(12))
b = moments.fuse(pretty, DUR, [], "scenic", top=3)
assert near(b[0], 12), b

# 4. profiles change the ranking: faces win for people, motion wins for action
mix = flat(motion={i: 1.0 for i in range(at(4), at(5))}, face_q={i: 0.9 for i in range(at(14), at(15))}, faces={i: 1.0 for i in range(at(14), at(15))})
assert near(moments.fuse(mix, DUR, [], "people")[0], 14)
assert near(moments.fuse(mix, DUR, [], "action")[0], 4)
assert near(moments.fuse(flat(speech={i: 0.9 for i in range(at(8), at(9))}), DUR, [], "talk")[0], 8)

# 5. windows: within the clip, 1 to 4 s, merged (no overlaps), at most `top`, sorted by score
busy = flat(cheer={i: 0.6 + 0.05 * (i % 3) for i in range(0, N)}, laugh={at(0): 0.9, at(19.5): 0.9})
ms = moments.fuse(busy, DUR, [], "auto", top=4)
assert 0 < len(ms) <= 4
for m in ms:
    assert 0 <= m["t0"] < m["t1"] <= DUR + 1e-6 and m["t1"] - m["t0"] <= moments.MAX_LEN + 1e-6, m
spans = sorted((m["t0"], m["t1"]) for m in ms)
assert all(x[1] <= y[0] + 1e-6 for x, y in zip(spans, spans[1:])), f"overlap: {spans}"
assert [m["score"] for m in ms] == sorted((m["score"] for m in ms), reverse=True)

# 6. scene cuts: a cut inside the window trims the side away from the peak
cut = flat(cheer={i: 0.9 for i in range(at(9), at(11))})
free = moments.fuse(cut, DUR, [], "auto")[0]
trimmed = moments.fuse(cut, DUR, [free["t0"] + 0.5], "auto")[0]
assert trimmed["t0"] >= free["t0"] + 0.5 - 1e-6 or trimmed["t1"] <= free["t0"] + 0.5 + 1e-6, (free, trimmed)

# 7. brief match lifts the matching shot; transcript snippet
brief = [0.0] * N
brief[at(16):at(17)] = [0.5] * (at(17) - at(16))
q = moments.fuse(flat(), DUR, [], "auto", brief=brief)
assert near(q[0], 16) and "matches query" in q[0]["reasons"], q
from captions import Line  # noqa: E402
assert moments.snippet([Line(15, 17, "hello there"), Line(30, 31, "no")], 16, 18) == "hello there"
assert moments.snippet([Line(30, 31, "no")], 16, 18) is None

# 8. empty / bad input
moments.fuse(flat(), DUR, [], "auto")  # a flat clip must not crash (z-score of a constant)
assert moments.parse_meta("frame:0 pts:0 pts_time:0.5\nlavfi.blur=nan\nlavfi.x=2") == [(0.5, {"lavfi.x": 2.0})]
try:
    moments.fuse(flat(), DUR, [], "nonsense")
    raise SystemExit("bad profile accepted")
except ValueError:
    pass

# 9. ffmpeg signal parse on a generated clip: black 2 s then a moving test pattern, with a 440 Hz tone
clip = tmp / "gen.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:size=320x240:rate=30:duration=2",
                "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2", "-f", "lavfi", "-i", "sine=f=440:duration=4",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(clip)], check=True)
sig, cuts = moments.ffmpeg_signals(clip, 4.0)
assert all(len(sig[k]) == 16 for k in ("motion", "blur", "yavg", "loud")), {k: len(v) for k, v in sig.items()}
assert sig["motion"][2] < 0.1 < sig["motion"][12], sig["motion"]  # still black vs moving pattern
assert sig["yavg"][2] < 30 < sig["yavg"][12], sig["yavg"]
assert sig["loud"][8] > 20, sig["loud"]  # tone is well above the -70 LUFS floor
assert any(abs(c - 2.0) < 0.3 for c in cuts), cuts  # black -> pattern is a scene cut

# 10. cache: second call reads the file; a touched proxy recomputes
moments.MOMENTS = tmp / "moments"
first = moments.signals("U", clip)
assert (tmp / "moments" / "U.json").is_file() and first["duration"] > 3.5
assert moments.signals("U", clip) == first
os.utime(clip, (1, 1))
assert moments.signals("U", clip)["mtime"] == 1.0

# 11. brief match from stored scene embeddings (text embedding stubbed: no model load in tests)
import numpy as np  # noqa: E402

core.embed_query = lambda text: np.array([1.0, 0.0], np.float32)
with core.connect() as c:
    c.execute("insert into assets(uuid, kind, path, duration, meta) values ('B', 'video', 'x', 20, 'x')")
    for t0, t1, v in ((0, 8, [0.0, 1.0]), (8, 16, [1.0, 0.0])):
        c.execute("insert into segments(uuid, t0, t1, source, vec) values ('B', ?, ?, 'scene', ?)", (t0, t1, np.array(v, np.float32).tobytes()))
bs = moments.brief_series("B", N, "cake")
assert bs[at(4)] == 0.0 and bs[at(12)] == 1.0 and len(bs) == N
assert moments.brief_series("nope", N, "cake") is None
print("ok")
