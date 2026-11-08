"""Render a project's Timeline to a preview (from proxies) or a final video (from originals).

Per clip one ffmpeg "segment" pass into a cache, then a chunked join and one finish pass (window, captions, delivery
encode). Design: notes/editing-design.md section 3 plus the Fable review at its top.
Needs from other branches (lazy imports, so this module loads without them): edit.snapshot / output_size /
look_filters and captions.ensure_transcripts / map_cues / build / write_srt.
"""
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import core
import library
import media
import projects
import settings
from timeline import Seg

router = APIRouter()
log = logging.getLogger("safelight.render")
RENDER_V = 1  # bump to invalidate every cached segment (e.g. when a look preset changes)
CACHE = core.DATA / "render_cache"  # /final and /preview, LRU by mtime
RENDERS = core.DATA / "renders"  # /<cid>/preview.mp4, .srt, <mode>-sheet.jpg
EXPORTS = projects.EXPORTS  # finals go to EXPORTS/<slug>/
CACHE_CAP = {"final": 20 * 10**9, "preview": 3 * 10**9}
JOIN_CHUNK = {720: 12, 1080: 8, 2160: 3}  # segments per join run; measured 5.4 GB for 20 at 1080x1920 in one graph
MAX_SECONDS = 15 * 60
MAX_CLIPS = 200
FINAL_MBPS = {720: 6, 1080: 12, 2160: 40}  # hevc; h264 is a bit more, covered by the x2 safety below
HDR_TRC = {"arib-std-b67", "smpte2084"}
TRANSPOSE = {0: "", 90: "transpose=2", 180: "hflip,vflip", 270: "transpose=1"}
SEG_CODEC = {"preview": ["-c:v", "h264_videotoolbox", "-b:v", "6M"],
             "final": ["-c:v", "prores_videotoolbox", "-profile:v", "lt"]}
SEG_RUNS = 0  # segment ffmpegs run since start (tests count it)
CANCEL: dict[tuple[int, str], threading.Event] = {}
_gate = threading.Lock()
_active = 0  # renders in flight; cache GC only runs when it is 0


@dataclass(frozen=True)
class Source:
    w: int  # display size (rotation applied)
    h: int
    rot: int  # 0, 90, 180, 270 (ffprobe's display-matrix rotation)
    fps: float
    duration: float
    has_audio: bool
    hdr: bool
    bits: int


def probe_source(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                       capture_output=True, text=True)
    info = json.loads(r.stdout or "{}")
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise RuntimeError(f"{Path(path).name} has no video stream")
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(round(float(sd["rotation"]))) % 360
    if rot not in TRANSPOSE:
        rot = 0
    w, h = int(v["width"]), int(v["height"])
    if rot in (90, 270):
        w, h = h, w
    num, _, den = str(v.get("r_frame_rate", "30/1")).partition("/")
    try:  # r_frame_rate is the nominal rate; avg_frame_rate is junk for iPhone VFR
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    try:
        dur = float(info.get("format", {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    return Source(w, h, rot, fps or 30.0, dur, any(s.get("codec_type") == "audio" for s in streams),
                  v.get("color_transfer") in HDR_TRC, 10 if "10" in str(v.get("pix_fmt", "")) else 8)


# ---------- sources

def still_for(uuid, mode, tmp):
    """A JPEG with orientation baked in (no EXIF guesswork left for ffmpeg). Preview: the indexed preview image.
    Final: the original (HEIC goes through sips first)."""
    if mode == "preview":
        with core.connect() as c:
            row = c.execute("select path from assets where uuid = ?", (uuid,)).fetchone()
        src = Path(row[0]) if row and row[0] else None
        if not src or not src.is_file():
            raise RuntimeError("no preview image on disk")
    else:
        with media.lock_for(uuid):  # same order as media.fetch: lock, then slot
            media.video_slot(uuid)
            try:
                src = projects.originals([uuid], tmp / "orig").get(uuid)
            finally:
                media.release_video_slot()
                media.stage(uuid, None)
        if not src:
            raise RuntimeError("iCloud download failed")
    if src.suffix.lower() in (".heic", ".heif"):
        jpg = tmp / f"{uuid}.sips.jpg"
        subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "4096", str(src), "--out", str(jpg)],
                       check=True, capture_output=True)
        src = jpg
    from PIL import Image, ImageOps
    out = tmp / f"{uuid}.jpg"
    with Image.open(src) as im:
        ImageOps.exif_transpose(im).convert("RGB").save(out, "JPEG", quality=95)
    return out


def source_for(seg, mode, tmp):
    """Before the render slot is taken: lock_for(uuid) then video_slot happen inside media.fetch / still_for."""
    return still_for(seg.uuid, mode, tmp) if seg.kind == "photo" else media.fetch(seg.uuid, mode, tmp / "src")


def src_id(seg, mode):
    """Identity of the source for the cache key, without touching the network. None = not available yet
    (preview proxy not built): the key is computed again after the fetch."""
    def stat_id(p):
        st = p.stat()
        return f"{seg.uuid}:{st.st_mtime_ns}:{st.st_size}"
    try:
        if seg.kind == "photo":
            if mode == "final":
                return seg.uuid  # downloaded originals are immutable (osxphotos runs with --skip-edited)
            with core.connect() as c:
                row = c.execute("select path from assets where uuid = ?", (seg.uuid,)).fetchone()
            p = Path(row[0]) if row and row[0] else None
            return stat_id(p) if p and p.is_file() else None
        if mode == "preview":
            p = media.PROXIES / f"{seg.uuid}.mp4"
            return stat_id(p) if p.is_file() else None
        p = media.local_original(seg.uuid)
        return stat_id(p) if p else seg.uuid
    except HTTPException:  # not indexed: the fetch will say so
        return None


def seg_key(seg, sid, mode, dims, fps):
    d = {"v": RENDER_V, "mode": mode, "src": sid, "in": seg.src_in, "out": seg.src_out, "speed": seg.speed,
         "volume": seg.volume, "adjust": seg.adjust, "look": seg.look, "reframe": seg.reframe, "dims": dims,
         "fps": fps, "kind": seg.kind}
    return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:20]


# ---------- ffmpeg commands (pure apart from writing the filter script)

def _n(x, places=4):
    return f"{x:.{places}f}".rstrip("0").rstrip(".") or "0"


def _even(x):
    x = int(round(x))
    return x - x % 2


def crop_box(sw, sh, W, H):
    """Largest even crop of an sw x sh frame with the aspect of W x H."""
    if sw * H >= sh * W:
        return min(sw, _even(sh * W / H)), sh - sh % 2
    return sw - sw % 2, min(sh, _even(sw * H / W))


def atempo(speed):
    out, s = [], speed
    while s > 2.0:
        out.append("atempo=2.0"); s /= 2.0
    while s < 0.5:
        out.append("atempo=0.5"); s /= 0.5
    if abs(s - 1.0) > 1e-9:
        out.append(f"atempo={_n(s, 6)}")
    return out


def seg_graph(seg, src, dims, fps, mode):
    import edit
    W, H = dims
    L = seg.frames / fps
    S = seg.speed
    v = ["[0:v:0]"]
    chain = []
    if seg.kind == "video" and src.hdr:  # VideoToolbox frames: autorotate is skipped, so rotate in software
        chain += ["scale_vt=color_matrix=bt709:color_primaries=bt709:color_transfer=bt709", "hwdownload",
                  "format=p010le" if src.bits > 8 else "format=nv12", "format=yuv420p"]
        if TRANSPOSE[src.rot]:
            chain.append(TRANSPOSE[src.rot])
    chain.append(f"setpts=(PTS-STARTPTS)/{_n(S, 6)}" if S != 1 else "setpts=PTS-STARTPTS")
    cw, ch = crop_box(src.w, src.h, W, H)
    if (cw, ch) != (src.w, src.h):
        mode_, a, b = seg.reframe
        a, b = (0.5, 0.5) if mode_ == "center" else (a, b)
        if mode_ == "pan":
            e = f"({_n(a)}+({_n(b)}-{_n(a)})*min(max(t/{_n(L, 6)},0),1))"
        else:
            e = _n(a)
        if cw < src.w:
            chain.append(f"crop={cw}:{ch}:x='(iw-{cw})*{e}':y=0")
        else:
            chain.append(f"crop={cw}:{ch}:x=0:y='(ih-{ch})*{e}'")
    chain.append(f"scale={W}:{H}:flags={'bicubic' if mode == 'preview' else 'lanczos'}")
    look = edit.look_filters(seg.look)
    if look:
        chain.append(look)
    ex, co, sa, wa = seg.adjust
    if (ex, co, sa) != (0, 1, 1):
        chain.append(f"eq=brightness={_n(0.2 * ex)}:contrast={_n(co)}:saturation={_n(sa)}")
    if wa:
        k = wa
        chain.append(f"colorbalance=rs={_n(0.15 * k)}:bs={_n(-0.15 * k)}:rm={_n(0.1 * k)}:bm={_n(-0.1 * k)}")
    chain += ["tpad=stop_mode=clone:stop_duration=1", f"fps={fps}", "format=yuv420p", "setsar=1"]
    g = v[0] + ",".join(chain) + "[v]"
    if seg.kind == "video" and src.has_audio:
        a = atempo(S) + [f"volume={_n(seg.volume)}", "aresample=48000", "aformat=channel_layouts=stereo",
                         "asetpts=PTS-STARTPTS", f"apad=whole_dur={_n(L, 6)}"]
        g += ";\n[0:a:0]" + ",".join(a) + "[a]"
    else:
        g += f";\nanullsrc=r=48000:cl=stereo:d={_n(L, 6)}[a]"
    return g


def seg_command(seg, src, src_path, out, mode, dims, fps, workdir):
    L = seg.frames / fps
    graph = Path(workdir) / f"seg_{out.name}.txt"
    graph.write_text(seg_graph(seg, src, dims, fps, mode))
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    if seg.kind == "photo":
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", _n(L, 6), "-i", str(src_path)]
    else:
        cmd += (["-noautorotate", "-display_rotation", "0", "-hwaccel", "videotoolbox",
                 "-hwaccel_output_format", "videotoolbox_vld"] if src.hdr else ["-hwaccel", "videotoolbox"])
        ss = seg.src_in
        if src.duration > 0:
            ss = min(ss, max(0.0, src.duration - 0.05))
        cmd += ["-ss", _n(ss, 6), "-t", _n(max(seg.src_out - seg.src_in, 0.04), 6), "-i", str(src_path)]
    return cmd + ["-/filter_complex", str(graph), "-map", "[v]", "-map", "[a]", "-frames:v", str(seg.frames),
                  "-t", _n(L, 6), *SEG_CODEC[mode], "-c:a", "pcm_s16le", "-f", "mov", str(out)]


def _graph(n, frames, trans, fps, window=None, has_cues=False):
    """Join n inputs (frames[i] long, trans[i] = (xfade name, frames) or None between i and i+1), then optionally
    trim to window (start, end) in seconds and overlay the caption input (index n). Ends in [vout][aout]."""
    p = [f"[{i}:v]fps={fps}[v{i}]" for i in range(n)]
    accv, acca, acc = "v0", "0:a", frames[0]
    for i in range(1, n):
        t = trans[i - 1]
        if t:
            name, tf = t
            p.append(f"[{accv}][v{i}]xfade=transition={name}:duration={_n(tf / fps, 6)}:offset={_n((acc - tf) / fps, 6)}[x{i}]")
            p.append(f"[{acca}][{i}:a]acrossfade=d={_n(tf / fps, 6)}[ja{i}]")
            accv, acca, acc = f"x{i}", f"ja{i}", acc + frames[i] - tf
        else:
            p.append(f"[{accv}][{acca}][v{i}][{i}:a]concat=n=2:v=1:a=1[cv{i}][ja{i}]")
            p.append(f"[cv{i}]fps={fps}[x{i}]")  # concat resets the timebase; xfade needs it back
            accv, acca, acc = f"x{i}", f"ja{i}", acc + frames[i]
    if window:
        s, e = window
        p.append(f"[{accv}]trim=start={_n(s, 6)}:end={_n(e, 6)},setpts=PTS-STARTPTS[w]")
        p.append(f"[{acca}]atrim=start={_n(s, 6)}:end={_n(e, 6)},asetpts=PTS-STARTPTS[aout]")
        accv, acca = "w", None
    if has_cues:  # never shortest=1: it would end the video when the cue list ends
        p.append(f"[{n}:v]fps={fps},format=rgba[cap]")
        p.append(f"[{accv}][cap]overlay=0:0:eof_action=pass[o]")
        accv = "o"
    p.append(f"[{accv}]setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv,format=yuv420p[vout]")
    if acca:
        p.append(f"[{acca}]anull[aout]")
    return ";\n".join(p)


def _run_cmd(files, graph, out, workdir, codec, cue_list=None):
    gf = Path(workdir) / f"join_{out.name}.txt"
    gf.write_text(graph)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    for f in files:
        cmd += ["-i", str(f)]
    if cue_list:
        cmd += ["-f", "concat", "-safe", "0", "-i", str(cue_list)]
    return cmd + ["-/filter_complex", str(gf), "-map", "[vout]", "-map", "[aout]", *codec, str(out)]


def join_command(seg_files, frames, trans, fps, out, workdir, codec):
    """One chunk: intermediate in the segment codec. Pure cuts at its edges are the caller's job."""
    return _run_cmd(seg_files, _graph(len(seg_files), frames, trans, fps), out, workdir, codec)


def finish_command(files, frames, trans, window, cue_list, fps, out, workdir, codec):
    """Last pass: join (when several files are left), window trim, caption overlay, colour tags, delivery encode."""
    g = _graph(len(files), frames, trans, fps, window, bool(cue_list))
    return _run_cmd(files, g, out, workdir, codec, cue_list)


def chunks(trans, K):
    """Index ranges (a, b) of at most K segments, boundaries only where trans is None (a cut)."""
    out, a = [], 0
    for i in range(1, len(trans) + 1):
        if i - a >= K and trans[i - 1] is None:
            out.append((a, i)); a = i
    out.append((a, len(trans) + 1))
    return out


def levels(frames, trans, K):
    """Join levels: yields (chunks, frames, trans) until one chunk covers everything."""
    while True:
        cs = chunks(trans, K)
        yield cs, frames, trans
        if len(cs) == 1:
            return
        frames = [sum(frames[a:b]) - sum(t[1] for t in trans[a:b - 1] if t) for a, b in cs]
        trans = [None] * (len(cs) - 1)


def delivery(mode, codec, size):
    if mode == "preview":
        return ["-c:v", "h264_videotoolbox", "-b:v", "3M", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    if codec == "h264":
        v = ["-c:v", "h264_videotoolbox", "-b:v", {720: "8M", 1080: "16M", 2160: "50M"}.get(size, "16M")]
    else:
        v = ["-c:v", "hevc_videotoolbox", "-b:v", f"{FINAL_MBPS.get(size, 12)}M", "-tag:v", "hvc1"]
    return v + ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart"]


# ---------- running ffmpeg

def run_ffmpeg(cmd, expect_seconds, on_progress, cancel, timeout, cwd=None):
    if cancel.is_set():
        raise RuntimeError("Cancelled")
    full = ["nice", "-n", "10", cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=err, text=True, cwd=cwd)
        killer = threading.Timer(timeout, p.kill)
        killer.start()
        try:
            for line in p.stdout:
                if cancel.is_set():
                    p.kill()
                    break
                k, _, v = line.strip().partition("=")
                if k == "out_time_us" and v.isdigit() and expect_seconds > 0:
                    on_progress(min(1.0, int(v) / 1e6 / expect_seconds))
            p.wait()
        finally:
            killer.cancel()
            if p.poll() is None:
                p.kill(); p.wait()
            p.stdout.close()
        if cancel.is_set():
            raise RuntimeError("Cancelled")
        if p.returncode != 0:
            err.seek(0)
            raise RuntimeError("ffmpeg failed" + (" (timed out)" if p.returncode == -9 else "") + ": "
                               + err.read().decode(errors="replace")[-2000:])


class Progress:
    """Writes progress / stage / eta into the live job dict (projects.start_job's), never goes backwards."""

    def __init__(self, state, total, slot_key, label):
        self.state, self.total, self.done, self.t0 = state, max(total, 1e-6), 0.0, time.time()
        self.slot_key, self.label = slot_key, label

    def stage(self, text):
        self.state["stage"] = text
        media.stage(self.slot_key, text, self.label)

    def cb(self, weight):
        def f(frac):
            p = min(1.0, (self.done + frac * weight) / self.total)
            if p >= self.state.get("progress", 0):
                self.state["progress"] = round(p, 4)
                if p >= 0.1:
                    self.state["eta_seconds"] = round((time.time() - self.t0) * (1 - p) / p)
        return f

    def add(self, weight):
        self.done += weight
        self.cb(0)(0)


def _free_check(path, need, what):
    p = Path(path)
    while not p.exists():
        p = p.parent
    free = shutil.disk_usage(p).free
    if free < need:
        raise RuntimeError(f"Not enough disk space for {what}: need about {need / 1e9:.1f} GB, "
                           f"{free / 1e9:.1f} GB free on that disk.")


def _gc_cache():
    with _gate:
        if _active:
            return
    for mode, cap in CACHE_CAP.items():
        files = sorted((f for f in (CACHE / mode).glob("*.mov") if f.is_file()), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        for f in files:
            if total <= cap:
                break
            total -= f.stat().st_size
            f.unlink(missing_ok=True)


def _stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _timeout(seconds):
    return max(120, 20 * seconds + 60)


# ---------- the job

def render(cid, mode, start=None, end=None, codec="hevc"):
    """Job target for projects.start_job(cid, mode, render.render, start, end, codec). Returns the result dict."""
    global SEG_RUNS, _active
    import captions
    import edit
    if mode not in ("preview", "final"):
        raise ValueError(f"mode must be preview or final, not {mode!r}")
    tl, spec = edit.snapshot(cid)
    name = library.get_collection(cid)["name"]
    if not tl.segs:
        raise RuntimeError("project has no clips")
    if len(tl.segs) > MAX_CLIPS:
        raise RuntimeError(f"The edit has {len(tl.segs)} clips; renders are limited to {MAX_CLIPS}.")
    if tl.duration > MAX_SECONDS:
        raise RuntimeError(f"The edit is {tl.duration / 60:.1f} minutes; renders are limited to 15 minutes.")
    fps = tl.fps
    dims = edit.output_size(tl.aspect, tl.size, mode == "preview")
    win = None  # (start_frame, end_frame) on the output timeline
    if start is not None or end is not None:
        if mode != "preview":
            raise RuntimeError("only previews can render a part of the edit")
        sf = round((start or 0) * fps)
        ef = min(tl.total_frames, round(end * fps) if end else tl.total_frames)
        if not 0 <= sf < ef:
            raise RuntimeError("the preview range is outside the edit")
        win = (sf, ef) if (sf, ef) != (0, tl.total_frames) else None
    jkey = (cid, mode)
    cancel = CANCEL[jkey] = threading.Event()
    state = projects.jobs.setdefault(jkey, {})
    tmp = Path(tempfile.mkdtemp(prefix="cs-render-"))
    work = tmp / "work"
    work.mkdir()
    cdir = CACHE / mode
    rdir = RENDERS / str(cid)
    parts = []  # .part files outside tmp, removed on any failure
    with _gate:
        _active += 1
    try:
        # 1. transcripts (own locks and slots; must not run while holding the render slot)
        warnings = list(tl.warnings)
        transcripts = {}
        on = spec["captions"]["on"]
        if on:
            uuids = list(dict.fromkeys(s.uuid for s in tl.segs if s.kind == "video"))
            state["stage"] = f"Transcribing {len(uuids)} clips"
            terrors = {}
            transcripts = captions.ensure_transcripts(uuids, None, terrors)
            if terrors:
                warnings.append(f"{len(terrors)} clips could not be transcribed and have no captions")
        if cancel.is_set():
            raise RuntimeError("Cancelled")
        # 2. cache lookup, disk check, then fetch what is missing (before the render slot)
        sel = [s for s in tl.segs if not win or (s.start_frame < win[1] and s.start_frame + s.frames > win[0])]
        cdir.mkdir(parents=True, exist_ok=True)
        keys = {}

        def keyed(s):
            sid = src_id(s, mode)
            return seg_key(s, sid, mode, dims, fps) if sid else None
        miss = {}  # key-or-id -> seg
        for s in sel:
            k = keys[s.item_id] = keyed(s)
            if not k or not (cdir / f"{k}.mov").is_file():
                miss.setdefault(k or f"id{s.item_id}", s)
        miss_s = sum(s.frames for s in miss.values()) / fps
        out_s = ((win[1] - win[0]) if win else tl.total_frames) / fps
        if mode == "final":
            folder = EXPORTS / projects.slug(name)
            _free_check(CACHE, miss_s * 10e6 + 2e9, "the render cache")
            _free_check(folder, out_s * FINAL_MBPS.get(tl.size, 12) * 1e6 / 8 * 2 + 2e9, "the finished video")
        srcs, errors = {}, []
        for s in {s.uuid: s for s in miss.values()}.values():
            if cancel.is_set():
                raise RuntimeError("Cancelled")
            state["stage"] = "Fetching originals" if mode == "final" else "Preparing playable copies"
            try:
                p = source_for(s, mode, tmp)
                srcs[s.uuid] = (p, probe_source(p))
            except (RuntimeError, HTTPException, OSError, subprocess.SubprocessError) as e:
                msg = e.detail if isinstance(e, HTTPException) else str(e)
                errors.append(f"{media.label_for(s.uuid)} ({msg[:120]})")
        if errors:
            raise RuntimeError(f"{len(errors)} clips could not be fetched: {'; '.join(errors)}. "
                               "Finished segments are kept; try again.")
        todo = {}
        for s in sel:
            k = keys[s.item_id] = keys[s.item_id] or keyed(s)
            if not (cdir / f"{k}.mov").is_file():
                todo.setdefault(k, s)
        hits = len(sel) - len(todo)
        for s in sel:
            f = cdir / f"{keys[s.item_id]}.mov"
            if f.is_file():
                os.utime(f)  # LRU
        # plan the joins and the progress weights
        base = sel[0].start_frame
        files = [cdir / f"{keys[s.item_id]}.mov" for s in sel]
        frames = [s.frames for s in sel]
        trans = [s.trans_after for s in sel[:-1]]
        K = JOIN_CHUNK.get(tl.size, 8)
        plan = list(levels(frames, trans, K))
        join_s = sum(sum(fr[a:b]) - sum(t[1] for t in tr[a:b - 1] if t) for cs, fr, tr in plan for a, b in cs if b - a > 1) / fps
        prog = Progress(state, miss_s + join_s + 0.0001, f"render:{cid}:{mode}", f"Render: {name} ({mode})")
        # 3. the render slot covers ffmpeg only
        if media.RUNNING >= settings.get().max_agents:
            state["note"] = "Waiting for a free slot"
        media.video_slot(prog.slot_key, prog.label)
        state.pop("note", None)
        try:
            for n, (k, s) in enumerate(todo.items(), 1):
                if cancel.is_set():
                    raise RuntimeError("Cancelled")
                prog.stage(f"Rendering clip {n} of {len(todo)}")
                path, src = srcs[s.uuid]
                part = cdir / f"{k}.part.mov"
                parts.append(part)
                SEG_RUNS += 1
                L = s.frames / fps
                run_ffmpeg(seg_command(s, src, path, part, mode, dims, fps, work), L, prog.cb(L), cancel, _timeout(L), work)
                part.replace(cdir / f"{k}.mov")
                prog.add(L)
            # 4. captions
            cap_file, cues, all_cues = None, [], []
            if on:
                prog.stage("Captions")
                cues = all_cues = captions.map_cues(tl, transcripts)
                wsec = (win[0] / fps, win[1] / fps) if win else None
                cap = captions.build(tl, spec["captions"]["style"], dims, cues, work, wsec)
                cap_file = cap.list_file
            # 5. join levels, then the finish pass
            if mode == "preview":
                rdir.mkdir(parents=True, exist_ok=True)
                target, folder = rdir / "preview.mp4", None
                fpart = rdir / "preview.part.mp4"
            else:
                folder.mkdir(parents=True, exist_ok=True)
                stem = f"{projects.slug(name)}-{_stamp()}"
                i = 1
                while any((folder / f"{stem}{'' if i == 1 else f'-{i}'}{x}").exists() for x in (".mp4", ".part.mp4")):
                    i += 1
                stem += "" if i == 1 else f"-{i}"
                target, fpart = folder / f"{stem}.mp4", folder / f"{stem}.part.mp4"
            parts.append(fpart)
            window = ((win[0] - base) / fps, (win[1] - base) / fps) if win else None
            mid = SEG_CODEC[mode] + ["-c:a", "pcm_s16le"]
            for li, (cs, fr, tr) in enumerate(plan):
                last = len(cs) == 1
                if last:
                    prog.stage("Joining" if not cap_file else "Joining and adding captions")
                    fl = (sum(fr) - sum(t[1] for t in tr if t)) / fps
                    cmd = finish_command(files, fr, tr, window, cap_file, fps, fpart, work, delivery(mode, codec, tl.size))
                    run_ffmpeg(cmd, (window[1] - window[0]) if window else fl, prog.cb(fl), cancel, _timeout(fl), work)
                    prog.add(fl)
                    break
                prog.stage(f"Joining (pass {li + 1})")
                nxt = []
                for ci, (a, b) in enumerate(cs):
                    if b - a == 1:
                        nxt.append(files[a]); continue
                    o = work / f"j{li}_{ci}.mov"
                    fl = (sum(fr[a:b]) - sum(t[1] for t in tr[a:b - 1] if t)) / fps
                    run_ffmpeg(join_command(files[a:b], fr[a:b], tr[a:b - 1], fps, o, work, mid), fl, prog.cb(fl),
                               cancel, _timeout(fl), work)
                    prog.add(fl)
                    nxt.append(o)
                for a, b in cs:  # consumed intermediates only, never the cache
                    for f in files[a:b] if b - a > 1 else ():
                        if f.parent == work:
                            f.unlink(missing_ok=True)
                files = nxt
        finally:
            media.release_video_slot()
            media.stage(prog.slot_key, None)
        # 6. publish
        if mode == "preview":
            fpart.replace(target)
        else:
            for _ in range(100):  # link fails if the name exists: a finished video is never replaced
                try:
                    os.link(fpart, target)
                    break
                except FileExistsError:
                    target = target.with_name(f"{target.stem}-{_stamp()}{'x'}{_}.mp4")
            else:
                raise RuntimeError("could not find a free file name")
            fpart.unlink()
        assert target.resolve().parent == (rdir if mode == "preview" else folder).resolve()
        srt = target.with_suffix(".srt")
        if all_cues:  # the unsplit cues; write_srt shifts them by the window start
            captions.write_srt(all_cues, srt, win[0] / fps if win else 0.0, (win[1] - win[0]) / fps if win else None)
        else:
            srt = None
            if mode == "preview":
                target.with_suffix(".srt").unlink(missing_ok=True)
        state["stage"] = "Contact sheet"  # not prog.stage: the activity entry was cleared with the slot
        sheet = _sheet(target, sel, win, fps, rdir / f"{mode}-sheet.jpg")
        prog.state["progress"] = 1.0
        _gc_cache()
        return {"path": str(target), "folder": str(target.parent), "seconds": round(out_s, 2),
                "srt": str(srt) if srt else None, "sheet": str(sheet) if sheet else None, "edit_hash": tl.edit_hash,
                "rendered": len(todo), "cached": hits, "warnings": warnings,
                "message": f"{out_s:.0f}s, {len(todo)} clips rendered, {hits} reused"}
    finally:
        for p in parts:
            p.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        with _gate:
            _active -= 1
        if CANCEL.get(jkey) is cancel:
            del CANCEL[jkey]


def _sheet(video, segs, win, fps, out):
    """Middle frame of each clip (at most 24, evenly picked) on a contact sheet. Best effort: None on failure."""
    import cv2
    try:
        pick = [segs[int(i * len(segs) / 24)] for i in range(24)] if len(segs) > 24 else segs
        off = win[0] if win else 0
        cap = cv2.VideoCapture(str(video))
        frames, spans = [], []
        for s in pick:
            t0 = max(0.0, (s.start_frame - off) / fps)
            t1 = (s.start_frame + s.frames - off) / fps
            frames.append(media.frame_at(cap, (t0 + t1) / 2))
            spans.append((t0, t1))
        cap.release()
        return media.contact_sheet(frames, spans, out)
    except Exception:
        log.exception("contact sheet failed for %s", video)
        return None


# ---------- routes

class RenderBody(BaseModel):
    mode: Literal["preview", "final"]
    start: float | None = None
    end: float | None = None
    codec: Literal["hevc", "h264"] = "hevc"


Mode = Literal["preview", "final"]


@router.post("/collections/{cid}/render", status_code=202)
def render_route(cid: int, body: RenderBody):
    with library.conn() as c:
        col = library.require_collection(c, cid)
    if col["kind"] != "project":
        raise HTTPException(422, "only projects can be rendered")
    if (body.start or 0) < 0 or (body.end or 0) < 0 or (body.end and body.end <= (body.start or 0)):
        raise HTTPException(422, "start must be 0 or more and end after start")
    if body.mode == "final" and (body.start is not None or body.end is not None):
        raise HTTPException(422, "only previews can render part of the edit")
    return projects.start_job(cid, body.mode, render, body.mode, body.start, body.end, body.codec)


@router.get("/collections/{cid}/render/{mode}")
def render_status(cid: int, mode: Mode):
    return projects.job(cid, mode)


@router.post("/collections/{cid}/render/{mode}/cancel", status_code=204)
def render_cancel(cid: int, mode: Mode):
    ev = CANCEL.get((cid, mode))
    if ev:
        ev.set()


def _recorded(cid, mode, field):
    """Only the exact path the finished job recorded, and only inside our own output folders."""
    j = projects.job(cid, mode)
    if j.get("status") != "done" or not j.get(field):
        raise HTTPException(404, "nothing rendered yet")
    p = Path(j[field]).resolve()
    if not p.is_file() or not any(p.is_relative_to(root.resolve()) for root in (RENDERS, EXPORTS)):
        raise HTTPException(404, "file is gone")
    return p


@router.get("/collections/{cid}/render/{mode}/file")
def render_file(cid: int, mode: Mode):
    return FileResponse(_recorded(cid, mode, "path"), media_type="video/mp4")  # Range supported


@router.get("/collections/{cid}/render/{mode}/sheet")
def render_sheet(cid: int, mode: Mode):
    return FileResponse(_recorded(cid, mode, "sheet"), media_type="image/jpeg")


@router.post("/collections/{cid}/render/{mode}/reveal", status_code=204)
def render_reveal(cid: int, mode: Mode, play: int = 0):
    p = _recorded(cid, mode, "path")
    subprocess.run(["open", str(p)] if play else ["open", "-R", str(p)], check=False)
