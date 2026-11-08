# CLI video editing for Safelight: research and recommendation

Date: 2026-11-08. Scope: let Claude make real edits (looks, transitions, speed, titles, music and beat sync, stabilization, vertical reframing) and render a finished video with open-source CLI tools, exposed as MCP tools. Research only, no app or engine code changed.

## TL;DR

1. **Engine: plain ffmpeg, driven from Python.** It is already installed (Homebrew ffmpeg 9.0.2) and already used by `media.py`. Everything in Phase 1 was verified on this machine (see "What I tested").
2. **Architecture: Claude edits a declarative edit spec through validated MCP tools. It never writes ffmpeg commands.** A Python compiler (`render.py`) turns the spec into ffmpeg runs: per-clip normalized segments (cached), then one join pass. The same spec renders a cheap preview from the 540p proxies and the final from originals. It also still exports OTIO and FCP XML for Resolve.
3. **First phase (about 1 day):** spec + renderer for cuts, crossfade transitions, speed, looks (eq + bundled .cube LUTs), HDR to SDR, 9:16/1:1/16:9 with center or manual crop, preview and final render jobs, and 7 MCP tools.

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
