"""Search the index from the terminal.

  uv run search.py "sunset from the car"         # top 10
  uv run search.py "dog on the beach" --open     # preview top 5 in Quick Look
"""
import argparse
import subprocess

import core


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--videos", action="store_true", help="videos only")
    a = ap.parse_args()

    hits = core.Index(core.connect()).search(a.query, a.k, kind="video" if a.videos else None)
    if not hits:
        print("No matches.")
        return
    for i, h in enumerate(hits, 1):
        why = "visual+keyword" if h["keyword"] and h["visual"] else "keyword" if h["keyword"] else "visual"
        print(f"{i:>2}. {h['score']:6.2f}  vis {h['visual']:.3f}  {why:<14} {h['kind']:<6} {h['uuid'][:8]}  {h['meta'][:70]}")
    if a.open:
        subprocess.run(["qlmanage", "-p", *[h["path"] for h in hits[:5] if h["path"]]],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
