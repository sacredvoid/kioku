# Safelight handoff (2026-11-08, ~02:45)

Read this first in a fresh session, then `WORK.md` (task list) and the build plan artifact:
https://claude.ai/artifact/PzRFydjjED5yaPAK8jJuu6 (keep it updated as items finish: tasks marked `data-shipped`, collapsible "Build notes" per phase; source file lives in the old session's scratchpad, so read it back with the Artifact tool `read` before republishing).

Private repo: https://github.com/sacredvoid/safelight (remote `personal-git:sacredvoid/safelight.git`, push as `sacredvoid`).
Folder is still `contact-sheet/`. Rename to `safelight/` once indexing stops (the indexer writes to `data/index.db` by absolute path).

## What it is

A local Mac app for the user's Photos library (58,396 items; 54k iCloud-only):
- `engine/`: Python, run with `uv`. FastAPI on 127.0.0.1:8765.
  - `core.py`: db schema, GPU image model, CPU text query model, hybrid `Index`.
  - `index.py`: indexer, uses `--since`, `--videos`, `--rebuild-meta`.
  - `server.py`: the API.
  - `library.py`: tags, captions, albums and projects, stored as pointers.
  - `projects.py`: FCP XML export, plus Ask Claude via `claude -p`, streamed as live steps.
  - `media.py`: iCloud download via PhotoKit, 540p proxy, scene detection, contact sheets, scene embeddings, `/media/activity`.
  - `mcp_server.py`: 14 tools, registered user-wide as `safelight`.
  - `status.py`: indexing progress bar.
  - Tests: `selfcheck.py`, `test_library.py`, `test_media.py`.
- `app/`: Tauri 2 with plain TypeScript and Vite, modeled on `~/Documents-Local/github/otto`.
  - `src/grid.ts`: virtualized grid.
  - `src/viewer.ts`: viewer with info panel, video playback and scene chips.
  - `src/main.ts`: sidebar, views, selection bar, project editor, live refresh, activity panel.
- `skills/safelight-editor/SKILL.md`: the editing workflow, symlinked into `~/.claude/skills`. The in-app Ask Claude uses the same text as its system prompt.
- `data/` (git-ignored, personal): index.db, thumbs, proxies, audio, sheets.

## Running right now

- **Engine**: runs in the user's **iTerm** (it needs Photos access for iCloud downloads; Orca can't get that permission).
  - Restart: Ctrl+C in iTerm, then `↑` and Enter.
  - Code changes to `media.py` / `projects.py` (activity panel, clip labels in steps) need that restart, which kills any running Ask job.
- **Indexer**: `index.py --since 2026-08-01` at 560 tokens, about 72% done (1,824 / 2,545), about 45 min left. Check with `uv run status.py`.
- **App**: `cd app && npm run tauri dev` (Vite on :1420, hot reload).
- **Ask job**: project id 2 "Halloween 2026" was re-running at handoff time (24 steps, 0 clips yet). Check it with `curl -s 127.0.0.1:8765/collections/2/ask`.

## Known issues / decisions

- iCloud download: PhotoKit only, 180 s timeout per video. The AppleScript export hangs (`AppleEvent timed out`), so it was removed. Some videos still fail through PhotoKit; the reason is unknown. They're now reported and skipped.
- Download progress is stage-based (downloading → playable copy → scenes → understanding). osxphotos doesn't expose byte progress.
- Query embedding runs on CPU (text-only fp32, ~40 ms). The GPU is used only by the indexer and scene embedding.
- Image tokens are 560 (about 3× slower than 280). Still to do: A/B 280 vs 560 on the user's own queries, and pick one before indexing all 58k.
- Disk: the index is about 5.3 KB per item (about 310 MB for everything). Thumbs are 41 KB each, made on view. Proxies are about 4.4 MB per minute, made on play or analyze; all 3,551 video minutes would be about 15.6 GB. Option offered: 360p proxies.
- Photos is never written to. Albums and projects are pointers. Only a project *export* copies originals, to `~/Movies/Safelight/<project>/`.

## Since the first handoff (night of 2026-11-08)

- Logging: `data/engine.log` (rotated). Successful polling requests are filtered out of the access log.
- `settings.py` + `data/settings.json`, edited from the ⚙ dialog. `max_agents` caps both Claude Ask jobs and parallel video analysis. `new_cut_model` (opus) for empty projects, `revise_model` (sonnet) otherwise.
- Engine refuses foreign web origins (403) and non-localhost Host headers (audit, 003fbb4).
- MCP read-only tools carry `readOnlyHint` so Claude Code runs them in parallel.
- Deferred fixes are GitHub issues #1 to #5 on sacredvoid/safelight (also listed in WORK.md). Close them from commits with `Fixes #N`.
- Tests: `uv run test_library.py`, `test_media.py`, `test_settings.py` (settings, queues, log filter, origin checks).

## Next up (in order)

1. When the Fall 2026 edit (project 3) finishes, restart the engine in iTerm to load everything above. Then check that a new Ask analyzes several videos at once (sidebar activity panel).
2. Ask the user for 3 searches that came out wrong, then A/B 280 vs 560 tokens.
3. Full-library index overnight (photos and posters first, then `--videos` in batches).
4. Neural Engine spike (offered, not started): pass if cosine > 0.99 and speed > 1.3×.
5. Not built yet: drag-out of originals to Finder or Resolve, and transcripts (whisper.cpp) for speech search.
