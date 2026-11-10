"""Render a project's Timeline to a preview (from proxies) or a final video (from originals).

Per clip one ffmpeg "segment" pass into a cache, then a chunked join and one finish pass (window, captions, delivery
encode). Design: notes/editing-design.md section 3 plus the Fable review at its top.
Needs from other branches (lazy imports, so this module loads without them): edit.snapshot / output_size /
look_filters and captions.ensure_transcripts / map_cues / build / write_srt.
"""
import hashlib
import json
import logging
import math
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
from pydantic import BaseModel, ConfigDict

import color
import core
import library
import media
import projects
import reframe
import seamfx
import versions
import settings
from timeline import Seg

router = APIRouter()
log = logging.getLogger("kioku.render")
DECLICK = 0.008  # seconds of audio fade either side of a hard cut
RENDER_V = 3  # bump to invalidate every cached segment (e.g. when a look preset changes)
CACHE = core.DATA / "render_cache"  # /final and /preview, LRU by mtime
RENDERS = core.DATA / "renders"  # /<cid>/preview.mp4, .srt, <mode>-sheet.jpg
EXPORTS = projects.EXPORTS  # finals go to EXPORTS/<slug>/
CACHE_CAP = {"final": 20 * 10**9, "preview": 3 * 10**9}
ORIG_CACHE = core.DATA / "originals"  # iCloud originals kept between renders, so a changed clip is not downloaded again
ORIG_CAP = 15 * 10**9  # LRU by mtime, swept with the render cache
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

def still_for(uuid, mode, tmp, orig=None):
    """A JPEG with orientation baked in (no EXIF guesswork left for ffmpeg). Preview: the indexed preview image.
    Final: the original, already downloaded by fetch_originals when `orig` is given (HEIC goes through sips first)."""
    if mode == "preview":
        try:
            src = media.preview_source(uuid)
        except media.NoPreview:
            raise RuntimeError("no preview image on disk") from None
    elif orig:
        src = orig
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


def cached_original(uuid):
    """A previously downloaded original, or None. Touches it so the LRU sweep keeps what renders use."""
    for f in ORIG_CACHE.glob(f"{uuid}.*"):
        if f.is_file() and not f.name.endswith(".part"):
            try:
                os.utime(f)
            except OSError:
                pass
            return f
    return None


def keep_original(p):
    """Move a fresh download into ORIG_CACHE (via .part + rename, so a crash never leaves a half file that looks
    complete). Returns the cached path, or p itself if the move fails: the render still has its file."""
    try:
        ORIG_CACHE.mkdir(parents=True, exist_ok=True)
        dst = ORIG_CACHE / p.name
        part = dst.with_name(dst.name + ".part")
        shutil.move(str(p), part)
        os.replace(part, dst)
        return dst
    except OSError as e:
        log.warning("could not keep %s in the originals cache: %s", p.name, e)
        return p if p.is_file() else None


def fetch_originals(segs, tmp, slot_key, label, report):
    """Final renders: every original that is not already on disk, in ONE projects.originals call (one osxphotos
    launch). Returns ({uuid: path}, {uuid: readable error}). Lock order as media.fetch: lock_for(uuid) for each
    (sorted, so two batches cannot deadlock), then one video_slot. Never called while holding the render slot.
    report(done, total) is called about once a second while downloading."""
    got, errs, need = {}, {}, []
    for s in segs:
        if s.kind == "video":  # photos always go through originals (the indexed path is only a preview image)
            try:
                p = media.local_original(s.uuid)
            except HTTPException as e:
                errs[s.uuid] = str(e.detail)
                continue
            if p:
                got[s.uuid] = p
                continue
        if (hit := cached_original(s.uuid)):  # downloaded by an earlier render
            got[s.uuid] = hit
            continue
        need.append(s.uuid)
    need = list(dict.fromkeys(need))
    if not need:
        return got, errs
    limit = settings.get().download_seconds * len(need)  # what projects.originals allows
    dest = tmp / "orig"
    locks = [media.lock_for(u) for u in sorted(need)]
    for lk in locks:
        lk.acquire()
    stop = threading.Event()

    def poll():  # originals() blocks; the files appearing in dest are the progress
        while not stop.wait(1):
            try:
                report(len({f.stem for f in dest.iterdir()} & set(need)), len(need))
            except OSError:
                pass
    poller = threading.Thread(target=poll, daemon=True)
    try:
        media.video_slot(slot_key, label)
        try:
            report(0, len(need))
            poller.start()
            res, why = {}, "iCloud download failed"
            try:
                res = projects.originals(need, dest)
            except subprocess.TimeoutExpired:
                why = f"iCloud download timed out after {limit} s"
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                why = str(e)[:200]
                log.warning("batch download failed: %s", e)
        finally:
            media.release_video_slot()
    finally:
        stop.set()
        if poller.is_alive():
            poller.join()
        for lk in locks:
            lk.release()
    for u in need:
        # only move files the download put in dest (never a file that lives somewhere else)
        if res.get(u) and (kept := keep_original(res[u]) if Path(res[u]).parent == dest else res[u]):
            got[u] = kept
        else:
            errs[u] = why
    report(len(need) - sum(u in errs for u in need), len(need))
    return got, errs


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
         "fps": fps, "kind": seg.kind, "frames": seg.frames}
    if seg.motion or seg.flash or seg.shake or seg.ramp:  # absent when unused, so older cached segments keep their keys
        d.update(motion=seg.motion, flash=seg.flash, shake=seg.shake, ramp=seg.ramp, beats=seg.beat_frames)
    if seg.motion:
        d["ease"] = 1  # eased motion and punch: re-renders only moving clips (no RENDER_V bump for the rest)
    if seg.track:  # the subject path and the algorithm version decide the crop, so both are in the key
        d.update(track=seg.track, reframe_v=reframe.VERSION)
    if seg.match:  # colour match on and this clip moved
        d["match"] = seg.match
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


def ramp_setpts(ramp, S, L):
    """setpts expression (after PTS-STARTPTS) mapping source time T to output time for a ramp preset at base speed S over
    an output length L: piecewise linear, so speed is constant inside each phase."""
    import edit
    so = oo = 0.0
    parts = []  # (source end of phase, output start, source start, rate)
    for frac, k in edit.RAMPS[ramp]:
        r = S * k
        parts.append((so + frac * L * r, oo, so, r))
        so += frac * L * r
        oo += frac * L
    expr = f"{_n(parts[-1][1], 6)}+(T-{_n(parts[-1][2], 6)})/{_n(parts[-1][3], 6)}"
    for end, o0, s0, r in reversed(parts[:-1]):
        expr = f"if(lt(T,{_n(end, 6)}),{_n(o0, 6)}+(T-{_n(s0, 6)})/{_n(r, 6)},{expr})"
    return f"({expr})/TB"


def punch_env(pts, frames):
    """Expression in output frames: on each beat a snap in (about 2 frames) that eases back out by the next beat, peak 1."""
    gap = min((b - a for a, b in zip(pts, pts[1:])), default=frames)
    rise, tau = 1.2, max(3.0, gap / 2)
    d = rise * math.log(1 + tau / rise)  # where (1-exp(-d/rise))*exp(-d/tau) peaks
    k = 1 / ((1 - math.exp(-d / rise)) * math.exp(-d / tau))
    terms = [f"gte(in,{p})*(1-exp(-(in-{p})/{_n(rise)}))*exp(-(in-{p})/{_n(tau)})" for p in pts]
    return f"min({_n(k)}*({'+'.join(terms)}),1)"


def motion_filter(seg, fps):
    """perspective filter (per frame, subpixel, so slow zooms do not jitter like zoompan) for motion, punch and shake.
    `in` is the output frame index: this runs after fps. '' when the clip does not move.
    Push, pull and drift ease in and out (smoothstep); under 1 s they stay linear, so a short clip reads as one move
    through the cut instead of a bump."""
    import edit
    name, amt = seg.motion or ("none", 0.0)
    A = edit.MAX_ZOOM * max(0.0, min(amt, 1.0))
    N = max(seg.frames - 1, 1)
    u = f"min(in/{N},1)"
    if seg.frames >= fps:
        u = f"({u}*{u}*(3-2*{u}))"
    z, dx = "1", "0"
    if name == "push":
        z = f"(1+{_n(A)}*{u})"
    elif name == "pull":
        z = f"(1+{_n(A)}*(1-{u}))"
    elif name in ("drift-left", "drift-right"):
        zc = 1 + max(A, 0.05)
        m = (1 - 1 / zc) / 2 * 0.9
        z = _n(zc)
        dx = f"({'' if name == 'drift-left' else '-'}{_n(m)}*(1-2*{u}))"
    elif name == "punch":
        pts = seg.beat_frames[:24] or (seg.frames // 2,)
        z = f"(1+{_n(A)}*{punch_env(pts, seg.frames)})"
    dy = "0"
    if seg.shake and seg.beat_frames:
        z = f"({z}*1.06)"  # room to move without showing edges: 0.012 offset needs 1/(1-0.024) zoom at least
        terms = [f"gte(in,{p})*exp(-(in-{p})/2.5)" for p in seg.beat_frames[:24]]
        env = "(" + "+".join(terms) + ")"
        dx = f"({dx}+0.012*{env}*sin(in*2.1))"
        dy = f"(0.012*{env}*cos(in*2.7))"
    if z == "1" and dx == "0":
        return ""
    hw, hh = f"(W/(2*{z}))", f"(H/(2*{z}))"
    cx, cy = f"(W/2+{dx}*W)", f"(H/2+{dy}*H)"
    x0, x1, y0, y1 = f"{cx}-{hw}", f"{cx}+{hw}", f"{cy}-{hh}", f"{cy}+{hh}"
    return (f"perspective=x0='{x0}':y0='{y0}':x1='{x1}':y1='{y0}':x2='{x0}':y2='{y1}':x3='{x1}':y3='{y1}'"
            ":interpolation=cubic:eval=frame")


def seg_graph(seg, src, dims, fps, mode):
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
    if seg.ramp:
        chain += ["setpts=PTS-STARTPTS", f"setpts='{ramp_setpts(seg.ramp, S, L)}'"]
    else:
        chain.append(f"setpts=(PTS-STARTPTS)/{_n(S, 6)}" if S != 1 else "setpts=PTS-STARTPTS")
    # hold the last frame when the source is shorter than the clip (beat sync), then fix the frame rate here so the motion
    # expressions below can count output frames
    import edit
    rate = S * edit.ramp_factor(seg.ramp)
    hold = L - (seg.src_out - seg.src_in) / rate if seg.kind == "video" else 0.0
    chain += [f"tpad=stop_mode=clone:stop_duration={_n(max(1.0, hold + 0.5), 3)}", f"fps={fps}"]
    cw, ch = crop_box(src.w, src.h, W, H)
    if (cw, ch) != (src.w, src.h):
        mode_, a, b = seg.reframe
        a, b = (0.5, 0.5) if mode_ == "center" else (a, b)
        keys = None
        if mode_ == "auto":
            if seg.track:
                horiz = cw < src.w
                f = (cw / src.w) if horiz else (ch / src.h)
                keys = reframe.path([p[0 if horiz else 1] for p in seg.track], f, L)
            else:  # no track (should not reach here; edit.compute_timeline falls back): centre
                keys = [(0.0, 0.5)]
        if keys:
            e = _n(keys[0][1])
            for (t0, p0), (t1, p1) in zip(keys, keys[1:]):  # piecewise linear, flat sum (no nesting)
                e += f"+{_n(p1 - p0)}*min(max((t-{_n(t0, 3)})/{_n(t1 - t0, 3)},0),1)"
            e = f"({e})"
        elif mode_ == "pan":
            e = f"({_n(a)}+({_n(b)}-{_n(a)})*min(max(t/{_n(L, 6)},0),1))"
        else:
            e = _n(a)
        if cw < src.w:
            chain.append(f"crop={cw}:{ch}:x='(iw-{cw})*{e}':y=0")
        else:
            chain.append(f"crop={cw}:{ch}:x=0:y='(ih-{ch})*{e}'")
    mf = motion_filter(seg, fps)
    if mf:
        chain.append(mf)
    chain.append(f"scale={W}:{H}:flags={'bicubic' if mode == 'preview' else 'lanczos'}")
    if seg.match:  # before the look, so the look grades clips that already agree
        chain.append(color.match_filter(seg.match))
    look = edit.look_filters(seg.look)
    if look:
        chain.append(look)
    ex, co, sa, wa = seg.adjust
    if (ex, co, sa) != (0, 1, 1):
        chain.append(f"eq=brightness={_n(0.2 * ex)}:contrast={_n(co)}:saturation={_n(sa)}")
    if wa:
        k = wa
        chain.append(f"colorbalance=rs={_n(0.15 * k)}:bs={_n(-0.15 * k)}:rm={_n(0.1 * k)}:bm={_n(-0.1 * k)}")
    if seg.flash:
        chain.append("eq=brightness='0.55*exp(-t*14)':contrast='1-0.25*exp(-t*14)':eval=frame")
    chain += ["format=yuv420p", "setsar=1"]
    g = v[0] + ",".join(chain) + "[v]"
    if seg.kind == "video" and src.has_audio and not seg.ramp:
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


def _audio_tail(acc, fx, first_in):
    """Filter lines from the joined clip audio [acc] to [aout]: clip volume, extra tracks (inputs first_in...) mixed
    in with ducking, loudnorm to -14 LUFS (only when there are tracks, so edits without any keep their levels), fades."""
    p, cur = [], acc
    if fx["cv"] != 1:
        p.append(f"[{cur}]volume={_n(fx['cv'])}[ca]")
        cur = "ca"
    if fx["tracks"]:
        names = []
        for k, t in enumerate(fx["tracks"]):
            ch = [f"atrim=start={_n(t['a'], 6)}:end={_n(t['a'] + t['dur'], 6)}", "asetpts=PTS-STARTPTS", "aresample=48000",
                  "aformat=channel_layouts=stereo", f"volume={_n(t['vol'])}"]
            if t["fi"]:
                ch.append(f"afade=t=in:st=0:d={_n(t['fi'], 6)}")
            if t["fo"]:
                ch.append(f"afade=t=out:st={_n(max(t['rem'] - t['fo'], 0), 6)}:d={_n(t['fo'], 6)}")
            if t["delay"] > 0:
                ch.append(f"adelay={round(t['delay'] * 1000)}:all=1")
            p.append(f"[{first_in + k}:a]" + ",".join(ch) + f"[t{k}]")
            names.append(f"[t{k}]")
        vo = "t0"
        if len(names) > 1:
            p.append("".join(names) + f"amix=inputs={len(names)}:normalize=0:duration=longest[vo]")
            vo = "vo"
        if fx["duck"]:  # the key is padded to the full length so the clip audio never ends early
            p.append(f"[{vo}]apad=whole_dur={_n(fx['D'], 6)},asplit=2[k1][k2]")
            p.append(f"[{cur}][k1]sidechaincompress=threshold=0.01:ratio=6:attack=20:release=500[cd]")
            cur, vo = "cd", "k2"
        p.append(f"[{cur}][{vo}]amix=inputs=2:normalize=0:duration=first[mix]")
        cur = "mix"
    post = ["loudnorm=I=-14:TP=-1.5:LRA=11", "aresample=48000"] if fx["tracks"] else []
    if fx["fi"]:
        post.append(f"afade=t=in:st=0:d={_n(fx['fi'], 6)}")
    if fx["fo"]:
        post.append(f"afade=t=out:st={_n(max(fx['D'] - fx['fo'], 0), 6)}:d={_n(fx['fo'], 6)}")
    p.append(f"[{cur}]" + ",".join(post or ["anull"]) + "[aout]")
    return p


# Curated CC0 sounds (engine/sfx/MANIFEST.json lists the source and licence of each). Per sound: file, dur (s), before (s the
# file runs before its moment lands on the seam) and volume.
SFX_SRC = Path(__file__).resolve().parent / "sfx"
SFX_META = json.loads((SFX_SRC / "MANIFEST.json").read_text())


def sfx_file(name):
    return SFX_SRC / SFX_META[name]["file"]


def sfx_tracks(tl):
    """The seam and title sound effects as audio-track dicts (the same shape as spec audio), so the finish pass mixes them
    like music. A sound lands on the seam's frame, or on a title's first frame; it starts earlier by its "before".
    ponytail: they also key the ducking, which dips clip sound for the length of the effect."""
    hits = [*tl.sfx, *((t["start_frame"], t["sfx"]) for t in tl.texts if t.get("sfx") in SFX_META)]
    out = []
    for f, name in hits:
        m = SFX_META[name]
        at = f / tl.fps - m["before"]
        cut = max(0.0, -at)  # an effect right at the start loses its lead-in
        out.append({"id": f"sfx{f}{name}", "path": str(sfx_file(name)), "start": at + cut, "in": cut, "out": m["dur"],
                    "volume": m["volume"], "fade_in": 0.0, "fade_out": 0.0})
    return out


def audio_fx(tl, win, D):
    """What the finish pass needs beyond the clips: fades, clip volume, ducking and the extra tracks cut to the output
    (win = (start_frame, end_frame) of a window preview, D = output seconds). Fades only apply where the window
    reaches the real start / end of the edit. ponytail: a track's fade-in is not shifted when a window cuts its start."""
    o = win[0] / tl.fps if win else 0.0
    fi = min(tl.fade_in, D / 2) if o == 0 else 0.0
    fo = min(tl.fade_out, D / 2) if not win or win[1] == tl.total_frames else 0.0
    tracks = []
    for t in (*tl.audio, *sfx_tracks(tl)):
        delay = t["start"] - o
        a = t["in"] + max(0.0, -delay)
        dur = min(t["out"] - a, D - max(delay, 0.0))
        if dur > 0.01:
            tracks.append(dict(path=t["path"], a=a, dur=dur, rem=t["out"] - a, delay=max(delay, 0.0), vol=t["volume"],
                               fi=t["fade_in"], fo=t["fade_out"]))
    return dict(D=D, fi=fi, fo=fo, cv=tl.clip_volume, duck=tl.duck, tracks=tracks)


# whip: (xfade slide, dblur angle). The slide moves the frame about W / frames per frame; dblur adds the streak a
# shutter would record, light over the whole seam and doubled in its middle third, where a real whip is fastest.
WHIPS = {"whipleft": ("slideleft", 0), "whipright": ("slideright", 0), "whipup": ("slideup", 90), "whipdown": ("slidedown", 90)}


def seam_blur(name, tf, o, fps, dims):
    """Filters run on the joined stream right after a whip or zoom-in xfade that starts at output second o: blur
    only while the seam moves (its first frame is still A at rest). '' for every other transition."""
    if name not in WHIPS and name != "zoomin":
        return ""
    if not dims:
        raise ValueError(f"{name} needs the output size for its blur")
    a, b = o + 0.5 / fps, o + (tf - 0.5) / fps
    m0, m1 = o + tf / 3 / fps, o + 2 * tf / 3 / fps
    if name == "zoomin":  # a soft defocus at the peak of the zoom
        r = max(dims) * 0.006
        return f",gblur=sigma={_n(r, 2)}:enable='between(t,{_n(m0, 6)},{_n(m1, 6)})'"
    ang = WHIPS[name][1]
    r = 0.4 * (dims[0] if ang == 0 else dims[1]) / max(tf, 1)  # 0.4 / 0.8 of the per-frame travel
    return (f",dblur=angle={ang}:radius={_n(r, 2)}:enable='between(t,{_n(a, 6)},{_n(b, 6)})'"
            f",dblur=angle={ang}:radius={_n(r, 2)}:enable='between(t,{_n(m0, 6)},{_n(m1, 6)})'")


def _graph(n, frames, trans, fps, window=None, has_cues=False, fx=None, dims=None, seams=None, finish=""):
    """Join n inputs (frames[i] long, trans[i] = (xfade name, frames) or None between i and i+1), then optionally
    trim to window (start, end) in seconds, apply the finish layer (color.finish_filters text) and overlay the caption
    input (index n), so titles and captions stay clean. fx (audio_fx) adds fades and the extra audio tracks (inputs
    after the caption input). dims (W, H) sizes the whip and zoom-in blur.
    seams {i: input index}: the seamfx clip for trans[i] when it is a seamfx effect; it is laid over a plain fade of the
    same length, frame for frame, so lengths and audio stay exactly xfade's. Ends in [vout][aout]."""
    p = [f"[{i}:v]fps={fps}[v{i}]" for i in range(n)]
    accv, acca, acc = "v0", "0:a", frames[0]
    for i in range(1, n):
        t = trans[i - 1]
        if t:
            name, tf = t
            o = (acc - tf) / fps
            base = "fade" if name in seamfx.FX else WHIPS.get(name, (name,))[0]
            p.append(f"[{accv}][v{i}]xfade=transition={base}:duration={_n(tf / fps, 6)}"
                     f":offset={_n(o, 6)}{seam_blur(name, tf, o, fps, dims)}[x{i}]")
            p.append(f"[{acca}][{i}:a]acrossfade=d={_n(tf / fps, 6)}[ja{i}]")
            accv, acca = f"x{i}", f"ja{i}"
            if name in seamfx.FX:  # fps first, so the shift is a whole number of frames in a 1/fps time base
                if i - 1 not in (seams or {}):
                    raise ValueError(f"the {name} transition after clip {i} has no seam clip")
                p.append(f"[{seams[i - 1]}:v]fps={fps},setpts=PTS-STARTPTS+{acc - tf}[s{i}]")
                p.append(f"[x{i}][s{i}]overlay=eof_action=pass:repeatlast=0[y{i}]")
                accv = f"y{i}"
            acc += frames[i] - tf
        else:
            # a hard cut joins two waveforms mid-cycle: dip the audio for a few ms either side of the join so it cannot click
            d = min(DECLICK, frames[i] / fps / 2, acc / fps / 2)
            if d > 0:
                p.append(f"[{acca}]afade=t=out:st={_n(acc / fps - d, 6)}:d={_n(d, 6)}[ao{i}]")
                p.append(f"[{i}:a]afade=t=in:st=0:d={_n(d, 6)}[ai{i}]")
                acca, ain = f"ao{i}", f"ai{i}"
            else:
                ain = f"{i}:a"
            p.append(f"[{accv}][{acca}][v{i}][{ain}]concat=n=2:v=1:a=1[cv{i}][ja{i}]")
            p.append(f"[cv{i}]fps={fps}[x{i}]")  # concat resets the timebase; xfade needs it back
            accv, acca, acc = f"x{i}", f"ja{i}", acc + frames[i]
    if window:
        s, e = window
        p.append(f"[{accv}]trim=start={_n(s, 6)}:end={_n(e, 6)},setpts=PTS-STARTPTS[w]")
        p.append(f"[{acca}]atrim=start={_n(s, 6)}:end={_n(e, 6)},asetpts=PTS-STARTPTS[aw]")
        accv, acca = "w", "aw"
    if finish:
        p.append(f"[{accv}]{finish}[fin]")
        accv = "fin"
    if has_cues:  # never shortest=1: it would end the video when the cue list ends
        p.append(f"[{n}:v]fps={fps},format=rgba[cap]")
        p.append(f"[{accv}][cap]overlay=0:0:eof_action=pass[o]")
        accv = "o"
    fade = ""
    if fx and fx["fi"]:
        fade += f"fade=t=in:st=0:d={_n(fx['fi'], 6)},"
    if fx and fx["fo"]:
        fade += f"fade=t=out:st={_n(max(fx['D'] - fx['fo'], 0), 6)}:d={_n(fx['fo'], 6)},"
    p.append(f"[{accv}]{fade}setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv,format=yuv420p[vout]")
    p += _audio_tail(acca, fx, n + bool(has_cues)) if fx else [f"[{acca}]anull[aout]"]
    return ";\n".join(p)


def _run_cmd(files, graph, out, workdir, codec, cue_list=None, extra=()):
    gf = Path(workdir) / f"join_{out.name}.txt"
    gf.write_text(graph)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    for f in files:
        cmd += ["-i", str(f)]
    if cue_list:
        cmd += ["-f", "concat", "-safe", "0", "-i", str(cue_list)]
    for f in extra:
        cmd += ["-i", str(f)]
    return cmd + ["-/filter_complex", str(gf), "-map", "[vout]", "-map", "[aout]", *codec, str(out)]


def join_command(seg_files, frames, trans, fps, out, workdir, codec, dims=None, seams=None):
    """One chunk: intermediate in the segment codec. Pure cuts at its edges are the caller's job.
    seams {i: seam clip path} for the seamfx transitions in trans."""
    seams = seams or {}
    n = len(seg_files)
    idx = {i: n + k for k, i in enumerate(seams)}
    return _run_cmd(seg_files, _graph(n, frames, trans, fps, dims=dims, seams=idx), out, workdir, codec,
                    extra=list(seams.values()))


def finish_command(files, frames, trans, window, cue_list, fps, out, workdir, codec, fx=None, dims=None, seams=None,
                   finish=""):
    """Last pass: join (when several files are left), window trim, finish layer, caption overlay, fades, audio mix,
    colour tags, delivery encode. seams as in join_command; their inputs come after the audio tracks."""
    seams = seams or {}
    tracks = [t["path"] for t in fx["tracks"]] if fx else []
    first = len(files) + bool(cue_list) + len(tracks)
    idx = {i: first + k for k, i in enumerate(seams)}
    g = _graph(len(files), frames, trans, fps, window, bool(cue_list), fx, dims, idx, finish)
    return _run_cmd(files, g, out, workdir, codec, cue_list, tracks + list(seams.values()))


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


def delivery(mode, codec, size, grain=0.0):
    """Encoder arguments. grain (finish layer, 0 to 1) raises the final bitrate: VideoToolbox holds the bitrate, so
    without more bits the grain is smeared into blotches."""
    if mode == "preview":
        return ["-c:v", "h264_videotoolbox", "-b:v", "3M", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    k = 1 + color.GRAIN_MBPS * grain
    if codec == "h264":
        v = ["-c:v", "h264_videotoolbox", "-b:v", f"{_n({720: 8, 1080: 16, 2160: 50}.get(size, 16) * k, 1)}M"]
    else:
        v = ["-c:v", "hevc_videotoolbox", "-b:v", f"{_n(FINAL_MBPS.get(size, 12) * k, 1)}M", "-tag:v", "hvc1"]
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
    # hold the gate for the whole sweep (a glob and some unlinks): a render registers in _active under this gate before
    # it looks for cache hits, so no render can pick a segment that is about to be deleted (issue #9)
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
        files = sorted((f for f in ORIG_CACHE.glob("*") if f.is_file()), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        for f in files:
            if total <= ORIG_CAP:
                break
            total -= f.stat().st_size
            f.unlink(missing_ok=True)


def _stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _timeout(seconds):
    return max(120, 20 * seconds + 60)


# ---------- the job

def preview_key(tl, spec, win):
    """What a preview depends on: the edit, the window, and (captions on) the cached transcripts it would draw."""
    import captions
    words = ""
    if spec["captions"]["on"]:
        words = json.dumps([captions.cached(u) for u in sorted({s.uuid for s in tl.segs})], default=str)
    files = []  # an audio file replaced in place keeps its path: mtime and size tell
    for t in tl.audio:
        try:
            st = Path(t["path"]).stat()
            files.append((t["path"], st.st_mtime_ns, st.st_size))
        except OSError:
            files.append((t["path"], None, None))
    return hashlib.sha1(f"{tl.edit_hash}|{win}|{words}|{files}".encode()).hexdigest()


def saved_preview(cid):
    """The last finished preview of this project (survives engine restarts), or None."""
    try:
        meta = json.loads((RENDERS / str(cid) / "preview.json").read_text())
    except (OSError, ValueError):
        return None
    return meta if Path(meta.get("path", "")).is_file() else None


def saved_final(cid):
    """The last finished final render, if its file is still there unchanged (same size), else None."""
    try:
        meta = json.loads((RENDERS / str(cid) / "final.json").read_text())
        return meta if Path(meta["path"]).stat().st_size == meta.get("size") else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def render(cid, mode, start=None, end=None, codec="hevc"):
    """Job target for projects.start_job(cid, mode, render.render, start, end, codec). Returns the result dict."""
    global SEG_RUNS, _active
    import captions
    import edit
    if mode not in ("preview", "final"):
        raise ValueError(f"mode must be preview or final, not {mode!r}")
    tl, spec = edit.snapshot(cid)
    if (spec.get("match_color") or {}).get("on"):  # measure new clips first (a preview builds missing proxies, as it would anyway)
        projects.jobs.setdefault((cid, mode), {})["stage"] = "Measuring colour"
        if color.ensure([(s.uuid, s.kind) for s in tl.segs], build=mode == "preview"):
            tl, spec = edit.snapshot(cid)
    name = library.get_collection(cid)["name"]
    # the file is named after the version it shows (saved now if it isn't yet), so every render maps to one in the app
    ver = versions.number(cid, "Rendered") if mode == "final" and tl.segs else None
    if not tl.segs:
        raise RuntimeError("project has no clips")
    if len(tl.segs) > MAX_CLIPS:
        raise RuntimeError(f"The edit has {len(tl.segs)} clips; renders are limited to {MAX_CLIPS}.")
    if tl.duration > MAX_SECONDS:
        raise RuntimeError(f"The edit is {tl.duration / 60:.1f} minutes; renders are limited to 15 minutes.")
    for t in tl.audio:
        if not Path(t["path"]).is_file():
            raise RuntimeError(f"Audio file {Path(t['path']).name} is missing ({t['path']}). Remove the track or pick the file again.")
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
    if mode == "preview":  # nothing changed since the last preview: hand it back instead of re-rendering
        key = preview_key(tl, spec, win)
        meta = saved_preview(cid)
        if meta and meta.get("key") == key:
            return {**meta, "rendered": 0, "message": "Unchanged since the last preview, so it was reused"}
    else:  # same for finals, as long as the file is still there and untouched
        key = f"{preview_key(tl, spec, None)}|{codec}"
        meta = saved_final(cid)
        if meta and meta.get("key") == key:
            return {**meta, "rendered": 0, "reused": True, "message": "Nothing changed since the last render, so that video was kept"}
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
        picked = {s.item_id for s in tl.segs if s.kind == "video" and spec["clips"].get(str(s.item_id), {}).get("captions")}
        if on and picked:  # only the clips picked for captions are transcribed and drawn
            uuids = list(dict.fromkeys(s.uuid for s in tl.segs if s.item_id in picked))
            state["stage"] = f"Transcribing {len(uuids)} clips"
            terrors = {}
            transcripts = captions.ensure_transcripts(uuids, None, terrors, captions.needs_words(spec["captions"]))
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
            mbps = FINAL_MBPS.get(tl.size, 12) * (1 + color.GRAIN_MBPS * (tl.finish or {}).get("grain", 0))
            _free_check(folder, out_s * mbps * 1e6 / 8 * 2 + 2e9, "the finished video")
        # progress weights: the join plan only needs frame counts, so progress can cover the downloads too
        frames = [s.frames for s in sel]
        trans = [s.trans_after for s in sel[:-1]]
        K = JOIN_CHUNK.get(tl.size, 8)
        plan = list(levels(frames, trans, K))
        join_s = sum(sum(fr[a:b]) - sum(t[1] for t in tr[a:b - 1] if t) for cs, fr, tr in plan for a, b in cs if b - a > 1) / fps
        dl_w = (miss_s + join_s) * 0.1 if mode == "final" else 0.0
        prog = Progress(state, miss_s + join_s + dl_w + 0.0001, f"render:{cid}:{mode}", f"Render: {name} ({mode})")
        srcs, errors = {}, []
        uniq = list({s.uuid: s for s in miss.values()}.values())
        got = {}
        if mode == "final" and uniq:
            def report(n, total):
                prog.stage(f"Downloading originals: {n} of {total}")
                prog.cb(dl_w)(n / total)
            got, derrs = fetch_originals(uniq, tmp, prog.slot_key, prog.label, report)
            prog.add(dl_w)
        else:
            derrs = {}
        for s in uniq:
            if cancel.is_set():
                raise RuntimeError("Cancelled")
            if s.uuid in derrs:
                errors.append(f"{media.label_for(s.uuid)} ({derrs[s.uuid][:120]})")
                continue
            if mode == "preview":
                state["stage"] = "Preparing playable copies"
            try:
                if mode == "final":
                    p = still_for(s.uuid, mode, tmp, got[s.uuid]) if s.kind == "photo" else got[s.uuid]
                else:
                    p = source_for(s, mode, tmp)
                srcs[s.uuid] = (p, probe_source(p))
            except (RuntimeError, HTTPException, OSError, subprocess.SubprocessError) as e:
                if cancel.is_set():
                    raise RuntimeError("Cancelled") from None
                msg = (e.detail if isinstance(e, HTTPException) else
                       "iCloud download timed out" if isinstance(e, subprocess.TimeoutExpired) else
                       f"download failed (exit {e.returncode})" if isinstance(e, subprocess.CalledProcessError) else str(e))
                log.warning("fetch %s failed: %s", s.uuid, e)
                errors.append(f"{media.label_for(s.uuid)} ({msg[:120]})")
        if cancel.is_set():
            raise RuntimeError("Cancelled")
        if errors and any(projects.PHOTOS_DENIED[:40] in e for e in errors):
            raise RuntimeError(projects.PHOTOS_DENIED)  # one cause for every clip: say it once
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
        base = sel[0].start_frame
        files = [cdir / f"{keys[s.item_id]}.mov" for s in sel]
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
            # 3b. seam clips for the seamfx transitions (cached by both segment keys, so usually already there)
            seam_files = {}
            for i, s in enumerate(sel[:-1]):
                if s.trans_after and s.trans_after[0] in seamfx.FX:
                    if cancel.is_set():
                        raise RuntimeError("Cancelled")
                    prog.stage("Rendering transitions")
                    name_, tf_ = s.trans_after
                    sk = seamfx.key(name_, tf_, keys[s.item_id], keys[sel[i + 1].item_id], mode, dims, fps)
                    seam_files[i] = seamfx.ensure(name_, tf_, files[i], s.frames, files[i + 1], sk, cdir, mode, dims, fps,
                                                  SEG_CODEC[mode], parts, cancel)
            # 4. captions
            cap_file, cues, all_cues, cap = None, [], [], None
            wsec = (win[0] / fps, win[1] / fps) if win else None
            if on:
                prog.stage("Captions")
                cues = all_cues = captions.map_cues(tl, transcripts, picked)
                cap = captions.build(tl, spec["captions"], dims, cues, work, wsec)
                cap_file = cap.list_file
            if tl.texts:  # titles and captions share one overlay stream, so the finish pass keeps one overlay input
                import titles
                prog.stage("Titles")
                seg_files = list(zip(sel, files))

                def at(f):  # the rendered clip file and frame that show output frame f, so texts can see what is under them
                    return next(((str(p), f - s.start_frame) for s, p in seg_files if s.start_frame <= f < s.start_frame + s.frames), None)
                cap_file = titles.build(tl, dims, work, wsec, cap,
                                        avoid_bottom=bool(cap_file) and spec["captions"].get("position", "bottom") == "bottom",
                                        at=at, warnings=warnings)
            # 5. join levels, then the finish pass
            if mode == "preview":
                rdir.mkdir(parents=True, exist_ok=True)
                target, folder = rdir / "preview.mp4", None
                fpart = rdir / "preview.part.mp4"
            else:
                folder.mkdir(parents=True, exist_ok=True)
                # e.g. All-our-halloweens_v3_2026-11-10_1308.mp4
                st = _stamp()  # YYYYmmdd-HHMMSS
                stem = "_".join(x for x in (projects.slug(name), f"v{ver}" if ver else "", f"{st[:4]}-{st[4:6]}-{st[6:8]}_{st[9:13]}") if x)
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
                    prog.stage("Joining" if not cap_file else "Joining and adding text")
                    fl = (sum(fr) - sum(t[1] for t in tr if t)) / fps
                    D = (window[1] - window[0]) if window else fl
                    grain = tl.finish["grain"] if tl.finish else 0.0
                    cmd = finish_command(files, fr, tr, window, cap_file, fps, fpart, work, delivery(mode, codec, tl.size, grain),
                                         audio_fx(tl, win, D), dims, seam_files if li == 0 else None,
                                         color.finish_filters(tl.finish, dims))
                    run_ffmpeg(cmd, D, prog.cb(fl), cancel, _timeout(fl), work)
                    prog.add(fl)
                    break
                prog.stage(f"Joining (pass {li + 1})")
                nxt = []
                for ci, (a, b) in enumerate(cs):
                    if b - a == 1:
                        nxt.append(files[a]); continue
                    o = work / f"j{li}_{ci}.mov"
                    fl = (sum(fr[a:b]) - sum(t[1] for t in tr[a:b - 1] if t)) / fps
                    sf = {j - a: seam_files[j] for j in range(a, b - 1) if li == 0 and j in seam_files}
                    run_ffmpeg(join_command(files[a:b], fr[a:b], tr[a:b - 1], fps, o, work, mid, dims, sf), fl, prog.cb(fl),
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
        state["stage"] = "Checking frames"  # not prog.stage: the activity entry was cleared with the slot
        sheet = _sheet(target, sel, win, fps, rdir / f"{mode}-sheet.jpg")
        prog.state["progress"] = 1.0
        _gc_cache()
        import sounds  # late: sounds imports edit, which the render job already uses
        credit = sounds.write_credits(cid, target.parent, f"{target.stem}-credits") if mode == "final" else None
        result = {"path": str(target), "folder": str(target.parent), "seconds": round(out_s, 2), "credits": credit,
                "srt": str(srt) if srt else None, "sheet": str(sheet) if sheet else None, "edit_hash": tl.edit_hash,
                "rendered": len(todo), "cached": hits, "warnings": warnings,
                "message": f"{out_s:.0f}s, {len(todo)} clips rendered, {hits} reused"}
        result["key"] = key
        if mode == "final":
            result["size"] = target.stat().st_size
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / f"{mode}.json").write_text(json.dumps(result))
        return result
    finally:
        for p in parts:
            p.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        media.stage(f"render:{cid}:{mode}", None)  # the last stages run after the slot is released; never leave a card behind
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
    model_config = ConfigDict(allow_inf_nan=False)  # NaN/inf start made the job fail with "Unexpected error" instead of a 422
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
    j = projects.job(cid, mode)
    if j.get("status") == "idle" and mode == "preview" and (meta := saved_preview(cid)):
        # after an engine restart: show the saved preview, flagged if the edit changed since
        import edit
        tl, _ = edit.snapshot(cid)
        return {**meta, "status": "done", "saved": True, "stale": meta.get("edit_hash") != tl.edit_hash}
    return j


@router.post("/collections/{cid}/render/{mode}/cancel", status_code=204)
def render_cancel(cid: int, mode: Mode):
    ev = CANCEL.get((cid, mode))
    if ev:
        ev.set()


def _recorded(cid, mode, field):
    """Only the exact path the finished job recorded, and only inside our own output folders."""
    j = projects.job(cid, mode)
    if j.get("status") == "idle" and mode == "preview":
        j = {**(saved_preview(cid) or {}), "status": "done"}  # same fallback as render_status, else Exact plays a 404 after a restart
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
