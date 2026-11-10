"""Captions: whisper.cpp transcripts (cached per clip), mapping onto the edit's output timeline, PNG cue track, .srt.

Transcription runs once per (uuid, MODEL); the cue overlay and srt are rebuilt on every render from the cache,
so toggling captions or changing speed/transitions never re-transcribes. Design: notes/editing-design.md section 4.
"""
import hashlib
import itertools
import json
import logging
import math
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
import settings
from timeline import Timeline

router = APIRouter()
log = logging.getLogger("kioku.captions")
MODEL = "ggml-small.bin"
MODELS = core.DATA / "models"
SILENT_DB = -45.0  # mean volume under this: whisper would hallucinate "Thank you." on wind or music
MIN_CUE = 0.25
SUP = "/System/Library/Fonts/Supplemental/"
FONTS = {"arial": SUP + "Arial Bold.ttf", "impact": SUP + "Impact.ttf", "georgia": SUP + "Georgia Bold.ttf",
         "verdana": SUP + "Verdana Bold.ttf", "trebuchet": SUP + "Trebuchet MS Bold.ttf",
         "rounded": SUP + "Arial Rounded Bold.ttf", "courier": SUP + "Courier New Bold.ttf"}  # keys: edit.CAPTION_FONTS
FONT = FONTS["arial"]
SIZE_FRAC = {"S": 0.045, "M": 0.055, "L": 0.07}  # of the short side
DEFAULT = {"style": "bold", "position": "bottom", "font": "arial", "size": "M", "color": "#FFFFFF",  # same keys as edit.CAPTIONS_DEFAULT
           "highlight_color": "#FFD400", "highlight": False, "uppercase": False, "max_words": 0}
LOGPROB_MIN = -1.0  # whisper's own fail threshold; mean token log-probability under this is invented text
HALLU = re.compile(r"thank(s| you) for watching|please subscribe|like and subscribe|subtitles? (by|from)|amara\.org", re.I)
FONT_UNI = "/Library/Fonts/Arial Unicode.ttf"
_whisper = threading.Lock()  # one whisper at a time, on top of the media slot

SCHEMA = """
create table if not exists transcripts(
  uuid text not null, model text not null, lang text, status text not null
    check(status in ('ok','no_audio','silent')), created real,  -- silent also holds clips judged hallucinated
  primary key(uuid, model));
create table if not exists transcript_lines(
  uuid text not null, model text not null, idx integer not null,
  t0 real not null, t1 real not null, text text not null,
  primary key(uuid, model, idx));
"""
# additive columns, added once per process; safe on a live WAL db (a brief write lock, no rewrite of existing rows)
ADD = (("transcripts", "req_lang", "text"), ("transcript_lines", "words", "text"))
_migrated = False


@dataclass(frozen=True)
class Line:
    t0: float
    t1: float
    text: str  # source-time seconds
    words: tuple = ()  # ((t0, t1, text), ...) source-time; empty for transcripts made before word timing


@dataclass(frozen=True)
class Cue:
    t0: float
    t1: float
    text: str  # output-time seconds
    words: tuple = ()  # same, output-time


@dataclass(frozen=True)
class Captions:
    cues: tuple[Cue, ...]
    list_file: Path | None  # ffconcat list; None when there is nothing to draw
    runs: tuple = ()        # ((PNG name in the workdir or None for blank, frames), ...): what titles.build merges with


# ---------- setup

def whisper_bin():
    return shutil.which("whisper-cli") or next((p for p in ("/opt/homebrew/bin/whisper-cli",) if Path(p).is_file()), None)


def model_path():
    return MODELS / MODEL


def setup_status():
    w, m = whisper_bin(), model_path().is_file()
    hint = "" if w and m else (
        ("brew install whisper-cpp\n" if not w else "") +
        (f"mkdir -p ~/Kioku/models && curl -L -o ~/Kioku/models/{MODEL} "
         f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL}\n" if not m else "")).strip()
    return {"whisper": w, "model": m, "hint": hint}


def require_setup():
    s = setup_status()
    if s["hint"]:
        raise RuntimeError("Captions need whisper.cpp and its model. Run once in a terminal:\n" + s["hint"])


# ---------- transcription

def _db(c):
    global _migrated
    c.executescript(SCHEMA)
    if not _migrated:
        for tbl, col, typ in ADD:
            if col not in {r[1] for r in c.execute(f"pragma table_info({tbl})")}:
                try:
                    c.execute(f"alter table {tbl} add column {col} {typ}")
                except core.sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):  # another process added it first
                        raise
        c.commit()
        _migrated = True
    return c


def _load(uuid):
    """(status, lang, requested language, [Line]) for MODEL, or None."""
    with core.connect() as c:
        row = _db(c).execute("select status, lang, req_lang from transcripts where uuid = ? and model = ?", (uuid, MODEL)).fetchone()
        if not row:
            return None
        rows = c.execute("select t0, t1, text, words from transcript_lines where uuid = ? and model = ? order by idx", (uuid, MODEL)).fetchall()
    return row[0], row[1], row[2] or "auto", [Line(r[0], r[1], r[2], tuple(tuple(w) for w in json.loads(r[3])) if r[3] else ()) for r in rows]


def cached(uuid):
    """(status, [Line]) if this clip was transcribed with MODEL, else None."""
    got = _load(uuid)
    return (got[0], got[3]) if got else None


def fresh(uuid, want, words=False):
    """cached(), unless it was made for another language or lacks the word times `words` asks for."""
    got = _load(uuid)
    if not got:
        return None
    status, _, req, lines = got
    if status != "no_audio" and req != want:
        return None
    if words and status == "ok" and any(not l.words for l in lines):
        return None
    return status, lines


def store(uuid, status, lang, lines, want="auto"):
    import time
    with core.connect() as c:
        _db(c)
        c.execute("delete from transcript_lines where uuid = ? and model = ?", (uuid, MODEL))
        c.execute("insert or replace into transcripts(uuid, model, lang, status, created, req_lang) values (?,?,?,?,?,?)",
                  (uuid, MODEL, lang, status, time.time(), want))
        c.executemany("insert into transcript_lines(uuid, model, idx, t0, t1, text, words) values (?,?,?,?,?,?,?)",
                      [(uuid, MODEL, i, l.t0, l.t1, l.text, json.dumps(l.words) if l.words else None) for i, l in enumerate(lines)])
        c.commit()


def speech_status(uuid):
    got = cached(uuid)
    return "not transcribed" if got is None else "speech" if got[0] == "ok" and got[1] else "no speech"


def likely_speech(uuid):
    """Cheap guess for preselecting clips: known result, else the audio is above the silence gate. No audio file = no."""
    got = cached(uuid)
    if got:
        return got[0] == "ok" and bool(got[1])
    opus = core.AUDIO_DIR / f"{uuid}.opus"
    db = mean_db(opus) if opus.is_file() else None
    return db is not None and db >= SILENT_DB


_TAG = re.compile(r"^[\[\(\*][^\]\)\*]*[\]\)\*]$")


def clean(lines):
    """Drop empty lines, non-speech tags ([MUSIC], (applause), *laughs*, music notes) and collapse 3+ identical repeats."""
    out = []
    for l in lines:
        t = l.text.strip()
        if not t or _TAG.match(t) or not re.sub(r"[\s♪♫#.\-]", "", t):
            continue
        out.append(Line(l.t0, l.t1, t, l.words))
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


def language():
    return settings.get().caption_language


def run_whisper(wav, base, seconds, lang):
    """whisper-cli 1.9.5 flags, checked with `whisper-cli -h`: -ojf writes per-token times and probabilities."""
    cmd = [whisper_bin(), "-m", str(model_path()), "-f", str(wav), "-l", lang, "-ojf", "-of", str(base), "-sns", "-t", "4", "-np"]
    # auto language detection is ~25x slower on noisy clips, so it gets a far longer leash
    timeout = max(600, 40 * seconds) if lang == "auto" else max(300, 12 * seconds)
    for attempt in (cmd, cmd + ["-ng"]):  # -ng = no GPU, the retry when Metal fails
        try:
            # nice as a prefix, like render.run_ffmpeg: preexec_fn is documented unsafe in a threaded process
            r = subprocess.run(["nice", "-n", "10", *attempt], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("whisper took too long on one clip; skipped it")
        if r.returncode == 0:
            return
        if attempt is cmd and "metal" not in (r.stdout + r.stderr).lower():
            break
    raise RuntimeError("whisper failed: " + ((r.stderr or r.stdout).strip().splitlines() or ["no output"])[-1][:300])


def parse(data):
    """whisper -ojf JSON -> (language, [Line with words], mean token log-probability or None)."""
    lines, lps = [], []
    for seg in data.get("transcription", []):
        words = []
        for k in seg.get("tokens", []):
            txt, o = k.get("text", ""), k.get("offsets") or {}
            if (txt.startswith("[_") and txt.endswith("]")) or "from" not in o:  # [_BEG_], [_TT_210]: markers, not speech
                continue
            if k.get("p", 0) > 0:
                lps.append(math.log(k["p"]))
            t0, t1 = o["from"] / 1000, max(o["to"], o["from"]) / 1000
            if words and not txt.startswith(" "):  # continuation of the previous word; punctuation does not stretch it
                w = words[-1]
                words[-1] = (w[0], max(w[1], t1) if any(ch.isalnum() for ch in txt) else w[1], w[2] + txt)
            elif txt.strip():
                words.append((t0, t1, txt.strip()))
        o = seg["offsets"]
        lines.append(Line(o["from"] / 1000, o["to"] / 1000, seg["text"], tuple(words)))
    return (data.get("result") or {}).get("language"), lines, (sum(lps) / len(lps) if lps else None)


def hallucinated(raw, lang, want, logprob):
    """True when whisper invented text for noise: all segments exactly 2.0 s, a language other than the one asked for,
    a very low mean log-probability, 'thanks for watching' style boilerplate, one to three words stretched over seconds,
    or the same line three times."""
    texts = [l.text.strip() for l in raw if l.text.strip()]
    if not texts:
        return False
    if all(abs(l.t1 - l.t0 - 2.0) < 0.005 for l in raw):
        return True
    if want != "auto" and lang and lang != want:
        return True
    if logprob is not None and logprob < LOGPROB_MIN:
        return True
    if 2 * sum(bool(HALLU.search(t)) for t in texts) >= len(texts):
        return True
    ws = [w for l in raw for w in l.words]
    if 0 < len(ws) <= 3 and any(w[1] - w[0] > 2.5 for w in ws):  # "Thank you." smeared over 8 s of noise (seen with pink noise)
        return True
    return max(texts.count(t) for t in set(texts)) >= 3


def transcribe(uuid, words=False):
    """Transcribe one clip if not cached (or cached for another language / without word times). Lock order as
    everywhere: lock_for(uuid), then video_slot."""
    with media.lock_for(uuid):
        want = language()
        got = fresh(uuid, want, words)
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
                store(uuid, "no_audio", None, [], want)
                return "no_audio", []
            db = mean_db(opus)
            if db is not None and db < SILENT_DB:
                store(uuid, "silent", None, [], want)
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
                    run_whisper(wav, tmp / "out", media.probe_duration(opus), want)
                try:
                    data = json.loads((tmp / "out.json").read_text())
                    lang, raw, logprob = parse(data)
                except (OSError, ValueError, KeyError):
                    raise RuntimeError("whisper wrote no readable result")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if hallucinated(raw, lang, want, logprob):
                log.info("captions: %s judged hallucinated (lang=%s logprob=%s)", uuid, lang, logprob)
                store(uuid, "silent", lang, [], want)
                return "silent", []
            lines = clean(raw)
            store(uuid, "ok", lang, lines, want)
            return "ok", lines
        finally:
            media.release_video_slot()
            media.stage(key, None)


def ensure_transcripts(uuids, on_progress=None, errors=None, words=False):
    """uuid -> [Line] (empty for silent / no audio / failed). A clip that fails is reported in `errors` (if given) and
    retried next time; missing whisper or model raises RuntimeError with the install hint. Never call while holding a slot."""
    uuids = list(dict.fromkeys(uuids))
    want = language()
    if any(fresh(u, want, words) is None for u in uuids):
        require_setup()
    out = {}
    for n, u in enumerate(uuids, 1):
        try:
            out[u] = transcribe(u, words)[1]
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

def _trim(c: Cue, t1):
    """Cue ending at t1 instead; words starting after t1 go, the last one is cut there."""
    return Cue(c.t0, t1, c.text, tuple((w0, min(w1, t1), t) for w0, w1, t in c.words if w0 < t1))


def map_cues(tl: Timeline, transcripts, only=None):
    """Source-time lines -> output-time cues, through in/out, speed and the position of each clip (section 4).
    `only`: item_ids to caption; other clips get nothing (None = all)."""
    cues = []
    for s in tl.segs:
        if s.kind != "video" or (only is not None and s.item_id not in only):
            continue
        a, b = s.src_in, s.src_out
        start = s.start_frame / tl.fps
        end = start + s.frames / tl.fps
        to_out = lambda x: min(end, start + (min(max(x, a), b) - a) / s.speed)  # noqa: E731
        for l in transcripts.get(s.uuid, ()):
            overlap = min(l.t1, b) - max(l.t0, a)
            if overlap < 0.5 * (l.t1 - l.t0) or overlap < 0.3:
                continue
            c0, c1 = max(l.t0, a), min(l.t1, b)
            words = tuple((to_out(w0), to_out(w1), t) for w0, w1, t in l.words if w1 > a and w0 < b)
            cues.append(Cue(start + (c0 - a) / s.speed, min(end, start + (c1 - a) / s.speed), l.text, words))
    cues.sort(key=lambda c: c.t0)
    fixed = [_trim(c, min(c.t1, nxt.t0)) for c, nxt in zip(cues, cues[1:])] + cues[-1:]  # tail inside a crossfade
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

def needs_words(cfg):
    return bool(cfg.get("highlight")) or cfg.get("style") == "pop"


def _font(text, size, key="arial"):
    from PIL import ImageFont
    path = FONT_UNI if any(ord(ch) > 0x24F for ch in text) else FONTS.get(key, FONT)
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        raise RuntimeError(f"caption font missing: {path}")


def font_size(cfg, dims):
    return max(12, round(SIZE_FRAC[cfg["size"]] * min(dims) * (1.3 if cfg["style"] == "pop" else 1)))


def wrap(text, font, width, max_words=0):
    """Greedy wrap by measured width and word count. Words when the text has spaces, characters otherwise (CJK)."""
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("L", (1, 1)))
    sep = " " if " " in text else ""
    toks = text.split(" ") if sep else list(text)
    lines, cur, n = [], "", 0
    for t in toks:
        trial = f"{cur}{sep}{t}" if cur else t
        if cur and (d.textlength(trial, font=font) > width or (sep and max_words and n >= max_words)):
            lines.append(cur)
            cur, n = t, 1
        else:
            cur, n = trial, n + 1
    return lines + [cur] if cur else lines


def split_cue(c: Cue, font, width, max_words=0):
    """At most 2 lines per cue: longer text becomes consecutive cues, durations proportional to characters."""
    lines = wrap(c.text.replace("\n", " "), font, width, max_words)
    pages = ["\n".join(lines[i:i + 2]) for i in range(0, len(lines), 2)]
    total, t = sum(len(p) for p in pages) or 1, c.t0
    out = []
    for p in pages:
        t1 = t + (c.t1 - c.t0) * len(p) / total
        out.append(Cue(t, t1, p))
        t = t1
    return out


def word_states(c: Cue, cfg, font, width):
    """Cue with word times -> [(t0, t1, page text, active word index or None)]. Pages are 2 lines (pop: 1 to 3 words);
    with highlight on, one state per word, each lasting until the next word starts."""
    words = [w for w in c.words if w[2]]
    if cfg["style"] == "pop":
        n = cfg["max_words"] if 1 <= cfg["max_words"] <= 3 else 3
        groups = [words[i:i + n] for i in range(0, len(words), n)]
    else:
        groups = [words]
    out = []
    for g in groups:
        lines = wrap(" ".join(w[2] for w in g), font, width, 0 if cfg["style"] == "pop" else cfg["max_words"])
        i = 0
        for k in range(0, len(lines), 2):
            page = lines[k:k + 2]
            cnt = sum(ln.count(" ") + 1 for ln in page)
            pw, i = g[i:i + cnt], i + cnt
            if not pw:
                continue
            text = "\n".join(page)
            end = min(max(pw[-1][1], pw[0][0]), c.t1)
            if not cfg["highlight"]:
                out.append((max(pw[0][0], c.t0), end, text, None))
                continue
            for j, w in enumerate(pw):
                t1 = pw[j + 1][0] if j + 1 < len(pw) else end
                out.append((max(w[0], c.t0), min(max(t1, w[0]), c.t1), text, j))
    return out


def draw_png(text, cfg, dims, path: Path, active=None):
    """One caption frame. `active`: index (over all words of `text`) drawn in highlight_color."""
    from PIL import Image, ImageColor, ImageDraw
    W, H = dims
    size = font_size(cfg, dims)
    font = _font(text, size, cfg["font"])
    style = cfg["style"]
    color, hl = ImageColor.getrgb(cfg["color"]), ImageColor.getrgb(cfg["highlight_color"])
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    stroke = size // 10 if style == "pop" else size // 14 if style == "bold" else 0
    lines = text.split("\n")
    lh, gap = sum(font.getmetrics()), size // 4
    bw, bh = max(d.textlength(ln, font=font) for ln in lines), len(lines) * lh + (len(lines) - 1) * gap
    land = W > H
    if cfg["position"] == "top":
        y = (0.08 if land else 0.15) * H
    elif cfg["position"] == "middle":
        y = (H - bh) / 2
    else:
        y = (0.92 if land else 0.80) * H - bh
    if style == "box":
        pad = size // 3
        d.rounded_rectangle(((W - bw) / 2 - pad, y - pad, (W + bw) / 2 + pad, y + bh + pad), radius=pad, fill=(0, 0, 0, 140))
    n = 0
    for ln in lines:
        x = (W - d.textlength(ln, font=font)) / 2
        for wd in ln.split(" "):
            d.text((x, y), wd, font=font, fill=hl if n == active else color, stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            x += d.textlength(wd + " ", font=font)
            n += 1
        y += lh + gap
    im.save(path)


def build(tl: Timeline, style, dims, cues, workdir: Path, window=None):
    """Draw one full-frame PNG per caption state (a cue page, or with highlight / pop each word change) plus a blank,
    write workdir/cues.txt (ffconcat, `option framerate` under every file line so states land on exact frames).
    `style` is the spec's captions dict (or just a style name). Times shift by -window start.
    Returns Captions(states drawn, list file)."""
    cfg = {**DEFAULT, **(style if isinstance(style, dict) else {"style": style})}
    fps = tl.fps
    w0, w1 = window if window else (0.0, tl.duration)
    w1 = min(w1, tl.duration)
    width = 0.86 * dims[0]
    size = font_size(cfg, dims)
    up = (lambda t: t.upper()) if cfg["uppercase"] else (lambda t: t)  # noqa: E731
    shifted = []
    for c in cues:
        t0, t1 = max(c.t0, w0) - w0, min(c.t1, w1) - w0
        if t1 - t0 < MIN_CUE:
            continue
        font = _font(c.text, size, cfg["font"])
        words = tuple((x0 - w0, x1 - w0, up(t)) for x0, x1, t in c.words if x1 > w0 and x0 < w1)
        if needs_words(cfg) and words:
            shifted += word_states(Cue(t0, t1, up(c.text), words), cfg, font, width)
        else:
            shifted += [(p.t0, p.t1, p.text, None) for p in split_cue(Cue(t0, t1, up(c.text)), font, width, cfg["max_words"])]
    total = round((w1 - w0) * fps)
    entries, cur, drawn = [], 0, []
    for t0, t1, text, active in shifted:
        f0, f1 = max(round(t0 * fps), cur), min(round(t1 * fps), total)
        if f1 <= f0:
            continue
        if f0 > cur:
            entries.append((None, None, f0 - cur))
        entries.append((text, active, f1 - f0))
        drawn.append(Cue(f0 / fps, f1 / fps, text))
        cur = f1
    if not drawn:
        return Captions((), None)
    entries.append((None, None, max(total - cur, 1)))
    workdir.mkdir(parents=True, exist_ok=True)
    names = {}
    ck = json.dumps({k: cfg[k] for k in DEFAULT}, sort_keys=True)

    def png(text, active):
        key = hashlib.sha1(f"{text}|{active}|{ck}|{dims}".encode()).hexdigest()[:16] if text else "blank"
        if key not in names:
            names[key] = f"cap_{key}.png"
            if text:
                draw_png(text, cfg, dims, workdir / names[key], active)
            else:
                from PIL import Image
                Image.new("RGBA", dims, (0, 0, 0, 0)).save(workdir / names[key])
        return names[key]

    lines = ["ffconcat version 1.0"]
    for text, active, n in entries:
        lines += [f"file '{png(text, active)}'", f"option framerate {fps}", f"duration {n / fps:.6f}"]
    lines += [f"file '{png(None, None)}'", f"option framerate {fps}"]  # the demuxer ignores the last duration
    lst = workdir / "cues.txt"
    lst.write_text("\n".join(lines) + "\n")
    return Captions(tuple(drawn), lst, tuple((png(t, a) if t else None, n) for t, a, n in entries))


# ---------- job + routes

def video_items(cid):
    return [i for i in library.get_collection(cid)["items"] if i["kind"] != "image"]


def selected_uuids(cid, spec):
    """Unique uuids of the video clips picked for captions, in order."""
    return list(dict.fromkeys(i["uuid"] for i in video_items(cid) if spec["clips"].get(str(i["item_id"]), {}).get("captions")))


def counts(uuids, cfg):
    want, words = language(), needs_words(cfg)
    ready = pending = quiet = 0
    for u in uuids:
        got = fresh(u, want, words)
        if got is None:
            pending += 1
        elif got[0] == "ok" and got[1]:
            ready += 1
        else:
            quiet += 1
    return {"ready": ready, "pending": pending, "no_speech": quiet}


def _job(cid):
    """Job target: transcribe the selected clips that are pending, progress in the shared jobs dict."""
    import edit
    j = projects.jobs[(cid, "captions")]
    spec, _ = edit.load(cid)
    cfg = spec["captions"]
    want, words = language(), needs_words(cfg)
    todo = [u for u in selected_uuids(cid, spec) if fresh(u, want, words) is None]
    errors = {}
    j.update(stage=f"Transcribing 0 of {len(todo)}", progress=0.0)
    ensure_transcripts(todo, lambda n, t: j.update(stage=f"Transcribing {min(n + 1, t)} of {t}", progress=n / t), errors, words)
    return {"progress": 1.0, "message": f"Transcribed {len(todo) - len(errors)} of {len(todo)} clips",
            "warnings": [f"{media.label_for(u)}: {e}" for u, e in errors.items()]}


def start_transcription(cid):
    """Called by PATCH /edit when captions turn on or clips are picked. Idempotent: a running job is returned, not an error."""
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
    """Master switch, style, and per video clip: picked or not, speech status (speech / no speech / not transcribed), language."""
    import edit  # lazy: edit owns the spec
    spec, _ = edit.load(cid)
    cfg = spec["captions"]
    clips = []
    for i in video_items(cid):
        got = _load(i["uuid"])
        clips.append({"item_id": i["item_id"], "uuid": i["uuid"], "selected": bool(spec["clips"].get(str(i["item_id"]), {}).get("captions")),
                      "speech": speech_status(i["uuid"]), "lang": got[1] if got else None})
    return {"on": cfg["on"], "style": cfg["style"], "config": cfg, "language": language(), "clips": clips,
            **counts(selected_uuids(cid, spec), cfg), "setup": setup_status()}


@router.get("/collections/{cid}/captions/cues")
def captions_cues(cid: int):
    """Output-time cues for the clips with captions on, from transcripts already stored (never transcribes), plus the
    per-clip speech status. Same timing math as the render."""
    import edit
    tl, spec = edit.snapshot(cid)
    picked = {i["item_id"] for i in video_items(cid) if spec["clips"].get(str(i["item_id"]), {}).get("captions")}
    transcripts = {}
    for u in {s.uuid for s in tl.segs if s.item_id in picked}:
        got = cached(u)
        transcripts[u] = got[1] if got and got[0] == "ok" else []
    owner = {(c.t0, c.text): iid for iid in picked for c in map_cues(tl, transcripts, {iid})}  # who each cue belongs to
    cues = [{"t0": c.t0, "t1": c.t1, "text": c.text, "item_id": owner[(c.t0, c.text)]} for c in map_cues(tl, transcripts, picked)]
    clips = []
    for i in video_items(cid):
        got = _load(i["uuid"])
        clips.append({"item_id": i["item_id"], "uuid": i["uuid"], "selected": i["item_id"] in picked,
                      "speech": speech_status(i["uuid"]), "lang": got[1] if got else None})
    return {"cues": cues, "clips": clips}


@router.post("/collections/{cid}/captions/transcribe", status_code=202)
def transcribe_route(cid: int):
    return start_transcription(cid)


@router.get("/collections/{cid}/captions/job")
def captions_job(cid: int):
    return projects.job(cid, "captions")


@router.get("/media/{uuid}/transcript")
def transcript_route(uuid: str):
    got = _load(uuid)
    if got is None:
        raise HTTPException(404, "not transcribed yet")
    return {"uuid": uuid, "model": MODEL, "status": got[0], "lang": got[1], "requested_lang": got[2],
            "lines": [{"t0": l.t0, "t1": l.t1, "text": l.text, "words": [{"t0": w[0], "t1": w[1], "text": w[2]} for w in l.words]} for l in got[3]]}
