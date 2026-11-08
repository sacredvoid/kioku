"""Video projects: export a timeline for a real editor, and let Claude work on a project.

Export is the one place originals get copied: editors need real files. They go to
~/Movies/Safelight/<project>/media, downloaded from iCloud if needed. Albums never copy.
"""
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import core
import library
import settings

router = APIRouter()
log = logging.getLogger("safelight.jobs")
EXPORTS = Path.home() / "Movies" / "Safelight"
ENGINE = Path(__file__).resolve().parent
STILL_SECONDS = 3.0
jobs: dict[tuple[int, str], dict] = {}  # (collection id, "export" | "ask") -> status
jobs_lock = threading.Lock()


def slug(name):
    return re.sub(r"[^\w\- ]+", "", name).strip().replace(" ", "-")[:60] or "project"


def start_job(cid, kind, target, *args):
    with jobs_lock:
        if jobs.get((cid, kind), {}).get("status") == "running":
            raise HTTPException(409, f"{kind} already running for this project")
        jobs[(cid, kind)] = {"status": "running", "started": time.time()}

    def run():
        t = time.time()
        log.info("%s started for collection %d", kind, cid)
        try:
            jobs[(cid, kind)].update(status="done", **target(cid, *args))
            log.info("%s done for collection %d in %.0fs", kind, cid, time.time() - t)
        except Exception as e:  # report, don't crash the server
            # RuntimeError carries a message meant for the user; anything else is a bug: keep the traceback
            log.error("%s failed for collection %d after %.0fs: %s", kind, cid, time.time() - t, e,
                      exc_info=not isinstance(e, RuntimeError))
            msg = str(e) if isinstance(e, RuntimeError) else f"Unexpected error ({type(e).__name__}): {e}"
            jobs[(cid, kind)].update(status="failed", error=msg[:1000])
    threading.Thread(target=run, daemon=True).start()
    return job(cid, kind)


def job(cid, kind):
    """A snapshot: the job thread keeps changing the live dict, and serializing it mid-change can fail."""
    j = jobs.get((cid, kind))
    if not j:
        return {"status": "idle"}
    snap = dict(j)
    if "steps" in snap:
        snap["steps"] = list(snap["steps"])
    return snap


# ---------- export

def originals(uuids, dest):
    """uuid -> local original file in dest; downloads only what isn't there yet."""
    dest.mkdir(parents=True, exist_ok=True)
    have = {f.stem: f for f in dest.iterdir() if f.is_file() and not f.name.startswith(".")}
    need = [u for u in dict.fromkeys(uuids) if u not in have]
    if need:
        ufile = dest / ".uuids.txt"
        ufile.write_text("\n".join(need))
        r = subprocess.run([str(Path(sys.executable).parent / "osxphotos"), "export", str(dest),
                            "--uuid-from-file", str(ufile), "--download-missing", "--use-photokit",
                            "--skip-edited", "--skip-live", "--skip-raw", "--filename", "{uuid}", "--no-progress"],
                           capture_output=True, text=True, timeout=settings.get().download_seconds * len(need))
        ufile.unlink(missing_ok=True)
        if "authorization" in (r.stdout + r.stderr).lower():
            raise RuntimeError("No Photos access for the process running the engine. Grant it in System Settings > "
                               "Privacy & Security > Photos (the app or terminal that started Safelight).")
        have = {f.stem: f for f in dest.iterdir() if f.is_file() and not f.name.startswith(".")}
        if any(u not in have for u in need):
            log.warning("export: %d of %d downloads missing; osxphotos said: %s", sum(u not in have for u in need),
                        len(need), (r.stdout + r.stderr).strip()[-2000:])
    return have


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=avg_frame_rate:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    info = json.loads(r.stdout or "{}")
    num, _, den = (info.get("streams") or [{}])[0].get("avg_frame_rate", "30/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    return round(fps, 3) or 30.0, float(info.get("format", {}).get("duration") or 0)


def build_timeline(name, clips, rate):
    """clips: [(path, is_video, fps, start_s, dur_s, media_len_s)] -> OTIO timeline at `rate`."""
    import opentimelineio as otio
    tl = otio.schema.Timeline(name=name, global_start_time=otio.opentime.RationalTime(0, rate))
    track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    tl.tracks.append(track)
    for path, is_video, fps, start, dur, media_len in clips:
        r = fps if is_video else rate
        # editors (and the FCP XML writer) need the full source length, not just the used range
        ref = otio.schema.ExternalReference(
            target_url=path.resolve().as_uri(),
            available_range=otio.opentime.TimeRange(otio.opentime.RationalTime(0, r),
                                                    otio.opentime.RationalTime(round(max(media_len, start + dur) * r), r)))
        track.append(otio.schema.Clip(
            name=path.name, media_reference=ref,
            source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(round(start * r), r),
                                                 otio.opentime.RationalTime(round(dur * r), r))))
    return tl


def do_export(cid):
    import opentimelineio as otio
    col = library.get_collection(cid)
    if col["kind"] != "project":
        raise RuntimeError("only projects can be exported")
    if not col["items"]:
        raise RuntimeError("project has no clips")
    out = EXPORTS / slug(col["name"])
    try:
        files = originals([i["uuid"] for i in col["items"]], out / "media")
    except subprocess.TimeoutExpired:
        raise RuntimeError("iCloud downloads timed out. Try again (finished files are kept), or raise the "
                           "iCloud download timeout in Settings.")
    clips, missing = [], []
    for it in col["items"]:
        f = files.get(it["uuid"])
        if not f:
            missing.append(it["uuid"]); continue
        is_video = it["kind"] != "image"
        fps, length = probe(f) if is_video else (0, STILL_SECONDS)
        t0 = it["t0"] or 0
        t1 = it["t1"] if it["t1"] is not None else (length or it["duration"] or STILL_SECONDS)
        clips.append((f, is_video, fps, t0, max(t1 - t0, 0.04), length or t1))
    if not clips:
        raise RuntimeError(f"no clips could be downloaded ({len(missing)} failed)")
    rate = Counter(round(c[2]) for c in clips if c[1]).most_common(1)[0][0] if any(c[1] for c in clips) else 30
    tl = build_timeline(col["name"], clips, rate)
    xml = out / f"{slug(col['name'])}.xml"
    otio.adapters.write_to_file(tl, str(xml), adapter_name="fcp_xml")
    otio.adapters.write_to_file(tl, str(xml.with_suffix(".otio")))
    total = sum(c[4] for c in clips)
    return {"path": str(xml), "folder": str(out), "clips": len(clips), "missing": missing,
            "seconds": round(total, 1), "message": f"{len(clips)} clips, {total:.0f}s at {rate} fps"
            + (f" ({len(missing)} couldn't be downloaded from iCloud and were left out)" if missing else "")}


@router.post("/collections/{cid}/export", status_code=202)
def export(cid: int):
    with library.conn() as c:
        col = library.require_collection(c, cid)
    if col["kind"] != "project":
        raise HTTPException(422, "only projects can be exported")
    return start_job(cid, "export", do_export)


@router.get("/collections/{cid}/export")
def export_status(cid: int):
    return job(cid, "export")


@router.post("/collections/{cid}/reveal", status_code=204)
def reveal(cid: int):
    """Show the export in Finder."""
    j = job(cid, "export")
    if j.get("status") != "done":
        raise HTTPException(404, "nothing exported yet")
    subprocess.run(["open", "-R", j["path"]], check=False)


# ---------- ask Claude

def claude_bin():
    for p in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude")):
        if p.exists():
            return str(p)
    return shutil.which("claude")


def mcp_config():
    cfg = core.DATA / "mcp.json"
    uv = next((str(p) for p in (Path.home() / ".local/bin/uv", Path("/opt/homebrew/bin/uv")) if p.exists()), "uv")
    cfg.write_text(json.dumps({"mcpServers": {"safelight": {
        "command": uv, "args": ["run", "--directory", str(ENGINE), "mcp_server.py"]}}}))
    return cfg


SKILL = ENGINE.parent / "skills" / "safelight-editor" / "SKILL.md"
SYSTEM = ("You are running inside the Safelight app. Use only the safelight tools. You are editing the EXISTING project "
          "with id {cid} named {name!r} (don't create a new one). Its brief: {brief!r}. Follow the editing workflow "
          "below.\n\n{skill}")


class Ask(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


STEP_LABELS = {
    "search_media": lambda i: f"Searching “{i.get('query', '')}”",
    "analyze_clip": lambda i: "Watching a clip: downloading if needed, finding scenes",
    "get_media": lambda i: "Reading a clip's details",
    "add_to_collection": lambda i: f"Adding {len(i.get('uuids', []))} clip(s)",
    "set_clip_range": lambda i: f"Trimming a clip to {i.get('in_seconds', 0):.1f}–{i.get('out_seconds', 0):.1f}s",
    "move_clip": lambda i: "Reordering",
    "remove_from_collection": lambda i: "Dropping a clip",
    "set_brief": lambda i: "Writing the brief",
    "get_collection": lambda i: "Reviewing the cut",
    "export_project": lambda i: "Exporting the timeline",
}


def step_label(name, inp):
    short = name.split("__")[-1]
    if short in ("analyze_clip", "get_media") and (inp or {}).get("uuid"):
        import media
        what = "Watching" if short == "analyze_clip" else "Reading"
        return f"{what}: {media.label_for(inp['uuid'])}"
    try:
        return STEP_LABELS.get(short, lambda i: short.replace("_", " ").capitalize())(inp or {})
    except Exception:
        return short


RUNNING_AGENTS = 0  # guarded by settings.changed


def agent_slot(job_state):
    """Wait for a free Claude slot (Settings: max agents). Re-checks when settings change."""
    global RUNNING_AGENTS
    with settings.changed:
        while RUNNING_AGENTS >= settings.get().max_agents:
            job_state["note"] = (f"Waiting for a free Claude slot: {RUNNING_AGENTS} edit(s) running, "
                                 f"limit {settings.get().max_agents} (Settings).")
            settings.changed.wait(2)
        RUNNING_AGENTS += 1
        job_state.pop("note", None)


def release_slot():
    global RUNNING_AGENTS
    with settings.changed:
        RUNNING_AGENTS -= 1
        settings.changed.notify_all()


def do_ask(cid, prompt):
    exe = claude_bin()
    if not exe:
        raise RuntimeError("Claude Code CLI not found (expected ~/.local/bin/claude)")
    col = library.get_collection(cid)
    job_state = jobs[(cid, "ask")]
    job_state["steps"] = []
    agent_slot(job_state)
    try:
        return run_claude(exe, cid, col, prompt, job_state)
    finally:
        release_slot()


def run_claude(exe, cid, col, prompt, job_state):
    # creative first cut gets the strong model; revisions to an existing cut use the cheaper one (Settings)
    model = settings.get().revise_model if col["items"] else settings.get().new_cut_model
    job_state["model"] = model
    log.info("ask for collection %d using %s", cid, model)
    cmd = [exe, "-p", prompt, "--model", model, "--append-system-prompt",
           SYSTEM.format(cid=cid, name=col["name"], brief=col["brief"], skill=SKILL.read_text().split("---", 2)[-1]),
           "--mcp-config", str(mcp_config()), "--strict-mcp-config",
           "--tools", "",  # no built-in tools: no shell, no file edits; only Safelight's own tools
           "--allowedTools", "mcp__safelight", "--output-format", "stream-json", "--verbose"]
    # stderr goes to a file: an undrained PIPE fills up (~64 KB) and blocks claude forever
    errf = tempfile.TemporaryFile(mode="w+")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True, cwd=str(ENGINE))
    minutes = settings.get().ask_minutes
    timed_out = threading.Event()
    timer = threading.Timer(minutes * 60, lambda: (timed_out.set(), proc.kill()))
    timer.start()
    final, err = None, None
    try:
        for line in proc.stdout:  # one JSON event per line; surface tool calls as live steps
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        job_state["steps"].append(step_label(block.get("name", ""), block.get("input")))
                    elif block.get("type") == "text" and block.get("text", "").strip():
                        job_state["note"] = block["text"].strip()[:300]
            elif ev.get("type") == "result":
                final = ev.get("result", "")
                if ev.get("is_error"):
                    err = final or "Claude reported an error"
        proc.wait()
        errf.seek(0)
        stderr = errf.read().strip()
    finally:
        timer.cancel()
        if proc.poll() is None:  # we bailed out early: don't leave claude running with nobody reading its output
            proc.kill()
            proc.wait()
        errf.close()
    if stderr:
        log.info("claude stderr for collection %d: %s", cid, stderr[-2000:])
    if timed_out.is_set():
        raise RuntimeError(f"Stopped after {minutes} min (Settings > Ask time limit). "
                           f"Clips placed so far are kept; {len(job_state['steps'])} steps ran.")
    if err or proc.returncode != 0:
        raise RuntimeError(err or stderr[-800:] or f"Claude exited with code {proc.returncode}")
    return {"output": (final or "").strip()}


@router.post("/collections/{cid}/ask", status_code=202)
def ask(cid: int, body: Ask):
    with library.conn() as c:
        library.require_collection(c, cid)
    return start_job(cid, "ask", do_ask, body.prompt)


@router.get("/collections/{cid}/ask")
def ask_status(cid: int):
    return job(cid, "ask")
