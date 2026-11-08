# CLI video editing for Safelight: research and recommendation

Date: 2026-11-08. Scope: let Claude make real edits (looks, transitions, speed, titles, music and beat sync, stabilization, vertical reframing) and render a finished video with open-source CLI tools, exposed as MCP tools. Research only, no app or engine code changed.

## TL;DR

1. **Engine: plain ffmpeg, driven from Python.** It is already installed (Homebrew ffmpeg 9.0.2) and already used by `media.py`. Everything in Phase 1 was verified on this machine (see "What I tested").
2. **Architecture: Claude edits a declarative edit spec through validated MCP tools. It never writes ffmpeg commands.** A Python compiler (`render.py`) turns the spec into ffmpeg runs: per-clip normalized segments (cached), then one join pass. The same spec renders a cheap preview from the 540p proxies and the final from originals. It also still exports OTIO and FCP XML for Resolve.
3. **Style references (section 8):** extract a measurable style profile from an inspiration video (TransNetV2 shots and transition types, beat alignment, camera motion, OCR, color statistics baked into a `.cube`) and let Claude judge the rest from a contact sheet. Tested on a synthetic reference.
4. **First phase (about 1 day):** spec + renderer for cuts, crossfade transitions, speed, looks (eq + bundled .cube LUTs), HDR to SDR, 9:16/1:1/16:9 with center or manual crop, preview and final render jobs, and 7 MCP tools.

## What is installed here

| Tool | Status |
|---|---|
| ffmpeg / ffprobe | 9.0.2, Homebrew core bottle, GPL-3.0 build |
| VideoToolbox | `h264_videotoolbox`, `hevc_videotoolbox`, `prores_videotoolbox` encoders; `scale_vt` filter |
| Filters present | `xfade`, `acrossfade`, `lut3d`, `haldclut`, `eq`, `curves`, `colorbalance`, `colorspace`, `tonemap`, `zoompan`, `deshake`, `sidechaincompress`, `loudnorm`, `crop`, `overlay`, `fade` |
| Filters MISSING | `drawtext` (no freetype), `subtitles`/`ass` (no libass), `zscale` (no zimg), `libplacebo`, `vidstab` |
| melt (MLT) | not installed (`brew install mlt` gives melt 7.42) |
| Pillow, OpenTimelineIO, OpenCV | in the engine venv already |
| librosa, aubio | not installed (librosa works via `uv run --with librosa`) |

The missing filters matter. The Homebrew core formula dropped most optional libraries. Two ways around it:

- **Use what we have (recommended for Phase 1 and 2):** titles as Pillow-rendered PNGs composited with `overlay` (also avoids all filtergraph text escaping), HDR to SDR with `scale_vt`, stabilization with `deshake`.
- **Swap to the tap build later (Phase 3, optional):** `brew uninstall ffmpeg && brew tap homebrew-ffmpeg/ffmpeg && brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-zimg --with-libvidstab` adds `zscale` and `vidstab`; freetype, fontconfig and libass are on by default. `--with-libplacebo` exists but needs Vulkan via MoltenVK on macOS; skip it. Builds from source, about 10 to 20 minutes. The engine's proxy code uses the same `ffmpeg` binary, so re-run `test_media.py` after the swap.

## What I tested (generated lavfi clips, scratch dir, nothing in data/)

| Test | Command core | Result |
|---|---|---|
| Crossfade video + audio | `xfade=transition=smoothleft:duration=1:offset=3`, `acrossfade=d=1`, `h264_videotoolbox` | OK, 4 s + 4 s gives 7.0 s, frame shows mid-transition |
| LUT + grade | `lut3d=file=warm.cube:interp=tetrahedral,eq=contrast=1.05:saturation=1.1,curves=preset=medium_contrast` | OK |
| 2x speed | `setpts=PTS/2`, `atempo=2` | OK, 4 s to 2.07 s |
| 9:16 reframe with pan | `crop=ih*9/16:ih:x='(iw-ih*9/16)*t/4':y=0,scale=1080:1920` | OK, 1080x1920 |
| Ken Burns on a still | `zoompan=z='min(zoom+0.0015,1.3)':d=120:s=1920x1080:fps=30` | OK |
| Stabilize | `deshake` | OK (weaker than vidstab, no two-pass analysis) |
| Music ducking | `sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300`, `amix`, `loudnorm=I=-14:TP=-1.5` | OK |
| Title | Pillow PNG (Helvetica, stroke) + `fade=...:alpha=1` + `overlay` | OK, verified visually |
| HDR (HLG, 10-bit HEVC, bt2020) to SDR | `-hwaccel videotoolbox -hwaccel_output_format videotoolbox_vld`, `scale_vt=w=1280:h=720:color_matrix=bt709:color_primaries=bt709:color_transfer=bt709` | OK, output tagged bt709 yuv420p; red bars stay red |
| Same, then CPU filters | `...,hwdownload,format=p010le,format=yuv420p,lut3d=...` | OK (note: `hwdownload,format=nv12` fails, use p010le) |
| Software `tonemap=hable` without zscale | runs, but wrong: tonemap needs linear-light input, which only zscale provides | Do not use |
| Beat detection | `librosa.beat.beat_track` on a synthetic 120 BPM click | 117 BPM, beats every 0.5 s; first run 33 s (install + numba JIT) |
| OTIO to FCP XML | `Transition(SMPTE_Dissolve)` + `LinearTimeWarp(2.0)` | Dissolve exports as Cross Dissolve. **Speed is silently dropped** by the fcp_xml adapter |

The synthetic HLG clip is not real iPhone footage. Phase 1 must include one visual check on a real iPhone HLG clip (see Risks).

## 1. Candidate engines

| Engine | How you drive it | License | Install | Maturity / speed | Verdict |
|---|---|---|---|---|---|
| **ffmpeg** | CLI filtergraphs from Python `subprocess` | LGPL core; this build is GPL-3 | brew (installed) | The standard. VideoToolbox encode is fast. Filtergraphs are expressive but easy to get subtly wrong (timebases, offsets, matching formats for xfade) | **Use.** Hide it behind a compiler |
| MLT / melt | XML project (`.mlt`) rendered by `melt`; Python via XML | LGPL core, some modules GPL | `brew install mlt` (7.42) | Mature (Shotcut, Kdenlive backend). Big transition library, keyframes, proper timeline model. Second engine to learn and debug; macOS headless needs care | Second choice. Worth it only if we want Shotcut-openable projects |
| MoviePy 2 | Python, frame by frame through numpy | MIT | pip | Fine for small jobs; slow (decodes every frame into Python); v2 broke the v1 API | No. Too slow for 4K iPhone footage |
| PyAV | Python bindings to libav* | BSD | pip | Excellent for decode/encode and frame access; not a compositor | Only for analysis (frame sampling for reframing) |
| GStreamer / GES | `ges-launch-1.0`, PyGObject | LGPL | brew | Real NLE library, poor ergonomics on macOS, sparse docs | No |
| Remotion | React compositions, renders via headless Chrome | Remotion License (free for individuals and companies up to 3 people) | npm | Best-looking titles and motion graphics; heavy (Node + Chromium); slow for long 4K video | Later, only for fancy title cards rendered as overlay clips |
| Motion Canvas | TypeScript scenes, browser editor | MIT | npm | Built for explainer animation, not footage editing | No |
| editly | Declarative JSON5 spec over ffmpeg + headless-gl | MIT | npm | 5.6k stars; gl-transitions, titles, audio norm. Closest prior art to what we want, but Node + headless-gl on Apple Silicon is fragile | Borrow the spec ideas, not the code |
| auto-editor | CLI; cuts silence/motion, exports to Resolve/FCP XML | Unlicense | pip | Mature for talking-head trimming | Not needed; our scene detection already finds cuts |

## 2. Music and beat sync

**Detection:** use **librosa** (ISC license, maintained, `beat_track` + `onset_detect`). Downsides: pulls in numba and llvmlite (about 26 packages), and the first call JITs for a few seconds. Alternatives: aubio (GPL-3, lighter, spotty Python 3.13 wheels), madmom (best downbeat tracking but unmaintained and breaks on new numpy), essentia (AGPL, heavy). Run detection once per track and cache `{tempo, beats[], duration}` in the db.

**Snapping cuts to beats** (pure Python, testable):

1. Compute each cut time on the output timeline (sum of clip durations after speed, minus transition overlaps).
2. For cut `k`, find the nearest beat (or every Nth beat for slower pacing; treat every 4th beat as a rough downbeat).
3. Move the cut by changing the **out point of the clip before it**, limited by the clip's available media and a minimum clip length (0.6 s). Never touch in points, so the chosen moment stays.
4. If the shift exceeds the tolerance (default 25% of a beat interval) or no media is left, leave the cut and report it.
5. Cascade: each shift moves every later cut, so snap left to right.
6. Return the list of changes so Claude can read what moved.

**Mixing:** music on its own input, `volume`, optional fade in/out (`afade`), `sidechaincompress` keyed by the clip audio for ducking under speech, `amix=normalize=0`, then `loudnorm=I=-14:TP=-1.5` (social media target). Tested above. Clip audio volume and mute go per clip in the spec.

**Music source:** a user file under an allowlisted folder (`~/Music`, `~/Movies/Safelight/music`). Photos has no music. Validate with ffprobe (has an audio stream, under 20 min).

## 3. Color and HDR

- **LUTs:** `lut3d=file=X.cube:interp=tetrahedral`. Ship 4 to 6 vetted LUTs in `engine/looks/` and expose them as named looks (Claude picks a name, never a path). `haldclut` covers Hald PNG LUTs if needed.
- **Simple grades:** map a small, clamped parameter set to filters: exposure and contrast via `eq` (brightness -0.2..0.2, contrast 0.8..1.3, saturation 0..1.6, gamma), warmth via `colorbalance` (rs/bs -0.2..0.2), a few `curves` presets. Avoid anything with free-form expressions.
- **iPhone HDR:** iPhone HDR video is HEVC Main10, HLG (arib-std-b67), bt2020. Dolby Vision iPhone clips are profile 8.4, which has an HLG base layer, so the same HLG to SDR path works and the DV metadata is ignored. Plan:
  1. ffprobe each source for `color_transfer`.
  2. If `arib-std-b67` or `smpte2084`: decode with VideoToolbox and convert with `scale_vt` to bt709 (tested). Then `hwdownload,format=p010le` before any CPU filter (LUT, eq, crop).
  3. If the transfer is already bt709 (bt2020 primaries only), convert gamut only; never tone-map SDR.
  4. Grade after conversion, so LUTs always see Rec.709 SDR. All LUTs we ship must be Rec.709-in.
  5. Fallback if `scale_vt` looks wrong on real clips: tap ffmpeg with zimg and `zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p` (CPU, slower).
- **Known issue:** FFmpeg trac #10884 reports wrong colors (red turning yellow) with `scale_vt` + `videotoolbox_vld`. My synthetic test did not reproduce it, but real footage might. Hence the visual check in Phase 1.
- **Side finding (not changed):** `media.ensure_proxy` scales with plain `scale=` and no tone mapping, so HDR proxies keep 10-bit HLG or get flattened depending on the source. Previews should run the same HDR branch on proxies, keyed off the proxy's own ffprobe tags.

## 4. Reframing to vertical

Use normalized coordinates in the spec (`x` = horizontal center of the crop, 0..1) so the same value works on a 540p proxy and a 4K original.

| Mode | How | When |
|---|---|---|
| `center` | `crop=ih*9/16:ih` | Default |
| `manual` | `x` from Claude, who already sees the frames through `look_at` | Phase 1, covers most clips |
| `pan` | linear from `x_start` to `x_end` over the clip (tested `crop` with a `t` expression) | Phase 1 |
| `track` | Sample the proxy at 3 to 5 fps with PyAV or OpenCV, run Apple Vision via pyobjc (`pyobjc-framework-Vision`): `VNDetectFaceRectanglesRequest`, `VNDetectHumanRectanglesRequest`, and `VNGenerateAttentionBasedSaliencyImageRequest` as fallback. Smooth (moving average or one-euro filter, with a dead zone), then fit to a constant or linear pan per clip | Phase 3 |

Why Apple Vision over OpenCV: it is on-device, runs on the Neural Engine, needs no model download, and its saliency covers non-face subjects (pets, food, scenery). Fitting to a constant or linear pan per clip avoids jitter and keeps the ffmpeg expression trivial. Full keyframed tracking (via `sendcmd`) is only worth it if linear pans look bad in practice.

Rotation: iPhone portrait clips carry a display matrix; ffmpeg autorotates by default, so the crop sees the upright frame. Keep autorotate on.

## 5. Architecture

### Declarative spec, not raw ffmpeg

Claude composing raw ffmpeg commands is the wrong call:

- **Safety:** a command string is arbitrary execution (filters like `movie=` and `sendcmd` read files, `-f` muxers write anywhere). Validating it means parsing ffmpeg.
- **Reliability:** xfade needs every input at the same size, fps, pixel format and timebase, and offsets are cumulative. LLMs get this wrong in ways that fail late, deep into a render.
- **Editability:** "make clip 4 warmer" should be one field change, not a regenerated 40-line filtergraph.

So: Claude calls typed tools, tools mutate a JSON spec with clamped values and enums, the compiler is deterministic and unit-tested. This matches the existing design, where Claude edits a cut list through `set_clip_range` / `move_clip`. Prior art agrees: Mnehmos/ffmpeg-llm keeps "a JSON timeline as the source of truth" compiled to `-filter_complex`, and editly is a JSON spec over ffmpeg.

### The spec

Stored as a JSON column on the project (collection) row. Clips keep using the existing items table (uuid, in, out, position); the spec adds effects keyed by item id.

```json
{
  "version": 1,
  "format": {"aspect": "9:16", "height": 1920, "fps": 30},
  "look": {"name": "warm-film", "strength": 0.8},
  "clips": {
    "17": {"speed": 1.0, "adjust": {"exposure": 0.05, "saturation": 1.1}, "look": null,
           "reframe": {"mode": "pan", "x_start": 0.4, "x_end": 0.6},
           "stabilize": false, "volume": 0.6}
  },
  "transitions": {"17": {"type": "dissolve", "duration": 0.5}},
  "titles": [{"id": "t1", "text": "Kyoto, 2026", "at": 0.5, "duration": 3, "style": "lower-third"}],
  "music": {"path": "~/Music/track.m4a", "volume": 0.8, "duck": true, "offset": 0, "fade_out": 2}
}
```

### Renderer (`engine/render.py`)

1. **Resolve sources:** preview uses `data/proxies/<uuid>.mp4` (built on demand by `ensure_proxy`); final uses originals fetched through the existing `projects.originals()` path (PhotoKit download for iCloud items).
2. **Segment pass, one ffmpeg run per clip:** trim (`-ss`/`-t` on input for speed), HDR branch, speed (`setpts` + chained `atempo`, each step within 0.5..2), stabilize, grade/LUT, reframe crop, `scale` + `fps` + `format=yuv420p` + `settb`, and silent audio (`anullsrc`) if the clip has none. Output: normalized intermediate (preview: 540p H.264; final: ProRes 422 via `prores_videotoolbox`, or high-bitrate HEVC to save disk). Cache key = hash(source uuid, in/out, clip fx, format), so changing one clip re-renders one segment.
3. **Join pass:** chain `xfade`/`acrossfade` where the spec has transitions and plain concat elsewhere; overlay title PNGs (Pillow) with alpha fades; mix music with ducking and loudnorm; encode with `h264_videotoolbox` (preview) or `hevc_videotoolbox` (final).
4. **Progress:** `-progress pipe:1` parsed for `out_time_us` against the known total duration.
5. **Proof frames:** after a render, write a contact sheet of the output (reuse the existing `sheets` code) so Claude can look at its own result.

### Preview vs final

| | Preview | Final |
|---|---|---|
| Source | 540p proxies (already on disk) | Originals, downloaded on demand |
| Size | 540 px short side | 1080 or 2160 short side |
| Encode | `h264_videotoolbox`, low bitrate | `hevc_videotoolbox` with `-tag:v hvc1`, or H.264 for compatibility |
| Range | Whole edit or `start`/`end` window | Whole edit |
| Output | `data/renders/<project>/preview.mp4` (git-ignored) | `~/Movies/Safelight/<project>/<project>.mp4` |
| Speed | Seconds for a 1-minute edit | Minutes, dominated by iCloud downloads |

### Keeping OTIO and Resolve export working

The spec compiles to two outputs. The OTIO side maps what Resolve can read and documents the rest:

- Cuts and order: as today.
- Transitions: `otio.schema.Transition(SMPTE_Dissolve)`; exports to FCP XML as Cross Dissolve (verified). Other transition types fall back to dissolve.
- Speed: `LinearTimeWarp` stays in the `.otio` file, but the fcp_xml adapter drops it (verified). Add a marker per retimed clip ("speed 2x") so it is visible in Resolve.
- Looks: copy the `.cube` files into the export folder and write the look name into clip metadata and a marker. The user applies the LUT in Resolve in one click.
- Titles, music: markers, plus the music file copied next to the XML.

Rendered MP4 and Resolve export stay independent buttons. Nobody loses the Resolve route.

## 6. Proposed MCP tools

Added to the existing 14 in `mcp_server.py`. All wrap engine HTTP routes, like today. Times in seconds; item ids from `get_collection`.

| Tool | Arguments | Returns |
|---|---|---|
| `get_edit` | `collection_id` | Spec, computed duration, cut times on the output timeline, validation warnings, available looks and transition types |
| `set_format` | `collection_id`, `aspect` (16:9, 9:16, 1:1, 4:5), `height` (720, 1080, 2160), `fps` (24, 25, 30, 60) | Updated format, new duration |
| `set_clip_fx` | `collection_id`, `item_id`, any of: `speed` (0.25..4), `adjust` {exposure, contrast, saturation, warmth}, `look`, `reframe` {mode, x, x_start, x_end}, `stabilize`, `volume` (0..2) | Clip fx after clamping, plus what was clamped |
| `set_transition` | `collection_id`, `after_item_id`, `type` (cut, dissolve, fade-black, wipe-left, wipe-right, slide-left, zoom), `duration` (0.2..2, at most half the shorter clip) | Transition, new duration |
| `apply_look` | `collection_id`, `look` (name from `get_edit`), `strength` (0..1), `item_ids` (optional, default whole edit) | Applied to N clips |
| `add_title` / `remove_title` | `collection_id`, `text` (max 80 chars), `at`, `duration`, `style` (title-card, lower-third, caption), `position` | `title_id` |
| `set_music` | `collection_id`, `path` (allowlisted folders), `volume`, `duck`, `offset`, `fade_out`; `path=""` removes | Tempo, beat count, track duration vs edit duration |
| `snap_to_beats` | `collection_id`, `every` (1, 2, 4 beats), `tolerance` (0..0.5 of a beat), `dry_run` | List of {item_id, old_out, new_out} and cuts left unsnapped with reasons |
| `render_preview` | `collection_id`, optional `start`, `end` | `job_id` |
| `render_final` | `collection_id`, `codec` (h264, hevc) | `job_id` |
| `render_status` | `job_id` | state, progress 0..1, ETA, output path, contact sheet path for `look_at`, last 20 stderr lines on failure |

Read-only tools (`get_edit`, `render_status`) get `readOnlyHint`, same as the existing ones.

### Jobs

- Reuse the existing job pattern in `projects.py` (`start_job`, status polling, like export and Ask).
- One render at a time (global lock); VideoToolbox sessions and disk I/O are the bottleneck. A second request returns `busy` with the running job id. Preview cancels an older preview of the same project.
- Per-run timeout scaled to output duration (for example 20x realtime for final, then kill).
- `render_status` is polled by Claude; the app shows the same job in the activity panel.

### Safety limits

- No tool accepts ffmpeg arguments, filter strings, or arbitrary paths. Every value is an enum or a clamped number. Source files come from uuids resolved by the engine.
- Title text never enters a filtergraph: Pillow renders it to a PNG, so there is no escaping problem and no injection.
- Music paths: resolved with `Path.resolve()` and required to sit under allowlisted folders; ffprobe-validated.
- Output: only under `~/Movies/Safelight/<slug>/` (final) and `data/renders/` (preview), checked after `resolve()`. Never overwrite a file the renderer did not create.
- `subprocess.run([...])` with list args, never `shell=True`; `-nostdin`; stderr captured and truncated.
- Free-disk check before final render (estimate from bitrate x duration, plus intermediates); intermediates deleted after success.
- Limits: max 200 clips, max 15 minutes output, max 20 titles.

## 7. Prior art

| Project | What it is | Lesson |
|---|---|---|
| [Mnehmos/ffmpeg-llm](https://github.com/Mnehmos/mnehmos.ffmpeg-llm.mcp) | MCP server, 47 tools, JSON timeline as source of truth compiled to `-filter_complex`, low-res preview engine, background renders, undo history. MIT, very new (5 commits) | Same architecture we chose. Undo history is a good idea later |
| [kyanitelabs/mcp-video](https://pypi.org/project/mcp-video/) | Python MCP server + CLI, 119 tools, "guardrailed" ffmpeg editing; v1.4 added a `search_tools` meta-tool because the tool list got too large for context. Apache-2.0 | Keep the tool count small (about 12). Too many tools forced them to build tool search |
| [mifi/editly](https://github.com/mifi/editly) | Declarative JSON5 edit spec over ffmpeg; gl-transitions, titles, audio normalization. MIT, 5.6k stars | Good spec vocabulary (clip layers, default transition, audio tracks). Node + headless-gl is the fragile part |
| conneroisu/vfx-mcp, dahshury/video_editing_mcp, kush36agrawal/video_editor_mcp | Thin wrappers that run one ffmpeg operation per tool call on files | The "one tool = one ffmpeg op on a file" style leaves many intermediate files and no timeline. Avoid |
| [auto-editor](https://github.com/WyattBlue/auto-editor) | Cuts on silence/motion, exports to Resolve/FCP XML. Unlicense | Confirms that XML handoff to Resolve is a normal pattern |

None of these handle iPhone HDR, Photos libraries, or a proxy-then-original workflow. Building our own compiler (a few hundred lines) beats adopting one.

## Phased build plan

Estimates are for a Claude Code session doing the work, plus the user's review time.

| Phase | Scope | Estimate |
|---|---|---|
| **1. Render core** | Spec column + validation; `render.py` segment and join passes; cuts, dissolve/fade/wipe transitions, speed, looks (eq + 4 bundled LUTs), HDR to SDR via `scale_vt`, format/aspect with center/manual/pan crop; preview from proxies, final from originals; render jobs with progress; contact sheet of output; MCP: `get_edit`, `set_format`, `set_clip_fx`, `set_transition`, `apply_look`, `render_preview`, `render_final`, `render_status`; `test_render.py` on lavfi clips (durations, xfade offsets, clamping, path checks); one real iPhone HLG clip compared side by side | About 1 day |
| **2. Music + titles** | librosa beat cache, `set_music`, `snap_to_beats` (pure function + tests), ducking + loudnorm; Pillow titles in 3 styles, `add_title`/`remove_title`; update `skills/safelight-editor/SKILL.md` with the new workflow (cut, then look, then music, then preview, then final) | Half a day to 1 day |
| **3. Smart reframe + quality** | Apple Vision tracking via pyobjc for `reframe.mode=track`; stabilization (`deshake` now; switch to the tap ffmpeg build for `vidstab` and `zscale` if needed); Ken Burns for stills | About 1 day |
| **4. App + Resolve polish** | Preview player and fx badges in the project editor; OTIO export of transitions, speed markers, LUT copies | Half a day to 1 day |

## Risks

1. **HDR conversion quality.** `scale_vt` passed a synthetic test only, and trac #10884 reports a color bug on this exact path. Mitigation: Phase 1 visual check on a real clip; zscale fallback via the tap build.
2. **Final renders need originals.** 54k of 58k items are iCloud-only, and PhotoKit downloads sometimes fail (HANDOFF). A final render can stall on downloads. Mitigation: download all originals first with per-clip status, fail fast with a list of missing clips, offer "render from proxies" as a low-quality fallback.
3. **Filtergraph edge cases.** Variable frame rate iPhone clips, clips without audio, mixed rotations, very short clips under the transition length. Mitigation: the segment pass normalizes everything (fps, size, pixel format, timebase, silent audio), and the validator rejects transitions longer than half the shorter clip.
4. **Taste.** Claude cannot judge a render it cannot see. The output contact sheet in `render_status` closes that loop; a short-clip preview (`start`/`end`) keeps iteration cheap.
5. **Dependencies and licenses.** librosa brings numba (about 26 packages, slow first call). ffmpeg here is a GPL-3 build: fine for a personal app, but redistributing the app with it bundled would bring GPL obligations. Remotion's license is free only for individuals and companies of up to 3 people.

## 8. Copying the editing style of a reference video

Goal: the user drops in an inspiration video (a reel, a trip edit) and Claude makes their footage "cut like that". Split the reference into two layers:

- **Measurable layer, extracted by code:** shot boundaries, transition types, shot lengths, how cuts sit on the beat, camera motion per shot, on-screen text, color statistics. These are numbers a renderer can apply.
- **Semantic layer, judged by Claude:** shot scale (wide, medium, close), subject, story order, speed ramps, energy. Claude already reads contact sheets through `look_at`, so give it one frame per reference shot and let it judge. No extra model.

### What I tested

Synthetic reference built from lavfi sources with known ground truth: 6 shots (1.0, 1.5, 1.0, 1.5, 1.0, 1.0 s), hard cuts at 1.0, 2.5, 4.5, 5.5 s, a 0.5 s dissolve at 3.0 to 3.5 s, a pan shot (moving crop over a still), a push-in (zoompan), the warm LUT from section "What I tested", a burned-in title, and a 120 BPM click track. Scripts: `notes/poc/style_profile.py` (reference to profile JSON) and `notes/poc/look_lut.py` (profile look to `.cube`). Both are throwaway PoCs, run with `uv run --no-project --with ...`.

| Step | Tool | Result |
|---|---|---|
| Hard cuts | PySceneDetect `ContentDetector` | 1.0, 2.5, 4.5, 5.5: all exact. Missed the dissolve |
| Hard cuts | PySceneDetect `AdaptiveDetector` (what `media.py` uses) | Same 4, plus the dissolve start (3.03) reported as a plain cut |
| Cuts + transition type | TransNetV2 (`transnetv2-pytorch`, CPU) | All 5 boundaries. The many-hot head marks the transition extent: 1 frame for hard cuts, 13 frames (0.43 s) for the 0.5 s dissolve. So "cut vs gradual + duration" falls out of the model for free. About 10 s for a 6.5 s clip including model load |
| Hand-rolled frame-difference classifier | numpy | **Failed**: high-motion shots (pans, cellular automaton) produced 9 false transitions. Do not hand-roll this |
| Beat alignment | librosa beats vs cut times | Tempo 117.5 (true 120), 4/5 cuts within 60 ms of a beat (the dissolve center is off-beat, correctly), shot lengths in beats `[1.96, 2.94, 1.57, 2.35, 1.96, 1.96]` |
| Camera motion | OpenCV LK optical flow + `estimateAffinePartial2D` per frame pair, averaged per shot | Pan shot: `pan-right` (correct). Push-in: `push-in` (correct). Static test patterns: `static` (correct). The scrolling cellular automaton came out as `pull-out` (false positive) |
| On-screen text | Apple Vision `VNRecognizeTextRequest` via `pyobjc-framework-Vision`, frames at 2 fps | Found "Kyoto, 2026" with confidence 1.0, position and size, at 0.0 and 0.5 s only (the title was on screen for 1 s). Also read the test pattern's timecode, so filter tiny or changing strings |
| Look transfer | Reinhard (Lab mean/std) baked into a 33-point `.cube`, applied with `lut3d` to an ungraded copy of the same edit | Warmth (Lab b mean) offset to the reference dropped from 5.77 to 1.15; mean per-pixel Lab difference from 3.77 to 2.54 |

Apple Silicon fit: everything above ran on CPU in seconds for short clips. TransNetV2 also runs on MPS, but the port prints a warning about numerical inconsistency on MPS, so keep it on CPU (it is a small model working on 48x27 frames). Apple Vision runs on-device on the Neural Engine. librosa and OpenCV are CPU. A 60 s reel should analyze in well under a minute.

### Tooling by question

| Question | Recommended | Alternatives and notes |
|---|---|---|
| Where are the shots? | **TransNetV2** (`pip install transnetv2-pytorch`, inference-only PyTorch port, MIT-labelled, weights bundled) | PySceneDetect (already in the engine) is fine for hard cuts and keeps working for the user's own footage. AutoShot (Kuaishou, 2023) is trained on short-form vertical video, so it may fit reels better; code availability not verified. OmniShotCut and TransVLM (2026) handle complex transitions; research code, heavier |
| Cut, dissolve, or wipe? | TransNetV2 transition extent (1 to 2 frames = cut, longer = gradual) | Telling wipe from dissolve: check whether the blend is spatially uniform (dissolve) or has a moving edge (wipe) inside the gradual window; untested. TransVLM classifies transition types directly. AutoTransition (ECCV 2022, code public) learns which of 30 transition types editors use between two shots; useful as research, too heavy to run here |
| Pacing? | Shot-length list, in seconds and in beats; median, coefficient of variation, cuts per minute | All arithmetic on the boundary list |
| Do cuts follow the music? | librosa beats; per-cut offset to the nearest beat; % within 60 ms | madmom downbeats would say "cuts land on bar starts" but it is unmaintained |
| Camera motion | OpenCV similarity transform per frame pair: translation = pan/tilt, scale = push-in/pull-out | Flags textured motion (water, crowds) as camera motion. Feature-sparse shots return `unknown`. Good enough as a hint, since Claude also sees the frames |
| Shot scale | Claude judges from the reference contact sheet | Optional numeric version: Apple Vision face/human box height as a fraction of the frame, or zero-shot with the engine's existing image embedding model ("close-up shot" vs "wide shot" prompts); both untested |
| Speed ramps | Claude judges from frames, plus a flow-magnitude jump inside one shot as a hint | No reliable open detector found. Low priority |
| Text overlays | Apple Vision OCR (on-device, free) | Gives text, position and size, which map directly onto our title styles. Filter strings that change every frame (timecodes, scrolling text) |
| Color look | Reinhard Lab transfer baked into a `.cube` (tested) | `color-matcher` (pip, hahnec) adds MKL, histogram matching and the compound HM-MVGD-HM method, which the author reports works best; it transfers image to image, so baking into a LUT means re-implementing its transform on a lattice. Check its license before vendoring |

### The style profile

One JSON per reference, stored in the db and shown to Claude. Trimmed example from the PoC run:

```json
{
  "source": "ref_title.mp4", "duration": 6.5, "fps": 30,
  "pacing": {"shots": 6, "cuts_per_min": 46.2, "median_shot": 1.0, "shot_length_cv": 0.2},
  "transitions": {"cut": 4, "gradual": 1,
                  "list": [{"t": 3.3, "type": "gradual", "duration": 0.43}]},
  "music": {"tempo": 117.5, "cuts_on_beat_pct": 80, "median_offset_s": 0.028},
  "shots": [{"start": 1.0, "length": 1.5, "beats": 2.94, "camera": "pan-right"},
            {"start": 3.3, "length": 1.2, "beats": 2.35, "camera": "push-in"}],
  "look": {"lab_mean": [55.2, 12.6, 6.9], "lab_std": [25.3, 45.4, 42.8],
           "warmth_b": 6.9, "saturation": 0.62, "contrast_L_std": 25.3, "black_level_L_p2": 0.4},
  "text": [{"t": 0.0, "text": "Kyoto, 2026", "y": 0.07, "height": 0.21}]
}
```

Plus a contact sheet with one frame per reference shot, labelled with shot index, length and camera motion (reuse `media.contact_sheet`).

### How it plugs into the Phase 1 spec and renderer

1. **Ingest:** reference comes from a Photos item (saved reels are often in the library, so a uuid) or a file in an allowlisted folder (`~/Movies/Safelight/references`). No URL downloading; the user saves the file. Analysis runs as a background job, like `analyze_clip`.
2. **New spec field:** `"style": {"reference_id": 4, "look_strength": 0.7, "follow_pacing": true}`.
3. **Look:** at render time `render.py` computes Lab statistics of the project's own clips (from proxies), builds the Reinhard `.cube` against the reference statistics, and applies it in the segment pass as one more `lut3d`, after HDR to SDR and before the user's per-clip adjustments. `look_strength` blends toward identity. Cache the LUT by (reference id, clip set hash).
4. **Pacing:** the profile gives a sequence of shot lengths in beats and a transition pattern. With music set, `snap_to_beats` gets a new mode `pattern=reference`: slot k gets length `beats[k mod n] x beat period` of the user's track, so a reference that cuts every 2 beats with a 4-beat hold before the drop cuts the same way on different music. Without music, use the lengths in seconds, scaled to the target duration.
5. **Transitions:** copy the ratio and placement of gradual transitions (for example "dissolve, 0.4 s, only into the last shot") with `set_transition`.
6. **Camera motion:** when a slot calls for `push-in` but the user's clip is static, the renderer can fake it with a time-varying crop (the zoompan test in section "What I tested" works on stills; for video use `crop` with `t` in the width expression plus `scale`). Add as a `reframe.mode=push` option.
7. **Titles:** text style and position from OCR boxes map onto `add_title` style and position.

Claude stays in charge of selection and order: the profile is a constraint and a hint. The renderer only applies measurable things (look, durations, transitions).

### MCP tools (2 more, on top of section 6)

| Tool | Arguments | Returns |
|---|---|---|
| `analyze_reference` | `uuid` (Photos item) or `path` (allowlisted folder) | `reference_id`, job id. Re-analysis is cached by file hash |
| `get_style_profile` | `reference_id` | Profile JSON plus the contact sheet path for `look_at` |

Applying it uses the existing tools: `apply_look(look="ref:4", strength=0.7)`, `snap_to_beats(pattern="reference", reference_id=4)`, `set_transition`, `add_title`. No new render path.

### Prior art and research

| Work | What it does | Relevance |
|---|---|---|
| [TransNet V2](https://www.alphaxiv.org/abs/2008.04838) (2020), [PyTorch port](https://pypi.org/project/transnetv2-pytorch/) | Shot transition detection, cuts and gradual | **Use it.** Tested above |
| [AutoShot](https://www.alphaxiv.org/abs/2304.06116) (Kuaishou, 2023) | Short-video shot boundary dataset and model | Better match for reels; code not verified |
| [OmniShotCut](https://www.alphaxiv.org/abs/2604.24762), [TransVLM](https://www.alphaxiv.org/abs/2604.27975) (2026) | Shot detection with transition types and complex transitions | Watch; heavier research code |
| [AutoTransition](https://github.com/acherstyx/AutoTransition) (ECCV 2022) | Recommends transition type between two shots, learned from edited videos | Ideas only; Claude does this choice |
| [CutClaw](https://www.alphaxiv.org/abs/2603.29664) (2026, [code](https://github.com/GVCLab/CutClaw)) | Multi-agent LLM editor: music keypoints (downbeats, energy shifts) anchor shot selection, with Playwriter, Editor and Reviewer agents | Closest system to Safelight's Ask Claude. Worth borrowing: music-structure segments as the pacing skeleton, and a reviewer pass |
| [EditDuet](https://www.alphaxiv.org/abs/2509.10761) (Adobe, 2025) | Multi-agent editor/critic for non-linear editing | Same reviewer-loop idea |
| [Let Your Video Listen to Your Music](https://www.alphaxiv.org/abs/2506.18881) (2025) | Aligns visual motion rhythm to music beats | Beyond cut snapping: retime motion peaks to beats. Later |
| [TemplateCraft](https://www.alphaxiv.org/abs/2609.31451) (Kuaishou, 2026) | Agentic generation of reusable CapCut-style visual templates | Confirms "template = timing + effects + text slots", which is our style profile |
| [RefVFX](https://www.alphaxiv.org/abs/2601.07833) (Snap, 2026) | Generative transfer of visual effects from a reference video | Generative, GPU-heavy, not for this app |
| Google patent US 12,437,782 B2 | Shot boundaries + per-shot camera motion to characterize and transfer editing style | Same decomposition as ours; a patent, not code |

No open-source project does "edit my footage like this video" end to end. The building blocks are mature (TransNetV2, librosa, OpenCV, Apple Vision) and an LLM covers the semantic part, so a Safelight-specific glue layer of about 300 lines is the right size.

### Plan and risks

- **Phase 5, style reference (about 1 day, after Phases 1 and 2):** `reference.py` (profile extraction, from the PoC), look LUT in `render.py`, `pattern=reference` in `snap_to_beats`, two MCP tools, a test on the synthetic reference (assert boundaries within 1 frame, gradual vs cut, LUT reduces the b offset). New deps: `transnetv2-pytorch` (torch is already in the engine), `pyobjc-framework-Vision`, librosa from Phase 2.
- **Risk, look transfer copies content:** Reinhard matches global statistics, so a sunset reference turns daytime footage orange. Mitigation: default strength 0.6, clamp the per-channel shift, and compute reference statistics from mid-tones only. Upgrade path: MKL or histogram-matching methods.
- **Risk, reference audio:** reels often have music plus voice; beat tracking on the mix is noisier. Mitigation: report beat confidence and fall back to seconds-based pacing.
- **Risk, copyright:** copying pacing and a look is fine; the reference's music may not be, if the user plans to post. The music stays the user's choice.

## Sources

- FFmpeg filters docs: https://ffmpeg.org/ffmpeg-filters.html (xfade, lut3d, tonemap, zoompan, deshake, sidechaincompress, loudnorm)
- scale_vt docs (FFmpeg 8.1): https://ayosec.github.io/ffmpeg-filters-docs/8.1/Filters/VideoToolbox-Video/scale_vt.html
- FFmpeg trac #10884, scale_vt color bug: https://trac.ffmpeg.org/ticket/10884
- HLG/PQ tone mapping chains: https://ffmpeg-cookbook.pages.dev/en/articles/hdr-to-sdr/
- homebrew-ffmpeg tap and options: https://github.com/homebrew-ffmpeg/homebrew-ffmpeg
- MLT Homebrew formula: https://formulae.brew.sh/formula/mlt ; headless macOS rendering: https://mltframework.org/docs/headlessmacos/
- Shotcut XML to melt workflow: https://forum.shotcut.org/t/is-there-a-way-to-use-shotcut-as-a-command-line-in-macbook/33968
- MoviePy on PyPI: https://pypi.org/project/moviepy/
- Remotion license: https://www.remotion.dev/docs/pricing , https://github.com/remotion-dev/remotion/blob/main/LICENSE.md
- editly: https://github.com/mifi/editly
- auto-editor: https://github.com/WyattBlue/auto-editor
- Mnehmos ffmpeg-llm MCP: https://github.com/Mnehmos/mnehmos.ffmpeg-llm.mcp
- kyanitelabs mcp-video: https://pypi.org/project/mcp-video/
- Other ffmpeg MCP servers: https://lobehub.com/ja/mcp/conneroisu-vfx-mcp , https://lobehub.com/es/mcp/dahshury-video_editing_mcp , https://awesome.ecosyste.ms/projects/github.com%2Fkush36agrawal%2Fvideo_editor_mcp
- Apple Vision saliency request: https://docs.rs/objc2-vision/latest/objc2_vision/struct.VNGenerateAttentionBasedSaliencyImageRequest.html (API reference via Rust bindings; same Objective-C class through pyobjc-framework-Vision)
- librosa beat tracking: https://librosa.org/doc/latest/generated/librosa.beat.beat_track.html
- TransNetV2 PyTorch port: https://pypi.org/project/transnetv2-pytorch/ ; original: https://github.com/soCzech/TransNetV2
- color-matcher: https://github.com/hahnec/color-matcher
- AutoTransition: https://github.com/acherstyx/AutoTransition
- CutClaw: https://arxiv.org/abs/2603.29664 , https://github.com/GVCLab/CutClaw
- Google patent US 12,437,782 B2: https://patentsgazette.uspto.gov/week40/OG/html/1539-1/US12437782-20251007.html
- Apple Vision text recognition: https://developer.apple.com/documentation/vision/vnrecognizetextrequest
