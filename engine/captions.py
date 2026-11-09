"""Captions: whisper.cpp transcripts (cached per clip), mapping onto the edit's output timeline, PNG cue track, .srt.

Transcription runs once per (uuid, MODEL); the cue overlay and srt are rebuilt on every render from the cache,
so toggling captions or changing speed/transitions never re-transcribes. Design: notes/editing-design.md section 4.
"""
import hashlib
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException

import core
import library
import media
import projects
from timeline import Timeline

router = APIRouter()
log = logging.getLogger("safelight.captions")
MODEL = "ggml-small.bin"
MODELS = core.DATA / "models"
SILENT_DB = -45.0  # mean volume under this: whisper would hallucinate "Thank you." on wind or music
MIN_CUE = 0.25
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_UNI = "/Library/Fonts/Arial Unicode.ttf"
_whisper = threading.Lock()  # one whisper at a time, on top of the media slot

SCHEMA = """
create table if not exists transcripts(
  uuid text not null, model text not null, lang text, status text not null
    check(status in ('ok','no_audio','silent')), created real,
  primary key(uuid, model));
create table if not exists transcript_lines(
  uuid text not null, model text not null, idx integer not null,
  t0 real not null, t1 real not null, text text not null,
  primary key(uuid, model, idx));
"""


@dataclass(frozen=True)
class Line:
    t0: float
    t1: float
    text: str  # source-time seconds


@dataclass(frozen=True)
class Cue:
    t0: float
    t1: float
    text: str  # output-time seconds


@dataclass(frozen=True)
class Captions:
    cues: tuple[Cue, ...]
    list_file: Path | None  # ffconcat list; None when there is nothing to draw


# ---------- setup

def whisper_bin():
    return shutil.which("whisper-cli") or next((p for p in ("/opt/homebrew/bin/whisper-cli",) if Path(p).is_file()), None)


def model_path():
    return MODELS / MODEL


def setup_status():
    w, m = whisper_bin(), model_path().is_file()
    hint = "" if w and m else (
        ("brew install whisper-cpp\n" if not w else "") +
        (f"mkdir -p data/models && curl -L -o data/models/{MODEL} "
         f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL}\n" if not m else "")).strip()
    return {"whisper": w, "model": m, "hint": hint}


def require_setup():
    s = setup_status()
    if s["hint"]:
        raise RuntimeError("Captions need whisper.cpp and its model. Run once in a terminal:\n" + s["hint"])


# ---------- transcription

def _db(c):
    c.executescript(SCHEMA)
    return c


def cached(uuid):
    """(status, [Line]) if this clip was transcribed with MODEL, else None."""
    with core.connect() as c:
        row = _db(c).execute("select status from transcripts where uuid = ? and model = ?", (uuid, MODEL)).fetchone()
        if not row:
            return None
        rows = c.execute("select t0, t1, text from transcript_lines where uuid = ? and model = ? order by idx", (uuid, MODEL)).fetchall()
    return row[0], [Line(*r) for r in rows]


def store(uuid, status, lang, lines):
    import time
    with core.connect() as c:
        _db(c)
        c.execute("delete from transcript_lines where uuid = ? and model = ?", (uuid, MODEL))
        c.execute("insert or replace into transcripts values (?,?,?,?,?)", (uuid, MODEL, lang, status, time.time()))
        c.executemany("insert into transcript_lines values (?,?,?,?,?,?)", [(uuid, MODEL, i, l.t0, l.t1, l.text) for i, l in enumerate(lines)])
        c.commit()


_TAG = re.compile(r"^[\[\(\*][^\]\)\*]*[\]\)\*]$")


def clean(lines):
    """Drop empty lines, non-speech tags ([MUSIC], (applause), *laughs*, music notes) and collapse 3+ identical repeats."""
    out = []
    for l in lines:
        t = l.text.strip()
        if not t or _TAG.match(t) or not re.sub(r"[\s♪♫#.\-]", "", t):
            continue
        out.append(Line(l.t0, l.t1, t))
    res = []
    for _, g in itertools.groupby(out, key=lambda l: l.text):
        run = list(g)
        res += run[:1] if len(run) >= 3 else run
    return res


def mean_db(path):
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)", r.stderr)
    return float(m.group(1)) if m else None


def run_whisper(wav, base, seconds):
    """Real-run flags are from memory of whisper.cpp 1.9.x: confirm with `whisper-cli -h` after installing."""
    cmd = [whisper_bin(), "-m", str(model_path()), "-f", str(wav), "-l", "auto", "-oj", "-of", str(base), "-sns", "-t", "4", "-np"]
    for attempt in (cmd, cmd + ["-ng"]):  # -ng = no GPU, the retry when Metal fails
        try:
            r = subprocess.run(attempt, capture_output=True, text=True, timeout=max(120, 2 * seconds),
                               preexec_fn=lambda: os.nice(10))
        except subprocess.TimeoutExpired:
            raise RuntimeError("whisper took too long on one clip; skipped it")
        if r.returncode == 0:
            return
        if attempt is cmd and "metal" not in (r.stdout + r.stderr).lower():
            break
    raise RuntimeError("whisper failed: " + ((r.stderr or r.stdout).strip().splitlines() or ["no output"])[-1][:300])


def transcribe(uuid):
    """Transcribe one clip if not cached. Lock order as everywhere: lock_for(uuid), then video_slot."""
    with media.lock_for(uuid):
        got = cached(uuid)
        if got:
            return got
        key = f"captions:{uuid}"
        media.video_slot(key, label=f"Captions: {media.label_for(uuid)}")
        try:
            media.stage(key, "listening for speech", f"Captions: {media.label_for(uuid)}")
            opus = core.AUDIO_DIR / f"{uuid}.opus"
            if not opus.is_file():
                proxy = media.PROXIES / f"{uuid}.mp4"
                if proxy.is_file():
                    import index
                    index.extract_audio(proxy, uuid)
                else:
                    media.ensure_proxy(uuid)  # downloads from iCloud; also writes the opus
            if not opus.is_file():
                store(uuid, "no_audio", None, [])
                return "no_audio", []
            db = mean_db(opus)
            if db is not None and db < SILENT_DB:
                store(uuid, "silent", None, [])
                return "silent", []
            require_setup()
            tmp = Path(tempfile.mkdtemp(prefix="cs-whisper-"))
            try:
                wav = tmp / "a.wav"
                r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(opus), "-ar", "16000", "-ac", "1",
                                    "-c:a", "pcm_s16le", str(wav)], capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError("could not read the clip's audio: " + r.stderr.strip()[-200:])
                with _whisper:
                    run_whisper(wav, tmp / "out", media.probe_duration(opus))
                try:
                    data = json.loads((tmp / "out.json").read_text())
                except (OSError, ValueError):
                    raise RuntimeError("whisper wrote no readable result")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            raw = [Line(t["offsets"]["from"] / 1000, t["offsets"]["to"] / 1000, t["text"]) for t in data.get("transcription", [])]
            lines = clean(raw)
            store(uuid, "ok", (data.get("result") or {}).get("language"), lines)
            return "ok", lines
        finally:
            media.release_video_slot()
            media.stage(key, None)


def ensure_transcripts(uuids, on_progress=None, errors=None):
    """uuid -> [Line] (empty for silent / no audio / failed). A clip that fails is reported in `errors` (if given) and
    retried next time; missing whisper or model raises RuntimeError with the install hint. Never call while holding a slot."""
    uuids = list(dict.fromkeys(uuids))
    if any(cached(u) is None for u in uuids):
        require_setup()
    out = {}
    for n, u in enumerate(uuids, 1):
        try:
            out[u] = transcribe(u)[1]
        except RuntimeError as e:
            out[u] = []
            if errors is not None:
                errors[u] = str(e)
        except HTTPException as e:  # not a video / not indexed
            out[u] = []
            if errors is not None:
                errors[u] = str(e.detail)
        if on_progress:
            on_progress(n, len(uuids))
    return out


# ---------- timing (pure)

def map_cues(tl: Timeline, transcripts):
    """Source-time lines -> output-time cues, through in/out, speed and the position of each clip (section 4)."""
    cues = []
    for s in tl.segs:
        if s.kind != "video":
            continue
        a, b = s.src_in, s.src_out
        start = s.start_frame / tl.fps
        end = start + s.frames / tl.fps
        for l in transcripts.get(s.uuid, ()):
            overlap = min(l.t1, b) - max(l.t0, a)
            if overlap < 0.5 * (l.t1 - l.t0) or overlap < 0.3:
                continue
            c0, c1 = max(l.t0, a), min(l.t1, b)
            cues.append(Cue(start + (c0 - a) / s.speed, min(end, start + (c1 - a) / s.speed), l.text))
    cues.sort(key=lambda c: c.t0)
    fixed = [Cue(c.t0, min(c.t1, nxt.t0), c.text) for c, nxt in zip(cues, cues[1:])] + cues[-1:]  # tail inside a crossfade
    return [c for c in fixed if c.t1 - c.t0 >= MIN_CUE]


def fmt_srt(t):
    ms = round(max(t, 0) * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(cues, path: Path, shift: float = 0.0, end: float | None = None):
    """Cues shifted by -shift; those outside 0..end (the rendered window's length) are dropped or clipped."""
    blocks, n = [], 0
    for c in cues:
        t0, t1 = c.t0 - shift, c.t1 - shift
        if end is not None:
            t1 = min(t1, end)
        if t1 <= 0 or t1 <= t0:
            continue
        n += 1
        blocks.append(f"{n}\n{fmt_srt(t0)} --> {fmt_srt(t1)}\n{c.text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


# ---------- overlay (Pillow; this ffmpeg has no drawtext/subtitles filters)

def _font(text, size):
    from PIL import ImageFont
    path = FONT_UNI if any(ord(ch) > 0x24F for ch in text) else FONT
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        raise RuntimeError(f"caption font missing: {path}")


def wrap(text, font, width):
    """Greedy wrap by measured width. Words when the text has spaces, characters otherwise (CJK)."""
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("L", (1, 1)))
    sep = " " if " " in text else ""
    toks = text.split(" ") if sep else list(text)
    lines, cur = [], ""
    for t in toks:
        trial = f"{cur}{sep}{t}" if cur else t
        if cur and d.textlength(trial, font=font) > width:
            lines.append(cur)
            cur = t
        else:
            cur = trial
    return lines + [cur] if cur else lines


def split_cue(c: Cue, font, width):
    """At most 2 lines per cue: longer text becomes consecutive cues, durations proportional to characters."""
    lines = wrap(c.text.replace("\n", " "), font, width)
    pages = ["\n".join(lines[i:i + 2]) for i in range(0, len(lines), 2)]
    total, t = sum(len(p) for p in pages) or 1, c.t0
    out = []
    for p in pages:
        t1 = t + (c.t1 - c.t0) * len(p) / total
        out.append(Cue(t, t1, p))
        t = t1
    return out


def draw_png(text, style, dims, path: Path):
    from PIL import Image, ImageDraw
    W, H = dims
    size = max(12, round(0.055 * min(W, H)))
    font = _font(text, size)
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    stroke = size // 14 if style == "bold" else 0
    kw = dict(font=font, spacing=size // 4, stroke_width=stroke, align="center")
    x0, y0, x1, y1 = d.multiline_textbbox((0, 0), text, **kw)
    bottom = 0.92 * H if W > H else 0.80 * H
    ox, oy = (W - (x1 - x0)) / 2 - x0, bottom - (y1 - y0) - y0
    if style == "box":
        pad = size // 3
        d.rounded_rectangle((ox + x0 - pad, oy + y0 - pad, ox + x1 + pad, oy + y1 + pad), radius=pad, fill=(0, 0, 0, 140))
    d.multiline_text((ox, oy), text, fill=(255, 255, 255, 255), stroke_fill=(0, 0, 0, 255), **kw)
    im.save(path)


def build(tl: Timeline, style, dims, cues, workdir: Path, window=None):
    """Draw one full-frame PNG per cue plus a blank, write workdir/cues.txt (ffconcat, `option framerate` under every
    file line so cues land on exact frames). Times shift by -window start. Returns Captions(cues drawn, list file)."""
    fps = tl.fps
    w0, w1 = window if window else (0.0, tl.duration)
    w1 = min(w1, tl.duration)
    width = 0.86 * dims[0]
    size = max(12, round(0.055 * min(dims)))
    shifted = []
    for c in cues:
        t0, t1 = max(c.t0, w0) - w0, min(c.t1, w1) - w0
        if t1 - t0 >= MIN_CUE:
            shifted += split_cue(Cue(t0, t1, c.text), _font(c.text, size), width)
    total = round((w1 - w0) * fps)
    entries, cur, drawn = [], 0, []
    for c in shifted:
        f0, f1 = max(round(c.t0 * fps), cur), min(round(c.t1 * fps), total)
        if f1 <= f0:
            continue
        if f0 > cur:
            entries.append((None, f0 - cur))
        entries.append((c.text, f1 - f0))
        drawn.append(Cue(f0 / fps, f1 / fps, c.text))
        cur = f1
    if not drawn:
        return Captions((), None)
    entries.append((None, max(total - cur, 1)))
    workdir.mkdir(parents=True, exist_ok=True)
    names = {}

    def png(text):
        key = hashlib.sha1(f"{text}|{style}|{dims}".encode()).hexdigest()[:16] if text else "blank"
        if key not in names:
            names[key] = f"cap_{key}.png"
            if text:
                draw_png(text, style, dims, workdir / names[key])
            else:
                from PIL import Image
                Image.new("RGBA", dims, (0, 0, 0, 0)).save(workdir / names[key])
        return names[key]

    lines = ["ffconcat version 1.0"]
    for text, n in entries:
        lines += [f"file '{png(text)}'", f"option framerate {fps}", f"duration {n / fps:.6f}"]
    lines += [f"file '{png(None)}'", f"option framerate {fps}"]  # the demuxer ignores the last duration
    lst = workdir / "cues.txt"
    lst.write_text("\n".join(lines) + "\n")
    return Captions(tuple(drawn), lst)


# ---------- job + routes

def video_uuids(cid):
    return list(dict.fromkeys(i["uuid"] for i in library.get_collection(cid)["items"] if i["kind"] != "image"))


def counts(uuids):
    ready = pending = quiet = 0
    for u in uuids:
        got = cached(u)
        if got is None:
            pending += 1
        elif got[0] == "ok" and got[1]:
            ready += 1
        else:
            quiet += 1
    return {"ready": ready, "pending": pending, "no_speech": quiet}


def _job(cid):
    """Job target: transcribe the project's pending clips, progress in the shared jobs dict."""
    j = projects.jobs[(cid, "captions")]
    todo = [u for u in video_uuids(cid) if cached(u) is None]
    errors = {}
    j.update(stage=f"Transcribing 0 of {len(todo)}", progress=0.0)
    ensure_transcripts(todo, lambda n, t: j.update(stage=f"Transcribing {min(n + 1, t)} of {t}", progress=n / t), errors)
    return {"progress": 1.0, "message": f"Transcribed {len(todo) - len(errors)} of {len(todo)} clips",
            "warnings": [f"{media.label_for(u)}: {e}" for u, e in errors.items()]}


def start_transcription(cid):
    """Called by PATCH /edit when captions turn on. Idempotent: a running job is returned, not an error."""
    try:
        require_setup()
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    try:
        return projects.start_job(cid, "captions", _job)
    except HTTPException as e:
        if e.status_code == 409:
            return projects.job(cid, "captions")
        raise


@router.get("/collections/{cid}/captions")
def captions_state(cid: int):
    import edit  # lazy: edit owns the spec
    spec, _ = edit.load(cid)
    return {"on": spec["captions"]["on"], "style": spec["captions"]["style"], **counts(video_uuids(cid)), "setup": setup_status()}


@router.post("/collections/{cid}/captions/transcribe", status_code=202)
def transcribe_route(cid: int):
    return start_transcription(cid)


@router.get("/collections/{cid}/captions/job")
def captions_job(cid: int):
    return projects.job(cid, "captions")


@router.get("/media/{uuid}/transcript")
def transcript_route(uuid: str):
    got = cached(uuid)
    if got is None:
        raise HTTPException(404, "not transcribed yet")
    return {"uuid": uuid, "model": MODEL, "status": got[0], "lines": [{"t0": l.t0, "t1": l.t1, "text": l.text} for l in got[1]]}
