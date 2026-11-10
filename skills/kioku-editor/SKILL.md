---
name: kioku-editor
description: Edit a video from the user's own Photos library with Kioku. Use when the user asks to make, cut, assemble or recap a video, reel, montage, story or trip edit from their photos/videos ("make a 45s video of Japan", "cut a reel of the beach day", "recap October", "make it snappy", "cut it to this song", "edit it like this video / this YouTuber"). Finds the best moments in their footage, cuts to the beat of music, adds motion, transitions, sound effects, titles and a look, can copy the style of a reference video, then renders a finished video or a DaVinci Resolve timeline.
---

# Kioku editor

You edit videos from the user's Photos library through the `kioku` MCP tools. Projects hold pointers (Photos uuid + in/out seconds). Nothing is copied until a render or export, and Photos itself is never modified.

If the tools error with "engine isn't running", tell the user to open Kioku (or run `uv run server.py` in `kioku/engine`) and stop.

## 1. Brief (no questions unless it's truly ambiguous)

Work out, and default when unstated:
- **Subject**: what or who, which trip or event, which dates.
- **Length**: default 30-45 s. A reel is 15-30 s, a recap 60-90 s.
- **Style**, which decides everything below:
  - **Snappy** (default for reels, trips, parties, sports, "make it cool"): music, beat sync, motion, 9:16 if it's a reel.
  - **Calm** (memories, family, "slow", "cinematic"): music on a half or bar grid, long shots, dissolves, gentle push/drift only.
  - **Talk / vlog** (clips with speech): longer takes, captions, music ducked low or none.
  - **Like a reference**: the user gave a video file. Do section 2b first.
- **Order**: chronological by default. Use a story arc (arrive, highlights, wind down) when it fits.

Create the project with `create_collection(name, "project")`, then `set_brief` with one line covering all of it. If the user named an existing project, find it with `list_collections` and edit that one.

## 2. Gather candidates

- Run `search_media` 4-8 times with different phrasings: the place, the people, activities, objects, times of day, plus Photos-style labels ("Beach", "Sunset", "Crowd"). Use `kind="video"` for motion, and photos as accents.
- Use dates ("October 2026") and place names to stay on topic. Drop screenshots, documents, receipts and other events.
- Aim for 2-3 times more candidates than final shots. `best_moment` on a hit means that scene matched; prefer it.

### 2b. Reference video (only when the user gave one)

`analyze_reference(path)` on the local file (links are not downloaded). Read its contact sheet and its PLAN: sync grid, beats-per-shot pattern, motion / flash / punch budgets, transition mix with sounds, text style, look. Use the plan's numbers instead of the defaults below for the rest of the edit.

## 3. Find the moments

1. `find_moments(uuids, profile, query)` on the candidate videos, up to 6 per call, several calls in parallel. It ranks 1-4 s windows (laughs, cheers, faces, motion peaks, aesthetics, loudness) and returns a sheet with before / peak / after frames. Profile: `action` (sports, trips), `people` (parties, family), `scenic` (landscapes), `talk` (vlogs), `auto` otherwise. Pass the brief's key subject as `query`.
2. Judge the sheet yourself: the score is a hint, the frames are the truth. Drop moments that are blurry, dark, or show the phone being raised.
3. `analyze_clip(uuid)` only when you need a clip's full scene list (a long clip with several events, or `find_moments` found nothing good). Several in parallel, 3-6 at a time.
4. Photos: `look_at([uuids])`, up to 6 per call, before using any.

## 4. Build the cut

- `add_to_collection` the chosen moments. Each moment is its own item; the same uuid can appear more than once.
- `set_clip_range(item_id, in, out)` from the moment's t0/t1 (a little wider is fine when synced: the beat grid sets the final length).
- Variety: alternate wide and close, people and places. Never two near-identical shots back to back. Strongest moment near the end or on the music's drop.
- Reorder with `move_clip` (positions are floats: 2.5 goes between 2 and 3).

## 5. Music and rhythm (snappy, calm and reference edits)

1. Music: `search_sounds` then `add_sound`, or `set_audio` with the user's own file. Pick music that matches the style (energy, tempo).
2. `set_edit(sync_track=<track id>)`. Every cut now lands on the beat. `sync_grid`: `beat` for snappy, `half` or `downbeat` for calm. Read `grid` from `get_edit` to think in beats.
3. Shot lengths in beats with `set_clip_fx(beats=)`. Snappy: mostly 2, bursts of 1 (2 to 4 shots in a row), a 4 or 8 for the hero shot. Calm: 4 to 8. Never the same length 4 times in a row.
4. `hit`: set it to the moment's `peak` (the jump, the splash, the laugh starting) so it lands on a beat.
5. Check `get_edit`: the total should land within ±15% of the target length. Drop or add shots to fit; don't stretch the beats.

## 6. Motion, seams, sound

- **Motion** (`set_clip_fx`): snappy edits give most shots `push` or `drift-left` / `drift-right` at `motion_amount` 0.3 to 0.5, and photos always move. Calm: `push` 0.2 to 0.3 on some shots.
- **Accents**, peaks only and at most one per shot: `punch` on 1 to 3 shots, `flash` on the drop or the single biggest moment, `shake` on 1 or 2 hard hits, `ramp="slow-mid"` on one hero action shot (its own sound goes silent).
- **Seams** (`set_transition`): mostly hard cuts.
  - Snappy: `whip-left` / `whip-right` / `zoom-in` with `sfx="whoosh"` for a change of place, `flash-white` into the drop, `sfx="hit"` on a hard cut at the drop. Not every seam needs a sound; 3-5 sounds in 30 s is plenty.
  - Calm: `dissolve` 0.4 to 0.6 s between places, no transition inside one scene, never whips or wipes.
- Unsynced transitions shorten the total; recheck with `get_edit`.

## 7. Format, look, text, captions

- One `set_edit` call: `aspect="9:16"` for a reel, story, TikTok, Shorts or "vertical", otherwise 16:9. One `look` for the whole edit at `look_strength` 0.6 to 0.8 unless the user wants it strong. Options are in `get_edit`; film LUTs (`teal-orange`, `warm-film`, `bleach`, `clean-pop`) suit trips and nights, `warm` / `film` suit memories.
- **Titles** (`set_text`), on snappy and reference edits, or when the brief wants them:
  - An opener: `title-pop` or `title-slide` on the first downbeat (a `grid` time), 1.5 to 2.5 s.
  - A `location-tag` when the place changes, on that clip's first beat. `big-number` for a year or a count.
  - At most 1 text on screen, under 6 words. Keep `position="auto"` so text stays off faces.
- **Captions** (`set_edit(captions=true)`) only when clips contain speech, never on a music montage. Check `get_edit` `captions_status.pending` reaches 0 before rendering. If whisper is missing, tell the user the install command the tool returns.
- `set_clip_fx` adjustments only for a reason: `speed` 0.5 for slow-mo, 1.5-2 for a boring stretch, `reframe_*` when the crop cuts off the subject, `exposure` / `warmth` to match a shot to its neighbours.

## 8. Check

- `render_preview` (use `start` and `end` for one section), then `render_status(wait_seconds=60)` and look at the contact sheet. Fix what you see (cuts, crop, colour, text position). At most 3 rounds.
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
