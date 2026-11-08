"""Video projects: export a timeline for a real editor, and let Claude work on a project.

Export is the one place originals get copied: editors need real files. They go to
~/Movies/Safelight/<project>/media, downloaded from iCloud if needed. Albums never copy.
"""
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import core
import library

router = APIRouter()
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
        try:
            jobs[(cid, kind)].update(status="done", **target(cid, *args))
        except Exception as e:  # report, don't crash the server
            jobs[(cid, kind)].update(status="failed", error=f"{type(e).__name__}: {e}"[:1000])
    threading.Thread(target=run, daemon=True).start()
    return jobs[(cid, kind)]


def job(cid, kind):
    return jobs.get((cid, kind), {"status": "idle"})


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
                           capture_output=True, text=True)
        ufile.unlink(missing_ok=True)
        if "authorization" in (r.stdout + r.stderr).lower():
            raise RuntimeError("No Photos access for the process running the engine. Grant it in System Settings > "
                               "Privacy & Security > Photos (the app or terminal that started Safelight).")
        have = {f.stem: f for f in dest.iterdir() if f.is_file() and not f.name.startswith(".")}
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
    files = originals([i["uuid"] for i in col["items"]], out / "media")
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
            "seconds": round(total, 1), "message": f"{len(clips)} clips, {total:.0f}s at {rate} fps"}


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


def do_ask(cid, prompt):
    exe = claude_bin()
    if not exe:
        raise RuntimeError("Claude Code CLI not found (expected ~/.local/bin/claude)")
    col = library.get_collection(cid)
    r = subprocess.run(
        [exe, "-p", prompt, "--append-system-prompt",
         SYSTEM.format(cid=cid, name=col["name"], brief=col["brief"], skill=SKILL.read_text().split("---", 2)[-1]),
         "--mcp-config", str(mcp_config()), "--strict-mcp-config",
         "--tools", "",  # no built-in tools: no shell, no file edits; only Safelight's own tools
         "--allowedTools", "mcp__safelight", "--output-format", "text"],
        capture_output=True, text=True, timeout=1800, cwd=str(ENGINE))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-800:] or f"claude exited {r.returncode}")
    return {"output": r.stdout.strip()}


@router.post("/collections/{cid}/ask", status_code=202)
def ask(cid: int, body: Ask):
    with library.conn() as c:
        library.require_collection(c, cid)
    return start_job(cid, "ask", do_ask, body.prompt)


@router.get("/collections/{cid}/ask")
def ask_status(cid: int):
    return job(cid, "ask")
