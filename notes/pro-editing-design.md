# Pro editing: rhythm, motion, moments, titles, style references

Date: 2026-11-09. Goal: snappy, music-driven edits from the user's own footage, all local, no generation.
Claude is the only AI brain for now (via MCP / `claude -p`). Gemini may come later as a re-ranker.
Reviewed with Fable (plan in the session of 2026-11-09). This doc is the contract for four phases.

## Measured on this Mac (2026-11-09)

| Check | Result |
|---|---|
| `beat_this` (final0, CPU) on a synthetic 128 BPM track | beats every 0.46-0.48 s (true 0.46875), within 20 ms; 77 MB checkpoint in `~/.cache/torch/hub` |
| One ffmpeg pass on a 22 s 540p proxy: `signalstats,scdet,blurdetect` + `ebur128` | 0.23 s (about 100x real time) |
| Apple Vision via pyobjc: aesthetics + face capture quality + humans, 44 frames at 384 px | 0.91 s (about 50 frames/s) |
| `perspective` (cubic, per-frame) push-in on 2160x3840, then scale to 1080x1920 | 3 s of video in 2.7 s |
| `eq=brightness=...:eval=frame` flash | works; expressions use `t`, perspective uses `in` (frame index), not `T` |

Not available in the Homebrew ffmpeg: `dnn_*` filters (no TensorFlow / OpenVINO / LibTorch backend), `drawtext`, `libass`, `zscale`.
So no ML inside ffmpeg. That is fine: the fast detectors are Apple's (Neural Engine) and ffmpeg's own non-ML analysis filters.

## Phase 1: rhythm and motion (branch feat/rhythm-motion)

### 1a. Beat grid (`engine/beats.py`)

- `analyze(path) -> {"tempo", "beats", "downbeats"}` with `beat_this` (`File2Beats(checkpoint_path="final0", dbn=False)`),
  model loaded once per process, cached in `data/beats/<sha1(path|mtime|size)>.json`.
- Device: CPU (MPS gives no gain for a few-second job and has op gaps).
- Route `GET /collections/{cid}/edit/audio/{track_id}/beats` for the timeline lane.

### 1b. Spec: `sync`

```json
"sync": {"track": "music", "grid": "beat", "lead": 1}
```

- `track`: an audio track id, or null (off, the default). `grid`: `beat`, `half` (every 2nd beat) or `downbeat`. `lead`: frames the cut lands
  before the beat, 0 to 3, default 1 (a cut exactly on the beat reads as late).
- Grid on the output timeline: `track.start + (b - track.in)` for each source beat in `[in, out]`, extended past both ends with the
  median period so clips before or after the music still get a grid.
- Per clip `beats` (1 to 32): how many grid units the clip lasts. Absent: `max(1, round(natural_length / unit))`.
- Per clip `hit` (source seconds): the moment that must land on a grid point (a jump, a clap). The clip's in point slides so the hit sits on
  the nearest grid point inside the clip.
- Cut positions are absolute: clip k starts at `round(grid[g_k] * fps) - lead` where `g_{k+1} = g_k + beats_k`. Never summed durations,
  so there is no drift. With a transition, the overlap is centred on the beat.
- Source too short for its beats: the last frame holds, plus a warning.

### 1c. Motion (per clip, enums and bounded numbers only)

| Field | Values | Rendered as |
|---|---|---|
| `motion` | `none`, `push`, `pull`, `drift-left`, `drift-right`, `punch` | `perspective` per frame (subpixel, no zoompan jitter) on the cropped source, before scaling |
| `motion_amount` | 0 to 1 (zoom up to 1.3x) | default 0.35 (1.105x) |
| `flash` | bool | white `eq` brightness pulse at the clip's first frame (on a beat when synced) |
| `shake` | bool | 6-frame decaying offset at each grid point inside the clip |
| `ramp` | `none`, `slow-mid`, `fast-in`, `slow-out` | piecewise-linear `setpts`; the clip's own audio is silenced |

`punch` steps between 1.0 and 1+amount on every grid point inside the clip (or at the midpoint when unsynced).
Caps: zoom at most 1.3x (sources go soft past that), and validation refuses `shake` on more than half the clips.

### 1d. Transitions and sound effects

- More xfade built-ins: `zoom-in` (zoomin), `whip-left` / `whip-right` (smoothleft / smoothright), `flash-white` (fadewhite),
  `blur` (hblur), `slide-up` (slideup), `circle` (circleopen), `squeeze` (squeezeh).
- `sfx` on a seam (`whoosh`, `hit`, `riser`): synthesized once with ffmpeg `aevalsrc` into `data/sfx/` (no licences), mixed in the finish pass
  like an audio track starting at the seam. ponytail: they also key the ducking, which dips clip audio for 0.3 s; fine for now.

### 1e. Tools and skill

- `set_edit(sync_track, sync_grid, sync_lead)`, `set_clip_fx(beats, hit, motion, motion_amount, flash, shake, ramp)`, `set_transition(sfx)`.
- `get_edit` also returns `grid` (output-time beats, first 400) so Claude can reason in beats.
- Skill: music first, then `sync`, then shot lengths in beats (vary 1, 2, 4; faster in the chorus), motion on most shots, flash or punch
  on downbeats only at peaks, at most one effect type per shot.

## Phase 2: moments v2 (branch feat/moments, separate worktree)

`engine/moments.py`, cached per uuid in `data/moments/<uuid>.json`, computed on the 540p proxy.

Signals at 4 Hz:
1. ffmpeg, one pass: `signalstats` (YAVG exposure, SATAVG), `blurdetect`, `scdet`, frame-difference via `siti` TI, `ebur128` momentary loudness.
2. Apple Vision on 2 frames/s at 384 px: `VNCalculateImageAestheticsScoresRequest` (overallScore, isUtility), `VNDetectFaceCaptureQualityRequest`
   (count, best quality, box), `VNDetectHumanRectanglesRequest`, `VNGenerateAttentionBasedSaliencyImageRequest` (most salient box centre, used for reframing).
3. Apple SoundAnalysis built-in classifier (`SNClassifySoundRequest` version 1) on the audio: laughter, cheering, applause, shout, music, speech.
   Needs `pyobjc-framework-SoundAnalysis`.
4. Whisper words when a transcript is cached (captions already makes them); never start a transcription from here.
5. Brief match: cosine of the existing image embeddings at 1 frame/s against the text query, only when a query is given.

Fusion: z-score each signal per clip, peak-pick, merge peaks into 1 to 4 s windows, snap edges to scene cuts, score with a weight profile
(`action`, `people`, `scenic`, `talk`, `auto`), penalise shake (high TI with low sharpness) and `isUtility`.
Output per moment: `t0, t1, peak, score, reasons[]` (for example `laugh 0.8`, `face 0.71`, `motion peak`, `matches "cake" 0.31`), plus the transcript snippet.

Surfaces: `GET /media/{uuid}/moments?profile=&q=`, a 3-frame sheet per moment (before, peak, after), MCP `find_moments(uuids, profile, query, top)`
returning a text table plus one sheet image of the top moments. `analyze_clip` stays as is.
Speed budget: under 2 s per minute of proxy after the proxy exists.

With Gemini later: local signals remain the candidate generator and the frame-accurate `peak`; Gemini re-ranks the top N windows only.

## Phase 3: titles and finish

- Overlay layer: Pillow (already used by captions) renders RGBA frames for animated text templates into a per-frame ffconcat list, cached by
  template + text + dims. Upgrade to skia-python only if antialiasing or motion blur looks rough.
- Templates (enum): `title-pop`, `title-slide`, `word-stagger`, `lower-third`, `location-tag`, `big-number`. Spec: `texts: [{id, template, text,
  start, dur, position}]`, times snap to the grid when synced.
- Fonts: 3 to 4 bundled OFL fonts (for example Inter, Anton, Playfair Display, Space Grotesk) in `engine/fonts/`.
- Looks: `.cube` LUTs via `lut3d`, generated from the existing presets plus Phase 4 references.
- Safe margins enforced by the template code, not by Claude.

## Phase 4: style reference

`analyze_reference(path)` builds on `notes/poc/style_profile.py`: TransNetV2 shots and transition types, beat_this beats, cut-on-beat ratio,
shot length in beats per section, punch-in rate (scale jumps inside a shot from optical flow), OCR text boxes (Vision), Lab stats baked to a `.cube`.
Profile JSON feeds the planner: a beats pattern per section, transition mix, motion rate, title template, look LUT. A reviewer pass compares
the rendered preview's profile with the reference and lists the gaps. The user supplies the file; no URL downloads.

## Risks to design against

1. Late or uniform sync: `lead` default 1 frame, click-track test, vary beats per section.
2. Effect spam and soft pixels: caps in validation (zoom 1.3x, shake on at most half the clips).
3. Loud and shaky moments ranking first: shake penalty, and Claude still reviews preview sheets.
