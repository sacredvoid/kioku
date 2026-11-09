"""Safelight tools for Claude (MCP over stdio). Talks to the running engine at 127.0.0.1:8765.

Add to Claude Code once:
  claude mcp add safelight -- uv run --directory ~/Documents-Local/github/contact-sheet/engine mcp_server.py
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

API = "http://127.0.0.1:8765"
server = MCPServer("safelight", instructions=(
    "Search and organize the user's Photos library and build video projects. Albums and projects hold "
    "pointers to Photos items (uuid), never copies. Times are in seconds."))


# Read-only tools never change the library or a project, so Claude Code may run several at once.
# (analyze_clip fills the engine's cache, which is invisible to the user.) The engine caps real parallel work
# at Settings > max agents.
READ_ONLY = ToolAnnotations(readOnlyHint=True)


def call(method, path, body=None, timeout=120):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {"ok": True}
    except urllib.error.HTTPError as e:
        return {"error": f"{e.code}: {e.read().decode()[:500]}"}
    except TimeoutError:
        return {"error": f"No answer after {timeout} s. The engine keeps working; call the same tool again to wait for it."}
    except urllib.error.URLError:
        return {"error": "Safelight engine isn't running. Open Safelight or run `uv run server.py`."}


def slim(h):
    out = {"uuid": h["uuid"], "kind": "video" if h["kind"] != "image" else "photo", "seconds": h.get("duration"),
           "labels": (h.get("meta") or "")[:160], "score": h.get("score")}
    if h.get("moment"):
        out["best_moment"] = h["moment"]  # analyzed video: the scene that matched, in seconds
    return out


@server.tool(annotations=READ_ONLY)
def search_media(query: str, kind: str = "", limit: int = 20) -> list[dict]:
    """Search photos and videos by description, people, place or label. kind: "", "image" or "video"."""
    q = urllib.parse.urlencode({"q": query, "k": max(1, min(limit, 100)), **({"kind": kind} if kind else {})})
    res = call("GET", f"/search?{q}")
    return [slim(h) for h in res] if isinstance(res, list) else [res]


@server.tool(annotations=READ_ONLY)
def analyze_clip(uuid: str) -> list:
    """Understand a video before cutting it: detects scenes (shot boundaries; long takes split into <=8s pieces),
    scores each scene's sharpness (higher = crisper; under ~40 is usually blurry) and motion (higher = more
    movement or camera shake), and returns a contact sheet image with one labelled frame per scene (#idx, start-end s).
    First call per video downloads it from iCloud and takes 10-60 s; later calls are instant.
    Analyze several candidates at once (parallel tool calls in one turn): the engine downloads them in parallel.
    Cut on scene boundaries: use a scene's t0/t1 (or a sub-range of it) with set_clip_range."""
    # an iCloud download alone may take up to Settings > iCloud download timeout (180 s by default)
    res = call("POST", f"/media/{uuid}/analyze", timeout=600)
    if "error" in res:
        return [res]
    req = urllib.request.Request(API + res["sheet"])
    with urllib.request.urlopen(req, timeout=60) as r:
        sheet = r.read()
    return [{"uuid": uuid, "scenes": res["scenes"]}, Image(data=sheet, format="jpeg")]


@server.tool(annotations=READ_ONLY)
def get_media(uuid: str) -> dict:
    """Details for one item: date, place, labels, your tags and caption, collections it's in."""
    return call("GET", f"/asset/{uuid}")


@server.tool(annotations=READ_ONLY)
def look_at(uuids: list[str]) -> list:
    """See photos (or a video's still frame) before using them: returns a small image of each, up to 6 per call.
    Use it on every photo you consider for a cut, so you judge the picture, not just its labels."""
    out: list = []
    for u in uuids[:6]:
        try:
            with urllib.request.urlopen(f"{API}/thumb/{urllib.parse.quote(u)}", timeout=30) as r:
                out += [{"uuid": u}, Image(data=r.read(), format="jpeg")]
        except urllib.error.HTTPError as e:
            out.append({"uuid": u, "error": f"no preview ({e.code})"})
        except (urllib.error.URLError, TimeoutError):
            out.append({"uuid": u, "error": "engine not reachable"})
    return out


@server.tool()
def tag_media(uuid: str, add: list[str] = [], remove: list[str] = []) -> dict:  # noqa: B006
    """Add or remove the user's own tags on an item."""
    cur = call("GET", f"/asset/{uuid}")
    if "error" in cur:
        return cur
    tags = [t for t in cur["tags"] if t.lower() not in {r.lower() for r in remove}] + add
    return call("PUT", f"/asset/{uuid}/tags", {"tags": tags})


@server.tool(annotations=READ_ONLY)
def list_collections(kind: str = "") -> list[dict]:
    """Albums and video projects. kind: "", "album" or "project"."""
    return call("GET", "/collections" + (f"?kind={kind}" if kind else ""))


@server.tool()
def create_collection(name: str, kind: str = "project") -> dict:
    """Create an album or a video project. kind: "album" or "project"."""
    return call("POST", "/collections", {"name": name, "kind": kind})


@server.tool(annotations=READ_ONLY)
def get_collection(collection_id: int) -> dict:
    """A collection with its items in order (item_id, uuid, in/out seconds, labels)."""
    col = call("GET", f"/collections/{collection_id}")
    if "items" in col:
        col["items"] = [{"item_id": i["item_id"], "uuid": i["uuid"], "seconds": i["duration"],
                         "kind": "photo" if i["kind"] == "image" else "video",  # "poster" = video kept in iCloud
                         "in": i["t0"], "out": i["t1"], "labels": (i["meta"] or "")[:120]} for i in col["items"]]
    return col


@server.tool()
def set_brief(collection_id: int, brief: str) -> dict:
    """Set the project's brief (what the edit should feel like, length, music, audience)."""
    return call("PATCH", f"/collections/{collection_id}", {"brief": brief})


@server.tool()
def add_to_collection(collection_id: int, uuids: list[str]) -> dict:
    """Append items (by uuid) to an album or project. Pointers only, nothing is copied."""
    return call("POST", f"/collections/{collection_id}/items", {"uuids": uuids})


@server.tool()
def set_clip_range(collection_id: int, item_id: int, in_seconds: float, out_seconds: float) -> dict:
    """Trim a project clip to [in, out] seconds of its source video."""
    return call("PATCH", f"/collections/{collection_id}/items/{item_id}", {"t0": in_seconds, "t1": out_seconds})


@server.tool()
def move_clip(collection_id: int, item_id: int, position: float) -> dict:
    """Reorder: set a clip's position (items sort ascending; use e.g. 2.5 to go between 2 and 3)."""
    return call("PATCH", f"/collections/{collection_id}/items/{item_id}", {"pos": position})


@server.tool()
def remove_from_collection(collection_id: int, item_id: int) -> dict:
    """Remove one item from an album or project (the photo itself is untouched)."""
    return call("DELETE", f"/collections/{collection_id}/items/{item_id}")


@server.tool()
def export_project(collection_id: int) -> dict:
    """Export the project as an FCP XML timeline (opens in DaVinci Resolve / Premiere) under ~/Movies/Safelight.
    Runs in the background; call export_status to see when it's done."""
    return call("POST", f"/collections/{collection_id}/export")


@server.tool(annotations=READ_ONLY)
def export_status(collection_id: int) -> dict:
    """Status of the last export: running, done (with file path) or failed."""
    return call("GET", f"/collections/{collection_id}/export")


# ---------- editing: format, look, captions, per-clip effects, transitions, renders

def drop_unset(**kw):
    return {k: v for k, v in kw.items() if v is not None}


@server.tool(annotations=READ_ONLY)
def get_edit(collection_id: int) -> dict:
    """The project's edit: format, look, per-clip speed/adjust/look/reframe, transitions, captions state, where each clip
    starts on the output timeline, total length, warnings, and the allowed options (looks, aspects, transition types).
    Call it before changing anything and after, to check the length. Also returns edit_hash and captions readiness
    (ready, pending, no_speech clip counts)."""
    res = call("GET", f"/collections/{collection_id}/edit")
    if "error" in res:
        return res
    cap = call("GET", f"/collections/{collection_id}/captions")
    res["captions_status"] = cap if "error" not in cap else cap["error"]
    return res


@server.tool()
def set_edit(collection_id: int, aspect: str = "", size: int = 0, fps: int = 0, look: str = "", look_strength: float = -1,
             captions: bool | None = None, caption_style: str = "", caption_clips: list[int] | None = None,
             caption_position: str = "", caption_font: str = "", caption_size: str = "", caption_color: str = "",
             caption_highlight_color: str = "", caption_highlight: bool | None = None, caption_uppercase: bool | None = None,
             caption_max_words: int = -1, fade_in: float = -1, fade_out: float = -1,
             clip_volume: float = -1, duck: bool | None = None) -> dict:
    """Project-wide settings. Unset arguments keep their value. aspect: 16:9, 9:16, 1:1, 4:5 (use 9:16 for reels and
    stories; clips are center-cropped to fit, set_clip_fx reframe moves the crop). size: short side 720, 1080 or 2160.
    fps: 24, 25, 30, 60. look: a name from get_edit, or "none" to clear; one look for the whole edit, strength 0.6 to 0.8
    unless the user wants it strong. captions=true burns the spoken words in and writes an .srt next to the video: it
    transcribes each picked clip's audio once (needs whisper.cpp; the error says how to install it); silent clips and
    music get no captions. Captions are per clip: caption_clips is the list of item_ids to caption (exactly those; [] picks
    none). Turning captions on with nothing picked preselects clips that likely have speech. get_edit captions_status.clips
    shows each clip's speech: "speech", "no speech" or "not transcribed", plus its language: pick only "speech" clips.
    Style: caption_style bold, box or pop (1 to 3 words at a time, reel style); caption_position bottom, middle or top;
    caption_font arial, impact, georgia, verdana, trebuchet, rounded or courier; caption_size S, M or L; caption_color and
    caption_highlight_color as #RRGGBB; caption_highlight=true colours the word being spoken (needs word timing, which
    is transcribed on demand); caption_uppercase; caption_max_words per line (0 = fit the frame).
    Turning captions on or picking clips starts the transcription and returns at once; check get_edit
    captions_status.pending before rendering. fade_in / fade_out: seconds 0 to 3, the whole video fades from / to
    black and silence (0 = none). clip_volume 0 to 1 scales every clip's own sound; duck (default true) lowers clip
    sound under the audio tracks from set_audio."""
    body = drop_unset(aspect=aspect or None, size=size or None, fps=fps or None, look_strength=None if look_strength < 0 else look_strength,
                      captions=captions, caption_style=caption_style or None, caption_clips=caption_clips,
                      caption_position=caption_position or None, caption_font=caption_font or None, caption_size=caption_size or None,
                      caption_color=caption_color or None, caption_highlight_color=caption_highlight_color or None,
                      caption_highlight=caption_highlight, caption_uppercase=caption_uppercase,
                      caption_max_words=None if caption_max_words < 0 else caption_max_words,
                      fade_in=None if fade_in < 0 else fade_in, fade_out=None if fade_out < 0 else fade_out,
                      clip_volume=None if clip_volume < 0 else clip_volume, duck=duck)
    if look:
        body["look"] = None if look.lower() == "none" else look
    if not body:
        return {"error": "Nothing to change: pass at least one argument."}
    return call("PATCH", f"/collections/{collection_id}/edit", body)


@server.tool()
def set_clip_fx(collection_id: int, item_id: int, speed: float | None = None, volume: float | None = None,
                exposure: float | None = None, contrast: float | None = None, saturation: float | None = None,
                warmth: float | None = None, look: str | None = None, look_strength: float | None = None,
                reframe_mode: str | None = None, reframe_pos: float | None = None, reframe_from: float | None = None,
                reframe_to: float | None = None, mute: bool | None = None) -> dict:
    """Change one clip's look and motion. Only the arguments you pass change; speed also scales its audio (0.25 to 4;
    photos cannot change speed). volume 0 to 2. exposure and warmth -1 to 1, contrast 0.8 to 1.3, saturation 0 to 1.6.
    look: a name from get_edit, or "none" for no look on this clip even when the project has one.
    reframe_mode: center, manual (reframe_pos) or pan (reframe_from to reframe_to); positions are 0 to 1 along the
    axis being cropped. mute=true silences this clip's own sound (voiceover and music tracks stay). Item ids come from
    get_collection."""
    body = drop_unset(speed=speed, volume=volume, exposure=exposure, contrast=contrast, saturation=saturation,
                      warmth=warmth, look_strength=look_strength, reframe_mode=reframe_mode, reframe_pos=reframe_pos,
                      reframe_from=reframe_from, reframe_to=reframe_to, mute=mute)
    if look is not None:
        body["look"] = look.lower() if look.lower() == "none" else look  # engine: "none" = no look on this clip
    if not body:
        return {"error": "Nothing to change: pass at least one effect."}
    return call("PATCH", f"/collections/{collection_id}/edit/clips/{item_id}", body)


@server.tool()
def set_transition(collection_id: int, after_item_id: int, type: str, duration: float = 0.5) -> dict:
    """Join a clip to the next one. type: cut, dissolve, fade-black, wipe-left, wipe-right, slide-left. Duration 0.2 to 2 s,
    capped at half the shorter clip; transitions shorten the edit by their duration. Use 'cut' to remove one."""
    path = f"/collections/{collection_id}/edit/transitions/{after_item_id}"
    return call("DELETE", path) if type == "cut" else call("PUT", path, {"type": type, "duration": duration})


@server.tool()
def set_audio(collection_id: int, track_id: str, path: str = "", start: float | None = None, source_in: float | None = None,
              source_out: float | None = None, volume: float | None = None, fade_in: float | None = None,
              fade_out: float | None = None, remove: bool = False) -> dict:
    """Add, change or remove an audio track (voiceover or music): at most 3 per project, mixed with the clips' own
    sound and normalized to -14 LUFS, in previews and finals. track_id: any short name (letters, digits, - _); a new id
    adds a track and needs path, an existing id changes only the arguments you pass. path: a local mp3, m4a, wav, aac,
    aiff or caf file. start: seconds on the output timeline where it begins. source_in / source_out: the part of the
    file to use (seconds; default all). volume 0 to 2. fade_in / fade_out: seconds. remove=true deletes the track.
    While any track plays, clip sound is lowered under it (set_edit duck=false turns that off). Returns the edit."""
    p = f"/collections/{collection_id}/edit/audio/{track_id}"
    if remove:
        return call("DELETE", p)
    body = drop_unset(path=path or None, start=start, out=source_out, volume=volume, fade_in=fade_in, fade_out=fade_out)
    if source_in is not None:
        body["in"] = source_in
    if not body:
        return {"error": "Nothing to change: pass a path for a new track, or at least one setting."}
    return call("PUT", p, body)


@server.tool()
def render_preview(collection_id: int, start: float = 0, end: float = 0) -> dict:
    """Render a quick low-resolution preview from the playable copies. Optional start/end (seconds on the output
    timeline) render only that part; end=0 means to the end. Returns at once; poll render_status."""
    body = {"mode": "preview", **({"start": start} if start > 0 else {}), **({"end": end} if end > 0 else {})}
    return call("POST", f"/collections/{collection_id}/render", body)


@server.tool()
def render_final(collection_id: int, codec: str = "hevc") -> dict:
    """Render the finished video from the original files (downloads iCloud originals, can take minutes). Only when the
    user asked for the finished video. codec: hevc or h264. Output goes under ~/Movies/Safelight. Poll render_status."""
    return call("POST", f"/collections/{collection_id}/render", {"mode": "final", "codec": codec})


@server.tool(annotations=READ_ONLY)
def render_status(collection_id: int, mode: str = "preview", wait_seconds: int = 0) -> list:
    """State of the last preview or final render (mode: preview or final): stage, progress, output path, .srt path,
    warnings, error. When done it also returns a contact sheet image of the result: look at it and fix what you see
    (cuts, crop, color, caption position). wait_seconds (0 to 90) waits for it to finish, so you do not need to poll
    in a tight loop."""
    if mode not in ("preview", "final"):
        return [{"error": "mode must be preview or final"}]
    path = f"/collections/{collection_id}/render/{mode}"
    deadline = time.time() + max(0, min(wait_seconds, 90))
    while True:
        j = call("GET", path)
        if "error" in j or j.get("status") != "running" or time.time() >= deadline:
            break
        time.sleep(1)
    if j.get("status") != "done":
        return [j]
    try:
        with urllib.request.urlopen(f"{API}{path}/sheet", timeout=60) as r:
            return [j, Image(data=r.read(), format="jpeg")]
    except (urllib.error.URLError, TimeoutError):
        return [{**j, "sheet_error": "no contact sheet available"}]


if __name__ == "__main__":
    server.run()
