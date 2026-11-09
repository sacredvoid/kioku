# Changelog

## v0.2.0 (2026-11-09)

- Release cycle: semver, scripts/release.sh, CHANGELOG, version on the Settings page
- Ask for Photos access from the app; say a Photos denial once
- Audio: cut start/end at the playhead (buttons + I/O keys) and End with video
- Sounds: fixes from a live Openverse check
- Timeline: Music & sounds panel (free music default, sound effects, My music), credits with Copy
- Project job status sits above the player, not at the bottom of the page
- MCP: search_sounds (music by default, free or your ~/Music) and add_sound
- Engine: free music and sounds from Openverse, your own files from ~/Music, credits
- Pool db connections: fixes the engine deadlock in SQLite 3.51 (SQLCipher)
- Sign builds with a stable local identity so macOS keeps Full Disk Access across rebuilds
- notes/requests.md: tracker of Sam's requests and bugs
- Project view: Claude's run state is unmistakable
- Settings is a page like every other tab; Cmd+, opens it; html paints the full viewport
- Use the window: wider content, cover-card grids for projects and albums
- Project brief: labeled, read-only until Edit
- Appearance popover with four generative backgrounds
- Show icons in the installed app: allow data: images in the CSP; stronger nav underline
- Encrypt the index with SQLCipher and keep all engine data in ~/Kioku
- Activity center: mini loader in the top bar, job popover, toasts on finish
- Status loaders: 3D wireframe motifs for download, render, index and think
- Status design: research, audit of every status, and a four-tier placement system
- Engine boot loader: self-drawing harmonograph while the engine first starts

Kioku follows semantic versioning: patch = fixes, minor = new features, major = breaking changes to your data or the engine API. Cut a release with `scripts/release.sh patch|minor|major`.

## v0.1.0 (2026-11-08)

- First Kioku build after the Safelight rename: search, albums, smart albums, Claude video projects, timeline editor, captions, audio, renders.
