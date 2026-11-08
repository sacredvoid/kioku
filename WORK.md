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
- [ ] Halloween 2026 edit (project 2): verify result + export after restart

## Running in the background

- Index Aug-Nov 2026 at 560 tokens, ~72% (`uv run status.py --watch`)
- Engine runs in iTerm (Photos access); see HANDOFF.md

## Waiting on you

- Turn on iTerm in System Settings > Privacy & Security > Photos, then run the engine from iTerm (iCloud video downloads; AppleScript fallback times out)
- 3 searches that returned wrong results

## Decisions log

- 2026-11-08 App shell: Tauri (Otto's setup). Name: Safelight.
- 2026-11-08 Photos library is never written to. Tags, captions, albums live in data/index.db.
- 2026-11-08 Albums/projects store pointers only. Only a project *export* copies originals (editors need real files).
