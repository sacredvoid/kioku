# Safelight build plan: editing + captions + smart albums

Date: 2026-11-08 (Fable review). Inputs: `notes/editing-design.md` (branch `design/editing`) and `notes/smart-album-design.md` (branch `design/smart-album`), both reviewed and amended today; main is at `a594e1c` (index details panel, `/index/info`, `reindex?newer`), which both designs predate. The a594e1c changes touch `server.py` (`/index/info`, `QUIET`), `main.ts` (index dialog, after `heartbeat`), `index.html` (`#index-dlg`), `styles.css`, `test_settings.py`: none of them overlap a hunk below, but every branch starts from main after the contract commit, not from the design branches.

Rules for everyone: nothing writes to Photos; tests set `os.environ["CS_DB"]` to a temp path **before** `import core` and assert `"data/index.db" not in str(core.DB_PATH)`; nobody installs anything (whisper-cpp and its model are the user's step); nobody restarts the engine in iTerm or touches `index_months.sh`; no em dashes in text; `uv run test_library.py test_settings.py` (and your own suite) pass before you ask for a merge. `test_media.py` downloads a video, so it runs once at the end, by hand.

## 1. Contract commit on main (integrator, before anyone branches)

One commit, about 60 lines of code plus docs, that every worktree builds on:

| File | Change | Why first |
|---|---|---|
| `engine/timeline.py` | NEW, the frozen `Seg` / `Timeline` dataclasses from editing-design section 2, no logic | A, B, C all import it |
| `engine/media.py` | `stage(key, name, label=None)` and `video_slot(key, label=None)` accept a label (existing calls unchanged); `local_original(uuid) -> Path \| None` factored out of `ensure_proxy`; `fetch(uuid, mode, tmp) -> Path` = `lock_for(uuid)` then `video_slot` then (`ensure_proxy` for preview, `local_original` or `projects.originals` for final) then release, same lock order as `analyze` | B and C both need it; putting it in the contract removes every `media` stub from their tests and keeps `media.py` out of A's branch |
| `engine/core.py` | `STOP` gains `photo photos picture pictures image images video videos` | D's fallback path and plain search both benefit (verified today: `/search?q=photos` returns 155 keyword hits of 300); one line, zero risk |
| `notes/editing-design.md`, `notes/smart-album-design.md`, `notes/build-plan.md` | the reviewed docs | so implementers read one version |

Nothing in the contract commit needs the engine restarted (the running engine keeps its old `media.py` in memory; the new functions are only called by new code).

## 2. Four parallel worktrees

Branch from main after the contract commit. Each owns only the files listed; anything else is a request to the integrator.

| Implementer | Branch | Owns | Imports from others (no edits) |
|---|---|---|---|
| **A: edit spec** | `edit/spec` | `engine/edit.py`, `engine/test_edit.py` | `timeline`, `library`, `core`; lazily `captions.start_transcription` inside the `PATCH /edit` route (tests stub it) |
| **B: render** | `edit/render` | `engine/render.py`, `engine/test_render.py` | `timeline`, `media.fetch/video_slot/stage/contact_sheet/frame_at`, `projects.start_job/job/EXPORTS/slug`, `edit.snapshot/output_size/look_filters` and `captions.*` (fakes until merged) |
| **C: captions + surface** | `edit/captions` | `engine/captions.py`, `engine/test_captions.py`, `engine/mcp_server.py` (7 tools), `engine/projects.py` (`STEP_LABELS` entries and the one `SYSTEM` sentence only), `skills/safelight-editor/SKILL.md`, `app/src/api.ts` (extend `Job`, add `Captions`), `app/src/main.ts` (**only** `renderProject` and `pollJob`, roughly lines 211 to 380 of today's file), `app/src/styles.css` (render row, progress) | `timeline`, `media.fetch/video_slot/lock_for`, `index.extract_audio`, `edit.mutate` (stub) |
| **D: smart albums** | `album/draft` | `engine/albums.py`, `engine/test_albums.py`, `app/src/review.ts`, `app/src/review.css`, `engine/settings.py` (one field `album_model`), `app/src/index.html` (review container, Smart album button, Drafts group), `app/src/main.ts` (**only** the `View` union, `go()`, `renderHead`, `markNav`, `syncCollections`, the `document` keydown handler) | `projects.agent_slot/release_slot/claude_bin/mcp_config` (phase 2 only), `library`, `core`; `server.fresh_index` via a lazy import inside `albums.search` |

`main.ts` is the one file two people touch. C's hunks are inside `renderProject` and `pollJob`; D's are the `View` type, `go`, `renderHead`, `markNav`, `syncCollections` and the keydown block. Neither reformats the file, neither touches the index dialog or settings code at the bottom. `api.ts`: C only; D keeps its `Draft` types in `review.ts`. `index.html` and `settings.py`: D only. `styles.css`: C only; D uses `review.css`. `server.py`: nobody but the integrator. `mcp_server.py`, `SKILL.md`, `projects.py`: C only (D does not need any of them for v1).

Names that must stay stable (D and B import them): `projects.agent_slot`, `projects.release_slot`, `projects.claude_bin`, `projects.mcp_config`, `projects.start_job`, `projects.job`, `media.video_slot`, `media.release_video_slot`, `media.lock_for`, `media.fetch`, `server.fresh_index`.

## 3. Merge order

1. Contract commit (section 1).
2. **D** (independent of A, B, C; smallest blast radius; lands the `main.ts` navigation hunks first so C rebases onto them).
3. **A** (root of the editing stack; `test_edit.py` has no ffmpeg).
4. **C** (captions module, tools, skill, app). Rebase onto D's `main.ts`; run `npm run build` in `app/`.
5. **B** (needs A at test time only through stubs; after A and C are in, delete the stubs and run `test_render.py` against the real modules).
6. **Integration commit** (integrator, section 4).
7. By hand: `brew install whisper-cpp`, download `ggml-small.bin`, one real transcription; one real iPhone HLG portrait clip through preview and final and a side-by-side in QuickTime; `test_media.py`; then restart the engine in iTerm (kills any running Ask) and the app.

Each merge is a plain `git merge --no-ff` of the branch into main after rebasing it; conflicts outside the listed hunks mean someone edited a file they do not own, so stop and ask.

## 4. Integration commit (integrator)

- `server.py`: `import albums, captions, edit, render`; four `app.include_router` lines next to the existing ones; `QUIET` gains `collections/\d+/(render/(preview|final)|captions/job)` and `drafts/\d+/job`.
- `settings.py` already has `album_model` (D). Check `test_settings.py` still passes with the extra field in the PUT bodies.
- Delete any stubs or `try: import` shims left in `test_render.py` and `test_captions.py`.
- Run: `uv run test_library.py`, `test_settings.py`, `test_edit.py`, `test_render.py`, `test_captions.py`, `test_albums.py`; `cd app && npm run build`.
- Boot a second engine on a spare port with a temp db (`CS_DB=/tmp/x.db uv run uvicorn server:app --port 8766`, never the iTerm one), create a project through curl, drive one preview render, one `PATCH /edit`, one draft round with `albums.plan` pointing at a fake; check `/media/activity` shows the render label.
- `git grep -nP "\x{2014}"` (em dash) over `notes/ skills/ engine/*.py app/src/` is empty.
- Update `HANDOFF.md` and `WORK.md` (new tests, new tools, what still needs the user).

## 5. Integrator checklist before calling it done

- [ ] No test ever opened `data/index.db` (`ls -la data/index.db*` mtime unchanged across the test run while the indexer is idle, or compare `select count(*) from assets` before and after).
- [ ] Every segment and final output has no `stream_side_data rotation` (editing-design review item 1).
- [ ] `JOIN_CHUNK` respected: a 20-clip test with dissolves peaks under 2 GB at 540x960.
- [ ] Caption overlay uses `eof_action=pass` without `shortest`; cue list has `option framerate N` lines.
- [ ] Render final output path is under `~/Movies/Safelight/<slug>/` after `resolve()`, never overwrites, `GET .../file` serves only the recorded path.
- [ ] Free-disk check runs before a final render; temp dirs removed on success, failure and cancel; no `.part*` files left.
- [ ] Lock order everywhere is `lock_for(uuid)` then `video_slot`; nothing waits on a lock or slot while holding the render slot.
- [ ] Draft save is one transaction under `begin immediate`; second save returns the same album id; a refine whose album was deleted returns 409.
- [ ] Review screen: tiles are `<button aria-pressed>`, arrow keys move focus, X/Delete/Enter/Space toggle, V opens the viewer, status line is `aria-live`.
- [ ] 21 MCP tools listed by `claude mcp list`-equivalent (start `mcp_server.py` and count), read-only ones carry `readOnlyHint`.
- [ ] `SKILL.md` steps 5 and 6 mention `set_edit`, not `set_format` / `apply_look` / `set_captions`.
- [ ] Engine restart scheduled with the user (it kills running Ask and render jobs).

## 6. What the user installs or decides (not implementers)

- `brew install whisper-cpp` and `ggml-small.bin` into `data/models/` (editing-design section 4). Nothing else: Pillow, fonts (`Arial Bold.ttf`, `Arial Unicode.ttf`) and ffmpeg 9.0.2 are present.
- Decisions listed in the review report: default aspect, caption model, album planner model, Save order, whether to trust `scale_vt` after the real-clip check.
