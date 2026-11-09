"""A/B: image tokens 280 vs 560 for search quality and indexing speed.  uv run ab_tokens.py [--n 600]

Ground truth = Apple's Photos labels (imperfect, but the same for both arms). For each label query we rank a
random sample of already-indexed stills by visual similarity only (no keyword boost) and score average
precision and precision@10. 560 uses the vectors already in the index; 280 re-embeds the same files.
Reads ~/Kioku/index.db.
"""
import argparse
import random
import time

import numpy as np

import core

LABELS = ["Document", "Foliage", "Automobile", "Sunset", "Food", "Skyline", "Mountain", "Lake", "Crowd", "Bicycle",
          "Stadium", "Statue", "Drink", "Maple Tree", "Eyeglasses", "Luggage", "Bench", "Darkness", "Water Body",
          "Interior Room", "Street", "Sunglasses", "Dish", "Backpack", "Forest", "Performance", "Sign"]


def ap(scores, rel):
    order = np.argsort(-scores)
    hits = rel[order]
    if not hits.any():
        return None
    prec = np.cumsum(hits) / (np.arange(len(hits)) + 1)
    return float((prec * hits).sum() / hits.sum())


def p_at(scores, rel, k=10):
    return float(rel[np.argsort(-scores)[:k]].mean())


def encode(paths, tokens):
    m = core.model()
    from PIL import Image
    t = time.time()
    vecs = m.encode([{"image": Image.open(p).convert("RGB")} for p in paths], batch_size=8, normalize_embeddings=True,
                    processing_kwargs={"image": {"max_soft_tokens": tokens}})
    return np.asarray(vecs, dtype=np.float32), time.time() - t


def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--n", type=int, default=600)
    ap_.add_argument("--seed", type=int, default=7)
    a = ap_.parse_args()
    db = core.connect()
    rows = db.execute("select a.uuid, a.path, a.meta, s.vec from assets a join segments s using(uuid) "
                      "where a.kind in ('image','poster') and s.source in ('image','poster') and a.path is not null").fetchall()
    rows = [r for r in rows if r[1] and __import__("os").path.isfile(r[1])]
    random.Random(a.seed).shuffle(rows)
    rows = rows[: a.n]
    print(f"{len(rows)} items sampled")
    v560 = np.stack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
    v280, secs280 = encode([r[1] for r in rows], 280)
    sub = [r[1] for r in rows[:64]]  # same 64 files at both settings, for a fair speed number
    _, s280 = encode(sub, 280)
    _, s560 = encode(sub, 560)
    agree = float(np.mean(np.sum(v560 * v280, axis=1)))

    labels = [set(r[2].split(" ; ")) for r in rows]
    out = []
    for lab in LABELS:
        rel = np.array([lab in ls for ls in labels], dtype=float)
        if rel.sum() < 3:
            continue
        q = core.embed_query(lab.lower())
        s5, s2 = v560 @ q, v280 @ q
        out.append((lab, int(rel.sum()), ap(s5, rel), ap(s2, rel), p_at(s5, rel), p_at(s2, rel)))

    lines = [f"# Image tokens: 280 vs 560 ({time.strftime('%Y-%m-%d %H:%M')})", "",
             f"Sample: {len(rows)} indexed stills (seed {a.seed}); {len(out)} label queries with 3+ matches.",
             f"Ground truth: Apple Photos labels. Ranking: visual similarity only.", "",
             "| Label | matches | AP 560 | AP 280 | P@10 560 | P@10 280 |", "|---|---|---|---|---|---|"]
    lines += [f"| {l} | {n} | {a5:.3f} | {a2:.3f} | {p5:.2f} | {p2:.2f} |" for l, n, a5, a2, p5, p2 in out]
    m = lambda i: np.mean([o[i] for o in out])
    wins = sum(o[3] > o[2] for o in out)
    lines += ["", f"**Mean AP:** 560 = {m(2):.3f}, 280 = {m(3):.3f} (280 better on {wins} of {len(out)} labels)",
              f"**Mean P@10:** 560 = {m(4):.3f}, 280 = {m(5):.3f}",
              f"**Speed (same 64 files, batch 8):** 280 = {64 / s280:.2f} items/s, 560 = {64 / s560:.2f} items/s "
              f"({s560 / s280:.2f}x)",
              f"**Vector agreement:** mean cosine(560, 280) = {agree:.3f}"]
    text = "\n".join(lines)
    print(text)
    return text


if __name__ == "__main__":
    import pathlib
    res = main()
    (pathlib.Path(__file__).resolve().parent.parent / "notes").mkdir(exist_ok=True)
    (pathlib.Path(__file__).resolve().parent.parent / "notes" / "tokens-ab.md").write_text(res + "\n")
