"""Indexing progress bar.

  uv run status.py            # once
  uv run status.py --watch    # refresh every 2 s, Ctrl+C to stop
"""
import argparse
import json
import subprocess
import sys
import time

import core

PROGRESS = core.DATA / "progress.json"


def show():
    try:
        run = json.loads(PROGRESS.read_text())
    except (OSError, ValueError):
        run = {}
    db = core.connect()
    started = run.get("started", 0)
    n, photos, posters, videos, failed = db.execute(
        "select count(*), sum(kind='image'), sum(kind='poster'), sum(kind='video'), sum(error is not null) "
        "from assets where indexed_at >= ?", (started,)).fetchone()
    total = run.get("total") or n
    recent = db.execute("select count(*) from assets where indexed_at >= ?", (time.time() - 300,)).fetchone()[0]
    rate = recent / 300
    pct = n / total * 100 if total else 0
    bar = "█" * int(pct / 100 * 30) + "░" * (30 - int(pct / 100 * 30))
    eta = f"~{(total - n) / rate / 60:.0f} min left" if rate and n < total else "done" if n >= total else "stalled"
    running = subprocess.run(["pgrep", "-f", "index.py"], capture_output=True).returncode == 0
    return (f"[{bar}] {pct:.1f}%  {n:,} / {total:,}\n"
            f"{rate:.2f} items/s (last 5 min) · {eta}\n"
            f"photos {photos or 0} · video posters {posters or 0} · videos {videos or 0} · failed {failed or 0}\n"
            f"indexer: {'running' if running else 'not running'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    if not ap.parse_args().watch:
        print(show())
        sys.exit()
    try:
        while True:
            print("\033[2J\033[H" + show(), flush=True)
            time.sleep(2)
    except KeyboardInterrupt:
        pass
