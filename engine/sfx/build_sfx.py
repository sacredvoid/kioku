"""Rebuilds the curated seam sounds in this folder from their CC0 sources. Developer tool, not shipped in the app.

    python build_sfx.py            (needs network and ffmpeg; writes <name>.m4a and MANIFEST.json next to this file)

Every source is a Freesound sound under CC0 1.0 (public domain, no credit needed). The script reads each sound's own
Freesound page and refuses to build unless that page links the CC0 deed, so a licence change upstream cannot slip in.
Each sound is trimmed, faded a few ms at both ends (no click), made mono at 44.1 kHz and peak-normalised to -3 dBFS.
"before" is how long the file runs before its moment lands on the seam (the whoosh's peak, the hit's onset, the end of a riser).
"""
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CC0 = "https://creativecommons.org/publicdomain/zero/1.0/"
UA = {"User-Agent": "Mozilla/5.0"}
# name: (freesound id, preview-file suffix, author, title, keep seconds, fade in s, fade out s, before s, mix volume)
SOURCES = {
    "whoosh": (427823, "2247456", "Kinoton", "Whoosh #1", 0.8, 0.005, 0.05, 0.20, 0.8),
    "swish": (395369, "1675920", "ihitokage", "Swish 4", 0.6, 0.005, 0.05, 0.12, 0.7),
    "hit": (535592, "9015615", "XHALE303", "BOOM IMPACT 2", 1.3, 0.002, 0.4, 0.0, 0.85),
    "riser": (561207, "10825267", "Rizzard", "Riser", 2.0, 0.05, 0.02, 2.0, 0.6),
    "swell": (503799, "9637845", "neezen.", "reverse fx 9.wav", 1.85, 0.05, 0.03, 1.8, 0.55),
    "pop": (244654, "3624044", "greenvwbeetle", "Pop 2", 0.14, 0.002, 0.02, 0.03, 0.6),
    "shutter": (170229, "3133582", "roachpowder", "Camera Shutter", 0.3, 0.002, 0.03, 0.05, 0.6),
}


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()


def encode(src, dst, keep, fi, fo, gain):
    f = (f"atrim=end={keep},asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:channel_layouts=mono,aresample=44100,"
         f"afade=t=in:d={fi},afade=t=out:st={max(keep - fo, 0)}:d={fo}" + (f",volume={gain}dB" if gain is not None else ""))
    return subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-y", "-i", str(src), "-af", f + ",volumedetect" if gain is None else f,
                           *(["-f", "null", "-"] if gain is None else ["-c:a", "aac", "-b:a", "96k", str(dst)])],
                          capture_output=True, text=True, check=True)


def main():
    manifest = {}
    with tempfile.TemporaryDirectory() as td:
        for name, (fid, suffix, author, title, keep, fi, fo, before, vol) in SOURCES.items():
            page = f"https://freesound.org/people/{author}/sounds/{fid}/"
            if CC0 not in get(page).decode("utf8", "replace").replace("http://", "https://"):
                sys.exit(f"{name}: {page} does not carry the CC0 licence; not building")
            audio = f"https://cdn.freesound.org/previews/{fid // 1000}/{fid}_{suffix}-hq.mp3"
            src = Path(td) / f"{name}.mp3"
            src.write_bytes(get(audio))
            m = re.search(r"max_volume: (-?[\d.]+) dB", encode(src, None, keep, fi, fo, None).stderr)
            if not m:
                sys.exit(f"{name}: could not measure peak")
            out = HERE / f"{name}.m4a"
            encode(src, out, keep, fi, fo, round(-3.0 - float(m.group(1)), 2))
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                                       capture_output=True, text=True, check=True).stdout)
            manifest[name] = {"file": out.name, "title": title, "author": author, "source_url": page, "audio_url": audio,
                              "license": "CC0-1.0", "license_url": CC0, "dur": round(dur, 3), "before": before, "volume": vol}
            print(name, out.stat().st_size, "bytes", round(dur, 2), "s")
    (HERE / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
