# Safelight editing + captions: design and implementation plan

Date: 2026-11-08. Builds on `notes/cli-editing-research.md` (Phase 1 render core, plus captions from Phase 2). No code in this branch. Out of scope: music, titles, beat sync, reframe tracking, style reference, stabilize, Ken Burns, OTIO speed markers.

## Review (Fable, 2026-11-08)

Senior review before implementation, after re-verifying the ffmpeg claims with lavfi clips in a scratch dir (nothing installed, nothing in `data/`, engine and indexer untouched). Combined build plan for this and the smart-album design: `notes/build-plan.md`.

What changed in this document and why:

1. **HDR rotation was still wrong (high).** `-noautorotate` makes ffmpeg forward the source's display matrix into the output file: the segment is 540x960 on disk but tagged `rotation=90`, so QuickTime rotates it again and the join pass (which autorotates its inputs) turns it back to landscape and the xfade size check fails. Verified here: `ffprobe` shows `side_data rotation=90` on the segment. Fix (verified: no side data, upright, red stays red): add `-display_rotation 0` as an input option next to `-noautorotate`, keep the explicit software `transpose`. Section 0, 3 and 8 updated; the test now asserts the absence of rotation side data.
2. **One join graph for 200 clips does not fit in memory (high).** Measured: 20 one-input-per-clip ProRes LT segments at 1080x1920 joined with 0.5 s dissolves in one xfade chain peak at 5.4 GB RSS (40 clips at 540x960: 1.0 GB; concat-only: 0.3 GB). 200 clips would need tens of GB. The join is now chunked: at most `JOIN_CHUNK` segments per ffmpeg run (8 at 1080, 12 at 720, 3 at 2160), chunk outputs in the intermediate codec, chunk boundaries only on cuts, then one last pass for window, captions and the delivery encode. Section 3 and 8 updated.
3. **Caption track details were off (medium).** (a) `overlay=shortest=1` ends the output when the caption input ends: a cue list shorter than the video truncated 6.0 s to 3.8 s here. Removed `shortest=1`; `eof_action=pass` only, and the list always ends with a blank that reaches the timeline end. (b) The image demuxer gives PNGs a 1/25 s timebase, so a cue at 0.5 s landed on frame 16 instead of 15 (the design's own test showed frames 17 to 46 and called it exact). Fix: an `option framerate N` line under every `file` line in the ffconcat list (verified: frames 15 to 44 and 54 to 83, exact). Cue times are also snapped to the frame grid before writing the list. Section 0 and 4 updated.
4. **Looks simplified (ponytail).** Bundled `.cube` files, `make_looks.py`, `lut3d`, the `split/blend` strength path, cube-copying into the workdir and the cube sha in the cache key are gone. Looks are a dict of named parametric presets (`eq`, `colorbalance`, `curves=preset=...`) and strength scales the parameters linearly, so strength costs no extra filter. The spec shape (`{"name", "strength"}`) and the tool arguments are unchanged. `lut3d` is the documented upgrade when someone wants a real LUT.
5. **Seven MCP tools instead of nine.** `set_format`, `apply_look` and `set_captions` merge into one project-level `set_edit` (aspect, size, fps, look, look_strength, captions, caption_style; unset arguments keep their value). `apply_look(item_ids)` is dropped: per-clip looks go through `set_clip_fx`, which already has `look`. Total 21 tools. One route (`PATCH /collections/{cid}/edit`) replaces `edit/format` and `edit/look`.
6. **Slot ordering fixed (medium).** The render job took its `video_slot` first and then called `media.ensure_proxy` / `projects.originals` while holding it. `analyze` takes `lock_for(uuid)` then a slot; a render holding a slot while a concurrent `analyze_clip` holds that uuid's lock and waits for a slot deadlocks at `max_agents=1`, and two processes could build the same proxy at once. Rule now: every per-uuid fetch is `lock_for(uuid)`, then `video_slot`, then work, then release (one helper, `media.fetch`), run **before** the render slot; the render slot covers only ffmpeg.
7. **Tests and data safety.** Every test file sets `os.environ["CS_DB"]` to a temp path **before** `import core` and asserts `"data/index.db" not in str(core.DB_PATH)`. Module path constants bound at import (`render.CACHE`, `render.RENDERS`, `media.PROXIES`, `media.SHEETS`, `core.AUDIO_DIR`, `settings.FILE`) must be reassigned after import; reassigning `core.DATA` alone changes nothing. Test fixtures with a display matrix are made by remuxing (`ffmpeg -display_rotation 90 -i raw.mp4 -c copy out.mp4`): `-display_rotation` is an input option in ffmpeg 9 and fails as an output option.
8. **Verified unchanged:** `-/filter_complex file`; exact frame counts with `-frames:v n -t L`; `anullsrc`/`apad` normalization; 29.97 to 30 conform; VFR portrait clip upright on the software decode path with `-hwaccel videotoolbox`; `-ss` input seek on a VFR clip starts on the first frame at or after the seek time (so the `floor(t*fps)/fps` snap is cosmetic and stays optional); concat then xfade fails without `fps=N` and gives exactly 6.000 s with it; `setparams` tags bt709 on `hevc_videotoolbox` output; `scale_vt` keeps red red on a synthetic HLG clip (the real-footage check in section 10 stands); preview window trim gives exactly 2.000 s.
9. **Work split** now points at `notes/build-plan.md`, which adds the smart-album implementer (D) and the exact `main.ts` hunk ownership between C and D.

Open decisions for the user are in the review report, not here.

## 0. What I verified on this machine (ffmpeg 9.0.2, arm64, scratch dir, nothing installed, nothing in data/)

| Claim | Result |
|---|---|
| `drawtext`, `subtitles`, `ass`, `zscale` | All absent from `ffmpeg -filters`. Captions must be Pillow PNGs. Pillow 13.0 in the engine venv has freetype but **no raqm**, so Latin, Cyrillic, CJK render; Devanagari and Arabic will not shape correctly |
| Present | `xfade`, `acrossfade`, `lut3d`, `blend`, `eq`, `colorbalance`, `overlay`, `tpad`, `apad`, `atempo`, `setparams`, `scale_vt`, `transpose_vt`, `volumedetect`; encoders `h264/hevc/prores_videotoolbox`, `aac` |
| `-filter_complex_script` | **Removed** in ffmpeg 9 ("Unrecognized option"). Use `-/filter_complex file` (works) |
| `-progress pipe:1` | Emits `out_time_us=`, `progress=end`. Good enough for a progress bar |
| Mixed fps and no-audio sources | A 29.97 no-audio clip and a 30 fps clip with audio both normalize to 30 fps, 48 kHz stereo, same duration with `fps=30`, `anullsrc`, `apad=whole_dur` |
| Rotation, software decode (also with `-hwaccel videotoolbox` without `-hwaccel_output_format`) | Autorotate works: a clip with a 90 degree display matrix comes out upright (portrait) |
| Rotation on the **HDR hardware path** (`-hwaccel_output_format videotoolbox_vld`) | **Autorotate is silently skipped**: output stayed landscape. `-noautorotate` plus software `transpose` gives upright pixels **but forwards the display matrix into the output** (`ffprobe` side data `rotation=90`), so players and the join pass rotate it again. Fix (review, verified): `-noautorotate -display_rotation 0` on the input plus `transpose` after `hwdownload`: 540x960, upright, no side data, colours right |
| HDR (HLG 10-bit HEVC) | `scale_vt=...bt709` then `hwdownload,format=p010le,format=yuv420p` works and tags bt709. `format=nv12` after `hwdownload` fails for 10-bit; use `nv12` only for an 8-bit source |
| `concat` filter output timebase | `1/1000000`, which makes the next `xfade` fail ("timebase do not match"). Fix: `fps=N` after every concat (review: re-verified, exit code -22 without it, exactly 6.000 s with it) |
| Join graph memory (review) | 20 ProRes LT segments at 1080x1920 joined with 0.5 s dissolves in one xfade chain: 5.4 GB RSS, 12 s. 40 segments at 540x960: 1.0 GB; the same 40 with plain concat: 0.3 GB; concat demuxer `-c copy`: 16 MB. One graph for 200 clips is not viable; the join is chunked (section 3) |
| `-color_trc` on `hevc_videotoolbox` | Output stayed `unknown`. `setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv` at the end of the graph tags correctly |
| ProRes LT intermediate | `prores_videotoolbox -profile:v lt` works, 31 Mbps for 720p test content, so about 85 Mbps (10 MB/s) at 1080x1920 |
| Caption track | `ffconcat` list of full-frame RGBA PNGs with `duration` lines (blank PNG for gaps), then one `overlay` filter. One input and one filter for any number of cues. Review: the image demuxer gives PNGs a 1/25 s timebase, so a 0.5 s cue started one frame late (frame 16; the original test's "17 to 46" was two frames late). With an `option framerate N` line under each `file` line the cues land exactly (frames 15 to 44 and 54 to 83 at 30 fps). `overlay=shortest=1` truncated a 6.0 s video to 3.8 s when the list ended early: use `eof_action=pass` only and end the list with a blank that reaches the timeline end |
| whisper.cpp | `brew info whisper.cpp`: stable 1.9.5, bottled, not installed. Binary is `whisper-cli`. Flags below are from memory of 1.9.x, so the implementer must check `whisper-cli -h` once installed |
| Existing audio | `data/audio/<uuid>.opus` (16 kHz mono) exists only for proxies built by the current code. Today 26 proxies and 26 audio files; older proxies may lack it. whisper-cli does not read opus, so convert to a temp wav |

## 1. Spec schema

### Where it is stored

New table, created lazily by `edit.py` with `create table if not exists` (same pattern as `media.SCHEMA`; no change to `core.SCHEMA`, no ALTER on the live db):

```sql
create table if not exists edit_specs(
  collection_id integer primary key references collections(id) on delete cascade,
  spec text not null,            -- JSON below
  rev integer not null default 0,
  updated real);
```

No row means default spec. Only `kind='project'` collections get one (422 otherwise). Clips stay in `collection_items` (uuid, in/out, position); the spec adds effects keyed by `item_id`.

### Shape (version 1) and defaults

```json
{
  "version": 1,
  "format":   {"aspect": "16:9", "size": 1080, "fps": 30},
  "look":     {"name": null, "strength": 1.0},
  "clips": {
    "17": {"speed": 1.0, "volume": 1.0,
           "adjust":  {"exposure": 0, "contrast": 1.0, "saturation": 1.0, "warmth": 0},
           "look":    null,
           "reframe": {"mode": "center", "pos": 0.5, "pos_from": 0.5, "pos_to": 0.5}}
  },
  "transitions": {"17": {"type": "dissolve", "duration": 0.5}},
  "captions": {"on": false, "style": "bold"}
}
```

- `clips` and `transitions` are **sparse**: a missing key means defaults, and a clip entry equal to defaults is deleted on save. Keys are item ids as strings.
- `transitions["17"]` joins clip 17 to the clip after it. Missing or `cut` means a hard cut.
- `reframe`: `center` ignores pos. `manual` uses `pos`. `pan` goes linearly `pos_from` to `pos_to` over the clip. `pos` is 0..1 along the axis that gets cropped (horizontal when landscape source goes into a portrait frame, vertical when portrait goes into landscape). If source aspect equals target, no crop.
- `look` on a clip overrides the project look (`{"name": ..., "strength": ...}`); `{"name": null}` on a clip means "no look" even if the project has one.
- No ffmpeg strings, paths or free text anywhere in the spec. Only enums and numbers.

### Validation (pydantic with `extra="forbid"`, applied on every mutation)

| Field | Rule |
|---|---|
| `format.aspect` | `16:9`, `9:16`, `1:1`, `4:5`. 422 listing the allowed values otherwise |
| `format.size` | short side px: 720, 1080, 2160 |
| `format.fps` | 24, 25, 30, 60 (integers only; the output is always constant frame rate) |
| `look.name` | null or a key of `edit.LOOKS` (parametric presets: `warm`, `cool`, `film`, `mono`, `punch`). The name is looked up in a dict; a user string never reaches a filtergraph or a path |
| `look.strength`, clip look strength | 0..1, **clamped**. Strength scales the preset's parameters linearly (`exposure*k`, `1+(contrast-1)*k`, ...), so it costs no extra filter |
| `speed` | 0.25..4, clamped. Photos: speed other than 1 is rejected (422) |
| `volume` | 0..2, clamped |
| `adjust.exposure`, `warmth` | -1..1; `contrast` 0.8..1.3; `saturation` 0..1.6; all clamped |
| `reframe.mode` | `center`, `manual`, `pan`; `pos*` 0..1 clamped |
| `transitions.type` | `cut`, `dissolve`, `fade-black`, `wipe-left`, `wipe-right`, `slide-left` (maps to xfade `fade`, `fadeblack`, `wipeleft`, `wiperight`, `slideleft`) |
| `transitions.duration` | 0.2..2, clamped. Structural limits are applied when the timeline is computed, not stored (see below) |
| `captions.style` | `bold`, `box` |
| item ids | Must belong to the collection. Orphans (clip removed later) are pruned on every load, silently |
| Limits | 200 clips, 15 min output (reject the render, not the edit), `edit_specs.spec` under 100 KB |

Policy: **unknown keys and bad enums are rejected (422 with the allowed list); out-of-range numbers are clamped and reported** as `clamped: [{"field": "speed", "from": 9, "to": 4}]` in the tool result, so Claude learns the limit without a retry.

Structural limits live in `compute_timeline`, because trims change after the spec is saved: a transition is capped at half the shorter neighbour, and `d_in + d_out <= clip length`; below 3 frames it becomes a cut. Each cap adds a string to `Timeline.warnings`. Clips under 0.2 s output length also warn.

Atomicity: every mutation runs `begin immediate`, reads the row, applies, validates, writes, and bumps `rev`, so parallel tool calls from Claude cannot lose updates.

`edit_hash`: sha1 of the canonical JSON of (spec, items' uuid/in/out/pos). Returned by `GET /edit` and stored in render results, so Claude can tell whether a preview is stale.

## 2. Module and file layout

```
engine/timeline.py   NEW  dataclasses only (frozen interface, section 9 step 0)
engine/edit.py       NEW  spec models, storage, mutation, compute_timeline, looks, edit routes
engine/render.py     NEW  probe, segment pass, join pass, jobs, render routes
engine/captions.py   NEW  whisper, transcripts table, timing, PNG cue track, srt, captions routes
engine/media.py      EDIT slot label support, local_original() helper
engine/mcp_server.py EDIT 9 tools
engine/projects.py   EDIT step labels + system prompt line only
engine/server.py     EDIT integrator wires three routers + QuietPolls
skills/safelight-editor/SKILL.md  EDIT
app/src/{api.ts,main.ts,styles.css}  EDIT
engine/test_edit.py, test_render.py, test_captions.py  NEW
```

### `timeline.py` (types only; the contract between edit, render, captions)

```python
@dataclass(frozen=True)
class Seg:
    item_id: int; uuid: str
    kind: str                      # "video" | "photo"   (poster = iCloud-only video = "video")
    src_in: float; src_out: float  # seconds in the source; photo: 0..length
    speed: float; volume: float
    adjust: tuple[float, float, float, float]   # exposure, contrast, saturation, warmth
    look: tuple[str, float] | None              # effective (name, strength)
    reframe: tuple[str, float, float]           # (mode, a, b): center (.5,.5), manual (p,p), pan (from,to)
    frames: int                    # output length in frames at Timeline.fps
    start_frame: int               # position on the output timeline, after transition overlaps
    trans_after: tuple[str, int] | None         # (xfade name, frames) joining this seg to the next

@dataclass(frozen=True)
class Timeline:
    fps: int; aspect: str; size: int
    segs: tuple[Seg, ...]
    total_frames: int
    warnings: tuple[str, ...]
    edit_hash: str
    @property
    def duration(self) -> float: return self.total_frames / self.fps
```

### `edit.py`

```python
LOOKS: dict[str, Look]                            # name -> frozen dataclass of eq/colorbalance/curves params at strength 1
def default_spec() -> Spec
def load(cid: int) -> tuple[Spec, int]            # (spec with orphans pruned, rev)
def mutate(cid: int, fn: Callable[[Spec, dict], list[str]]) -> dict
    # atomic read-modify-write. fn(spec, collection) edits in place, returns notes (clamps).
    # returns {"spec":..., "rev": n, "clamped": [...]}; raises HTTPException 422 on invalid
def compute_timeline(items: list[dict], spec: Spec) -> Timeline
    # PURE. items = library.get_collection(cid)["items"]. Rounds each clip to whole frames,
    # resolves effective look, applies transition caps, fills start_frame and edit_hash.
def output_size(aspect: str, size: int, preview: bool) -> tuple[int, int]
    # even w,h. preview short side = min(size, 540)
def look_filters(look: tuple[str, float] | None) -> str
    # PURE. '' for none; else e.g. 'eq=contrast=1.08:saturation=1.12,colorbalance=rs=0.06:bs=-0.06' with strength applied
def snapshot(cid: int) -> tuple[Timeline, Spec]   # load + get_collection + compute_timeline in one read
router  # section 6
```

Timeline math (frames, `fps` = format.fps):
`n_i = max(1, round((src_out - src_in) / speed * fps))`; photos: `src_out - src_in` = trim span or 3.0 s (`projects.STILL_SECONDS`).
`tf_i` = transition frames after clip i = `round(d * fps)`, capped by `n_i // 2`, `n_{i+1} // 2`, and `n_i - tf_{i-1}`; under 3 becomes a cut.
`start_0 = 0`, `start_i = start_{i-1} + n_{i-1} - tf_{i-1}`, `total = start_last + n_last`.

### `render.py`

```python
RENDER_V = 1                                   # bump to invalidate every cached segment
CACHE = core.DATA / "render_cache"             # /final and /preview, capped 20 GB / 3 GB, LRU by mtime
RENDERS = core.DATA / "renders"                # /<cid>/preview.mp4, .srt, -sheet.jpg
@dataclass(frozen=True)
class Source: w:int; h:int; rot:int; fps:float; duration:float; has_audio:bool; hdr:bool; bits:int

def probe_source(path: Path) -> Source         # ffprobe; w,h are display size (rotation applied); fps = r_frame_rate
def source_for(uuid: str, mode: str, tmp: Path) -> Path
    # wraps media.fetch(uuid, mode, tmp): lock_for(uuid) -> video_slot -> ensure_proxy / local_original /
    # projects.originals -> release. Called BEFORE the render slot is taken. Photos: baked-orientation JPEG in tmp
def seg_key(seg: Seg, src_id: str, mode: str, dims: tuple[int, int], fps: int) -> str
def seg_command(seg, src: Source, src_path: Path, out: Path, mode, dims, fps, workdir: Path) -> list[str]  # PURE
def join_command(seg_files, trans, dims, fps, out: Path, workdir, codec) -> list[str]                       # PURE, one chunk
def finish_command(joined: Path, window, cue_list, dims, fps, out: Path, workdir, codec) -> list[str]     # PURE, last pass
JOIN_CHUNK = {720: 12, 1080: 8, 2160: 3}      # segments per join run; measured 5.4 GB for 20 at 1080x1920
def run_ffmpeg(cmd, expect_seconds, on_progress, cancel: threading.Event, timeout) -> None
def render(cid: int, mode: str, start=None, end=None, codec="hevc") -> dict   # job target for projects.start_job
router  # section 6
```

### `captions.py`

```python
MODEL = "ggml-small.bin"                       # data/models/
@dataclass(frozen=True)
class Line: t0: float; t1: float; text: str                # source-time seconds
@dataclass(frozen=True)
class Cue:  t0: float; t1: float; text: str                # output-time seconds
@dataclass(frozen=True)
class Captions: cues: tuple[Cue, ...]; list_file: Path | None   # ffconcat list, None if no cues

def setup_status() -> dict                                 # {"whisper": path|None, "model": bool, "hint": str}
def ensure_transcripts(uuids, on_progress=None) -> dict[str, list[Line]]
    # cached per (uuid, model). Takes its own media slot per clip. Never call while holding a slot.
def map_cues(tl: Timeline, transcripts: dict[str, list[Line]]) -> list[Cue]    # PURE, section 4
def build(tl: Timeline, style: str, dims: tuple[int, int], cues: list[Cue], workdir: Path,
          window: tuple[float, float] | None) -> Captions
    # draws PNGs, writes cues.txt (ffconcat) relative to workdir, shifts times by -window start
def write_srt(cues, path: Path, shift: float = 0.0) -> None
router  # section 6
```

## 3. Render pipeline

### Job flow (`render.render(cid, mode, ...)`, called from `projects.start_job(cid, mode, render.render, ...)`; job kinds are `"preview"` and `"final"`)

0. **Snapshot.** `edit.snapshot(cid)` gives `(Timeline, Spec)` once; later edits do not affect a running job. Reject over 15 min or 0 clips. Output dims from `edit.output_size`. Check free disk (final: `miss_seconds * 10 MB/s + out_seconds * bitrate * 2 + 2 GB`).
1. **Transcripts** (only if `captions.on`): `captions.ensure_transcripts(video uuids)`, stage "Transcribing 2 of 5". Runs **before** the render slot is taken (each clip takes and releases its own slot), so a render never holds a slot while waiting for another one. A per-clip failure becomes a warning and that clip has no captions; missing whisper or model fails the job with the setup hint.
2. **Sources, only for cache misses, before the render slot.** For each segment compute `seg_key`; if `CACHE/<mode>/<key>.mov` exists it is a hit and its source is never touched (so a re-render after a tweak needs no downloads). For misses call `media.fetch(uuid, mode, tmp)`, which takes `lock_for(uuid)`, then one `video_slot`, does the work (preview: `ensure_proxy`; final: `local_original` or `projects.originals(...)` into `tempfile.mkdtemp(prefix="cs-render-")`), and releases both. Same lock-then-slot order as `analyze`, so a concurrent `analyze_clip` of the same uuid can never deadlock it or build the same proxy twice. All failures are collected and raised together before any encoding: "3 clips could not be fetched from iCloud: <labels>. Finished segments are kept; try again." Same exception style as `projects.do_export`.
3. **Slot.** `media.video_slot("render:<cid>:<mode>", label="Render: <name> (preview)")`; released in `finally`. Held through steps 4 to 6 only (ffmpeg work, no downloads, no other slot).
4. **Segment pass**, sequential, one ffmpeg per miss, writing `<key>.part.mov` then rename. Progress weight = output seconds.
5. **Captions**: `captions.map_cues` then `captions.build` (needs dims and the window).
6. **Join pass**: chunked. Segments are grouped into runs of at most `JOIN_CHUNK[size]` with a boundary only at a cut (a transition at a boundary pulls the next segment into the chunk); each chunk is one ffmpeg writing an intermediate in the segment codec; chunk outputs are then joined the same way (cuts only) until one file remains; a single-chunk edit skips straight to the last pass. Weight = output seconds. Then **finish pass**: window trim, caption overlay, `setparams`, delivery encode (section "Join pass").
7. **Finish**: write `.part.mp4`, rename atomically; write `.srt`; contact sheet; GC the cache if no other render is running; `shutil.rmtree` of the temp dir only. Return the result dict (section 6).

`progress` = finished weight / total weight, `eta_seconds` after 10 percent. `stage` strings: "Transcribing 2 of 5", "Fetching originals", "Rendering clip 3 of 12", "Joining", "Contact sheet". Cancel: `render.CANCEL[(cid, mode)]` event, polled while reading `-progress` lines; set means `proc.kill()`, delete `.part*`, `RuntimeError("Cancelled")`. ffmpeg runs with `os.nice(10)` via `preexec_fn`, `-nostdin`, argument lists only, stderr to a temp file (tail 2000 chars goes into the error), timeout `max(120, 20 * expect_seconds + 60)`.

### Normalization

| Case | Handling |
|---|---|
| VFR iPhone video | Source `fps` comes from ffprobe `r_frame_rate` (nominal), never `avg_frame_rate` (iPhone averages like 2550/91). It is used for: snapping `src_in` to the source frame grid (`floor(t*fps)/fps`, avoids a one-frame sliver of the previous shot) and flagging high-rate sources (>= 100 fps slo-mo, which just play at real time after `fps=N`). Retiming uses the real timestamps: `setpts` first, then `fps=N` resamples by timestamp, so VFR input yields true CFR output |
| Output fps | Integer from the spec (24/25/30/60). Every clip is conformed to it, so segment lengths are exact frame counts (`-frames:v n`). 29.97 sources go to 30 by drop/dup. The Resolve export keeps native rates (issue #5 is separate) |
| Source shorter than metadata says | `tpad=stop_mode=clone:stop_duration=1` before `fps`, then `-frames:v n` cuts to the exact count, so a clip that ends early freezes on its last frame instead of making the join offsets wrong |
| No audio, photos | `anullsrc=r=48000:cl=stereo:d=L` generated in the graph, so every segment has audio of identical length |
| Audio | `atempo` chain for speed (factors within 0.5 to 2, e.g. 4x is `atempo=2.0,atempo=2.0`), `volume`, `aresample=48000`, `aformat=channel_layouts=stereo` (mono up-mixes), `apad=whole_dur=L`, `-t L` |
| Rotation | SDR: autorotate (default) handles the display matrix, crop sees upright frames, and ffmpeg drops the matrix from the output. HDR: `-noautorotate -display_rotation 0` on the input (the second option replaces the matrix with identity so nothing is forwarded to the output) plus software `transpose=2` (rotation 90), `transpose=1` (-90 or 270), `hflip,vflip` (180) after `hwdownload`. `probe_source.rot` is the source of truth; the output must have no rotation side data (tested) |
| Mixed sizes | Crop to the target aspect from `Source.w/h` (computed in Python as even integers, no `ih*9/16` expressions), then `scale=W:H`, then `setsar=1` |
| HDR | `probe_source.hdr` is true when `color_transfer` is `arib-std-b67` or `smpte2084`, **read from the file actually being decoded**, so a proxy is judged by its own tags |
| Stills | `-loop 1 -framerate N -t L -i still.jpg`. Preview source is the indexed preview image (`assets.path`, the same file `/full` serves). Final source is the original; HEIC/HEIF goes through `sips -s format jpeg -Z 4096`, then every still passes through Pillow `ImageOps.exif_transpose` into the temp dir so orientation is baked and ffmpeg never has to guess |

### Segment ffmpeg command (one per cache miss)

Video input options: SDR `-hwaccel videotoolbox -ss <src_in> -t <src_len> -i src` (hardware decode, frames land in system memory, autorotate works). HDR: `-noautorotate -display_rotation 0 -hwaccel videotoolbox -hwaccel_output_format videotoolbox_vld -ss ... -t ... -i src`. Photo: `-loop 1 -framerate N -t L -i still.jpg`.

Video graph, built from numbers only (`W,H,N,S,L` are computed integers/floats, `cx,cy,cw,ch` from the crop math):

```
[0:v:0]
 HDR only:  scale_vt=color_matrix=bt709:color_primaries=bt709:color_transfer=bt709,hwdownload,format=p010le,format=yuv420p,<transpose>
            (8-bit HDR-tagged source: format=nv12 instead of p010le)
 ,setpts=(PTS-STARTPTS)/S
 ,crop=CW:CH:x='(IW-CW)*(A+(B-A)*min(max(t/L,0),1))':y=0        (axis swaps to y when cropping height; constants when mode is center or manual)
 ,scale=W:H:flags=lanczos                                        (preview: bicubic)
 look (only if set):           ,<edit.look_filters(look)>      e.g. eq=contrast=1.08:saturation=1.12,colorbalance=rs=0.06:bs=-0.06  or  curves=preset=vintage,eq=saturation=0.9
 adjust (only if non-default): ,eq=brightness=0.2*E:contrast=C:saturation=SAT,colorbalance=rs=0.15*K:bs=-0.15*K:rm=0.1*K:bm=-0.1*K
 ,tpad=stop_mode=clone:stop_duration=1,fps=N,format=yuv420p,setsar=1
[v]
```

Audio graph:

```
[0:a:0]atempo..,volume=V,aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS,apad=whole_dur=L[a]
or     anullsrc=r=48000:cl=stereo:d=L[a]
```

Output: `-map [v] -map [a] -frames:v n -t L -c:v CODEC -c:a pcm_s16le -f mov`.
`CODEC` final: `prores_videotoolbox -profile:v lt`. Preview: `h264_videotoolbox -b:v 6M`.
The graph goes in `workdir/seg_<key>.txt` and is passed as `-/filter_complex file`. No filtergraph ever contains a path: looks are parameters, captions come in as a separate input. ffmpeg runs with `cwd=workdir`.

### Cache key

`sha1(json.dumps({v: RENDER_V, mode, src: src_id, in, out, speed, volume, adjust, look: (name, strength), reframe, dims, fps, kind}, sort_keys=True))[:20]`. Changing a preset's parameters means bumping `RENDER_V`.
`src_id` is `uuid + proxy mtime_ns + size` for preview, `uuid + original path mtime_ns + size` when local, plain `uuid` for downloaded originals (immutable; osxphotos runs with `--skip-edited`). Captions, transitions and the window are **not** in the key, so toggling captions, changing a transition or previewing a sub-range renders zero segments.

### Join pass (chunked) and finish pass

A join run takes at most `JOIN_CHUNK[size]` segment `.mov` files in order (boundaries only at cuts) and writes one intermediate in the segment codec; runs repeat on the intermediates until one file is left. Window (preview `start`/`end`): only segments overlapping the window are included; the first one's `start` becomes local zero. The finish pass takes that one file plus, if captions are on, `-f concat -safe 0 -i cues.txt`, and does the window trim, the caption overlay, colour tagging and the delivery encode. For a single-chunk edit the join and finish passes are one ffmpeg run.

```
[0:v]fps=N[v0]; [1:v]fps=N[v1]; ...                          every input back to timebase 1/N
cut between acc and seg i:
  [accv][acca][vi][i:a]concat=n=2:v=1:a=1[cv][ca];[cv]fps=N[accv2]   (fps again: concat resets the timebase to 1/1000000)
transition:
  [accv][vi]xfade=transition=wipeleft:duration=D:offset=ACC-D[accv2];[acca][i:a]acrossfade=d=D[acca2]
      ACC = accumulated seconds so far, in whole frames / N; D = tf/N
finish pass only:
window:   [v]trim=start=S:end=E,setpts=PTS-STARTPTS[v1];[a]atrim=start=S:end=E,asetpts=PTS-STARTPTS[a1]
captions: [cap]  = cues.txt input: fps=N,format=rgba
          [v1][cap]overlay=0:0:eof_action=pass[v2]        (never shortest=1: it would end the video when the cue list ends)
finish:   [v2]setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv,format=yuv420p[vout]
```

Encode:
- Final hevc: `-c:v hevc_videotoolbox -b:v {6|12|40}M -tag:v hvc1` for size 720|1080|2160. h264: `{8|16|50}M`. `-c:a aac -b:a 192k -ar 48000 -movflags +faststart`.
- Preview: `-c:v h264_videotoolbox -b:v 3M -c:a aac -b:a 128k -movflags +faststart`.
- Output to `<name>.part.mp4`, renamed after success.

Output locations: preview `data/renders/<cid>/preview.mp4` (overwritten each time, it is ours). Final `~/Movies/Safelight/<slug>/<slug>-YYYYMMDD-HHMMSS.mp4`, never overwriting anything; `out.resolve().parent` is asserted to be the expected folder.

Contact sheet: `media.frame_at` (cv2) on the finished file at the middle of each clip (at most 24, evenly picked), `media.contact_sheet(frames, [(t0, t1)...], RENDERS/<cid>/<mode>-sheet.jpg)`.

Known ceilings, marked in code with `ponytail:` comments: chunked xfade joins re-encode intermediates once per level (upgrade: join pure-cut runs with the concat demuxer and `-c copy`, which is free for all-intra ProRes); no frame interpolation for slow motion (frames repeat); crop only (no blurred-bars "fit" mode).

## 4. Captions

### Install and model (user runs this; I did not install anything)

```
brew install whisper-cpp                       # whisper.cpp 1.9.5 bottle, binary whisper-cli, Metal on arm64
mkdir -p data/models && curl -L -o data/models/ggml-small.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin     # about 466 MB, multilingual
shasum -a 1 data/models/ggml-small.bin         # compare with models/download-ggml-model.sh in the whisper.cpp repo
```

`small` multilingual: iPhone clips mix languages, and small is accurate enough for casual speech while running far faster than realtime on Metal. `base.en` (142 MB) is an English-only fallback; `large-v3-turbo` is the upgrade if small is not good enough. `MODEL` is a constant and the transcript key includes it, so changing it re-transcribes lazily. The engine never downloads anything itself: `captions.setup_status()` returns the exact commands above as `hint`, and `set_edit(captions=true)` / render return it as the error when something is missing. Locate the binary with `shutil.which("whisper-cli")` then `/opt/homebrew/bin/whisper-cli`.

### Transcription (once per clip, cached)

1. Audio: `data/audio/<uuid>.opus` if present. Else if a proxy exists, `index.extract_audio(proxy, uuid)` (older proxies have no opus; extracting from the AAC proxy is fine for speech). Else `media.ensure_proxy(uuid)` (downloads from iCloud, builds both). If still no opus afterwards, status `no_audio`.
2. Silence guard: `ffmpeg -af volumedetect`; mean volume under -45 dB means status `silent` (whisper hallucinates "Thank you." on wind and music).
3. Convert to a temp wav: `ffmpeg -nostdin -i x.opus -ar 16000 -ac 1 -c:a pcm_s16le tmp/a.wav`.
4. `whisper-cli -m data/models/ggml-small.bin -f tmp/a.wav -l auto -oj -of tmp/out -sns -t 4 -np` (arguments as a list, `os.nice(10)`, timeout `max(120, 2 * duration)`). If the process fails with a Metal error, retry once with `-ng`. Parse `tmp/out.json`: `transcription[].offsets.from/to` (ms) and `text`, `result.language`.
5. Clean: strip, drop empty lines and pure tags (`[MUSIC]`, `(applause)`, lines of only music notes), collapse three or more identical consecutive lines.
6. Store. Hold `media.lock_for(uuid)` (the same lock `analyze` uses, taken in the same order: lock then slot) and one `media.video_slot` for the whole step.

### Tables (created by `captions.SCHEMA`, same lazy pattern)

```sql
create table if not exists transcripts(
  uuid text not null, model text not null, lang text, status text not null
    check(status in ('ok','no_audio','silent')), created real,
  primary key(uuid, model));
create table if not exists transcript_lines(
  uuid text not null, model text not null, idx integer not null,
  t0 real not null, t1 real not null, text text not null,
  primary key(uuid, model, idx));
```

Failures are not cached (retry next time). `transcript_lines` is shaped so P4 speech search can later index it into `assets_fts` or embed it, with no migration. `GET /media/{uuid}/transcript` exposes it.

### Timing math (`map_cues`, pure)

For a clip with source window `[a, b]` = `[src_in, src_out]`, speed `v`, output start `s` seconds = `start_frame / fps`, end `e = s + frames / fps`, and a transcript line `(l0, l1)`:

- `overlap = min(l1, b) - max(l0, a)`. Keep the line if `overlap >= 0.5 * (l1 - l0)` and `overlap >= 0.3`. Otherwise drop it. (Ceiling: a line cut mid-sentence keeps its full text. Upgrade path is word timestamps with `-ml 1`; deliberately not built.)
- `c0 = max(l0, a)`, `c1 = min(l1, b)`.
- `out0 = s + (c0 - a) / v`, `out1 = min(e, s + (c1 - a) / v)`.

Worked example: in 4, out 10, speed 2, `s = 12.0`. Line (6, 8) becomes (13.0, 14.0). Line (3, 5): overlap 1 of 2, kept, c0 = 4, becomes (12.0, 12.5). Line (9.5, 12): overlap 0.5 of 2.5, dropped.

Then globally: sort by `out0`; where a cue overlaps the next (the outgoing clip's tail inside a crossfade), set the earlier `out1 = later.out0`; drop anything under 0.25 s. Long cues are wrapped by Pillow measuring (max width 86 percent of the frame, max 2 lines); a cue needing more than 2 lines is split into consecutive cues with durations proportional to character count.

### Overlay approach and style

`build()` draws one **full-frame RGBA PNG per cue** (frame size = render dims, mostly transparent, so each is a few KB) plus one blank PNG, named by sha1 of text and style, and writes an `ffconcat` list: for every entry a `file` line, an `option framerate N` line (without it the image demuxer uses a 1/25 s timebase and cues start up to one frame late) and a `duration` line; cue times are snapped to the output frame grid first; blank PNG in the gaps; the list ends with a blank whose duration reaches the timeline end, then the blank repeated once (the concat demuxer ignores the final duration). Render adds it as a single input and applies one `overlay=eof_action=pass` (section 3). Verified frame-exact at 30 fps and scales to any cue count, unlike one input per cue. Preview draws at preview size, so the text is sharp at that size.

- Font: `/System/Library/Fonts/Supplemental/Arial Bold.ttf` for Latin; `/Library/Fonts/Arial Unicode.ttf` when the text has characters above U+024F (CJK etc.). Both exist here. No complex-script shaping (no raqm).
- Size `0.055 * min(W, H)`. Bottom edge at 0.80 H (portrait, square, 4:5) or 0.92 H (16:9), leaving room for app UI on vertical video.
- `bold`: white fill, black stroke (`stroke_width = size // 14`). `box`: white text on a rounded rectangle, black at 55 percent alpha.
- No fades or per-word highlighting (out of scope).

### SRT

`write_srt(cues, path)`: standard numbered blocks, `HH:MM:SS,mmm --> HH:MM:SS,mmm`, UTF-8, text unwrapped. Times are output-timeline times, minus the window start for window previews. Written beside the render (`preview.srt`, or `<slug>-<stamp>.srt` next to the final), whenever captions are on.

## 5. MCP tools (seven new, 21 total)

All wrap engine HTTP routes via the existing `call()`. Mutating tools return `{... , "clamped": [...]}` when something was clamped. Item ids come from `get_collection`. `READ_ONLY` = `ToolAnnotations(readOnlyHint=True)` so Claude Code can run them in parallel.

| Tool | Hint | Args | Docstring (what the model sees) |
|---|---|---|---|
| `get_edit` | read-only | `collection_id` | "The project's edit: format, look, per-clip speed/adjust/look/reframe, transitions, captions state, where each clip starts on the output timeline, total length, warnings, and the allowed options (looks, aspects, transition types). Call it before changing anything and after, to check the length." Also returns `edit_hash` and captions readiness (`ready`, `pending`, `no_speech` clip counts) |
| `set_edit` | | `collection_id`, `aspect=""` (16:9, 9:16, 1:1, 4:5), `size=0` (720, 1080, 2160), `fps=0` (24, 25, 30, 60), `look=""` (name from get_edit, or "none"), `look_strength=-1`, `captions=None` (true/false), `caption_style=""` (bold, box) | "Project-wide settings. Unset arguments keep their value. Use 9:16 for reels and stories; clips are center-cropped to fit (set_clip_fx reframe moves the crop). One look for the whole edit, strength 0.6 to 0.8 unless the user wants it strong. captions=true burns the spoken words in and writes an .srt next to the video: it transcribes each clip's audio once (needs whisper.cpp; the error says how to install it); silent clips and music get no captions. Check get_edit captions.pending before rendering." Turning captions on starts the transcription job and returns at once |
| `set_clip_fx` | | `collection_id`, `item_id`, optional: `speed` (0.25 to 4), `volume` (0 to 2), `exposure`, `contrast`, `saturation`, `warmth`, `look` (name, or "none"), `look_strength`, `reframe_mode` (center, manual, pan), `reframe_pos`, `reframe_from`, `reframe_to` | "Change one clip's look and motion. Only the arguments you pass change; `speed` also scales its audio. Photos cannot change speed. Reframe positions are 0 to 1 along the axis being cropped." |
| `set_transition` | | `collection_id`, `after_item_id`, `type` (cut, dissolve, fade-black, wipe-left, wipe-right, slide-left), `duration=0.5` | "Join a clip to the next one. Duration 0.2 to 2 s, capped at half the shorter clip; transitions shorten the edit by their duration. Use 'cut' to remove one." |
| `render_preview` | | `collection_id`, `start=0`, `end=0` | "Render a quick low-resolution preview from the playable copies. Optional start/end (seconds on the output timeline) render only that part. Returns at once; poll render_status." |
| `render_final` | | `collection_id`, `codec="hevc"` (hevc, h264) | "Render the finished video from the original files (downloads iCloud originals, can take minutes). Only when the user asked for the finished video. Output goes under ~/Movies/Safelight. Poll render_status." |
| `render_status` | read-only | `collection_id`, `mode="preview"`, `wait_seconds=0` (0 to 90) | "State of the last preview or final render: stage, progress, output path, .srt path, warnings, error. When done it also returns a contact sheet image of the result: look at it and fix what you see (cuts, crop, color, caption position). `wait_seconds` waits for it to finish, so you do not need to poll in a tight loop." Implemented client-side: poll the engine every 1 s up to `wait_seconds`, then fetch the sheet bytes and return `[dict, Image(...)]` like `analyze_clip` |

Existing `export_project` stays (Resolve route). `projects.STEP_LABELS` gets labels for the seven ("Changing the edit settings", "Rendering a preview", and so on). Review: nine was over the research's "about 12 new" line only slightly, but `set_format`, `apply_look` and `set_captions` were three tools for one JSON object; one `set_edit` with optional arguments is the same validation and one fewer decision for the model.

### SKILL.md changes

- Add step **5. Polish (only if the brief or user asks for it; a plain cut needs none of this)**: one `set_edit` call first (9:16 for a reel, story or "vertical"; one look for the whole edit at `look_strength` 0.6 to 0.8 unless the user wants it strong; `captions=true` only if clips contain speech, which `get_edit` shows, never on a montage with music), then `set_clip_fx` only for a specific reason (a slow-mo moment with `speed` 0.5, a boring stretch at 1.5 to 2, a different look on one clip), `set_transition` sparingly (dissolve 0.4 to 0.6 s between scenes of different places, no transition inside one scene, never wipes on a calm edit).
- Add step **6. Check**: `render_preview` (use `start`/`end` for one section), `render_status(wait_seconds=60)`, look at the contact sheet, fix, repeat at most 3 times. Never `render_final` unless the user asked for the finished video; then tell them the path and the .srt.
- Step 5 "Finish" becomes: finished video means `render_final`; Resolve means `export_project`; no preference means offer both in one line.
- Rules: never describe ffmpeg or filters to the user; tool errors list allowed values, so fix the argument instead of retrying the same one; transitions shorten the total, recheck length with `get_edit`.
- `projects.SYSTEM` line "The app has an Export button" becomes "The app has Preview, Render video and Export buttons".

## 6. Engine endpoints and app UI

### Endpoints

| Route | Owner | Notes |
|---|---|---|
| `GET /collections/{cid}/edit` | edit | `{spec, rev, edit_hash, duration, clips:[{item_id, uuid, start, end, frames}], warnings, options:{looks, aspects, sizes, fps, transitions, caption_styles}}` |
| `PATCH /collections/{cid}/edit` | edit | body `{aspect?, size?, fps?, look?, look_strength?, captions?, caption_style?}`; `look: null` clears the project look; when `captions` flips to true the edit router calls `captions.start_transcription(cid)` (C's function, stubbed in A's tests) |
| `PATCH /collections/{cid}/edit/clips/{item_id}` | edit | body fields as `set_clip_fx`; explicit `null` clears a field (same `model_fields_set` trick as `library.update_item`) |
| `PUT` / `DELETE /collections/{cid}/edit/transitions/{item_id}` | edit | body `{type, duration}`; DELETE = cut |
| `GET /collections/{cid}/captions` | captions | `{on, style, ready, pending, no_speech, setup:{whisper, model, hint}}` |
| `POST /collections/{cid}/captions/transcribe` | captions | starts job kind `captions` for the project's pending clips; 202; 409 if running. `set_edit(captions=true)` reaches it through `edit`; the app's checkbox calls `PATCH /edit` too |
| `GET /collections/{cid}/captions/job` | captions | `projects.job(cid, "captions")` |
| `GET /media/{uuid}/transcript` | captions | cached lines or 404 |
| `POST /collections/{cid}/render` | render | body `{mode: "preview"\|"final", start?, end?, codec?}`; 202; 409 if that mode is already running for the project |
| `GET /collections/{cid}/render/{mode}` | render | job status (below) |
| `POST /collections/{cid}/render/{mode}/cancel` | render | 204 |
| `GET /collections/{cid}/render/{mode}/file` | render | `FileResponse` with Range, only the exact path the job recorded, under `RENDERS` or `EXPORTS` |
| `GET /collections/{cid}/render/{mode}/sheet` | render | contact sheet JPEG |
| `POST /collections/{cid}/render/{mode}/reveal` | render | `open -R <path>`; `?play=1` runs `open <path>` instead |

Job status (a snapshot via `projects.job`): `{status: idle|running|done|failed, started, stage, progress, eta_seconds, note}`; when done also `{path, folder, seconds, srt, sheet, edit_hash, rendered, cached, warnings, message}`; when failed `{error}`. The integrator adds `render/{mode}$` and `captions/job$` GET patterns to `server.QuietPolls`.

### App (`app/src/`)

- `api.ts`: extend `Job` with `progress?`, `stage?`, `eta_seconds?`, `sheet?`, `srt?`, `edit_hash?`, `warnings?`. Add `type Captions`.
- `main.ts`, `renderProject`: below the existing `proj-meta` row (clip count, length, Export button) add one row: a **Captions** checkbox, a **Preview** button, a **Render video** button, and a `render-out` block (class `ask-out`, so existing styling applies). Generalize `pollJob(cid, kind, ...)` so `kind` may be `preview` or `final`; the poll URL is `/collections/${cid}/render/${kind}` for those and `/collections/${cid}/${kind}` for ask and export. While running: a `<progress>` element plus "Rendering clip 3 of 12 - 42 percent" and a Cancel button (replaces the render button). Done, preview: an inline `<video controls src=".../render/preview/file?v=<edit_hash>">` under the row. Done, final: "Rendered video, 38 s. **Play** · **Show in Finder**" (the reveal route with and without `play=1`), plus a note when `srt` is set. Failed: the engine's message, as for export. A project opened mid-render picks the job up (same check the code does for export). Engine restarted mid-job: existing "stopped because the engine restarted" text, with the kind word adjusted.
- Captions checkbox: `GET /collections/{id}/captions` for state, `PATCH /collections/{id}/edit {captions: bool}` to change it. If `setup.hint` is non-empty and the user turns it on, show the hint in `render-out`. While the captions job runs show "Transcribing 2 of 5".
- Not in the app: format, look, speed and transition editing. Claude does that through tools; the app just renders. No stale-preview badge.

## 7. Concurrency

- **One shared pool.** Render and transcription use `media.video_slot`, the same counter that caps iCloud downloads and clip analysis at `settings.max_agents`. They do not use `projects.agent_slot` (that one is for `claude -p` runs; a render started by an Ask run must not wait behind its own parent).
- `media.video_slot(key, label=None)` and `media.stage(key, name, label=None)` get an optional label so the activity panel shows "Render: Halloween (preview) - rendering clip 3 of 12" instead of "video" (today `label_for` looks the key up in `assets`). Existing calls are unchanged.
- **No nested slots, one lock order.** Every per-uuid fetch (proxy, original, transcript) is `media.lock_for(uuid)` then `media.video_slot` then release, exactly like `analyze`, and all of them run **before** the render takes its own slot. The render slot covers ffmpeg only (steps 4 to 6) and nothing inside it waits on a lock or a slot. Review: the earlier order (slot first, then `ensure_proxy` inside it) could deadlock against `analyze_clip` at `max_agents=1` and could build one proxy twice.
- Segments within a job are sequential (VideoToolbox sessions and disk are the bottleneck). Different jobs run in parallel up to `max_agents`, each holding one slot. A final render holds a slot for minutes; with `max_agents=2` an Ask analyzing clips waits and shows "waiting for a free slot". Accepted.
- One job per `(collection, mode)`: `projects.start_job` already returns 409 for a duplicate. Preview and final of the same project may overlap.
- **GPU sharing with the indexer** (separate process, torch on the GPU): ffmpeg's VideoToolbox encode and decode use the media engines, not the shader cores, and `eq`, `colorbalance`, `curves`, `crop`, `scale`, `overlay` run on CPU. Only whisper (Metal) and `scale_vt` (HDR clips) touch the GPU. All ffmpeg and whisper children run at `nice 10`, whisper uses 4 threads, and at most one whisper runs at a time (module lock, on top of the slot). Measure indexer throughput during a transcription once (open question 3); the fallback is `-ng` (CPU whisper).
- Cache GC runs only when no render job is running (module counter), so a running join never loses a segment.
- Engine restart kills jobs (status `idle` after, as today). Cached segments and transcripts survive, so rerunning is cheap.
- The new tables are created with `create table if not exists` on the live WAL database; no schema change touches existing tables, so the running indexer is unaffected. Nothing here needs the engine restarted before merge; the user restarts it in iTerm when ready.

## 8. Test plan

Plain assert scripts, `uv run test_x.py`, lavfi media, no network, no Photos access. **Data safety, non-negotiable:** the first lines of every test set `os.environ["CS_DB"]` to a temp path **before** `import core` (`core.connect(path=DB_PATH)` binds the default at import; a test that only reassigns `core.DB_PATH` later writes to the real `data/index.db`, which happened once already), then `assert "data/index.db" not in str(core.DB_PATH)`. Module path constants bound at import must be reassigned after import, like `test_media.py` does: `media.PROXIES`, `media.SHEETS`, `core.AUDIO_DIR`, `render.CACHE`, `render.RENDERS`, `render.EXPORTS` (point the final-render folder at a temp dir too), `settings.FILE`. Reassigning `core.DATA` alone changes nothing already bound. Fixtures with a display matrix are remuxed (`ffmpeg -display_rotation 90 -i raw.mp4 -c copy rotated.mp4`): in ffmpeg 9 `-display_rotation` is an input option.

**`test_edit.py`** (no ffmpeg)
- Defaults for a new project; non-project collection gets 422.
- Clamps return `clamped` entries (speed 9 becomes 4); bad enum and unknown key return 422 with the allowed list; photo with speed 2 returns 422.
- Sparse storage: setting a field back to default removes the entry; item removal prunes orphans on load.
- `mutate` under 8 threads patching different clips: all changes land, `rev` equals the number of mutations.
- Cascade: deleting the collection deletes the spec.
- `compute_timeline`: three clips with speed 2 and 0.5, a 0.5 s dissolve and a cut give exact `start_frame` and `total_frames`; a 2 s transition on 1 s clips is capped with a warning; photo defaults to 3 s; `d_in + d_out` cap; `edit_hash` changes with an in/out edit and not with a no-op patch.
- `output_size` for all aspects and sizes (even numbers, 1080x1920 for 9:16 at 1080, preview short side 540); `look_filters` for every preset at strength 1 and 0.5 is a string of only `[a-z0-9_=:.,-]` and `look_filters(None) == ""`; strength 0 gives identity parameters.

**`test_render.py`** (ffmpeg, VideoToolbox; about a minute)
- Media made with lavfi: A 1280x720 30 fps 4 s with sine audio; B smptebars at 30000/1001, no audio; C phone-style VFR (the `select` trick from `test_settings.py`) with `-display_rotation 90` so it is portrait; D 10-bit HLG HEVC (libx265, bt2020, arib-std-b67) with a display rotation; E a red 4032x3024 JPEG still. Rows go into the temp db with `path` set, proxies are copies placed in `media.PROXIES`.
- Pure command checks: `seg_command` for D contains `-noautorotate`, `-display_rotation 0`, `scale_vt`, `hwdownload,format=p010le`, a `transpose`; for SDR contains none of them; for B contains `anullsrc`; the filter text contains only characters from the allowed set (digits, labels, known filter names, no `/`), for every preset.
- Preview then final of a 5-clip 9:16 project (cut, dissolve, wipe-left, one 2x clip, one still): output exists; ffprobe duration equals `Timeline.duration` within one frame; dims 540x960 (preview) and 1080x1920 (final); constant 30 fps (`r_frame_rate` 30/1); exactly one video and one audio stream; audio present although B and E have none.
- Orientation: frame 0 of the C and D segments has `h > w` after crop to 9:16 and, for a horizontally split test pattern, the left-right order is not mirrored. **`ffprobe -show_entries stream_side_data=rotation` on every segment and on the final output is empty** (the bug the review found).
- Colour: HLG red bar from D stays red in the output (R channel larger than G and B at a fixed pixel).
- Cache: second identical render runs zero segment ffmpegs (counter `render.SEG_RUNS`); changing one clip's speed runs exactly one; toggling captions and changing a transition run zero.
- Chunked join: 20 one-second segments with a dissolve every third cut and `JOIN_CHUNK` patched to 4: output duration equals the timeline within one frame, every chunk boundary fell on a cut (inspect the recorded commands), `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` stays under 2 GB at 540x960.
- Captions overlay (fake `cues.txt` with two coloured full-frame PNGs at 0.5 to 1.5 s and 1.8 to 2.8 s at 30 fps): sampled centre pixel is the cue colour on exactly frames 15 to 44 and 54 to 83 and nowhere else, and the output keeps its full length when the list ends early.
- Source shorter than its recorded duration (db says 5 s, file is 4 s): output duration still equals the timeline.
- Window render (`start=1, end=3`): duration 2 s.
- Progress callback values are non-decreasing and end at 1.0; cancel mid-render raises `RuntimeError("Cancelled")` and leaves no `.part*` files.
- Paths: final lands in the expected folder; an existing file with the same name is never replaced; `GET /file` refuses any path that is not the recorded one.
- Slots: with `settings.max_agents = 1`, two renders on threads never overlap (sample `media.RUNNING` inside a patched `run_ffmpeg`, assert max is 1) and the second shows a waiting note.
- Contact sheet exists and is a JPEG.
- Captions stubbed with a fake `captions.build` returning a prebuilt `cues.txt` (the real one is covered in `test_captions.py`).

**`test_captions.py`** (no whisper needed for most)
- `map_cues` on hand-built `Timeline` objects: the worked example in section 4 gives exactly (13.0, 14.0), (12.0, 12.5), and drops the third line; speed 0.5 stretches; overlapping tails are truncated; under 0.25 s dropped; two clips from the same uuid with different ranges both get their lines.
- `write_srt` output equals an exact expected string including milliseconds and the window shift.
- `build`: PNG sizes equal the frame size, non-blank where the text sits, `cues.txt` durations sum to the timeline length, a CJK line picks the Unicode font, a long line wraps to at most 2 lines.
- Transcription with a **fake `whisper-cli`** (shell script written to a temp dir that records its argv and writes JSON in whisper's format): assert the exact argument list (`-m`, `-l auto`, `-oj`, `-sns`, no shell), wav input, parsing, tag lines and 3x repeats removed, row counts in both tables, second call does not run the binary, `silent` status for a quiet lavfi sine (no binary call), `no_audio` for a video without audio, the `-ng` retry path.
- `setup_status` reports a missing binary and a missing model with the install commands in `hint`.
- Optional, skipped unless `whisper-cli` and the model exist: `say -o x.aiff "hello world"` converted to a clip, real transcription contains "hello". Run once by hand after install.

Existing suites (`test_library.py`, `test_media.py`, `test_settings.py`) must still pass at every merge step. `test_media.py` needs network (its existing cat video download), so it is run by hand at the end, not by implementers.

## 9. Work split

Review: the authoritative split, merge order and integrator checklist now live in `notes/build-plan.md`, which also covers the smart-album implementer (D) and the shared `main.ts` hunks. The text below is kept for the interface points; where the two disagree, `build-plan.md` wins.

### Step 0 (me, before anyone starts): interfaces commit on `main`

`engine/timeline.py` exactly as in section 2 (dataclasses only, no logic), plus this document. All three branches start from it, so nobody imports a module that does not exist yet. Contracts every implementer codes against: section 2 signatures, section 6 routes and job shape, `media` changes below, and `captions.Captions`/`Cue`/`Line`.

### Three parallel worktrees, no shared files

| | Branch | Owns (only these files) | Depends on at test time |
|---|---|---|---|
| **A: spec** | `edit/spec` | `engine/edit.py` (spec, mutation, timeline math, `LOOKS`, `look_filters`, edit routes), `engine/test_edit.py` | `timeline.py`, the `media.py` helpers from the contract commit |
| **B: render** | `edit/render` | `engine/render.py`, `engine/test_render.py` | `timeline.py`, `media.fetch` / `video_slot(key, label)` from the contract commit; stubs `edit.snapshot`, `edit.output_size`, `edit.look_filters` and `captions` with fakes until merged |
| **C: captions + surface** | `edit/captions` | `engine/captions.py`, `engine/test_captions.py`, `engine/mcp_server.py`, `engine/projects.py` (labels and `SYSTEM` line only), `skills/safelight-editor/SKILL.md`, `app/src/api.ts`, `main.ts`, `styles.css` | `timeline.py`; calls to edit and render routes go through HTTP or `call()`, so nothing to import except `edit.mutate` (stub in tests) |

Nobody edits `server.py`; the integrator does.

Exact interface points between them:

- **B calls A:** `edit.snapshot(cid) -> (Timeline, Spec)`, `edit.output_size(aspect, size, preview) -> (w, h)`, `edit.look_filters(look) -> str`; **B calls media (contract commit):** `media.fetch(uuid, mode, tmp) -> Path`, `media.video_slot(key, label=None)`, `media.release_video_slot()`, `media.stage(key, name, label=None)`, `media.contact_sheet`, `media.frame_at`.
- **B calls C:** `captions.ensure_transcripts(uuids, on_progress)`, `captions.map_cues(tl, transcripts)`, `captions.build(tl, style, dims, cues, workdir, window) -> Captions`, `captions.write_srt(cues, path, shift)`, and reads `Captions.list_file` / `.cues`. Only when `spec.captions.on`.
- **C calls A:** nothing at import time. **A calls C:** `captions.start_transcription(cid)` when `PATCH /edit` turns captions on (A imports it lazily inside the route and its tests stub it). The MCP tools call A's and B's HTTP routes with the argument names in section 5.
- **C's app code** needs B's job shape (section 6).

### Merge order

1. Step 0 commit (types, doc).
2. **A** (root dependency: `edit`, `media` helpers, looks). Run `test_edit.py`, `test_library.py`, `test_settings.py`.
3. **C** captions module and tests, then **B** render (either order; each only needs A merged). After the second one, the full integration check below becomes possible.
4. **Integration commit** (me): `import captions, edit, render` and three `app.include_router` lines in `server.py`; QuietPolls patterns; delete stubs left in tests if any. Run all three new suites plus the three existing ones, then boot the engine on a spare port with a temp `CS_DB` (not the iTerm one) and drive one preview through curl.
5. C's MCP/SKILL/app changes merge with C in step 3; `npm run build` in `app/` (typecheck) is part of C's done criteria.
6. By hand, not delegated: the real-footage check (open question 1), `brew install whisper-cpp` and a real transcription, then restart the engine in iTerm.

Rough sizes: A about 300 lines plus tests (no LUT files), B about 500 plus tests, C about 450 plus app and skill text.

## 10. Open questions and risks

Questions for the user:
1. **Real footage check before trusting HDR.** Needs one real iPhone HLG clip, ideally portrait, ideally also as a proxy: render preview and final, compare with the original in QuickTime. Risks: FFmpeg trac #10884 (red turning yellow with `scale_vt`), and I did not reproduce the portrait fix on real Dolby Vision profile 8.4 clips. Fallback is the tap build with `zscale` (research section 3).
2. **Default aspect.** I chose `16:9`. A reel-first user may prefer `9:16`; the skill sets it from the brief either way.
3. **Whisper on the GPU while the indexer runs.** I assumed short Metal bursts are fine. If indexing slows noticeably during a transcription, add a `whisper_gpu` setting or force `-ng`.
4. **Model.** `small` multilingual (466 MB) is the default. Say so if the clips are English-only (`base.en` is smaller) or accuracy disappoints (`large-v3-turbo`).
5. **Tone-map proxies at creation?** `media.ensure_proxy` scales HLG with plain `scale` and keeps whatever tags ffmpeg copies. The renderer judges each file by its own tags, so previews work either way, but in-app playback of HDR proxies may look flat. A separate small fix in `media.py`, not part of this plan.

Risks:
- **Rotation on the HDR path** (found here): VideoToolbox frames skip autorotate silently. Covered by `-noautorotate` + `transpose` and a test, but only on a synthetic clip.
- **Slot starvation:** a long final render occupies one of `max_agents` slots. Mitigation if it hurts: let `render` use a separate counter.
- **Disk:** ProRes LT intermediates measured at about 10 MB/s of 1080x1920 output; a 5 minute edit is about 3 GB, capped by the 20 GB cache limit and a pre-flight free-space check. Preview cache cap 3 GB.
- **iCloud originals for final:** same failure modes as export (PhotoKit timeouts, no Photos permission). Render fails fast with the list; segments already finished are cached, so retry is cheap. Final from proxies as a fallback is not built.
- **Whisper quality:** hallucination on quiet or musical audio (guards: volume check, `-sns`, tag stripping, repeat collapse, 0.5 overlap rule) and mid-sentence trims (line-level granularity).
- **Complex scripts:** no Devanagari or Arabic shaping without raqm; Latin and CJK are fine. Check with a real Hindi clip if relevant; the fix would be a `ponytail:`-marked change to render via a different library.
- **Stills:** the HEIC path (`sips`, then Pillow orientation bake) is untested on a real HEIC. Preview uses the indexed preview image, which is JPEG.
- **Join memory:** measured 5.4 GB for 20 segments at 1080x1920 in one xfade chain, hence `JOIN_CHUNK`. Each extra chunk level re-encodes the intermediate once (ProRes LT, visually lossless). If it shows, join pure-cut runs with the concat demuxer and `-c copy`.
- **Colour tags:** `-color_trc` is ignored by `hevc_videotoolbox` here; `setparams` fixes it (tested). Players that ignore tags assume bt709 anyway.
- **Slow motion:** no interpolation, frames repeat below 1x; `atempo` below 0.5 or above 2 is chained and sounds rough at 4x.
- **Tool count:** seven new tools (21 total), each one small typed operation.
- **Taste:** Claude can only judge a render through the contact sheet (one frame per clip). It cannot see captions timing or transition feel; the `start`/`end` preview and the user's own review cover that.
