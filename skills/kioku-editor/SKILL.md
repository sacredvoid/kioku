---
name: kioku-editor
description: Edit a video from the user's own Photos library with Kioku. Use when the user asks to make, cut, assemble or recap a video, reel, montage, story or trip edit from their photos/videos ("make a 45s video of Japan", "cut a reel of the beach day", "recap October", "make it snappy", "cut it to this song", "edit it like this video / this YouTuber"). Finds the best moments in their footage, cuts to the beat of music, adds motion, transitions, sound effects, titles and a look, can copy the style of a reference video, then renders a finished video or a DaVinci Resolve timeline.
---

# Kioku editor

You edit videos from the user's Photos library through the `kioku` MCP tools. Projects hold pointers (Photos uuid + in/out seconds). Nothing is copied until a render or export, and Photos itself is never modified.

If the tools error with "engine isn't running", tell the user to open Kioku (or run `uv run server.py` in `kioku/engine`) and stop.

## 0. Creative direction (ask before you search)

Skip this section when the request already contains "Creative direction, already decided by the user", when the user said "just make it" / "surprise me", or when you are running inside the Kioku app (you can't ask there).

Otherwise ask ONE round of questions before any editing, with your question tool if you have one (multiple choice, at most 4), else as a short numbered list:
1. Run `search_media` 1-2 times first, so the questions name real footage.
2. Ask what the request leaves open, most important first:
   - **Vibe**: snappy / calm / cinematic / funny / vlog.
   - **Length and shape**: 15 / 30 / 60 / 90 s; vertical 9:16 or wide 16:9.
   - **Which footage**: the real choices you found (which day or trip, whose story, the hero moment to build to).
   - **Music**: pick one for them, their own track (a file path), or none.
   - **Reference**: "Got a video you want it to feel like? A YouTube link or a file."
   - **Titles**: an opening title and its text, location tags.
3. Don't ask what the request already answers. Wait for the answers, then continue with section 1 using them.

## 1. Brief (no questions unless it's truly ambiguous)

Work out, and default when unstated:
- **Subject**: what or who, which trip or event, which dates.
- **Length**: default 30-45 s. A reel is 15-30 s, a recap 60-90 s.
- **Style**, which decides everything below:
  - **Snappy** (default for reels, trips, parties, sports, "make it cool"): music, beat sync, motion, 9:16 if it's a reel.
  - **Calm** (memories, family, "slow", "cinematic"): music on a half or bar grid, long shots, dissolves, gentle push/drift only.
  - **Talk / vlog** (clips with speech): longer takes, captions, music ducked low or none.
  - **Like a reference**: the user gave a video file or link. Do section 2b first.
- **Order**: chronological by default. Use a story arc (arrive, highlights, wind down) when it fits.

Create the project with `create_collection(name, "project")`, then `set_brief` with one line covering all of it. If the user named an existing project, find it with `list_collections` and edit that one.

## 2. Gather candidates

- Run `search_media` 4-8 times with different phrasings: the place, the people, activities, objects, times of day, plus Photos-style labels ("Beach", "Sunset", "Crowd"). Use `kind="video"` for motion, and photos as accents.
- Use dates ("October 2026") and place names to stay on topic. Drop screenshots, documents, receipts and other events.
- Aim for 2-3 times more candidates than final shots. `best_moment` on a hit means that scene matched; prefer it.

### 2b. Reference video (only when the user gave one)

`analyze_reference(path)` on the file or link (YouTube and most video sites are downloaded once; longer links use their first 15 min). Read its frame grid and its PLAN: sync grid, beats-per-shot pattern, motion / flash / punch budgets, transition mix with sounds, text style, look. Use the plan's numbers instead of the defaults below for the rest of the edit.

## 3. Find the moments

1. `find_moments(uuids, profile, query)` on the candidate videos, up to 6 per call, several calls in parallel. It ranks 1-4 s windows (laughs, cheers, faces, motion peaks, aesthetics, loudness) and returns a sheet with before / peak / after frames. Profile: `action` (sports, trips), `people` (parties, family), `scenic` (landscapes), `talk` (vlogs), `auto` otherwise. Pass the brief's key subject as `query`.
2. Judge the sheet yourself: the score is a hint, the frames are the truth. Drop moments that are blurry, dark, or show the phone being raised.
3. `analyze_clip(uuid)` only when you need a clip's full scene list (a long clip with several events, or `find_moments` found nothing good). Several in parallel, 3-6 at a time.
4. Photos: `look_at([uuids])`, up to 6 per call, before using any.

## 4. Build the cut

- `add_to_collection` the chosen moments. Each moment is its own item; the same uuid can appear more than once.
- `set_clip_range(item_id, in, out)` from the moment's t0/t1 (a little wider is fine when synced: the beat grid sets the final length).
- Variety: alternate wide and close, people and places. Never two near-identical shots back to back. Strongest moment on the music's drop (real time from `fit` / `get_edit`), else near the end.
- Reorder with `move_clip` (positions are floats: 2.5 goes between 2 and 3).

## 5. Music and rhythm (snappy, calm and reference edits)

1. Music: choose from data, not titles. `search_sounds(query, target_seconds=<planned length>)` returns a ranked shortlist; the top 3 carry `analysis` (tempo, `length_s`, `curve` = energy 0 to 9 over eight slices, `shape`, `drop_s`). Search 2 or 3 phrasings (mood words: "upbeat summer", "calm piano"), then pick by fit: tempo 100 to 130 and a high curve for snappy, 70 to 100 and a low flat curve for calm, a `drop_s` for reels, `length_s` at least the edit length, `instrumental=true` when clips have speech. Tell the user what you picked and the 2 runners-up with one line each. Read `notes` (a source can be off, e.g. no Jamendo key; do not pester the user about it unless results are thin). `add_sound` with the winner, or `set_audio` with the user's own file. When the user mentions their own music or a named song, try `source="mine"` first (feel words work: "upbeat", "calm", "fast", "slow", "120 bpm"; if `message` says files are still being analysed, ask again after a minute) and remind them that commercial tracks get muted on social platforms. When you add CC BY music, say the credit line from the result.
2. Fit and read the music (you cannot hear it): `set_audio(track_id, fit=true, target_seconds=<planned length>)` (or `fit=true` on `add_sound`; for reels add `drop_at` at 30 to 40% of the length). It picks `source_in` / `source_out` on bar lines, puts the drop where you asked and fades out over the last bar. Then `analyze_music(collection_id, track_id)`: tempo, `energy` (one digit 0 to 9 per bar), `sections`, `drop_s`. The fit result gives `drop_out_s` (output seconds where the drop lands) and `music_end_s` (where the music ends). Raw digits beat the guesses: if the sections or the drop look wrong for the song, say so and use the digits. `drop_s` null means no clear drop: open on the intro and put the hero shot on the loudest section.
3. `set_edit(sync_track=<track id>)`. Every cut now lands on the beat. `sync_grid`: `beat` for snappy, `half` or `downbeat` for calm. Read `grid` from `get_edit` to think in beats.
4. Shot lengths in beats with `set_clip_fx(beats=)`. Snappy: mostly 2, bursts of 1 (2 to 4 shots in a row), a 4 or 8 for the hero shot. Calm: 4 to 8. Never the same length 4 times in a row. Follow the energy: low bars (0 to 3) get 4 to 8 beat shots and calm motion, high bars (7 to 9) get 1 and 2 beat shots. `get_edit` gives each clip the energy digit under it. Open on the intro, not mid-chorus, unless the edit is under 15 s.
5. Hero shot on the drop: the clip whose start is closest to `drop_out_s` (`get_edit` `music.drop_out_s`) is your strongest moment; give it `flash` or a `hit` there. Reorder with `move_clip` until it is, never invent a drop time.
6. `hit`: set it to the moment's `peak` (the jump, the splash, the laugh starting) so it lands on a beat.
7. Check `get_edit`: the total should land within ±15% of the target length, ideally ending at `music_end_s` so the last clip sits on the final bar and holds through the fade. Drop or add shots to fit; don't stretch the beats. If the music is shorter than the edit, say so in the summary.

## 6. Motion, seams, sound

- **Motion** (`set_clip_fx`): moves ease in and out, so small amounts read. Snappy edits give most shots `push` or `drift-left` / `drift-right` at `motion_amount` 0.25 to 0.4, and photos always move. Calm: `push` 0.2 to 0.3 on some shots.
- **Accents**, peaks only and at most one per shot: `punch` on 1 to 3 shots, `flash` on the drop or the single biggest moment, `shake` on 1 or 2 hard hits, `ramp="slow-mid"` on one hero action shot (its own sound goes silent).
- **Seams** (`set_transition`): mostly hard cuts.
  - Snappy: a whip or `zoom-in` with `sfx="auto"` for a change of place, `flash-white` into the drop, `sfx="hit"` on a hard cut at the drop. Not every seam needs a sound.
  - Whips (`whip-left` / `-right` / `-up` / `-down`) are blurred slides, 0.2 to 0.3 s, always with `whoosh`, at most one per 8 to 10 s. The name is where the picture moves: follow the motion at the end of the outgoing shot (a pan to the right moves the picture left, so `whip-left`). No motion to follow: hard cut instead.
- **Sounds** (`sfx` on `set_transition`, on `set_text`): real CC0 recordings, quiet on purpose. 3 to 5 in 30 s is the ceiling; `get_edit` warns past it. A sound is a choice, so silence is the default.
  - `auto` picks by transition type: `whoosh` for whips and zoom, `swish` for slides, wipes and blur, `hit` for `flash-white`, `swell` for `fade-black`. Dissolves and plain cuts stay silent.
  - `hit` on a hard cut at the drop (once, maybe twice). `riser` builds into one big seam, at most once per edit. `swell` is the softer lead-in: use it instead of `riser` when the music is soft or the edit is calm.
  - `shutter` only on a photo burst or a freeze-frame moment. `pop` on a title's first frame (`set_text(sfx="pop")`), for the opener or a `location-tag`, not on every text.
  - Never put a sound on every seam, never stack a riser and a hit on the same one, never use sounds in a calm or sentimental edit unless the user asks.
  - Hard cuts are declicked automatically (a few ms of fade each side), so clip audio never pops; do nothing for that.
  - Calm: `dissolve` 0.4 to 0.6 s between places, no transition inside one scene, never whips or wipes.
  - Effect transitions are rare accents, at most 3 per edit (the engine refuses more), about one per 10 s, each on a strong beat and never next to another effect or a flash:
    - `zoom-through` 0.3 to 0.5 s with `whoosh`: into the hero shot or a new location.
    - `light-leak` 0.6 to 1.2 s: calm, nostalgic or golden-hour edits, at most 2.
    - `luma-fade` 0.6 to 1 s: instead of `dissolve` when the incoming shot has a bright sky or window.
    - `glitch` 0.2 to 0.3 s with `hit`: once, on a drop in an energetic edit, or when the reference uses it.
    - If you cannot name the reason for an effect, use a hard cut.
- **After assembling and syncing the cut, call `suggest_seams(collection_id)` once.** It is read-only and returns, per seam, the beat and bar position, energy either side, motion and its direction either side, how alike the two shots are, place and time change, the current transition and one suggestion with a reason. Hard cut is the default; a transition needs a reason from the tool (place change, loud downbeat with motion to follow, energy drop). Apply the suggestions that fit the brief with `set_transition` (use its `duration` and `sfx`), skip the ones that fight it, and keep seams marked `type: "cut"`. Null means not analysed yet, not "no": run `analyze_music` or `analyze_clip` and call it again if it matters. Its caps (one whip per 8 s, transitions on at most 30% of seams) are the limits, not targets. A "match cut" hint means line the subject up with `reframe_pos`.
- Unsynced transitions shorten the total; recheck with `get_edit`.

## 7. Format, look, text, captions

- One `set_edit` call: `aspect="9:16"` for a reel, story, TikTok, Shorts or "vertical", otherwise 16:9. One `look` for the whole edit at `look_strength` 0.6 to 0.8 unless the user wants it strong. Options are in `get_edit`; film LUTs (`teal-orange`, `warm-film`, `bleach`, `clean-pop`) suit trips and nights, `warm` / `film` suit memories.
- **Colour match**: when the clips come from different moments, cameras or light (most trips and recaps), add `match_color=true` to that same `set_edit`, before judging the look. It evens exposure and white balance under the look. Pass `match_ref` (an item_id) when one shot has the colour you want everything near, else it matches to the edit's median. It leaves night shots, sunsets and deliberate colour mostly alone and says so in `get_edit` warnings; that is intended, do not force them. If one clip should keep its own colour (a stylised shot, a black-and-white moment), `set_clip_fx(match_color=false)`. Only hand-tune `exposure` / `warmth` if the preview still shows a jump after matching.
- **Finish** (`set_edit(finish=..., finish_strength=...)`): one texture layer for the whole video, under the text. Subtle by default; match the vibe:
  - Calm, nostalgic, memories: `film` at 0.5 to 0.8 (grain, vignette, warm halation round lights and the sun).
  - Cinematic 16:9 (travel films, "make it cinematic"): `cinematic` at 0.6 to 1. Its letterbox bars only appear on 16:9, so on a reel it is just light grain, vignette and glow.
  - Clean, bright, product-like or most snappy reels: `clean` at 0.5 to 1, or `none`.
  - Never pair a strong finish with a strong look (look_strength above 0.8): pick one to carry the style. Grain makes the file about 10 to 15% larger; that is expected.
- **Titles** (`set_text`), on snappy and reference edits, or when the brief wants them:
  - An opener: `title-pop` or `title-slide` on the first downbeat (a `grid` time), 1.5 to 2.5 s.
  - A `location-tag` (text `{place}`) when the place changes, on that clip's first beat. `big-number` for a year or a count.
  - Pick one `set_edit(text_style=...)` before the first title and never mix fonts by hand: `clean` or `editorial` for calm and cinematic edits, `bold` for snappy ones, `playful` for parties and kids. Use one animation family per edit: `letter-rise` or `tracking-in` for calm, `title-pop` for snappy.
  - `word-on-beat` for a 3 to 5 word hook in the first 3 seconds or on the drop (sync on, so each word lands on a beat). `typewriter` only for dates and places.
  - `text-behind` at most once, 1 to 2 words, under 4 s, on a single shot with one clear subject and empty space above it (the render warns and draws it in front otherwise; then move it to a better shot).
  - Write `{place}`, `{date}` or `{year}` instead of typing a place or date: they are filled from the clip's Photos metadata. If it errors, that clip has none: leave it out rather than guess.
  - At most 1 text on screen, under 6 words. Keep `position="auto"`: at render time it looks at the frames under the text and picks the band (bottom, top, or middle as a last resort) that covers the fewest faces and subjects. 9:16 text also stays clear of the app's buttons and caption area. If the render warns that a text covers a face, move it to another clip before forcing a position.
  - Do not add a box or change `color` for readability: the render adds a soft scrim or a box only when the background under the text is too bright or busy for the shadow alone.
- **Captions** (`set_edit(captions=true)`) only when clips contain speech, never on a music montage. Check `get_edit` `captions_status.pending` reaches 0 before rendering. If whisper is missing, tell the user the install command the tool returns.
- **Vertical from wide footage** (9:16 edit, landscape clips): set `reframe_mode="auto"` on each clip that has people or a clear subject, after the clips are analysed (`find_moments` / `analyze_clip`). It follows faces, then the main subject, with a slow smoothed pan. Clips without analysis or a subject use `reframe_pos` (default center), and `get_edit` warns. Check the preview; fix only the clips that are wrong with a manual `reframe_pos`. Default stays center, so do not set auto on clips that do not crop.
- `set_clip_fx` adjustments only for a reason: `speed` 0.5 for slow-mo, 1.5-2 for a boring stretch, `reframe_mode="auto"` when the crop cuts off the subject (see below), other `reframe_*` for a fixed or panned crop, `exposure` / `warmth` to fix one shot that colour match left alone or got wrong.

## 8. Check

- `render_preview` (use `start` and `end` for one section), then `render_status(wait_seconds=60)` and look at the frame grid. Fix what you see (cuts, crop, colour, text position). At most 3 rounds.
- If it feels busy, remove effects before adding new ones.
- Reference edits: `compare_to_reference(collection_id, path)` once after the first preview. Fix the top 2 gaps only, then render again.
- Never `render_final` unless the user asked for the finished video.

## 9. Finish

- A finished video means `render_final` (poll `render_status(mode="final", wait_seconds=60)`), then give the path, the .srt if captions are on, and the music credits file if there is one. A Resolve timeline means `export_project`, then poll `export_status` and give the path (File › Import › Timeline). No preference stated: render a preview and offer both in one line.
- Reply with a short plain summary:
  - the project name
  - the clip count, total length, music and tempo
  - the shape of the edit in one or two lines
  - anything you couldn't find
  - one concrete suggestion (a different song, a tighter middle, a title)

Don't paste uuids unless asked.

## Rules

- Never describe ffmpeg or filters to the user. Tool errors list the allowed values: fix the argument instead of retrying the same one.
- Never invent uuids or times. Only use what the tools returned.
- Effects stay within the caps above. More effects never fix a weak shot; a better moment does.
- Don't add, remove or retag photos the user didn't ask about. Editing a project is fine. Changing the library's tags is not, unless asked.
- If iCloud downloads fail during `find_moments` or `analyze_clip`, carry on with `search_media` labels and posters only, and tell the user that video previews need Photos access for the app running Kioku.
