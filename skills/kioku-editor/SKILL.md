---
name: kioku-editor
description: Edit a video from the user's own Photos library with Kioku. Use when the user asks to make, cut, assemble or recap a video, reel, montage or trip edit from their photos/videos ("make a 45s video of Japan", "cut a reel of the beach day", "recap October"). Turns a prompt into a Kioku project: finds footage, looks at the actual shots, cuts on scene boundaries, orders them, and exports a timeline for DaVinci Resolve.
---

# Kioku editor

You edit videos from the user's Photos library through the `kioku` MCP tools. Albums and projects hold pointers (Photos uuid + in/out seconds). Nothing is copied until export, and Photos itself is never modified.

If the tools error with "engine isn't running", tell the user to open Kioku (or run `uv run server.py` in `kioku/engine`) and stop.

## 1. Pin down the brief (no questions unless it's truly ambiguous)

Work out, and default when unstated:
- **Subject**: what or who, which trip or event, which dates.
- **Length**: default 30-45 s. A reel is about 15-30 s, a recap about 60-90 s.
- **Pace**: upbeat means 1.5-3 s shots, calm means 3-6 s shots, vlog means longer takes with speech.
- **Order**: chronological by default. Use a story arc (arrive, highlights, wind down) when it fits.

Create the project with `create_collection(name, "project")`, then `set_brief` with one line covering all four. If the user named an existing project, use `list_collections` and edit that one instead.

## 2. Gather candidates (search wide, then narrow)

- Run `search_media` 4-8 times with different phrasings: the place, the people, activities, objects, times of day, plus Photos-style labels ("Beach", "Sunset", "Crowd"). Use `kind="video"` for motion and photos only as accents.
- Use the date in labels ("October 2026") and place names to stay on topic. Drop hits that are clearly off-topic: screenshots, documents, receipts, other events.
- Aim for about 2-3 times more candidates than final shots.
- `best_moment` on a hit means that scene matched. Prefer it.

## 3. Look before you cut

For every candidate video, call `analyze_clip(uuid)`. Call it for several candidates in the same turn (parallel tool calls), about 3 to 6 at a time: downloads run in parallel, so this is much faster than one by one. Look at the contact sheet image. Each frame is one scene, labelled `#idx start-end s`. For each scene, note:
- **What happens** in it (judge from the frame, not from the labels alone).
- **sharpness**: under about 40 is usually blurry or dark. Avoid it unless it's the only shot of a key moment.
- **motion**: very high means camera shake or whip pans. Good for energy, bad for calm edits.
- Skip the first and last half-second of a clip if it looks like the phone being raised or lowered.

Photos have no scenes. Look at every candidate photo with `look_at([uuids])` (up to 6 per call) before using it. Use photos as 2-3 s stills when they're the best image of a moment, or when there's no video of it.

## 4. Build the cut

- Add chosen items with `add_to_collection`. Each scene you use is its own item (the same uuid can appear more than once with different ranges).
- Trim each one with `set_clip_range(item_id, in, out)`. Keep cuts inside a scene: never straddle a scene boundary unless the cut is meant to happen there. Use the scene's t0/t1, or a sub-range around the action.
- Add variety: alternate wide and close, people and places, and never use two near-identical shots back to back.
- Reorder with `move_clip` (positions are floats, so 2.5 goes between 2 and 3).
- Check `get_collection` at the end. The total should land within ±15% of the target length, so trim or drop shots to fit.

## 4b. Snappy edits: music, beats, motion (reels, recaps, "make it cool", anything upbeat)

1. Music first: `search_sounds` / `add_sound` (or `set_audio` with the user's file), then `set_edit(sync_track=<that track id>)`. Every cut now lands on the beat. Read `grid` from `get_edit` to think in beats.
2. Shot lengths in beats, not seconds. Vary them: mostly 2, some 1 (quick bursts of 2 to 4 shots), a 4 or 8 for the hero shot. Never the same length 4 times in a row. Calm sections: `sync_grid="half"` or 4 to 8 beats per shot.
3. `hit`: when a shot has one decisive moment (a jump, a splash, a laugh starting, a cake cut), set `hit` to its source time so it lands on a beat.
4. Motion: give most shots a slow `push` or `drift` (amount 0.3 to 0.5); photos always move. `punch` only on 1 to 3 peak shots. `flash` only on the drop or the single biggest moment. `shake` only on the hardest-hitting 1 or 2 shots. At most one of punch / flash / shake per shot.
5. Seams: mostly hard cuts. `whip-left` / `whip-right` / `zoom-in` with `sfx="whoosh"` for a change of place, `flash-white` into the drop, `sfx="hit"` on a hard cut at the drop. `ramp="slow-mid"` for one hero action shot.
6. Then render a preview and check the sheet: if it feels busy, remove effects before adding new ones.

## 5. Polish (only if the brief or the user asks for it; a plain cut needs none of this)

- One `set_edit` call first: `aspect="9:16"` for a reel, story or "vertical"; one `look` for the whole edit at `look_strength` 0.6 to 0.8 unless the user wants it strong (names are in `get_edit`); `captions=true` only if the clips contain speech, never on a montage with music. Turning captions on starts a transcription: check `get_edit` `captions_status.pending` reaches 0 before rendering. If `set_edit` says whisper is missing, tell the user the install command it returns.
- `set_clip_fx` only for a specific reason: a slow-mo moment with `speed` 0.5, a boring stretch at 1.5 to 2, a different `look` on one clip, `reframe_*` when the crop cuts off the subject.
- `set_transition` sparingly on calm edits: dissolve 0.4 to 0.6 s between scenes of different places, no transition inside one scene, never wipes or whips on a calm edit. Unsynced transitions shorten the total, so recheck the length with `get_edit`.

## 6. Check

- `render_preview` (use `start` and `end` for one section), then `render_status(wait_seconds=60)` and look at the contact sheet. Fix what you see (cuts, crop, color, caption position) and repeat at most 3 times.
- Never `render_final` unless the user asked for the finished video. Then tell them the path, and the .srt if captions are on.

## 7. Finish

- A finished video means `render_final` (poll `render_status(mode="final", wait_seconds=60)` until it is done). A timeline for DaVinci Resolve means `export_project`, then poll `export_status` and give the path (File › Import › Timeline). No preference: do neither and offer both in one line.
- Reply with a short plain summary:
  - the project name
  - the clip count and total length
  - the shape of the edit in one or two lines
  - anything you couldn't find
  - one suggestion (for example, "add music in Resolve")

Don't paste uuids unless asked.

## Rules

- Never describe ffmpeg or filters to the user. Tool errors list the allowed values: fix the argument instead of retrying the same one.
- Never invent uuids or times. Only use what the tools returned.
- Don't add, remove or retag photos the user didn't ask about. Editing a project is fine. Changing the library's tags is not, unless asked.
- If iCloud downloads fail during `analyze_clip`, carry on using `search_media` labels and posters only, and tell the user that video previews need Photos access for the app running Kioku.
