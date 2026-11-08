# Safelight work tracker

Live mirror: https://claude.ai/artifact/PzRFydjjED5yaPAK8jJuu6 (build plan, "Now" box + phases)

Status keys: [x] done · [~] in progress · [ ] todo

## Sprint: library features (started 2026-11-08)

- [x] Perf: grid virtualized (DOM stays ~130 tiles at any library size)
- [x] Perf: 400px cached thumbs (42 KB vs 201 KB), /full for viewer
- [x] Perf: query embedding on CPU, cached (40 ms, no GPU wait); /timeline gzip
- [x] Data: collections (albums + projects) as pointers (uuid + in/out), never copies (engine/library.py, test_library.py passes)
- [x] Data: your own tags + caption, kept in Safelight db, searchable; Photos stays read-only
- [x] Data: place, coordinates, Photos albums, iCloud vs local per item (832 items filled)
- [x] UI: sidebar (Library / Photos / Videos / Albums / Projects), multi-select (⌘/shift-click, ⌘A), "Add to…" menu
- [x] UI: viewer info panel (place, where the original lives, labels, editable tags, caption) — tag edit verified searchable
- [x] UI: visual refresh (darkroom palette, New York serif headings, glow accent)
- [x] Projects: brief, clip list with in/out + reorder + remove, Export for Resolve button
- [x] Claude: MCP server, 13 tools, registered user-wide as `safelight` (connected)
- [x] Claude: "Ask Claude" box in each project (built a 3-clip project in 27 s, no shell/file tools)

## Next: editor skill (requested 2026-11-08)

- [x] Proxies: 540p video copy per clip in data/proxies; temp original deleted after (engine/media.py)
- [x] Scene detection (PySceneDetect adaptive) + sharpness/motion per scene + scene embeddings; test_media.py finds cuts at 3.0s and 9.0s
- [x] MCP: analyze_clip returns scenes + contact sheet image (verified text+image content); search returns best_moment
- [x] Skill `safelight-editor` in skills/ (symlinked to ~/.claude/skills), shows up in Claude Code
- [x] In-app Ask Claude uses the same skill instructions
- [x] Viewer plays videos (540p copy), "Play video" fetches from iCloud once, scene chips jump to scenes
- [ ] End-to-end run on real iCloud videos (blocked: needs Photos access, see Waiting on you)

## Session 2026-11-08 late additions

- [x] Private repo sacredvoid/safelight (secret/PII scan clean; data/ ignored)
- [x] Sidebar + open project auto-refresh every 5 s (projects made by Claude show up)
- [x] Esc always closes the viewer (even from caption/tag box/video)
- [x] iCloud download: PhotoKit only, 180 s cap, clear errors (AppleScript export hangs, removed)
- [x] Ask Claude streams live steps (searches, clips watched, trims) with a timer
- [x] Media activity panel in sidebar (per-video stage bar, done/skipped count) - needs engine restart
- [x] Halloween 2026 edit (project 2): 12 shots, 30.2 s; user approved. Export not run yet

## Session 2026-11-08 night: reliability, settings, audit

- [x] Engine logging: terminal + data/engine.log (rotated), polling noise hidden (88cdc23)
- [x] Readable errors everywhere: JSON 500s, error toasts, job lost on restart, lost-contact state (88cdc23, de2c2ba)
- [x] Settings dialog (⚙): max agents, Opus first cut / Sonnet revisions, Ask limit, iCloud timeout, proxy height (88cdc23, 7c3ce9d)
- [x] Fable audit: 8 fixes (search race, CSRF/DNS rebinding, trims clear, timeline 2.4x, grid/viewer bugs) (2903d54..8f362d6)
- [x] Parallel video watching capped by max agents; MCP read-only tools run in parallel (89fe33f)
- [x] Ask placeholder describes the project; empty Ask + brief = make it from the brief (89fe33f)
- [~] Fall 2026 edit (project 3), started on the old engine, one video at a time
- [ ] Restart engine to load all of the above (after Fall finishes)

## Editing, captions, smart albums (2026-11-08 afternoon)
- Designs: notes/editing-design.md, notes/smart-album-design.md, combined notes/build-plan.md (Fable-reviewed).
- Built in 4 worktrees and merged: edit.py (spec, validation, timeline, looks), render.py (ffmpeg preview/final, chunked join, HDR, portrait), captions.py (whisper-cli, cue PNG overlay, .srt), albums.py + app/src/review.ts (smart album drafts). 7 new MCP tools (22 total). Integration commit 04f1b03.
- Tests: test_edit, test_render, test_captions, test_albums, test_contract (all set CS_DB before importing core).
- Smoke-tested on a spare engine (port 8766, db copy): 8 s 9:16 warm preview of Halloween rendered upright, bt709; smart album draft via real claude -p returned 150 candidates.
- Index details panel: click "N indexed" (a594e1c); Fetch new photos = POST /reindex?newer=true.
- Still needs Sam: `brew install whisper-cpp` + ggml-small.bin into data/models/, then confirm whisper-cli flags with `whisper-cli -h`; one real iPhone HLG portrait clip through preview + final, compare in QuickTime; try the review screen; test_media.py once by hand.
- Known nit: preview contact sheet leaves wide gaps for portrait tiles.

## Open issues (deferred from the audit, tracked on GitHub)

- [ ] #1 WAL for index.db (do after the indexer stops)
- [ ] #2 Index reload blocks a search every 30 s while indexing
- [ ] #3 Thumbs generated for tiles scrolled past
- [ ] #4 Reorder float midpoints run out after ~50 moves (bug)
- [ ] #5 29.97 fps exports onto a 30 fps timeline (bug)

## Running in the background

- Index Aug-Nov 2026 at 560 tokens, ~72% (`uv run status.py --watch`)
- Engine runs in iTerm (Photos access); see HANDOFF.md

## Waiting on you

- 3 searches that returned wrong results

## Decisions log

- 2026-11-08 App shell: Tauri (Otto's setup). Name: Safelight.
- 2026-11-08 Photos library is never written to. Tags, captions, albums live in data/index.db.
- 2026-11-08 Models: Opus for a project's first cut (creative), Sonnet for revisions; user is on Max 5x. One cap (max agents) limits both Claude edits and parallel video downloads.
- 2026-11-08 Albums/projects store pointers only. Only a project *export* copies originals (editors need real files).
