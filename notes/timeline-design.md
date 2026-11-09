# Safelight timeline editor: design

Date: 2026-11-08. Branch `design/timeline`, doc only. Ask, verbatim: "build a UI to quick swap clips, reorder them, add more, and layover audio/subtitles? like a basic version of davinci resolve".

Scope statement up front: this is a **rough-cut editor**, not an NLE. One video track, up to 3 voiceover tracks, one read-only captions lane. It edits the same spec Claude edits (`engine/edit.py`), so the Ask box and the timeline are two hands on one cut. Finishing still happens in Resolve (`Export for Resolve`) or with `Render video`. Things a real NLE has that this deliberately does not: multiple video tracks, keyframes, titles, ripple and roll trims, markers, colour wheels, audio mixer, nested timelines.

Built against: `main` at `ae3fbbe`, plus the two branches landing before this is built, `feat/render-audio` (spec `format.fade_in/fade_out`, `audio` list of up to 3 tracks `{id, path, start, in, out, volume, fade_in, fade_out}`, per-clip `mute`, project `clip_volume`, `duck`, routes `PUT`/`DELETE /collections/{cid}/edit/audio/{id}`) and `feat/captions-2` (per-clip `captions` flag, speech status and language per clip, caption style: position, font, size, colour, highlight colour, bold/box/pop, uppercase, max words, word highlight). Where this doc names a field from those branches it uses the names above; the integrator reconciles names at the contract commit (section 7).

---

## 1. Layout

The project view becomes the editor once the project has at least one clip. Brief and Ask stay (Claude edits are the point of the app), the old clip list `<ol class="clips">` goes away, and everything below the Ask row is the editor, sized to the viewport so the timeline never scrolls off screen while you play.

```
┌─ side ─┐┌──────────────────────────────────────────────────────────────────────────────┐
│        ││ [search ..................................................................]  │
│ All    ││ Halloween 2026                        Video project               Delete     │
│ Photos ││ ┌ Brief: 30s upbeat recap of the kids' Halloween ......................... ┐ │
│ Videos ││ [✦ Ask for a change: shorter, slower, swap a shot ...........] (Ask Claude)  │
│        ││ ┌──────────────────────────────────────────────┐ ┌─────────────────────────┐ │
│ Albums ││ │                                              │ │ Clip 4 · pumpkin patch  │ │
│  ...   ││ │                                              │ │ Speed  [1x ▾]           │ │
│        ││ │              PLAYER  (16:9 / 9:16 box)       │ │ Look   [project: warm ▾]│ │
│Projects││ │                                              │ │ Volume [━━━━●━] Mute [ ]│ │
│ ● Hallo││ │                                              │ │ Captions [x]  en        │ │
│   Fall ││ │   "we're going to the pumpkin patch"          │ │ Reframe ◦━━━●━━━◦       │ │
│        ││ ├──────────────────────────────────────────────┤ │ [Swap…] [Split] [Remove]│ │
│        ││ │ ◀◀  ▶  ▶▶   00:12.40 / 00:30.20   Rough ● Exact │ └─────────────────────────┘ │
│        ││ └──────────────────────────────────────────────┘                             │
│        ││ ┌─────────────────────────────────────────────────────────────────────────┐ │
│        ││ │ 0s        5s        10s   ▼    15s        20s        25s        30s     │ │
│        ││ │V ┃▒▒▒▒▒▒▒▒┃░░░░░┃▓▓▓▓▓▓▓▓▓▓▓▓▓◆▒▒▒▒▒▒▒▒▒┃░░░░░░░░░░░┃▓▓▓▓▓▓▓▓▓▓▓▓┃      │ │
│        ││ │  ┃ 1 kids ┃2 door┃ 3 street  ┃ 4 pumpkin┃ 5 photo   ┃ 6 lanterns ┃      │ │
│        ││ │A1┃        ┃~~~~~~~~ vo "intro.m4a" ~~~~~~~┃          ┃            ┃      │ │
│        ││ │A2┃        ┃      ┃            ┃         ┃~~ vo 2 ~~┃            ┃      │ │
│        ││ │C ┃ [trick or treat][we're going..]  [look at that]           [bye!]    │ │
│        ││ ├─────────────────────────────────────────────────────────────────────────┤ │
│        ││ │ [+ Add clips ▸]   snap ●   zoom  −  [━━━━●━━━━]  +   fit     6 clips · 30.2s │
│        ││ └─────────────────────────────────────────────────────────────────────────┘ │
│ engine ││ [Captions]                                 (Preview) (Render video) (Export)  │
└────────┘└──────────────────────────────────────────────────────────────────────────────┘
```

Notes on the layout:

- **Player**: one `<video>` stage with a letterbox matching `format.aspect`. Shows the live "rough" cut by default (section 2) and switches to the exact rendered preview when one exists for the current edit. A small `Rough ● Exact` segmented control shows which one you are watching; `Exact` is disabled with the tooltip "Press Preview" when no fresh render exists.
- **Inspector**: a compact card to the right of the player for the selected clip (or voiceover block, or nothing). It is the only place with form controls, so the timeline itself stays clean. Below 1100 px it drops under the player as one row.
- **Transport**: play/pause, step back and forward one frame, timecode, mode toggle. J/K/L drive the same thing.
- **Timeline**: a ruler, the video track `V` with thumbnail strips sized by duration, up to three voiceover lanes `A1..A3` (only the ones in use plus one empty lane to drop into), the captions lane `C` with cue pills. Playhead is a 2 px accent line with a cap in the ruler; dragging in the ruler scrubs.
- **Footer**: Add clips (opens the search strip, section 3), snapping toggle, zoom slider with fit, totals. The existing Captions checkbox, Preview, Render video and Export buttons move into the row below, unchanged in behaviour.
- The whole editor is `height: calc(100vh - top)` with the timeline lanes in an inner horizontal scroll. The page itself no longer scrolls on a project; Claude's run history (`.runs`) collapses into its one-line bar above the player and expands as an overlay panel rather than pushing the editor down. (Open question 2.)

Style: everything uses the tokens in `styles.css`. Clip blocks are `var(--surface-solid)` with a `1px var(--line)` border and `border-radius: 8px`; the strip image fills them. Selected block: `box-shadow: 0 0 0 2px var(--accent)`. Voiceover blocks are tinted `color-mix(in srgb, var(--sky) 30%, var(--surface-solid))` with peaks drawn in `var(--accent)`. Caption pills use `var(--peach)` at 25 percent. Transition seams carry a small diamond in `var(--accent)`. Motion: blocks reorder with a FLIP transform transition (`.25s var(--ease)`), the search strip rises in with the existing `rise` keyframes, the playhead moves by `transform` only. Everything respects `prefers-reduced-motion` via the existing global rule.

---

## 2. Playback strategy

Two candidates, then the recommendation.

**A. Live proxy playback.** Two `<video>` elements (A and B) over `/media/{uuid}/proxy` (540p h264, already served with Range). The player keeps the current clip in one element and pre-seeks the next clip's in-point in the other, so a cut is a swap of `hidden` on two elements, not a load. Looks are approximated with CSS filters on the stage; speed is `playbackRate`; mute, volume and `clip_volume` are the element's `volume`; voiceover tracks are `<audio>` elements started at `start - in` offsets; captions come from the cues endpoint and are drawn as a DOM caption using the same style fields the renderer uses (font size as a fraction of the stage, position, box or bold); `fade_in`/`fade_out` are an opacity ramp on the stage. Dissolves are approximated by crossfading the two elements' opacity over the transition's frames; the other xfade types play as a cut with a small "wipe" badge on the stage.

**B. Rendered preview only.** Every change invalidates the preview; play means wait for `render_preview`.

Measured and estimated latencies on this machine (ffmpeg 9, VideoToolbox, 540p proxies, warm cache):

| Action | A: live proxies | B: rendered |
|---|---|---|
| Press play after an edit | 0 to 80 ms (element already buffered; one `seeked` wait if the edit moved the in-point) | 3 to 8 s for a window around the playhead, 7.6 s for a full 28 s preview (measured), plus iCloud download time for any clip without a proxy |
| Cross a cut | 0 to 1 frame (pre-seeked element swapped) | 0 |
| Seek inside a clip | 60 to 300 ms today: proxies are encoded with libx264's default GOP of 250 frames, so a seek may decode up to 8 s from the previous keyframe. With `-g 30 -keyint_min 30` on new proxies (section 5) this drops to 30 to 80 ms | 30 to 80 ms (the preview is encoded by `h264_videotoolbox` with short GOPs) |
| Scrub (drag playhead) | Frame updates at 10 to 15 Hz with the GOP fix, 3 to 5 Hz today | Smooth, it is one file |
| Fidelity | Looks approximate (CSS has no `colorbalance`; warmth is a tinted overlay at `mix-blend-mode: soft-light`), reframe crop is exact (CSS `object-position`), speed exact, transitions: dissolve approximate, wipes shown as cuts, HDR proxies look flat (known, editing-design open question 5) | Exact |

**Recommendation: A as the default, B one click away, both in the same stage.** The user's ask is speed of iteration ("quick swap, reorder, add"). A gives a cut you can judge for rhythm instantly; B is what you watch before rendering the final. Concretely:

- The player always has a `mode`: `rough` (live) or `exact` (the `preview.mp4` of the current `edit_hash`).
- `Preview` renders a **window** of 10 s around the playhead by default (`start`/`end` already supported, 3 to 8 s) and switches the stage to `exact` at the same output time when it lands. Shift-click renders the whole edit.
- Any edit flips the stage back to `rough` and marks the exact preview stale (the existing `stale` flag from `GET /render/preview`). No automatic re-rendering: the user decides when to spend 3 to 8 s.
- Clips without a proxy show their thumbnail in the stage and a "Preparing playable copy" pill; the player calls the existing `POST /media/{uuid}/analyze` (proxy, scenes and sheet in one go, progress in the sidebar activity panel) and plays when it returns. Scenes are wanted anyway for snapping and swap suggestions, so this reuses one route instead of adding a proxy-only one.
- Audio during rough playback: the current clip element's audio (volume x `clip_volume`, 0 when `mute`) plus the active voiceover `<audio>` elements. `duck` is approximated by multiplying clip volume by 0.3 while any voiceover block is active. Transitions do not crossfade audio in rough mode (acceptable; the Exact render does).
- Photos play as the `/full/{uuid}` image in an `<img>` on the stage for their 3 s (or trimmed) length; no video element involved.
- Scrubbing: dragging the playhead or the ruler seeks the A element with `fastSeek` when available, falls back to `currentTime`, throttled to one seek in flight (wait `seeked` before issuing the next).

Why not render-only: the user said "quick". Why not a canvas compositor (decode frames and draw with WebGL for exact looks and wipes): large, and the Exact mode already exists at a 3 to 8 s cost.

Known WKWebView risks (Tauri webview): two video elements plus up to three audio elements is well within limits; `playbackRate` 0.25 to 4 works with pitch correction; `play()` needs a user gesture once per page, which the Play button is. Memory: a 540p proxy element decodes at about 60 MB; two of them is fine.

---

## 3. Interactions

### Timeline model (client)

```ts
type Model = {
  cid: number;
  items: Item[];                 // from GET /collections/{cid}, ordered by pos
  edit: Edit;                    // from GET /collections/{cid}/edit: spec, rev, edit_hash, clips[{item_id,uuid,start,end,frames}], duration, fps
  cues: Cue[];                   // from GET /collections/{cid}/captions/cues (output time)
  peaks: Map<number, number[]>;  // audio track id -> 0..1 peaks
  scenes: Map<string, Scene[]>;  // uuid -> scenes, loaded lazily for snapping and swap
  sel: { kind: "clip"; item_id: number } | { kind: "audio"; id: number } | null;
  pps: number;                   // pixels per second (zoom), 4..240
};
```

Output time to source time for clip `k`: `src = t0 + (t - clips[k].start) * speed`. The server's `clips[].start/end` (which include transition overlaps) are the truth for layout; after every mutation the client refetches `/edit` (one local request, about 10 ms) instead of porting `compute_timeline` to TypeScript.

### Clip interactions on the V track

```
   select: click            trim: drag an edge              reorder: drag the body
 ┌──────────────┐        ┌┃─────────────┃┐  ◂ 9.2s ▸     ┌──────┐ ┌ ─ ─ ─ ┐ ┌──────┐
 │▒▒▒▒▒▒▒▒▒▒▒▒▒▒│   →    │▒▒▒▒▒▒▒▒▒▒▒▒▒▒│               │  1   │ │   3   │ │  2   │
 │ 3 street     │        ┃ 3 street     ┃               └──────┘ └ ─ ─ ─ ┘ └──────┘
 └──────────────┘        └┃─────────────┃┘                 caret ▲ shows the drop slot
  ring = selected          handles appear on hover;         the dragged block floats at
                           tooltip shows the new            60% opacity; others FLIP aside
                           length; snaps (dotted line)
                           to scene cuts, playhead, edges

   seam: click the gap                              split: S at the playhead
 ┌──────────┐◆┌──────────┐                        ┌────────────▼───────────┐
 │ 3 street │ │ 4 pumpkin│                        │ 4 pumpkin  ┃           │
 └──────────┘ └──────────┘                        └────────────┻───────────┘
      ┌───────────────────────────┐                 ┌───────────┐┌──────────┐
      │ Cut  Dissolve  Fade-black │                 │ 4 pumpkin ││ 5 pumpkin│
      │ Wipe ◂  Wipe ▸  Slide ◂   │                 └───────────┘└──────────┘
      │ 0.2 ━━━●━━━━━━━━━ 2.0 s   │               same uuid, split source time,
      └───────────────────────────┘               effects copied, transition moves right
```

| Action | Gesture / key | Request(s) | Notes |
|---|---|---|---|
| Select | click a block; `Tab`/`Shift+Tab` cycle; `Esc` clears | none | Inspector shows the clip; Space plays from the playhead regardless of selection |
| Reorder | pointer drag of the block body (pointer events, not HTML5 DnD: Tauri's file drag-drop handler is on, and HTML5 DnD ghosts look wrong) | `PATCH /items/{iid} {pos}` with the midpoint of the neighbours; the server renumbers 1..n | Optimistic: blocks FLIP into place on drop, request follows, revert on failure. Auto-scroll when dragging near the lane's edge |
| Trim in/out | drag the left or right 8 px of a block; `I`/`O` set the selected clip's in or out at the playhead | `PATCH /items/{iid} {t0}` or `{t1}` | Clamp to `[0, dur]` and to a 0.2 s minimum. Trimming the in-point keeps the block's right edge anchored (later clips shift, as with a ripple). Tooltip shows `in 2.40s · 5.10s long` |
| Snap | on by default; `N` toggles; hold `Alt` to bypass while dragging | none | Targets within 6 px: neighbour edges, playhead, scene boundaries of the clip being trimmed (source time mapped to output time), voiceover block edges. A dotted accent line shows the active target |
| Swap | `Swap…` in the inspector | `PUT /items/{iid} {uuid?, t0?, t1?}` (new route, section 5) | Opens the search strip in swap mode: row 1 "Other moments in this clip" shows the clip's scenes as tiles (frames cut out of the clip's strip with `background-position`), click one to set `t0/t1` to that scene; row 2 search results, prefilled with the clip's label words; click one to replace the source, keeping `item_id` (so speed, look, mute and captions flags carry over) and keeping the output length: `t0 = hit.moment?.t0 ?? 0`, `t1 = min(t0 + oldLen, hit.duration)` |
| Add clips | `+ Add clips` or `A`; search field with Videos / Photos toggle; results as a horizontal strip of tiles | `POST /items {uuids, pos?}` (`pos` is a new optional field: insert after the clip under the playhead, default append) | Click a tile inserts at the playhead; pointer-drag a tile onto the V track shows the same insertion caret as reorder. Multiple tiles can be selected (Cmd-click) and added together |
| Split | `S` with the playhead inside a clip; `Split` button | `POST /items/{iid}/split {at}` (source seconds; new route) | One transaction: shortens the clip to `at`, inserts a new item right after with the same uuid and `[at, old t1]`, copies the clip's spec entry, moves a transition-after to the new item |
| Delete | `Delete`/`Backspace` with a clip selected; `Remove` button | `DELETE /items/{iid}` | Later clips close the gap (there are no gaps on a single-track timeline). Undo re-adds via `POST /items {uuids, pos}` and restores t0/t1 and the spec entry with `PATCH /edit/clips/{new_id}`; see section 4 for why that is fine |
| Transition | click the seam between two blocks | `PUT`/`DELETE /edit/transitions/{item_id}` | Popover with the six types as chips and a duration slider 0.2 to 2 s. The blocks draw their overlap: block k+1 starts at `clips[k+1].start`, which the server already shortens by the transition. Capped transitions show the server's warning as the popover's footnote |
| Per-clip look, speed, volume, mute, captions, reframe | inspector controls | `PATCH /edit/clips/{iid}` with only the changed field | Speed is a select (0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4), disabled for photos. Look is a select of `options.looks` plus "project look" and "none", with a strength slider when a look is set. Reframe is one slider (manual position 0..1) only shown when the source aspect differs from the format; pan from/to is Claude's (tool only). Captions is a checkbox with the clip's speech status next to it (`en`, `no speech`, `pending`) |
| Voiceover: add | drop an audio file from Finder onto an A lane (Tauri `getCurrentWebviewWindow().onDragDropEvent`, paths come with the event; `@tauri-apps/api` is already a dependency) | `PUT /edit/audio/{id} {path, start, in: 0, out: dur, volume: 1}` | `id` is `crypto.randomUUID()` sliced to 8. Drop position becomes `start`. If the lane has a block there, the next free lane is used; all three full means a toast |
| Voiceover: move, trim, volume, fades | drag body = `start`; drag edges = `in`/`out`; inspector has volume and fade sliders | `PUT /edit/audio/{id}` (full object) | Peaks come from `GET /edit/audio/{id}/peaks` (section 5) and are drawn on a `<canvas>` inside the block, redrawn on zoom. Playback uses `GET /edit/audio/{id}/file` |
| Captions lane | click a cue seeks to it | none in v1 | Pills show the cue text, greyed for clips whose `captions` flag is off. **Editing transcript text is v2** (section 8): transcripts are cached per `(uuid, model)` and shared by every project using the clip, so edits need a per-project overrides table and a change in `captions.map_cues`; not worth it before the lane has been used |
| Playhead | click or drag in the ruler; `←`/`→` one frame; `Shift+←`/`→` one second; `Home`/`End`; `[`/`]` previous and next cut | none | Timecode shows `mm:ss.ff` at `format.fps` |
| Zoom | slider; `−`/`=`; `Cmd+wheel` or pinch over the lanes zooms around the cursor; `fit` sets `pps = width / duration` | none | `pps` persists per project in `localStorage` |

Keyboard (only when the editor is mounted, no input is focused and the viewer is closed; main.ts's existing keydown handler delegates to `timeline.handleKey(e)` first):

| Key | Does |
|---|---|
| `Space` | play / pause |
| `J` / `K` / `L` | reverse (step back 1 frame per tick at 10 Hz; WKWebView has no negative `playbackRate`), pause, forward; repeated `L` cycles 1x, 2x, 4x |
| `I` / `O` | set the selected clip's in / out at the playhead |
| `S` | split at the playhead |
| `Delete` / `Backspace` | remove the selected clip or voiceover block |
| `Cmd+Z` / `Shift+Cmd+Z` | undo / redo |
| `[` / `]` | previous / next cut |
| `←` / `→`, `Shift+←` / `→` | one frame, one second |
| `N` | snapping on / off |
| `A` | open Add clips |
| `Esc` | close the search strip or popover, else clear selection |

`Cmd+K` and `Cmd+A` keep their current meanings (search box, select all in grids) and are left alone.

---

## 4. Undo, redo, optimistic UI, and Claude editing at the same time

Facts to build on: `edit_specs.rev` increments on every spec mutation; `collections.updated` changes on every item or spec change; `GET /edit` returns `edit_hash` = sha1 of spec plus items' `(item_id, uuid, t0, t1, pos)`. The hash is the single version token the client needs.

**Undo stack (client side, per mounted project).**

```ts
type Cmd = { label: string; before: string; after: string;    // edit_hash before and after the command
             redo: () => Promise<void>; undo: () => Promise<void> };
```

- Every user action builds a `Cmd` whose `undo` is the inverse request with the values captured at the time (trim: the old `t0`; reorder: the old `pos` midpoint; transition: the old entry or DELETE; delete clip: `POST /items {uuids:[uuid], pos}` then `PATCH /items/{new} {t0, t1}` then `PATCH /edit/clips/{new}` with the saved entry, and the saved transition; split: `DELETE` the new item and restore `t1`; swap: `PUT` back the old `{uuid, t0, t1}`).
- After `redo()` succeeds the client refetches `/edit`, records `after = edit_hash`, pushes the command, and clears the redo stack. Capped at 100 entries.
- `Cmd+Z` checks `model.edit.edit_hash === top.after`. If it matches, run `undo()`, refetch, and verify the result is `top.before` (if not, keep going but toast "Undo landed on a different state"). If it does not match, someone else changed the edit in between (Claude, an MCP call, a second window): drop the whole stack with the toast "Claude changed the edit, so undo history was reset" and do nothing else. Honest and 5 lines; a merge of concurrent intents is not something a rough-cut editor needs.
- Deleting a clip and re-adding it yields a new `item_id`. The undo entry for a delete therefore re-keys itself after running (it stores the new id for a later redo). Entries below it that referenced the old id are not touched: they are older than the delete, so reaching them means the delete has been undone and the clip exists again under the new id; each entry resolves `item_id` through a `remap: Map<old, new>` kept on the stack. This is the only subtlety; everything else is value restoration.

**Optimistic UI.** Drags (trim, reorder, voiceover move) mutate the local model continuously with no requests; one request fires on pointer up. Clicks (speed, transition, mute) mutate the local model, re-render, then send. On failure the model reverts to a snapshot taken before the mutation and the existing `fail("...")` toast reports the engine's message (`"out point must be after in point"`, `"photos cannot change speed"`). Between the optimistic frame and the `/edit` refetch the layout uses naive math (clip lengths minus transition durations, no caps); the refetch replaces it within about 10 ms, so no visible jump in practice.

**Live refresh and conflicts.** Today `syncCollections` (every 5 s) calls `go(view)` when `updated` changed, which rebuilds the project view; `refreshProjectSoon` does the same after an Ask finishes. Both would tear down the player mid-playback, and our own edits bump `updated` too. Changes (all in `timeline.ts`, with two one-line hooks in `main.ts`):

1. `timeline.mount(host, c, items)` is idempotent: if the editor is already mounted for this `cid` it calls `timeline.sync()` and returns. `renderProject` keeps calling it on every render, so no special casing elsewhere.
2. `timeline.sync()`: `GET /edit`; if `edit_hash` equals the model's, nothing happens. Otherwise, unless a drag is in progress (then retry in 2 s), reload items, edit and cues, keep the playhead and selection where possible, and if the Ask job is running show a quiet "Updated by Claude" pill on the timeline for 3 s. The undo stack is checked as above (top.after mismatch clears it).
3. `syncCollections` hook: when `view.type === "col"` and `timeline.mounted(view.id)`, call `timeline.sync()` instead of `go(view)` (sidebar still reloads).
4. The server stays as it is: `edit.mutate` runs under `begin immediate`, so two writers never lose an update at the storage level; semantic conflicts surface as 404 "no such item" (Claude removed the clip you were trimming) and are handled by the revert-and-sync path. `If-Match: <rev>` on PATCH is listed as v2 and will probably never be needed.

Race worth naming: the user trims clip 4 while Claude's `set_clip_range` on clip 4 lands first. Both requests succeed; the last write wins per field; the user's drag result is what they see, which is what they expect. Not solved further.

---

## 5. API changes

Everything below is small and additive. Nothing changes existing routes' shapes. All new GETs that the client polls or lazy-loads join `server.QUIET`.

| Route | Module | Size | Purpose |
|---|---|---|---|
| `GET /media/{uuid}/strip` | `media.py` | ~30 lines | Filmstrip JPEG for the clip's block: `n = min(120, ceil(duration))` frames at `(k + 0.5) * duration / n`, each centre-cropped to 16:9 and resized to 160x90 with cv2 (`media.frame_at` on the proxy), concatenated horizontally, JPEG quality 70, cached at `data/strips/<uuid>.jpg` (regenerated when the proxy is newer). 404 when there is no proxy yet (the block shows the thumb). Tile count = `naturalWidth / 160` on the client, no sidecar needed. The existing contact sheets (`/media/{uuid}/sheet`) are scene-indexed 4-column grids with labels, wrong shape for a time-linear strip, so they are not reused |
| `GET /collections/{cid}/edit/audio/{id}/file` | `render.py` or wherever `feat/render-audio` keeps the audio routes | ~10 lines | `FileResponse` (Range) of exactly the path stored in the spec for that id, suffix in `{.m4a,.mp3,.wav,.aac,.aiff,.caf}`. If `feat/render-audio` already serves it, nothing to add |
| `GET /collections/{cid}/edit/audio/{id}/peaks?n=600` | same | ~25 lines | `ffmpeg -i file -ac 1 -ar 8000 -f s16le -` piped to numpy, `n` buckets of max-abs normalised to 0..1, two decimals, JSON `{duration, peaks}`, cached at `data/peaks/<sha1(path, mtime, n)>.json` |
| `GET /collections/{cid}/captions/cues` | `captions.py` | ~15 lines | `edit.snapshot(cid)` then `captions.map_cues(tl, {uuid: cached lines})` for clips with the `captions` flag on, over cached transcripts only (never transcribes). Returns `[{t0, t1, text, item_id}]` in output time plus per-clip speech status. The client never reimplements the cue timing math |
| `PUT /collections/{cid}/items/{iid}` | `library.py` | ~15 lines | Replace the source in place: body `{uuid?, t0?, t1?}`; validates the uuid is indexed and the range against the new duration (same rules as `update_item`), keeps `item_id` and `pos`. This is "swap" |
| `POST /collections/{cid}/items/{iid}/split` | `edit.py` | ~25 lines | Body `{at}` in source seconds, must be inside `(t0 + 0.2, t1 - 0.2)`. In one `mutate` transaction: `update collection_items set t1 = at`, insert the new row `(uuid, at, old t1, pos + 0.5)`, renumber, copy `spec.clips[iid]` to the new id, move `spec.transitions[iid]` to the new id. `edit.mutate` gains an optional third argument so `fn(spec, col, c)` can write items on the same connection. Returns `{item_id (new), spec, rev}` |
| `POST /collections/{cid}/items` gains optional `pos` | `library.py` | 3 lines | Insert position (float midpoint, renumbered as `update_item` already does) so "add at playhead" is one request |
| `media.ensure_proxy`: add `-g 30 -keyint_min 30` | `media.py` | 1 line | Keyframe every second so seeks and scrubbing in the live player land in 30 to 80 ms instead of up to 300 ms. Only affects new proxies; the 28 existing ones (35 MB) can be deleted by hand once so they regenerate on next play, or left as they are. Also touch `-movflags +faststart` (already present) |
| `server.QUIET` | `server.py` | 1 line | add `media/.+/strip`, `collections/\d+/captions/cues`, `collections/\d+/edit/audio/[^/]+/(peaks|file)`, `collections/\d+/edit$` |

Stays client side: layout math from `clips[].start/end`, snapping, the undo stack, CSS look approximation, cue drawing, playhead and transport state, zoom. Not added: batch PATCH (reorder is one request because the server renumbers; multi-select moves are v2), waveform peaks for clip audio (v2, section 8), `If-Match` (v2 at most), a proxy-only ensure route (`/analyze` covers it and also yields the scenes the editor wants).

Existing routes the editor uses as they are: `GET /collections/{cid}`, `GET/PATCH /collections/{cid}/edit`, `PATCH /edit/clips/{iid}`, `PUT/DELETE /edit/transitions/{iid}`, `PUT/DELETE /edit/audio/{id}`, `PATCH/DELETE /items/{iid}`, `POST /media/{uuid}/analyze`, `GET /media/{uuid}/scenes`, `GET /media/{uuid}/proxy` (Range), `GET /thumb`, `GET /full`, `GET /search?q=&kind=video&k=60` (hits carry `moment: {t0, t1}` when a scene matched), `POST /render {mode: "preview", start, end}`, `GET /render/preview` (`stale`), `GET /render/preview/file`.

---

## 6. Performance

- **200 clips** (the render cap). The V track is plain DOM: 200 absolutely positioned blocks is cheap. The cost is strip images: 200 x about 150 KB. Strips load only for blocks intersecting the visible lane range plus one screen on each side (an `IntersectionObserver` on the lane's scroll container), at most 3 in flight, released (`background-image` cleared) when they scroll far away. First paint with 200 clips: under 50 ms; strips fill in as they arrive.
- **Strips at any zoom**: one image per clip, drawn with `background-size: ${dur * pps / speed}px 100%` and `background-position-x: -${t0 * pps / speed}px`, so trims and speed changes move the window over the same image with no new requests. A 60 s clip's strip is 9600x90 px, about 150 KB; a 20 min clip is capped at 120 tiles (one every 10 s).
- **Playhead**: `requestAnimationFrame` loop only while playing, reading `video.currentTime` and setting `transform: translateX()`; nothing else in the lanes repaints. Timecode text updates at most 15 times a second.
- **Peaks**: 600 buckets per voiceover block, drawn once per zoom change on a small canvas (block width x 44 px), debounced 50 ms.
- **Scenes** for snapping load lazily on the first trim of a clip and are cached in the model. Snapping candidates are sorted once per drag; lookup is a binary search.
- **Virtualised timeline**: not needed at 200 blocks in one lane; the IntersectionObserver on strips is the only "virtualisation". If someone ignores the cap, nothing breaks, it just loads more strips.
- **Engine**: strips and peaks are generated once per source and cached on disk; generation runs in FastAPI's thread pool with `media.lock_for(uuid)` (no video slot: cv2 on a local proxy takes about 1 to 2 s for 60 tiles and never touches the network).

---

## 7. Files, build split, interfaces, merge order, tests

### Files

```
app/src/edit-types.ts   NEW   contract: Model, Edit, EditClip, Cue, Peaks, AudioTrack types; the Player interface   (integrator, first)
app/src/player.ts       NEW   live A/B proxy player, exact-mode switch, transport, captions overlay, voiceover audio  (T1)
app/src/player.css      NEW   stage, transport, caption overlay                                                       (T1)
app/src/timeline.ts     NEW   model, lanes, blocks, drag/trim/snap, seams, inspector, search strip, undo, keys, sync  (T2)
app/src/timeline.css    NEW   lanes, blocks, ruler, inspector, strip panel                                            (T2)
app/src/main.ts         EDIT  renderProject: mount the editor instead of the clip list; syncCollections and keydown hooks (integrator, ~15 lines)
app/src/index.html      EDIT  two <link> lines                                                                         (integrator)
app/src/api.ts          EDIT  nothing (types live in edit-types.ts to keep api.ts untouched by this feature)
engine/media.py         EDIT  strip route + cache, -g 30 on proxies                                                    (T3)
engine/library.py       EDIT  PUT items/{iid} (replace), pos on POST items                                             (T3)
engine/edit.py          EDIT  split route, mutate(c) option                                                            (T3)
engine/captions.py      EDIT  cues route                                                                               (T3)
engine/render.py or audio module  EDIT  audio file + peaks routes, unless feat/render-audio has them                   (T3)
engine/test_timeline_api.py  NEW                                                                                      (T3)
engine/server.py        EDIT  QUIET                                                                                    (integrator)
```

### Contract commit (integrator, before T1 and T2 branch)

`app/src/edit-types.ts`, about 60 lines, no logic:

```ts
export type EditClip = { item_id: number; uuid: string; start: number; end: number; frames: number };
export type Edit = { spec: Spec; rev: number; edit_hash: string; duration: number; fps: number; total_frames: number;
                     clips: EditClip[]; warnings: string[]; options: { looks: string[]; transitions: string[]; ... } };
export type Spec = { version: 1; format: { aspect: string; size: number; fps: number; fade_in?: number; fade_out?: number };
                     look: { name: string | null; strength: number }; clip_volume?: number; duck?: boolean;
                     clips: Record<string, ClipFx>; transitions: Record<string, { type: string; duration: number }>;
                     captions: { on: boolean; style: CaptionStyle }; audio?: AudioTrack[] };
export type ClipFx = { speed?: number; volume?: number; mute?: boolean; captions?: boolean; look?: { name: string | null; strength: number };
                       adjust?: {...}; reframe?: { mode: "center" | "manual" | "pan"; pos?: number; pos_from?: number; pos_to?: number } };
export type AudioTrack = { id: string; path: string; start: number; in: number; out: number; volume: number; fade_in: number; fade_out: number };
export type Cue = { t0: number; t1: number; text: string; item_id: number };
export type Scene = { idx: number; t0: number; t1: number; sharpness: number; motion: number };
export type Model = { cid: number; items: Item[]; edit: Edit; cues: Cue[]; peaks: Map<string, number[]>; scenes: Map<string, Scene[]> };

export interface Player {
  mount(stage: HTMLElement): void;
  load(model: Model): void;              // (re)build the sequence; keeps the current time if still inside the edit
  setExact(url: string | null): void;    // non-null switches the stage to the rendered preview; null back to rough
  seek(t: number): void;                 // output seconds
  play(): void; pause(): void; toggle(): void;
  rate(r: number): void;                 // 1, 2, 4 for L; -1 means J (stepping)
  step(frames: number): void;
  readonly time: number; readonly playing: boolean; readonly mode: "rough" | "exact";
  onTime(cb: (t: number) => void): () => void;   // rAF cadence while playing, once per seek otherwise
  onEnded(cb: () => void): () => void;
  destroy(): void;
}
```

The names for the `feat/render-audio` and `feat/captions-2` fields are fixed here from those branches' actual code at the time of the contract commit.

### Three builders

| | Branch | Owns | Starts | Needs from others |
|---|---|---|---|---|
| **T1 player** | `timeline/player` | `player.ts`, `player.css` | right after the contract commit | `edit-types.ts` only. Develops against `data/proxies` through the running engine on 8765 (read-only GETs) or a spare engine. Test page: a 20-line `player.dev.html` in its worktree, deleted before merge |
| **T2 timeline** | `timeline/ui` | `timeline.ts`, `timeline.css` | right after the contract commit | `edit-types.ts`; imports `Player` as a type and receives the instance from `main.ts` via `mount(host, player, ...)`. Until T1 lands it uses a 30-line `FakePlayer` in its worktree (time advances on a timer). Engine routes it needs that do not exist yet (strip, cues, split, replace, peaks) are behind one `api()` call each; while T3 builds them T2 points at fixtures in `app/src/fixtures/` (deleted before merge) |
| **T3 engine** | `timeline/engine` | the `engine/*` edits above and `test_timeline_api.py` | after `feat/render-audio` and `feat/captions-2` merge (they touch `edit.py`, `render.py`, `captions.py`) | nothing from T1/T2. Its routes are specified fully in section 5, so T2 codes against the spec |

Nobody edits `main.ts`, `index.html`, `api.ts`, `server.py`: the integrator does, in the integration commit. Builders do not install anything (no new npm or Python dependencies are needed), do not restart the engine in iTerm, do not touch `data/` except reading proxies.

### Interface points

- `main.ts` to `timeline.ts`: `timeline.mount(host: HTMLElement, cid: number, c: Collection, items: Item[], deps: { toast, fail, go, viewer })` (idempotent), `timeline.mounted(cid): boolean`, `timeline.sync(): Promise<void>`, `timeline.handleKey(e: KeyboardEvent): boolean`, `timeline.unmount()`. `renderProject` calls `mount` where it used to build `strip`, and keeps its brief, ask, runs, meta and render rows (the render row is appended to the editor's footer slot by passing it in `deps.footer`).
- `timeline.ts` to `player.ts`: the `Player` interface only; `timeline.ts` constructs `new LivePlayer()` from `player.ts`.
- `timeline.ts` to the engine: the routes in section 5 and the existing ones listed there.
- `player.ts` to the engine: `/media/{uuid}/proxy`, `/full/{uuid}`, `/media/{uuid}/analyze`, `/edit/audio/{id}/file`, `/render/preview/file`.

### Merge order

1. Contract commit on `main` (`edit-types.ts`, this doc moved to `notes/`), after `feat/render-audio` and `feat/captions-2` are in, so the field names are final.
2. **T3** (engine only, no app changes; `uv run test_timeline_api.py test_edit.py test_library.py test_contract.py test_settings.py` pass). The engine in iTerm keeps running the old code until the user restarts it; nothing in T3 needs a restart before T1/T2 are merged.
3. **T1** (`cd app && npm run build` passes; its dev page deleted).
4. **T2** (`npm run build`; fixtures and `FakePlayer` deleted; it now imports the real `LivePlayer`).
5. Integration commit: `main.ts` hooks, `index.html` links, `server.py` QUIET, remove the old `clipRow` and `.clips`/`.clip*` CSS rules once the editor replaces them (`clipLen` stays, the meta row uses it). Run every engine test suite, `npm run build`, then the smoke below on a spare engine (port 8766, temp `CS_DB`, lavfi clips), never the iTerm engine.
6. The user restarts the engine in iTerm and the app; one real project (Halloween 2026, project 2) through: play, trim, reorder, split, swap, a voiceover drop, Preview window, Render video.

### Test plan

**`engine/test_timeline_api.py`** (plain asserts, `uv run`, `os.environ["CS_DB"]` set to a temp path before `import core`, `assert "data/index.db" not in str(core.DB_PATH)`; reassign `media.PROXIES`, `media.STRIPS`, `render.CACHE`, `render.RENDERS` after import; lavfi media, no network):

- Strip: a 12 s lavfi `testsrc2` proxy gives a 12-tile 1920x90 JPEG; a 400 s one is capped at 120 tiles; the request on a uuid without a proxy is 404; the file is cached (second call does not open cv2, counter patched).
- Replace: `PUT items/{iid} {uuid: other}` keeps `item_id` and `pos`, validates the range against the new duration (422 past the end), `spec.clips[iid]` survives, `edit_hash` changes.
- Split: `{at}` inside the clip yields two items with `[t0, at]` and `[at, t1]`, positions renumbered 1..n, the spec entry copied to the new id, the transition moved to the new id, `rev` +1; `at` outside the clip is 422; failure inside the transaction (patched `insert` raising) leaves the item untouched (atomicity).
- `POST items {pos}`: inserts between two clips and renumbers.
- Cues: with fake rows in `transcript_lines` for two clips, one with `captions: false`, the route returns cues in output time only for the enabled one and never calls `run_whisper` (patched to raise).
- Peaks: a lavfi sine `.wav` gives 600 values in 0..1 with a peak near 1 and the right duration; the JSON is cached; a path outside the spec is 404.
- Proxy GOP: the `ensure_proxy` command list contains `-g 30` (assert on the argument list with `subprocess.run` patched).

**App**: `cd app && npm run build` (tsc strict + vite) is the gate for T1, T2 and the integration commit.

**Playwright smoke** (integrator, using the Playwright MCP already in this environment, no new dependency): spare engine on 8766 with a temp db seeded with 3 lavfi clips and proxies, `vite` on 1420 with `API` pointed at 8766 (a `VITE_API` env read in `api.ts` is a 1-line change; today it is a constant), then:

1. Open project, assert 3 blocks on the V track and the player stage.
2. Press Space, wait 1.5 s, assert the playhead `transform` moved and the stage `<video>` `currentTime > 1`.
3. Drag block 3 before block 1, assert the order from `GET /collections/{cid}` is `[3, 1, 2]`.
4. Press `S` with the playhead inside block 1, assert 4 items.
5. `Cmd+Z`, assert 3 items; `Shift+Cmd+Z`, assert 4.
6. Click the seam between blocks 1 and 2, choose Dissolve, assert `GET /edit` has the transition and block 2's left edge moved left.
7. Drag block 1's right edge 60 px left, assert its `t1` decreased (and snapped to a scene boundary when one is within 6 px).
8. Type in Add clips, click a result, assert 5 items.

Screenshots at steps 1, 3 and 6 land in the scratchpad for the lead to eyeball against the pastel style.

---

## 8. Phasing, estimates, risks, open questions

### v1 (about 1 to 2 days of agent work across the three builders, 10 to 14 hours wall clock)

| Piece | Owner | Estimate |
|---|---|---|
| Contract commit | integrator | 30 min |
| Live A/B player, exact-mode switch, transport, captions overlay, voiceover audio, CSS looks, duck approximation | T1 | 5 to 6 h |
| Lanes, blocks with strips, ruler, playhead, zoom, snap, trim, reorder, split, delete, seams, inspector, search strip (add and swap), voiceover drop/move/trim, cues lane, undo/redo, keys, sync | T2 | 9 to 11 h |
| Strip, replace, split, pos, cues, peaks, audio file, GOP | T3 | 3 to 4 h |
| Integration commit, smoke, old clip list removal | integrator | 2 to 3 h |

Included in v1: everything in section 3 except the rows marked v2. Excluded from v1 and listed for v2:

- Clip audio waveforms on the V track (peaks from the proxy's audio via the same peaks helper; cheap once v1 is in, deferred only to keep the first lane readable).
- Editing caption text (per-project overrides table, `map_cues` reads overrides first).
- Multi-select on the timeline (Cmd-click several blocks, drag or delete together; needs batch requests).
- Ripple and roll trims across a seam, slip inside a block.
- Reframe by dragging on the stage (the inspector slider covers manual; pan stays Claude's).
- Native "Choose file" for voiceover (`osascript -e 'choose file'` through the engine, 8 lines, if Finder drag turns out to be awkward).
- Voiceover recording from the Mac microphone.
- Markers, titles, Ken Burns on photos, keyframes, more video tracks: no.
- `If-Match` on mutations.

### Risks

- **WKWebView seek latency with 250-frame GOPs** (measured behaviour of the current proxies): the `-g 30` change fixes new proxies; existing ones feel sluggish when scrubbing until regenerated. Mitigation: a one-off `rm data/proxies/*.mp4` by the user when convenient, or accept.
- **Tauri file drop vs pointer drags**: Tauri's drag-drop handler is on by default and swallows OS file drops (good, that is how voiceover import works). All in-app dragging uses pointer events, never HTML5 DnD, so there is no interaction. Check once that `onDragDropEvent` fires with `core:default` permissions; if a capability is missing the fix is one line in `capabilities/default.json`.
- **Rough player fidelity**: colour looks are approximate and wipes play as cuts. The mode label and the one-click Exact render are the honest answer; nobody should judge colour on the rough stage.
- **Clips without proxies**: the first play of an un-analysed clip triggers a download (up to 180 s per clip, slot-capped). The stage shows the thumb and the activity panel the progress; the player skips to the next clip after 5 s if the proxy is still not there and marks the block.
- **HDR proxies look flat** in the webview (known since editing-design). Unchanged by this work.
- **Two writers**: Claude and the user editing the same project at once is handled by sync plus undo reset; it is not a merge. Expect the occasional "Claude changed the edit" toast during an Ask, which is correct.
- **Main.ts size**: `renderProject` already does a lot. The editor lives in its own module and `main.ts` grows by about 15 lines, so the file does not get worse.

### Open questions for the user (max 5, each with a recommendation)

1. **Player truth.** Live rough playback by default with Exact one click away, or always render? Recommendation: live rough; the ask was speed, and Exact costs 3 to 8 s whenever you want it.
2. **Where the editor lives.** Replace the project page below the Ask row with a viewport-high editor (no page scroll, Claude's run history becomes an overlay panel), or keep the scrolling page and add the player and lanes as another section? Recommendation: viewport-high editor. A timeline that scrolls off screen while playing is unusable, and the clip list it replaces was the bulk of the page anyway.
3. **Proxy keyframe interval.** Add `-g 30` to new proxies (slightly larger files, about 10 percent) and delete the 28 existing ones (35 MB) so they regenerate on next play? Recommendation: yes to both; it is the difference between scrubbing and waiting.
4. **Voiceover import.** Finder drag onto the lane only in v1, or also a Choose file button (native picker through the engine via `osascript`)? Recommendation: drag only; add the button in v2 if dragging from Finder onto a Tauri window feels wrong in practice.
5. **Caption text editing.** Defer to v2 as designed, or is fixing a misheard word part of the first version? Recommendation: v2. The lane shows the cues and per-clip on/off in v1; text fixes need a per-project overrides table and are a separate, small change once the lane has been used.
