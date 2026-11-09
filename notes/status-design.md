# Status design: how Kioku tells you what it's doing

Branch `feat/status-overhaul`. This is a sweep of every status the app shows (or should show), plus a placement system: where each kind of status lives, how loud it is, and what it looks like. It comes with a small family of 3D loaders in the harmonograph / Leisure Lab style.

## 1. What the guidance says

**Response time limits** (Nielsen, [Response Times: The 3 Important Limits](https://www.nngroup.com/articles/response-times-3-important-limits/))
- 0.1 s feels instant: show the result, nothing else.
- 1 s keeps the train of thought: past it, show that the system is working.
- 10 s is the limit of attention: past it, show percent done and offer a way to cancel.

**Progress indicators** (NN/g, [Progress Indicators Make a Slow System Less Insufferable](https://www.nngroup.com/articles/progress-indicators/))
- Nothing under 1 s (a spinner that flashes is a distraction).
- Looped animation for 2 to 10 s, with text saying what is happening ("Loading comments...").
- Percent done for 10 s or more, plus a time estimate with some buffer, plus a stop option. Step counts ("3 of 50") can beat percentages.
- Never a static "Loading..." that can't show a stuck system. People with a moving indicator waited about 3x longer.

**Skeletons vs spinners vs bars** (NN/g, [Skeleton Screens 101](https://www.nngroup.com/articles/skeleton-screens/))
- Skeletons for full-page loads under ~10 s. Spinners for a single module, 2 to 10 s. Progress bars for anything over 10 s and for downloads, uploads and conversions.

**Apple HIG** ([Progress indicators](https://developer.apple.com/design/human-interface-guidelines/progress-indicators); the live page is JS-rendered, read via the [markdown mirror](https://developer.apple.com/design/human-interface-guidelines/progress-indicators.md))
- Prefer determinate whenever possible; switch an indeterminate indicator to determinate as soon as the duration is known.
- Prefer a spinner for background operations or tight spaces.
- Finder pairs a determinate bar with short text so people can judge a copy. Keep indicators close to the content they describe, at a control size that fits (regular, small, mini).

**Material 3** ([Progress indicators](https://m3.material.io/components/progress-indicators/guidelines), JS-rendered; implementation notes from [material-web](https://material-web.dev/components/progress/))
- Same determinate / indeterminate split. Each indicator needs an accessible label.

**Notifications, toasts, inline status** (NN/g, [Indicators, Validations, and Notifications](https://www.nngroup.com/articles/indicators-validations-notifications/); Carbon, [Notification usage](https://carbondesignsystem.com/components/notification/usage/))
- Indicators are contextual and passive: put them next to the thing they describe, and use them sparingly.
- Notifications are for events the user didn't just trigger. Passive ones (badge, small popover) are easy to miss, so they can't be the only home of key information.
- Avoid toasts for errors: they fade before people read them. If a toast auto-dismisses, the same information must be findable elsewhere (Carbon).
- An actionable notification carries one action, and stays long enough to use it.

**Avoiding flashes** ([Firefox Acorn: loading indicators](https://acorn.firefox.com/latest/foundations/global-patterns/loading-indicators-SKyqf3Lj), [spin-delay](https://github.com/smeijer/spin-delay))
- Don't show a loader for waits under ~200 ms; delay showing one by ~300 to 500 ms so fast work never flashes.

**Reduced motion** ([MDN prefers-reduced-motion](https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-reduced-motion))
- Scaling and panning large objects can trigger vestibular discomfort. Under `reduce`, remove or replace non-essential motion; keep the information.

### Rules Kioku follows

1. **Under 1 s: nothing.** Every loader fades in after 350 ms (`.ld` animation delay), so quick work never flashes one.
2. **1 to 10 s: indeterminate**, always with words saying what is happening.
3. **Over 10 s: determinate** (percent, step "6 of 14", or both) and an eta when the engine has one; Cancel where the engine supports it.
4. **Status lives where the work is.** The background copy (activity center) is a second home, never the only one.
5. **Toasts only for background work ending while you look elsewhere**, with one action ("Show in Finder", "Open"). Errors toast longer and stay in the activity center's Finished list.
6. **Errors say the cause and the next step** (existing copy already does: "stopped because the engine restarted ... run it again to continue").
7. **Reduced motion:** loaders draw one still frame that still shows progress; no animation loop runs.

## 2. Audit: every status, before this branch

Visibility 1 (easy to miss) to 5 (can't miss). "Exposes" is what the engine API returns.

| Status | Where it appeared | Vis. | Exposes | Typical | Importance |
|---|---|---|---|---|---|
| Engine starting (first boot) | Boot hero: harmonograph + "Starting engine" | 5 | up / down (`/health`) | 5 to 60 s (model load, first run downloads weights) | High: blocks everything |
| Engine offline after boot | Top bar pill "Engine offline, retrying" (pulsing warn dot); views show "Can't reach the engine" | 3 | up / down | seconds to minutes | High |
| Index size | Top bar pill "N indexed" | 4 | `assets` | static | Low |
| Indexing (Fetch new photos) | Pill suffix " · indexing"; index dialog text | 2 | `reindex: running` only. `progress.json` (total, started) existed for the CLI `status.py` | minutes to hours | Medium |
| Indexing failed | Pill suffix + tooltip | 2 | `reindex: failed: ...` | n/a | Medium |
| First index (0 items) | Library empty text "Nothing indexed yet" | 4 | as above | hours | High |
| Video fetch / analyze (iCloud download, playable copy, scenes) | Floating panel bottom left, hidden under 760 px | 3 | `/media/activity`: label, stage (4 named stages), seconds | 5 to 120 s each | Medium |
| Viewer "Play video" fetch | Button text "Fetching from iCloud and finding scenes..." | 4 in place | none while it runs (one blocking POST) | 5 to 120 s | Medium |
| Claude edit (Ask) | Project view: live steps with a pulse dot; Ask page board line | 5 in the project, 1 anywhere else | `/collections/{id}/ask`: steps, model, started. No percent | 30 s to 10 min | High |
| Ask ended while away | Nothing (you find out on return) | 1 | job status | n/a | High |
| Engine lost mid-job | Inline "Lost contact with the engine, retrying" / "stopped because the engine restarted" | 4 in place | job goes `idle` | n/a | High |
| Export for Resolve | Inline text only | 3 in place, 0 elsewhere | status only (no stage or percent) | 10 s to 10 min (iCloud originals) | Medium |
| Render preview | Inline bar, stage, %, eta, Cancel | 4 in place, 0 elsewhere | progress, stage, eta, cancel | 10 s to 2 min | High |
| Render final | Same, plus Play / Show in Finder at the end | 4 in place, 0 elsewhere | same | 1 to 20 min | High |
| Captions transcription | Inline bar, "Transcribing n of t" | 4 in place | progress, stage | 10 s to 5 min | Medium |
| Smart album rounds (Review) | Review tab pulse dot, draft status line | 3 | `/drafts` running flag, step text | 30 s to 2 min | Medium |
| Timeline edits (trim, split, undo) | Toasts for undo/redo and errors | 4 | instant | under 1 s | Low (instant) |
| Settings / brief / audio saves | Toast "Settings saved", inline errors in the dialog | 4 | instant | under 1 s | Low |
| Empty / error states | `showEmpty` text in the main column | 4 | n/a | n/a | Medium |
| Model downloads (search model, whisper) | Covered by boot; whisper shows an install hint in captions | 3 | none | first run only | Low |

**Biggest gaps:** long jobs vanished the moment you left their project (visibility 0 to 1 elsewhere), finishing was silent, indexing had no progress at all, and the one cross-app panel (media fetches) sat bottom left where nothing else lives and disappeared on narrow windows.

## 3. Placement system

Four tiers. A status gets the quietest tier that still keeps it findable.

| Tier | Where | Used for | Form |
|---|---|---|---|
| **Big (hero)** | Fills the main column | Blocking waits only: engine boot, first index (Library with 0 items while indexing) | 120 to 168 px loader + title + one line of what's happening |
| **Medium (in context)** | Inside the panel where the work happens | Render / preview / captions progress row, export, Claude's live steps, viewer video fetch | 20 to 30 px loader beside the text; determinate when the engine has a number; Cancel next to it |
| **Mini (ambient)** | Top bar, left of the "N indexed" pill | Every background job, aggregated | 18 px loader of the first job's kind, count when more than one; a tick (or red tick after a failure) once all is done. Opens the activity center |
| **Toast** | Bottom center | A background job ended and you aren't looking at it | One line + one action; 8 s; errors use `role=alert`; also kept under Finished |

**Activity center** (popover from the mini button): one row per job with its loader, title ("Render · Kyoto in autumn"), detail (stage, percent, eta, or Claude's latest step), a progress bar when determinate, Cancel (renders) and Open (any project job). Below: up to 6 Finished items with Show in Finder / Open / Details and a Clear. Rows update in place, so focus on a button survives the 1.5 s refresh. Keyboard: the button is a real button with `aria-expanded` and `aria-controls`; opening moves focus into the popover; Escape closes and returns focus; clicking elsewhere closes. A visually hidden `role=status` region announces "Started: ..." when a job appears; completions are announced by the toast's live region.

What did **not** change: engine-offline stays on the pill (it's a connection state, not a job); quick saves keep their short toasts; Review's draft status stays on the Review tab.

## 4. Loader family (`app/src/loaders.ts`)

One motif per kind of work so you can tell them apart at a glance. All are thin-stroke wireframes projected from 3D into SVG by a small TS projection (spin about the vertical axis, fixed tilt, perspective), no dependencies. Far edges are faint (`--text-3`), near edges ink (`--text-2`), finished work in `--accent`, so every nature theme and both color schemes come for free.

| Motif | Shape | Determinate | Indeterminate | Mini (24 px or less) |
|---|---|---|---|---|
| `download` (iCloud, export) | Icosahedron with a water line | Edges below the line turn accent as it rises | The tide rises and falls | Octahedron |
| `render` (preview, final) | Film frames stacked in depth, twisting | Finished frames accent; the current frame's outline is traced edge by edge | A traced frame travels through the stack | 3 frames |
| `index` (indexing, captions, scene analysis) | Fibonacci point sphere | Points light pole to pole | A latitude band sweeps | 34 points |
| `think` (Claude edits) | The boot harmonograph lifted into 3D | n/a (Ask has no percent) | A lit pen runs along the curve | Shorter curve |

`new Loader(motif, sizePx, progress?)`, then set `.progress` (0..1 or null). The SVG is `aria-hidden`; the words beside it carry the status. One shared `requestAnimationFrame` loop draws visible loaders at ~30 fps; it stops when the document is hidden, skips loaders off screen (IntersectionObserver) and drops ones whose view was re-rendered. Under reduced motion no loop runs: each loader draws one still frame, redrawn only when its progress changes. The boot hero keeps its CSS self-drawing harmonograph and now shares the curve (`harmo`) with the `think` motif.

## 5. Engine changes (both tiny, existing endpoints)

- `projects.start_job` registers every project job (ask, export, preview, final, captions) in `media.ACTIVE` as `job:<project>:<kind>` with the project name, and clears it when the job ends. `/media/activity` therefore lists all background work in one call; the app reads percent / eta / steps from the job's own endpoint.
- `/health` adds `progress: {done, total}` while a reindex runs, read from the `progress.json` that `index.py` already writes (same math as `status.py`). The heartbeat polls every 2 s while indexing.

## 6. Covered vs deferred

Covered: engine boot (hero), first index (hero), indexing (mini + center, determinate, finish/fail toast), video fetch/analyze (center, step progress; viewer button loader), Claude edits (inline think loader, center, finish toast), export (inline loader, center, Show in Finder toast), render preview / final (inline determinate loader, center with Cancel, toasts), captions (inline + center), engine restart mid-job (center Finished + error toast).

Deferred, needs engine work:
- **Export progress.** `do_export` reports no stage or percent; the iCloud download in `originals()` is one osxphotos call. Could count files appearing in the dest folder, as `render.fetch_originals` does.
- **Ask cancel.** No cancel endpoint for Claude edits.
- **Smart album rounds in the activity center.** `albums.start_round` could register `job:draft:<id>` the same way; left out to keep the diff small, the Review tab already shows a live dot.
- **Model download progress** (search model weights on first boot, whisper model). Not exposed by the engine.
- **Indexing eta.** `status.py` computes a rate from the last 5 minutes; `/health` could return it.
- **Engine offline banner.** After boot, losing the engine is only on the pill and in empty views; a slim banner under the top bar would make it 5/5.
