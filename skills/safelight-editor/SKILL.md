---
name: safelight-editor
description: Edit a video from the user's own Photos library with Safelight. Use when the user asks to make, cut, assemble or recap a video, reel, montage or trip edit from their photos/videos ("make a 45s video of Japan", "cut a reel of the beach day", "recap October"). Turns a prompt into a Safelight project: finds footage, looks at the actual shots, cuts on scene boundaries, orders them, and exports a timeline for DaVinci Resolve.
---

# Safelight editor

You edit videos from the user's Photos library through the `safelight` MCP tools. Albums and projects hold pointers (Photos uuid + in/out seconds). Nothing is copied until export, and Photos itself is never modified.

If the tools error with "engine isn't running", tell the user to open Safelight (or run `uv run server.py` in `contact-sheet/engine`) and stop.

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

For every candidate video, call `analyze_clip(uuid)`. Look at the contact sheet image. Each frame is one scene, labelled `#idx start-end s`. For each scene, note:
- **What happens** in it (judge from the frame, not from the labels alone).
- **sharpness**: under about 40 is usually blurry or dark. Avoid it unless it's the only shot of a key moment.
- **motion**: very high means camera shake or whip pans. Good for energy, bad for calm edits.
- Skip the first and last half-second of a clip if it looks like the phone being raised or lowered.

Photos have no scenes. Use them as 2-3 s stills when they're the best image of a moment, or when there's no video of it.

## 4. Build the cut

- Add chosen items with `add_to_collection`. Each scene you use is its own item (the same uuid can appear more than once with different ranges).
- Trim each one with `set_clip_range(item_id, in, out)`. Keep cuts inside a scene: never straddle a scene boundary unless the cut is meant to happen there. Use the scene's t0/t1, or a sub-range around the action.
- Add variety: alternate wide and close, people and places, and never use two near-identical shots back to back.
- Reorder with `move_clip` (positions are floats, so 2.5 goes between 2 and 3).
- Check `get_collection` at the end. The total should land within ±15% of the target length, so trim or drop shots to fit.

## 5. Finish

- Export only if the user asked for it, or said "make the video": call `export_project`, then poll `export_status` until it's done, and give the path. It opens in DaVinci Resolve via File › Import › Timeline.
- Reply with a short plain summary:
  - the project name
  - the clip count and total length
  - the shape of the edit in one or two lines
  - anything you couldn't find
  - one suggestion (for example, "add music in Resolve")

Don't paste uuids unless asked.

## Rules

- Never invent uuids or times. Only use what the tools returned.
- Don't add, remove or retag photos the user didn't ask about. Editing a project is fine. Changing the library's tags is not, unless asked.
- If iCloud downloads fail during `analyze_clip`, carry on using `search_media` labels and posters only, and tell the user that video previews need Photos access for the app running Safelight.
