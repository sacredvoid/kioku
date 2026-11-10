# Editing power research: text, transitions, music

Date: 2026-11-10. Read and research only, no source changed. Machine: Apple M4, 24 GB, macOS 26, Homebrew ffmpeg 9.0.2, engine venv on Python 3.13.

How to read this: "measured" means I ran it on this Mac on synthetic clips in a scratch folder. "web" means read from a source today and not run. "unverified" means I could not confirm it and it needs a check before building on it.

## 1. Where Kioku stands

### Does well

- Beat sync is frame exact and drift free: cuts land on absolute grid frames with a 1 frame lead (`engine/edit.py:257-345`, `engine/beats.py:87`).
- Moment finding fuses ffmpeg stats, Apple Vision and SoundAnalysis at 4 Hz (`engine/moments.py:46`, `:104`, `:177`).
- Per clip segment cache plus chunked join keeps re-renders cheap (`engine/render.py:256`, `:578-596`).
- Titles are already Pillow sprites with easing, balanced line wrap and a title-safe box (`engine/titles.py:208-252`), merged with captions into one overlay input (`engine/titles.py:429`).
- Reference analysis returns a plan in Kioku's own enums and a gap list (`engine/reference.py:483`, `:557`).
- Music licensing is handled carefully: SA and ND licences never offered, CC BY credits written next to renders (`engine/sounds.py:44-45`, `:241`).
- Validation caps taste failures in code (shake on at most half the clips, `engine/edit.py:680-683`).

### Biggest gaps

| # | Gap | Evidence |
|---|---|---|
| G1 | The skill promises text stays off faces, the code does not do it. `position="auto"` only switches top/bottom when captions sit at the bottom. | `skills/kioku-editor/SKILL.md:88` vs `engine/titles.py:240-242`, `engine/render.py:918-919` |
| G2 | Saliency and face positions are computed and cached for every analysed clip, then never read. Reframe is center, manual or pan only. | `engine/moments.py:46`, `:135` (only definitions); `engine/edit.py:279-281` |
| G3 | "Whip" is xfade `smoothleft`, a soft wipe. No slide, no motion blur. | `engine/edit.py:47` |
| G4 | Push, pull and drift move linearly, punch is a hard step. Reads as robotic. | `engine/render.py:318`, `:331` |
| G5 | Speed ramps are three constant-speed phases (a visible gear change), no frame blending or interpolation. | `engine/edit.py:54-55`, `engine/render.py:294-308` |
| G6 | Claude picks music blind: title, tags, duration only. No energy, no sections, no drop time. The skill still says "strongest moment on the music's drop". | `engine/mcp_server.py:380-381`, `engine/beats.py:65`, `SKILL.md:62`, `:67` |
| G7 | Music always starts at its file start unless Claude guesses `source_in`; the end is a plain fade. Nothing picks a section or ends on a bar. | `engine/mcp_server.py:358-375` |
| G8 | Text sprites are placed on whole pixels, so slow eases stutter at the end. No motion blur, no per-letter animation. | `engine/titles.py:185` |
| G9 | Captions use system fonts and static states (colour change only on the active word). | `engine/captions.py:36`, `:483-516` |
| G10 | Seam sounds are three synthesized tones. | `engine/render.py:466-470` |
| G11 | Hard cuts join clip audio with plain `concat`, so ambience changes click. | `engine/render.py:531` |
| G12 | No colour consistency between clips beyond manual `exposure` / `warmth`. | `SKILL.md:90` |

### What this ffmpeg build can and cannot do (measured)

| Have | Missing |
|---|---|
| `xfade` with 57 built-ins plus `custom` expr, `dblur`, `gblur`, `tmix`, `minterpolate`, `framerate`, `blend`, `maskedmerge`, `displace`, `noise`, `vignette`, `lut3d`, `rgbashift`, `chromashift`, `deshake`, `gradients` source, `coreimage` (single input: CIZoomBlur, CIMotionBlur, CIBloom, CIGloom) | `vidstab*`, `libplacebo`, `frei0r`, `drawtext`, `libass`, `zscale`, `rubberband`, any GL transition filter |

Homebrew core slimmed the ffmpeg formula in January 2026 (web: homebrew-core PR 261303). Plan for fewer optional filters over time, not more.

Timings at 1080x1920, 2 s output, VideoToolbox encode:

| Test | Time |
|---|---|
| xfade smoothleft (today's whip) | 0.70 s |
| xfade slideleft + two `dblur` windows (blurred whip) | 1.09 s |
| xfade custom expr, eased slide | 2.44 s |
| Light leak: `gradients` source, `gblur`, screen blend in a 0.5 s window | 1.70 s |
| Grain + vignette on the whole clip | 0.45 s |
| `coreimage=CIZoomBlur` on the whole clip | 1.33 s |
| `minterpolate` (mci, aobmc, bidir) 0.4x slow motion, 1 s of source | 14.3 s |
| numpy + cv2 whip composite (roll, 121 px box blur, blend) | 168 ms per frame |
| Vision foreground instance mask, 540x960 | 18 ms per frame after a 320 ms first call |

Other probes in the engine venv: Pillow draws Apple Color Emoji with `embedded_color=True` at the 160 px strike. Pillow reports `raqm: False`, so complex scripts (Devanagari, Arabic) are not shaped. `cv2`, `scipy`, `torchaudio`, pyobjc `Quartz` and `AVFoundation` import. `librosa`, `skia`, pyobjc `VideoToolbox` and `CoreText` are not installed.

---

## 2. Area 1: text overlays

### Rendering approach: keep Pillow

| Option | Verdict |
|---|---|
| Pillow PNG states into ffconcat (today) | Keep. No new dependency, cached, already merged with captions. Fix its weak points (T2). |
| ffmpeg `drawtext` / libass | Not in this build. Skip. |
| skia-python (web: 144.0.post2, March 2026, BSD; arm64 wheel for 3.13 unverified) | Only if we need text on a path, shader blurs or variable-font animation. Not now. |
| CoreText through pyobjc (`pyobjc-framework-CoreText`, not installed) | The right fix for non-Latin shaping if the user needs Hindi or Arabic titles. Small, native. Do when asked. |
| HTML to video: Remotion, HyperFrames | No. Needs Node and headless Chromium. Remotion is free only for individuals and companies of up to 3 people (web); HyperFrames is Apache-2.0 (web) but the weight is the same. |
| Lottie | No. Claude cannot author After Effects files, and a renderer adds a dependency for little gain. |

One limit to lift later: `titles.MAX_FRAMES = 600` (`engine/titles.py:22`). Per-frame effects (T3 text-behind, T8 tracking) make one PNG per frame. Fine for a few texts; past that, pipe RGBA frames into one ProRes 4444 overlay file.

### T1. Face-aware placement and platform safe areas

- (a) `position="auto"` picks the band (top, middle, bottom, plus left/right for tags) with the least overlap with faces and salient regions under the text. A `safe` setting keeps text and captions clear of app UI.
- (b) `render.py` step 4 already has the cached segment files before titles are built (`engine/render.py:908-919`). For each text, grab 3 frames of the clips under it, run `VNDetectFaceRectanglesRequest` and saliency under `core.VISION_SLOTS` (same pattern as `moments.vision_signals`), pass `avoid=[boxes]` into `titles.layout` (`engine/titles.py:209`). Add `set_edit(safe="none|reels|tiktok|shorts")` stored in `spec["format"]`, read by `titles.layout` and `captions.draw_png` (`engine/captions.py:496-501`). `get_edit` returns each text's resolved position and a warning when no band is free.
- (c) 1 day. (d) High. (e) None new.
- (f) Skill: "Keep `position="auto"`. Set `safe` to match where the video will be posted. If `get_edit` warns that a text covers a face, move the text to another clip before forcing a position."
- Safe-area numbers are approximate and change with app updates: roughly top 10 to 14%, bottom 20 to 35%, right 10 to 12% of a 9:16 frame. Unverified today; take them from each platform's current template when building.

### T2. Motion quality: subpixel, more easings, per-letter, motion blur

- (a) Text moves smoothly and can animate per letter.
- (b) `titles.put` rounds to whole pixels (`engine/titles.py:185`); render the sprite through `Image.transform` (affine, bicubic) so scale and subpixel offset happen in one resample. Add `ease_out_expo` and a damped spring next to `ease_out_back` (`engine/titles.py:36-46`). Add letter-level sprites to `_stagger` (`engine/titles.py:306`) with templates `letter-rise`, `typewriter`, `tracking-in` (letter spacing closes), `blur-in`. Motion blur: average 3 sub-frame samples for frames where the sprite moves more than about 2% of the frame (sprites are cached, so this is cheap).
- (c) 1 to 1.5 days. (d) Medium to high. (e) None.
- (f) Skill: "One animation family per edit. `letter-rise` or `tracking-in` for calm and cinematic, `title-pop` for snappy, `typewriter` only for dates and places."

### T3. New templates: text-behind, word-on-beat, metadata tokens

- (a) Three looks that mark a modern edit:
  - `text-behind`: big title sits behind the subject (CapCut and Reels staple).
  - `word-on-beat`: each word lands on the next beat, one at a time, large and centred.
  - Tokens `{place}`, `{date}`, `{year}`, `{count}` filled from Photos metadata, so Claude never invents a place name. Plus `date-stamp` (camcorder style corner date) and `end-card`.
- (b) `text-behind`: in `titles.build`, for each frame of the text decode the frame from the cached segment, run `VNGenerateForegroundInstanceMaskRequest` (measured 18 ms per frame at 540x960), multiply the text alpha by `1 - mask`. Refuse it when the text spans a transition or when the mask covers under 5% or over 70% of the frame (falls back to `title-pop` with a note). `word-on-beat`: `compute_timeline` already has the grid `G` when it places texts (`engine/edit.py:349-357`); pass the beat frames inside the text into the text dict and let `_stagger` use them instead of the fixed `STAGGER`. Tokens: resolve in `edit.put_text` from the item under `start` using the same data `get_media` returns; store both the raw and resolved text.
- (c) 1.5 to 2 days. (d) High. (e) None new (Vision is already a dependency).
- (f) Skill: "`text-behind` once per edit, on a shot with one clear subject and empty space above it, 1 to 2 words. `word-on-beat` for 3 to 5 word hooks in the first 3 seconds or on the drop. Use `{place}` and `{date}` instead of typing them."

### T4. Legibility backing

- (a) Text reads on any footage without a heavy box.
- (b) Using the frames from T1, measure mean luma and variance behind the text block. Choose shadow only, a soft gradient scrim, or flip the text colour with the existing `readable_on` (`engine/titles.py:93`), which today is only used for bars and pills.
- (c) 0.5 day. (d) Medium. (e) None.
- (f) Skill: nothing to choose; mention that Claude should not add a box by hand.

### T5. Text style presets and font pairing

- (a) One `text_style` for the whole edit sets heading font, label font, colour, accent and animation family. Consistency is what makes text look designed.
- (b) `set_edit(text_style="clean|bold|editorial|retro|mono")` in `spec["format"]`; `titles.DEFAULT_FONT` (`engine/titles.py:17`) becomes a per-style table. Add 3 or 4 OFL fonts to `engine/fonts` (a condensed display, a high-contrast serif, a handwritten, a mono for dates). Accent colour: `accent="auto"` picks a dominant colour from the clips under the text (Pillow `quantize`), checked with `readable_on`.
- (c) 1 day. (d) Medium to high. (e) OFL fonts only; keep each licence file next to the font as today.
- (f) Skill: "Pick one `text_style` in step 7 and never mix fonts by hand. Pairing rule: one display face for titles, one neutral face for labels. White or near-white text, one accent colour."

### T6. Captions v2

- (a) Reel-style captions: active word pops and gets a coloured box, 1 or 2 emphasised keywords per line, bundled fonts.
- (b) `captions.draw_png` (`engine/captions.py:483`): add style `karaoke-box` (rounded box behind the active word) and a 2-frame scale pop (two extra states per word in `word_states`, `engine/captions.py:453`). Let captions use the `titles.FONTS` table so they match the titles. Add `set_edit(caption_emphasis={item_id: [words]})` drawn in the highlight colour at 1.15x.
- (c) 1.5 days. (d) High for talk and vlog edits, none for montages. (e) None.
- (f) Skill: "Talk edits: `pop` or `karaoke-box`, 2 to 3 words at a time, same font family as the titles. Emphasise at most one word per sentence, the one a viewer would repeat."

### T7. Emoji and stickers

- (a) Emoji inside titles and standalone sticker overlays with a pop.
- (b) Split text into runs; draw emoji runs with `/System/Library/Fonts/Apple Color Emoji.ttc` at size 160 with `embedded_color=True` (measured working), scale to the text size. New template `sticker` reusing `_pop`.
- (c) 0.5 day. (d) Low to medium. (e) Use the system font at render time; never bundle it.
- (f) Skill: "At most 2 stickers per edit, funny or party edits only."

### T8. Tracked text (later)

- Pin a label to a moving subject with `VNTrackObjectRequest` or per-frame face boxes (both present in pyobjc Vision). 2 days, medium impact, niche. After T3.

---

## 3. Area 2: transitions and effects

What modern edits actually use, in order of frequency: hard cuts, whip pans, zoom-through, flash or dip, dissolve, speed ramp into a cut, light leak, match cut, glitch (rare). Adding more wipes from xfade's list would make edits look like slideshows; the built-ins worth adding are only the missing directions.

### X1. Eased motion

- (a) Push, pull and drift ease in and out; punch snaps with a short eased overshoot.
- (b) `motion_filter` (`engine/render.py:311`): replace `u = min(in/N,1)` with a smoothstep of it (`u*u*(3-2*u)`), keep linear for drifts shorter than 1 s. Punch (`:331`): replace the `mod(gte())` step with a sum of decaying bumps like the shake envelope at `:335`. Optional `motion_ease` enum on `set_clip_fx`. Bump `RENDER_V`.
- (c) 0.25 to 0.5 day. (d) High for the effort. (e) None.
- (f) Skill: no new choices; note that motion now reads at lower amounts, so drop the default range to 0.25 to 0.4.

### X2. Real whips and zoom with motion blur (day-one version)

- (a) Whip becomes a fast slide with directional blur; zoom-in gets blur at its peak. Add `whip-up` and `whip-down`.
- (b) `edit.XFADE` (`engine/edit.py:45-48`): map whips to `slideleft/right/up/down`. In `render._graph` (`engine/render.py:527`), after the xfade for a whip append `dblur=angle=0|90:radius=R:enable='between(t,a,b)'` (two windows, stronger in the middle third), using the offset the function already tracks in `acc`. Measured 1.09 s against 0.70 s, and the mid frame is properly streaked. Cap whip duration at 0.35 s in `put_transition`.
- (c) 0.5 day. (d) High. (e) None.
- (f) Skill: "Whip in the direction the camera or subject was moving at the end of the outgoing shot (see `suggest_seams`). 0.2 to 0.3 s. Always with `whoosh`. At most one whip per 8 to 10 s."

### X3. Seam clip engine in numpy and cv2

- (a) Transitions rendered as small cached clips, so any effect is a short Python function instead of an ffmpeg expression.
- (b) New `engine/transitions.py`: registry `{name: fn(a, b, p) -> frame}`. Read the last N frames of segment A and first N of B from the cached segment files through ffmpeg rawvideo pipes, composite, encode with the segment codec, cache under a key of both segment keys plus type. `render._graph` gets a third seam kind: trim A's tail, concat the seam clip, trim B's head; audio keeps today's `acrossfade` (`engine/render.py:528`). First set:
  - `zoom-through` (A accelerates in with radial blur, B settles from 1.3x)
  - `whip-*` with blur proportional to velocity
  - `light-leak` and `film-burn` (procedural warm blobs, screen blend, peak on the cut; nothing to licence)
  - `luma-fade` (B's bright areas arrive first)
  - `glitch` (RGB split, block shifts, 2 frame stutter)
  - `spin`
- (c) 2.5 days. (d) High. (e) None new. Cost: about 2 s per 12 frame seam at 1080x1920, about 0.5 s in preview (from the 168 ms per frame measurement).
- (f) Skill: "`zoom-through` into the hero shot or a new location. `light-leak` for calm and nostalgic edits, at most 2. `glitch` only if the reference has it. `luma-fade` instead of dissolve when the incoming shot has a bright sky or window."
- Why not GPU now:
  - xfade custom expressions run on stock ffmpeg but are slow (2.44 s for a trivial one here). The xfade-easing project (MIT, 3.6.x, web) ports GL transitions to expressions; it needs single-threaded filtering and its own numbers show complex ones taking minutes.
  - gl-transitions and libplacebo shaders need a custom ffmpeg build.
  - ffmpeg's `coreimage` filter is single input, so Core Image transition filters are out of reach through ffmpeg.
  - A Swift sidecar with Core Image or Metal is the real GPU route: 3 to 5 days plus signing and bundling. Only if X3 proves too slow.

### X4. `suggest_seams`: let Claude choose with data

- (a) A read-only tool that returns, per seam, the facts an editor would look at and one suggestion.
- (b) New MCP tool `suggest_seams(collection_id)` and route `GET /collections/{cid}/edit/seams`. Per seam:
  - music: on a downbeat, section change, energy jump (from M1)
  - motion: direction and size at A's tail and B's head (reuse `reference._camera`, `engine/reference.py:230`, on proxies)
  - similarity: cosine of the stored scene embeddings plus a colour histogram distance
  - place and time gap from Photos metadata
  - suggestion, by rule: same scene gives cut; pan right gives whip-right; new place on a downbeat gives whip or zoom-through with whoosh; energy drop gives dissolve; very similar framing gives match cut (hard cut plus a reframe hint)
  - `get_edit` warns when more than about 30% of seams carry a transition.
- (c) 1.5 days. (d) High. (e) None.
- (f) Skill: "After ordering the clips, call `suggest_seams` once. Follow it unless the brief disagrees. Hard cut is the default; a transition needs a reason from the tool (place change, section change, motion to follow)."

### X5. Speed ramps that look intentional

- (a) Smooth ramps, beat-locked "velocity" ramps, and real slow motion where the source allows it.
- (b) Three parts:
  - Smooth: add 2 eased phases between the constant ones in `edit.RAMPS` (`engine/edit.py:54`) or generate 8 to 10 small steps in `ramp_setpts` (`engine/render.py:294`).
  - `ramp="velocity"`: alternate fast and slow with breakpoints on `seg.beat_frames`.
  - Source frame rate: `probe_source` already reads fps (`engine/render.py:86-90`); return it from `get_media` and `find_moments` so Claude picks 60 to 240 fps clips for slow motion. For 30 fps sources, add `tmix` frame blending in previews and `minterpolate` in finals for the slow phase only.
- (c) 1.5 days. (d) High for action edits. (e) None. `minterpolate` costs about 6 s per output second at 1080x1920 (measured), so limit it to one clip per edit.
- (f) Skill: "Slow motion only on clips whose `fps` is 60 or more, or on one hero shot. `velocity` on 1 or 2 action shots with clear movement, never on faces talking. Interpolated slow motion warps on water, crowds and hands: check the preview."
- Later: VideoToolbox `VTFrameProcessor` gives ML frame-rate conversion and motion blur (web: macOS 15.4+, Apple silicon). pyobjc `VideoToolbox` is not installed and its coverage of these classes is unverified, so this means a Swift helper.

### X6. Auto reframe from cached saliency

- (a) `reframe_mode="auto"` keeps the subject in a 9:16 crop.
- (b) `sal_x`, `sal_y`, `faces` are already in `data/moments/<uuid>.json` (`engine/moments.py:135`). Fit a constant or linear pan over the clip's source range (the `track` mode designed in `notes/cli-editing-research.md:93-106`, never built), write it as the existing `pan` values so the renderer needs no change (`engine/render.py:369-380`). Store face boxes as well as counts so faces outweigh saliency. `get_edit` warns when a center crop cuts off a face.
- (c) 1 day. (d) High for vertical edits from landscape footage. (e) None.
- (f) Skill: "Vertical edit from wide footage: set `reframe_mode="auto"` on every clip, then fix only the ones the preview shows wrong."

### X7. Colour match across clips

- (a) Neighbouring clips share exposure and white balance.
- (b) `set_edit(match_color=true)`: sample 5 proxy frames per clip, Lab mean and spread (the machinery is in `reference.bake_cube`, `engine/reference.py:348`), solve clamped exposure, warmth and saturation offsets toward the project median, write them into each clip's `adjust` so they stay visible and editable.
- (c) 1 day. (d) Medium to high. (e) None.
- (f) Skill: "Run `match_color` once before choosing a look. Do not hand-tune exposure unless the preview still shows a jump."

### X8. Finish layer

- (a) Grain, vignette, bloom and letterbox on the whole edit.
- (b) `spec["finish"] = {grain, vignette, bloom, letterbox}` (0 to 1 each, letterbox `none|2.39`), applied in `_graph` before the text overlay so titles stay clean (`engine/render.py:539-548`). Filters: `noise=c0s=..:c0f=t+u`, `vignette`, `coreimage=CIBloom` or `gblur` plus screen `blend`. Raise `FINAL_MBPS` by about 30% when grain is on.
- (c) 0.5 day. (d) Medium. (e) None.
- (f) Skill: "Calm and cinematic: grain 0.3, vignette 0.3. Snappy: none or grain 0.15. Letterbox only on 16:9 cinematic edits. Never bloom plus a strong look."

### X9. Sound design at the seams

- (a) Better seam sounds, text sounds, no clicks.
- (b) Three parts:
  - Declick: 10 ms `afade` in and out on each segment's audio in `seg_graph` (`engine/render.py:398-401`).
  - Sounds: replace or extend `SFX_SYNTH` (`engine/render.py:466`) with about 10 curated CC0 files (3 whooshes, 2 impacts, riser, reverse swell, pop, shutter, tape stop) and add `pop`, `shutter`, `swell` to `edit.SFX`.
  - `set_text(sfx="pop")` puts a sound at a text's start.
- (c) 0.5 to 1 day, mostly listening and choosing. (d) Medium to high. (e) CC0 only, so bundling needs no credit; keep a source list in the repo.
- (f) Skill: "`shutter` on a photo burst, `pop` on a title, `swell` into the drop instead of `riser` when the music is soft. Still 3 to 5 sounds per 30 s."

### X10. J and L cuts (later)

- Per seam `audio_lead` of up to 0.5 s needs segment audio rendered with handles. 1.5 to 2 days. Worth it for talk and vlog edits only.

### Not worth building

- Stabilization: `vidstab` is not in this ffmpeg, `deshake` is weak, and iPhone footage is stabilized in camera. If a shaky source shows up, a cv2 feature-track and smooth pass is about 60 lines.
- More xfade wipes, slices, pixelize, page curls.

---

## 4. Area 3: music

### Sources

| Source | API | Licence | Verdict |
|---|---|---|---|
| Openverse (today) | Yes, anonymous, 20 results per page | Aggregates CC; Kioku filters to CC0, PDM, BY, optional BY-NC | Keep as the no-key default. Weak metadata: no tempo, no mood, no instrumental flag. |
| Jamendo direct | Yes, v3.0, needs a free `client_id` (web) | CC per track; response carries licence flags and `audiodownload_allowed` | Add. Filters Openverse lacks: `speed` (verylow to veryhigh), `vocalinstrumental`, `fuzzytags`, `durationbetween`, `boost=popularity_month`, `include=musicinfo`. API terms for app use: unverified, read them before shipping. |
| Incompetech (Kevin MacLeod) | Static JSON catalog, no key (web: `pieces.json`, `genre.json`) | CC BY 4.0 | Add. Fields include bpm, feel, genre, instruments, length. Cheap to index whole. Downside: the tracks are widely recognised. |
| Freesound | Yes, v2, free token; previews need no OAuth, originals do (web) | CC0, BY, BY-NC per sound | Use directly for sound effects (duration, licence and bpm filters; similarity search in a `laion_clap` space). Already reached through Openverse today. |
| Wikimedia Commons | Through Openverse | Mixed CC | Already covered. |
| Epidemic Sound | Partner API is gated or tiered (web sources disagree; help page returned 403). An official MCP connector exists and needs the user's subscription (web) | Subscription licence | No engine work. If the user has a subscription and the connector, Claude searches there and passes the downloaded file to `set_audio`. |
| Artlist, Soundstripe, Uppbeat | Not checked in depth | Paid | Skip. Unverified. |
| Pixabay Music | API covers images and video only (web) | Pixabay licence | No API. Skip. |
| YouTube Audio Library | No API found | Platform terms | Manual download only. Skip. |
| Free Music Archive | Old API not confirmed working (web) | Mixed CC | Skip. |
| ccMixter | Not verified | CC | Skip unless Openverse results prove thin. |
| User's `~/Music` (today) | Local scan | User handles rights | Keep and improve (M4). FairPlay files are already skipped (`engine/sounds.py:265-267`). |

Licensing notes:
- CC BY needs the credit line where the video is posted; Kioku already writes it.
- BY-NC is unsafe for monetized channels; the Settings switch already covers this.
- A CC track can still trigger a Content ID claim if its artist registered it. Keep the stored licence URL and the source link with the render.
- The user's own commercial music will be muted or claimed on Instagram, TikTok and YouTube. See M6.

### M1. Music map: energy, sections, drop

- (a) Claude gets a readable picture of a track: tempo, energy per bar, section boundaries, the drop, where vocals are.
- (b) Extend `beats.analyze` (`engine/beats.py:49`) or add `engine/music.py`. From the mono signal `beats._decode` already returns:
  - RMS and low-band (under 150 Hz) energy per beat with `scipy.signal.stft` (installed)
  - average per bar using the downbeats, normalise to 0 to 9
  - section boundaries from energy novelty at bar starts (the same idea as `reference._sections`, `engine/reference.py:295`)
  - drop = the largest low-band jump that lands on a downbeat
  - vocals per bar from the SoundAnalysis classifier the engine already runs for moments (`engine/moments.py:177`)
  - cache next to the beat JSON; bump `BEATS_V`
  - New MCP tool `analyze_music(track_id or sound_id)` returning `{tempo, bars, energy: "2234456788997764...", sections: [{start, end, level}], drop_s, vocals: [...], suggested_windows}`. `get_edit` adds per clip `energy` (the bar level under each clip).
- (c) 1 day. (d) High. (e) None new. librosa 1.0 (web: August 2026, ISC, numba 0.61+) would add about 26 packages and a JIT pause for what numpy and scipy do here. essentia is AGPL. Skip both.
- (f) Skill: "After `add_sound`, call `analyze_music`. Put the hero shot on `drop_s`. Low-energy bars get longer shots (4 to 8 beats) and calm motion; high-energy bars get 1 and 2 beat shots. Open on the intro, not mid-chorus, unless the edit is under 15 s."

### M2. Fit the music to the edit

- (a) The track starts at a sensible place, the drop lands where Claude wants it, and the music ends on a bar instead of a fade in the middle of a phrase.
- (b) New params on `set_audio` / `add_sound`: `fit="auto"`, `target_seconds`, `drop_at` (output seconds). The engine picks `source_in` on a downbeat and `source_out` on a bar boundary from M1, sets a one-bar fade out, and returns the choice. Version 2 (2 more days): shorten a song by splicing at two downbeats in the same phrase position with the most similar spectrum, with a one-beat crossfade, so the real ending is kept.
- (c) 0.5 day for v1 once M1 exists. (d) High. (e) None.
- (f) Skill: "Use `fit="auto"` with the target length. For reels pass `drop_at` at 30 to 40% of the length. Never end on a hard cut of the music: the last clip should sit on the final bar and hold through the fade."

### M3. Choose from analysed candidates

- (a) Instead of guessing from titles, Claude compares 3 to 5 downloaded candidates by tempo, energy shape, vocals and drop.
- (b) `search_sounds(..., analyze=3)` downloads the top N previews through the existing `sounds.download` and returns a one-line M1 summary for each. Add filters `tempo_min`, `tempo_max`, `instrumental`, mapped to Jamendo `speed` and `vocalinstrumental` and to Incompetech `bpm`. New providers behind the same route: `sounds.search` gains `provider=openverse|jamendo|incompetech`; the `sounds` table already stores licence and attribution per file (`engine/sounds.py:47-51`). The Jamendo `client_id` goes in Settings; without it the provider is hidden.
- (c) 2 days. (d) High. (e) Jamendo key (free, user supplied). No paid key assumed.
- (f) Skill: "Search 2 or 3 phrasings, analyse the top 3, pick by fit: tempo 100 to 130 for snappy, 70 to 100 for calm; instrumental when clips have speech; a clear drop for reels. Tell the user the 2 runners-up."

### M4. The user's own library, searchable by feel

- (a) "Upbeat summer roadtrip" finds matching tracks in `~/Music`.
- (b) CLAP through `transformers.ClapModel` (transformers and torch are already dependencies). Checkpoint `laion/larger_clap_music` (web: Apache-2.0 on its model card; download size unverified, expect several hundred MB). Embed three 10 s windows per track in a background job, store vectors the way scene embeddings are stored, cosine against the text query in `sounds.local` (`engine/sounds.py:311`). Also cache the M1 summary per file. Use the same model to re-rank free candidates in M3.
- (c) 1.5 to 2 days. (d) Medium to high, depending on how much music the user keeps locally. (e) Model download on first use, on device after that. MuQ-MuLan weights are CC BY-NC (web), so not that one.
- (f) Skill: "Try `source="mine"` first when the user mentions their own music or a named song. Remind them that commercial tracks get muted on social platforms."
- Optional: Music.app metadata (BPM, genre, play count, loved) through the iTunesLibrary framework. Whether it works for an unsandboxed signed app is unverified.

### M5. Arc alignment check

- (a) A warning when the edit's intensity fights the music.
- (b) In `get_edit`, compare per-bar music energy (M1) with per-clip intensity (shot length in beats, motion amount, moment score). Warn on mismatches such as "bars 9 to 12 are the loudest but hold one 8 beat shot" or "the hero shot sits 2 bars before the drop".
- (c) 0.5 day after M1. (d) Medium. (e) None.
- (f) Skill: "Fix arc warnings before the first preview."

### M6. Guide-track export for social posts

- (a) Cut to the user's song, export without it, and tell the user the exact offset so they add the same song inside Instagram or TikTok and stay in sync.
- (b) `render_final(music="mute")` drops the sync track from the mix but keeps cuts on its grid; the result includes `music_offset_s` and the track name.
- (c) 0.25 day. (d) Medium, and it removes the largest legal risk. (e) None.
- (f) Skill: "When the user wants a trending or commercial song on a reel, cut to their file and export with `music="mute"`; give them the song name and the start offset."

---

## 5. Top 10 roadmap (impact per effort)

| # | What | Proposals | Days | Impact | Risk |
|---|---|---|---|---|---|
| 1 | Eased motion and blurred whips | X1, X2 | 1 | High | Low. Bump `RENDER_V`. |
| 2 | Music map and auto fit | M1, M2 v1 | 1 | High | Low. Drop detection is a heuristic; show the energy string so Claude can overrule it. |
| 3 | Face-aware text placement and safe areas | T1, T4 | 1 | High | Low. Also makes `SKILL.md:88` true. |
| 4 | Auto reframe from cached saliency | X6 | 1 | High | Low. Data already exists. |
| 5 | Seam sounds, text sounds, declick | X9 | 0.5 to 1 | Medium to high | Low. Curation time. |
| 6 | Text pack: text-behind, word-on-beat, tokens, style presets, subpixel | T2, T3, T5 | 3.5 | High | Medium. Mask flicker on busy shots. |
| 7 | `suggest_seams` | X4 | 1.5 | High | Low. |
| 8 | Seam clip engine (zoom-through, light leak, luma fade, glitch) | X3 | 2.5 | High | Medium. New cache kind and join path. |
| 9 | Music search v2: analysed shortlist, Jamendo and Incompetech, own library by feel | M3, M4 | 3.5 to 4 | High | Medium. Jamendo terms, model download. |
| 10 | Colour match and finish layer | X7, X8 | 1.5 | Medium to high | Low. |

Next in line: velocity ramps and slow-motion quality (X5, 1.5 days), captions v2 (T6, 1.5 days), arc check and guide-track export (M5, M6, under 1 day together), music shortening by splice (M2 v2), J and L cuts, tracked text, emoji.

Items 1 to 3 touch different files (`render.py` and `edit.py`; `beats.py` and the audio routes; `titles.py`, `captions.py` and render step 4), so three agents can build them in parallel worktrees.

## 6. Risks and things not to do

- Do not add an HTML or Chromium renderer, Remotion, or Lottie for text. Pillow covers the looks listed here.
- Do not depend on a custom ffmpeg build (vidstab, libplacebo, gl-transition). The app uses whatever ffmpeg is on the Mac, and Homebrew's is getting smaller.
- Do not expose more xfade built-ins "because they exist". Every new enum is something Claude will overuse. Each new effect ships with a cap in validation, the way shake has one.
- Do not let effects outrun the caps: one accent per shot, transitions on under about 30% of seams, one `text-behind`, one interpolated slow motion.
- Do not add librosa, essentia or madmom for music analysis. numpy, scipy and `beat_this` are enough.
- Do not generate music. `notes/pro-editing-design.md:3` says no generation, and licences for local generators are unclear (web sources disagree on Stable Audio Open Small's commercial terms).
- Do not scrape Pixabay, YouTube Audio Library, Mixkit or similar. No API, and their terms do not cover redistribution through an app.
- Do not assume a paid key. Jamendo and Freesound keys are free but user supplied; hide those providers when the key is absent.
- Vision calls must go through `core.VISION_SLOTS` (`engine/core.py:276`); per-frame masks for long texts will queue behind indexing. Keep `text-behind` short (under 4 s).
- Cache keys: anything that changes pixels in a segment needs to be in `seg_key` (`engine/render.py:256`) or behind a `RENDER_V` bump. Seam clips need their own key.
- `minterpolate` warps on water, crowds and fingers. Final renders only, one clip, and the preview must show a blended stand-in so Claude can still judge timing.
- Grain raises the bitrate needed for the same quality. Tie `FINAL_MBPS` to it.
- Non-Latin titles: Pillow here has no raqm, so Devanagari and Arabic will not shape correctly. Fix with CoreText before promising Hindi titles.
- Drop and section detection will be wrong on some songs (no clear drop, live recordings). Always return the raw per-bar energy so Claude and the user can overrule.
- Unverified items to check before building on them: Epidemic Sound API tiers, Jamendo API terms for apps, skia-python arm64 wheels, CLAP checkpoint size, iTunesLibrary access from this app, platform safe-area numbers, pyobjc coverage of `VTFrameProcessor`.

## 7. Sources

- Local: ffmpeg 9.0.2 filter list and timings, engine venv probes (this session).
- Jamendo tracks API parameters: https://developer.jamendo.com/v3.0/tracks
- Freesound API v2 resources: https://freesound.org/docs/api/resources_apiv2.html
- Incompetech machine-readable catalog: https://incompetech.com/agent-section/
- Epidemic Sound MCP and API docs: https://developers.epidemicsound.com/docs/mcp/ and https://developers.epidemicsound.com/docs/
- Pixabay API docs: https://pixabay.com/api/docs/
- LAION CLAP music checkpoint: https://huggingface.co/laion/larger_clap_music
- MuQ-MuLan licence: https://huggingface.co/openmuq/muq-mulan-large
- xfade-easing: https://github.com/scriptituk/xfade-easing
- librosa 1.0: https://librosa.org/blog/posts/1.0/
- Remotion licence: https://remotion.dev/license ; HyperFrames: https://github.com/heygen-com/hyperframes
- VideoToolbox frame processing: https://developer.apple.com/documentation/videotoolbox/vtmotionblurconfiguration
- Vision foreground mask: https://developer.apple.com/documentation/vision/vngenerateforegroundinstancemaskrequest
- Homebrew ffmpeg slim-down: https://github.com/Homebrew/homebrew-core/pull/261303
- skia-python: https://pypi.org/project/skia-python
