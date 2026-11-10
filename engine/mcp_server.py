"""Kioku tools for Claude (MCP over stdio). Talks to the running engine at 127.0.0.1:8765.

Add to Claude Code once:
  claude mcp add kioku -- uv run --directory ~/Documents-Local/github/kioku/engine mcp_server.py
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

API = "http://127.0.0.1:8765"
server = MCPServer("kioku", instructions=(
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
        return {"error": "Kioku engine isn't running. Open Kioku or run `uv run server.py`."}


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
    movement or camera shake), and returns a frame grid image with one labelled frame per scene (#idx, start-end s).
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
def find_moments(uuids: list[str], profile: str = "auto", query: str = "", top: int = 5) -> list:
    """Find the best 1-4 s moments (laughs, cheers, motion peaks, pretty or face shots) across videos, ranked, with a sheet of
    before/peak/after frames. profile: auto, action, people, scenic or talk. query (optional) boosts shots matching a phrase.
    Cut a moment with set_clip_range(t0, t1); the peak is where a beat hit should land. Up to 6 videos per call."""
    top = max(1, min(top, 12))
    found = []
    for k, u in enumerate(uuids[:6]):
        res = call("GET", f"/media/{urllib.parse.quote(u)}/moments?" + urllib.parse.urlencode({"profile": profile, "q": query, "top": top}), timeout=600)
        if "error" in res:
            found.append({"uuid": u, "error": res["error"]})
            continue
        found += [{"uuid": u, "tag": chr(65 + k), **m} for m in res["moments"]]
    ranked = sorted((m for m in found if "error" not in m), key=lambda m: -m["score"])[:top]
    seen: dict = {}
    lines = []
    for m in ranked:  # label = clip letter + rank within that clip, same as the sheet's
        seen[m["tag"]] = seen.get(m["tag"], 0) + 1
        lines.append(f"{m['tag']}{seen[m['tag']]}  {m['uuid']}  {m['t0']:.2f}-{m['t1']:.2f}s  peak {m['peak']:.2f}  score {m['score']:.2f}  "
                     f"{', '.join(m['reasons'])}" + (f'  "{m["transcript"]}"' if m.get("transcript") else ""))
    lines += [f"{m['uuid']}: {m['error']}" for m in found if "error" in m]
    out: list = ["\n".join(lines) or "no moments found"]
    imgs = []
    for k, u in enumerate(uuids[:6]):  # one sheet per clip with its share of the top list, stacked into one image
        n = sum(1 for m in ranked if m["uuid"] == u)
        if n:
            req = f"{API}/media/{urllib.parse.quote(u)}/moments/sheet?" + urllib.parse.urlencode({"profile": profile, "q": query, "top": n, "prefix": chr(65 + k)})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    imgs.append(r.read())
            except (urllib.error.URLError, TimeoutError):
                pass  # the table is still useful without the picture
    if imgs:
        out.append(Image(data=stack(imgs), format="jpeg"))
    return out


def stack(jpegs):
    """Stack JPEG sheets vertically into one JPEG."""
    import io
    from PIL import Image as Pil
    ims = [Pil.open(io.BytesIO(b)) for b in jpegs]
    sheet = Pil.new("RGB", (max(i.width for i in ims), sum(i.height for i in ims)), (12, 11, 11))
    y = 0
    for i in ims:
        sheet.paste(i, (0, y))
        y += i.height
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=82)
    return buf.getvalue()


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
    """Export the project as an FCP XML timeline (opens in DaVinci Resolve / Premiere) under ~/Movies/Kioku.
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
             clip_volume: float = -1, duck: bool | None = None, sync_track: str = "", sync_grid: str = "",
             sync_lead: int = -1, text_style: str = "") -> dict:
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
    sound under the audio tracks from set_audio.
    Beat sync: sync_track = the audio track id of the music (add it first with add_sound / set_audio), "none" turns it off.
    Every cut then lands on the music's beat grid; get_edit returns `grid` (beat times on the output timeline).
    sync_grid: beat (default), half (every 2nd beat, calmer) or downbeat (bar starts, slowest). sync_lead 0 to 3 frames the
    cut lands before the beat (default 1; it reads as on time). Set each clip's length in beats with set_clip_fx(beats=).
    text_style: one look for every title, "none" to clear: clean (Space Grotesk, white, expo ease; suits title-slide,
    letter-rise, typewriter), bold (Anton, white and yellow, back ease; title-pop, word-on-beat, big-number,
    text-behind), editorial (Playfair serif, warm white and gold, expo; tracking-in, title-slide, lower-third), playful
    (Anton, white and pink, spring; title-pop, word-on-beat). It fills only what a text leaves at its default (font auto,
    white, default accent, ease auto), so pick it once and don't set fonts or colours per text."""
    body = drop_unset(aspect=aspect or None, size=size or None, fps=fps or None, look_strength=None if look_strength < 0 else look_strength,
                      captions=captions, caption_style=caption_style or None, caption_clips=caption_clips,
                      caption_position=caption_position or None, caption_font=caption_font or None, caption_size=caption_size or None,
                      caption_color=caption_color or None, caption_highlight_color=caption_highlight_color or None,
                      caption_highlight=caption_highlight, caption_uppercase=caption_uppercase,
                      caption_max_words=None if caption_max_words < 0 else caption_max_words,
                      fade_in=None if fade_in < 0 else fade_in, fade_out=None if fade_out < 0 else fade_out,
                      clip_volume=None if clip_volume < 0 else clip_volume, duck=duck,
                      sync_grid=sync_grid or None, sync_lead=None if sync_lead < 0 else sync_lead,
                      text_style=text_style or None)
    if sync_track:
        body["sync_track"] = None if sync_track.lower() == "none" else sync_track
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
                reframe_to: float | None = None, mute: bool | None = None, beats: int | None = None,
                hit: float | None = None, motion: str | None = None, motion_amount: float | None = None,
                flash: bool | None = None, shake: bool | None = None, ramp: str | None = None) -> dict:
    """Change one clip's look and motion. Only the arguments you pass change; speed also scales its audio (0.25 to 4;
    photos cannot change speed). volume 0 to 2. exposure and warmth -1 to 1, contrast 0.8 to 1.3, saturation 0 to 1.6.
    look: a name from get_edit, or "none" for no look on this clip even when the project has one.
    reframe_mode: center, manual (reframe_pos) or pan (reframe_from to reframe_to); positions are 0 to 1 along the
    axis being cropped. mute=true silences this clip's own sound (voiceover and music tracks stay). Item ids come from
    get_collection.
    Rhythm (needs sync on, see set_edit): beats 1 to 32 = how many grid units the clip lasts (0 = from its in/out length);
    hit = a source time in seconds (the jump, the clap, the splash) that must land exactly on a beat; -1 clears it.
    Motion: motion push (slow zoom in), pull (zoom out), drift-left / drift-right (slow pan), punch (snaps in on each
    beat and eases back out before the next), none. Push, pull and drift ease in and out, so they read at low amounts:
    motion_amount 0 to 1 (default 0.35, 1 = 1.3x), 0.25 to 0.4 is usually right. flash=true: white pulse on the clip's first frame
    (a drop or a big moment). shake=true: a short shake on each beat in the clip (at most half the clips; peaks only).
    ramp: slow-mid (fast, slow-motion middle, fast), fast-in, slow-out, none; silences the clip's own sound."""
    body = drop_unset(speed=speed, volume=volume, exposure=exposure, contrast=contrast, saturation=saturation,
                      warmth=warmth, look_strength=look_strength, reframe_mode=reframe_mode, reframe_pos=reframe_pos,
                      reframe_from=reframe_from, reframe_to=reframe_to, mute=mute, beats=beats, hit=hit, motion=motion,
                      motion_amount=motion_amount, flash=flash, shake=shake, ramp=ramp)
    if look is not None:
        body["look"] = look.lower() if look.lower() == "none" else look  # engine: "none" = no look on this clip
    if not body:
        return {"error": "Nothing to change: pass at least one effect."}
    return call("PATCH", f"/collections/{collection_id}/edit/clips/{item_id}", body)


@server.tool()
def set_transition(collection_id: int, after_item_id: int, type: str, duration: float = 0.5, sfx: str = "") -> dict:
    """Join a clip to the next one. type: cut, dissolve, fade-black, flash-white, zoom-in, whip-left, whip-right,
    whip-up, whip-down, blur, slide-left, slide-up, wipe-left, wipe-right, circle, squeeze. Duration 0.2 to 2 s (whips
    at most 0.35 s), capped at half the shorter clip; unsynced, transitions shorten the edit by their duration (synced,
    the overlap is centred on the beat).
    whip-*: a fast motion-blurred slide; the direction is where the picture moves (whip-left = the picture flies left,
    as if the camera whipped right). Match the movement at the end of the outgoing shot, 0.2 to 0.3 s, with
    sfx="whoosh", about one per 8 s. zoom-in blurs at its peak.
    sfx: whoosh (pairs with whip, zoom-in, slide), hit (a hard cut on a drop), riser (builds into the seam), none.
    Leave sfx empty to keep the current one. type cut with an sfx keeps a hard cut with a sound."""
    path = f"/collections/{collection_id}/edit/transitions/{after_item_id}"
    body = {"type": type, "duration": duration}
    if sfx:
        body["sfx"] = sfx
    return call("PUT", path, body)


@server.tool()
def set_text(collection_id: int, text_id: str, template: str = "", text: str = "", start: float | None = None,
             dur: float | None = None, position: str = "", font: str = "", color: str = "", accent: str = "",
             ease: str = "") -> dict:
    """Add, change or remove an animated title. text_id: any short name; a new id needs template and text, an existing id
    changes only what you pass; template="none" removes it. template: title-pop (big opener), title-slide (clean
    subtitle-style title), word-stagger (words appear one by one), lower-third (name or caption on a bar),
    location-tag (small pin label, top-left), big-number (a year or count that counts up), word-on-beat (one big word
    at a time, each landing on the next beat when the edit is synced; 3 to 5 word hooks, at most 8), letter-rise
    (letters rise in one by one: calm, cinematic), tracking-in (letter spacing closes while it fades in: calm,
    editorial), typewriter (letters appear one at a time: dates and places only), text-behind (a big 1 to 2 word title
    the subject stands in front of, cut out per frame with Apple Vision; once per edit, at most 4 s, inside one shot
    with one clear subject and sky or space above it; drawn as a plain title, with a warning, when that fails).
    Restraint: one text on screen at a time, under 6 words, one animation family per edit.
    text: at most 80 characters. Tokens {place}, {date} (e.g. "Oct 12, 2024") and {year} are filled from the Photos
    metadata of the clip under the text's start, so never type a place or date you have not seen; an error means that
    clip has none. start / dur: output seconds (start snaps to the beat when synced; dur 0.5 to 15).
    position: auto (default: at render, the band covering the fewest faces and subjects, the template's usual place
    when free, middle only as a last resort), top, middle, bottom (kept as asked; the render warns if it covers a face).
    9:16 text keeps clear of the app's buttons and caption area. Readability is automatic (shadow, or a soft scrim or
    box on bright or busy footage), so don't change color for it. font: auto, inter, anton, playfair, grotesk.
    color / accent: #RRGGBB (accent colours the bar, rule or pin). ease: the entrance curve, auto (the template's or the
    text_style's), cubic (smooth), expo (fast then soft), back (small overshoot), spring (bouncy). Returns the edit."""
    p = f"/collections/{collection_id}/edit/texts/{text_id}"
    if template.lower() == "none":
        return call("DELETE", p)
    body = drop_unset(template=template or None, text=text or None, start=start, dur=dur, position=position or None,
                      font=font or None, color=color or None, accent=accent or None, ease=ease or None)
    if not body:
        return {"error": "Nothing to change: pass template and text for a new title, or at least one setting."}
    return call("PUT", p, body)


@server.tool()
def set_audio(collection_id: int, track_id: str, path: str = "", start: float | None = None, source_in: float | None = None,
              source_out: float | None = None, volume: float | None = None, fade_in: float | None = None,
              fade_out: float | None = None, remove: bool = False, fit: bool = False,
              target_seconds: float | None = None, drop_at: float | None = None) -> dict:
    """Add, change or remove an audio track (voiceover or music): at most 3 per project, mixed with the clips' own
    sound and normalized to -14 LUFS, in previews and finals. track_id: any short name (letters, digits, - _); a new id
    adds a track and needs path, an existing id changes only the arguments you pass. path: a local mp3, m4a, wav, aac,
    aiff or caf file. start: seconds on the output timeline where it begins. source_in / source_out: the part of the
    file to use (seconds; default all). volume 0 to 2. fade_in / fade_out: seconds. remove=true deletes the track.
    While any track plays, clip sound is lowered under it (set_edit duck=false turns that off).
    fit=true fits the track to the edit from its music map: it picks source_in and source_out on bar lines, puts the
    track's drop drop_at seconds into the video (default 35% of the length, when the track has a drop), starts at 0 and
    fades out over the last bar; arguments you pass explicitly win. target_seconds: the length to fill (default the
    edit's current length). Returns the edit plus `fit`: in, out, drop_out_s (output seconds where the drop lands, put
    the hero shot there), music_end_s (where the music ends: end the edit there) and notes."""
    p = f"/collections/{collection_id}/edit/audio/{track_id}"
    if remove:
        return call("DELETE", p)
    body = drop_unset(path=path or None, start=start, out=source_out, volume=volume, fade_in=fade_in, fade_out=fade_out,
                      target_seconds=target_seconds, drop_at=drop_at)
    if fit:
        body["fit"] = True
    if source_in is not None:
        body["in"] = source_in
    if not body:
        return {"error": "Nothing to change: pass a path for a new track, or at least one setting."}
    return call("PUT", p, body)


@server.tool(annotations=READ_ONLY)
def analyze_music(collection_id: int, track_id: str) -> dict:
    """The music map of an audio track on the project (add it first); you cannot hear it, this is what it sounds like.
    Returns tempo, bar_s (seconds per bar), bars (bar start times in the FILE, seconds), energy (one digit 0 to 9 per bar,
    loudness and bass mixed; raw numbers, overrule the guesses below when they look wrong), sections
    [{start, end, bar, bars, level}], drop_s (file seconds of the biggest bass jump on a bar start, or null: no clear drop)
    and drop_bar. vocals is always null (not detected). The first call takes a few seconds, then it is cached.
    Use it to put the hero shot on the drop, long calm shots on low-energy bars and 1 to 2 beat shots on high ones.
    get_edit gives each clip the same 0 to 9 digit for the bar under it. set_audio(fit=true) picks the part of the file."""
    return call("GET", f"/collections/{collection_id}/edit/audio/{urllib.parse.quote(track_id, safe='')}/map", timeout=300)


@server.tool(annotations=READ_ONLY)
def search_sounds(query: str, kind: str = "music", max_duration: float | None = None, source: str = "free") -> dict:
    """Find music (kind="music", the default) or sound effects (kind="sfx") for a project. You cannot hear audio:
    choose from title, tags, creator and duration_s, and tell the user what you picked so they can listen in the app.
    source="free" (default) searches free Creative Commons audio (Openverse), already limited to licenses the user
    allows; source="mine" searches the user's own files under ~/Music by title, artist or file name (the user handles
    the rights for those). max_duration: seconds. Returns results; pass a result's id (free) or path (mine) to
    add_sound."""
    if source == "mine":
        return call("GET", "/sounds/local?" + urllib.parse.urlencode({"q": query}))
    if source != "free":
        return {"error": "source must be free or mine"}
    params = {"q": query, "kind": kind} | ({"max_dur": max_duration} if max_duration else {})
    return call("GET", "/sounds/search?" + urllib.parse.urlencode(params))


@server.tool()
def add_sound(collection_id: int, sound_id: str, start: float = 0, track_id: str = "", volume: float | None = None,
              fade_in: float | None = None, fade_out: float | None = None, fit: bool = False,
              target_seconds: float | None = None, drop_at: float | None = None) -> dict:
    """Put a sound from search_sounds on the project's audio as a new track (at most 3). sound_id: the id of a free
    result, or the path of a "mine" result. start: seconds on the output timeline. The free file is downloaded once
    and checked. When the result has a "credit", say that credit line to the user: CC BY music must be credited
    where the video is posted (Kioku also writes credits.txt next to final renders and exports). Use set_audio to
    trim, fade or move the track afterwards. fit=true, target_seconds and drop_at fit the track to the edit like
    set_audio (see there); the result then has `fit`."""
    if sound_id.startswith("/"):
        return set_audio(collection_id, track_id or f"m{int(time.time()) % 100000}", path=sound_id, start=start,
                         volume=volume, fade_in=fade_in, fade_out=fade_out, fit=fit, target_seconds=target_seconds,
                         drop_at=drop_at)
    body = drop_unset(cid=collection_id, start=start, track_id=track_id or None, volume=volume, fade_in=fade_in,
                      fade_out=fade_out, target_seconds=target_seconds, drop_at=drop_at)
    if fit:
        body["fit"] = True
    return call("POST", f"/sounds/{urllib.parse.quote(sound_id, safe='')}/add", body)


@server.tool()
def render_preview(collection_id: int, start: float = 0, end: float = 0) -> dict:
    """Render a quick low-resolution preview from the playable copies. Optional start/end (seconds on the output
    timeline) render only that part; end=0 means to the end. Returns at once; poll render_status."""
    body = {"mode": "preview", **({"start": start} if start > 0 else {}), **({"end": end} if end > 0 else {})}
    return call("POST", f"/collections/{collection_id}/render", body)


@server.tool()
def render_final(collection_id: int, codec: str = "hevc") -> dict:
    """Render the finished video from the original files (downloads iCloud originals, can take minutes). Only when the
    user asked for the finished video. codec: hevc or h264. Output goes under ~/Movies/Kioku. Poll render_status."""
    return call("POST", f"/collections/{collection_id}/render", {"mode": "final", "codec": codec})


@server.tool(annotations=READ_ONLY)
def render_status(collection_id: int, mode: str = "preview", wait_seconds: int = 0) -> list:
    """State of the last preview or final render (mode: preview or final): stage, progress, output path, .srt path,
    warnings, error. When done it also returns a frame grid image of the result: look at it and fix what you see
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
        return [{**j, "sheet_error": "no frame grid available"}]


# ---------- style reference

@server.tool(annotations=READ_ONLY)
def analyze_reference(path: str) -> list:
    """Learn the style of a reference video the user gave you: a local .mp4/.mov/.m4v file or a link (YouTube and most
    video sites, downloaded once at up to 1080p and kept on this Mac), up to 15 minutes. Measures pacing in seconds and beats, cuts on the beat, transitions, camera motion, punch-ins, flashes,
    on-screen text and color, then returns a short summary, a concrete PLAN in Kioku's own settings (sync grid, beats per
    clip, motion/flash budgets, transitions with sounds, captions, look) and a frame grid of the reference's shots.
    Takes about 10 s per minute of video; the result is cached. Follow the plan with set_edit, set_clip_fx, set_transition."""
    res = call("POST", "/references/analyze", {"path": path}, timeout=900)
    if "error" in res:
        return [res]
    out: list = [res["summary"] + f"\n\nreference_id: {res['id']}" + (f"\nlook cube: {res['profile']['cube']}" if res["profile"].get("cube") else "")]
    try:
        with urllib.request.urlopen(API + res["sheet"], timeout=60) as r:
            out.append(Image(data=r.read(), format="jpeg"))
    except (urllib.error.URLError, TimeoutError):
        out.append("no frame grid available")
    return out


@server.tool(annotations=READ_ONLY)
def compare_to_reference(collection_id: int, path: str) -> dict:
    """After render_preview, measure the project's preview the same way as the reference video at path and list the
    biggest gaps (pacing, sync, motion, transitions, text, color) as short fixes. Call it once, fix the top two gaps,
    and stop: matching every number would make the edit worse."""
    res = call("POST", "/references/compare", {"path": path, "collection_id": collection_id}, timeout=900)
    if "error" in res:
        return res
    return {"gaps": res["gaps"] or ["No big gaps: the preview matches the reference closely."], "preview": res["mine"],
            **({"warning": "the preview is older than the latest edit; render_preview again first"} if res.get("stale") else {})}


if __name__ == "__main__":
    server.run()
