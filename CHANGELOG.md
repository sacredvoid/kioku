# Changelog

## v0.15.2 (2026-11-12)

- Review fixes: render cache sweep runs after each render (caps were never applied), tests always use a temp data folder (testenv.py), one ~/Music walk at a time, timeouts on ffmpeg/ffprobe calls, cached proxies skip the video slot queue, per-sound download locks

## v0.15.1 (2026-11-12)

- Ask screen detects a song or artist named in the request and preselects "A song" with it filled in (BUNT reel got free music and the song name as a title)

## v0.15.0 (2026-11-11)

- Find a song by name or link: find_track searches YouTube for song-length candidates and downloads the chosen one; Ask screen gets an "A song" music option

## v0.14.2 (2026-11-10)

- Index info counts unreadable items as failed, not as library items (fixes the Ask page and index dialog totals)
- Index details: item total is photos plus videos; unreadable items stay on their own row
- Text style presets (clean, bold, editorial, playful) can be picked in the inspector, not only by Claude
- Exact playback of a partial preview falls back to the rough cut outside the rendered part (was a frozen frame, playback stalled)
- Inspector no longer collapses to a strip in windows under 1100 px (media rule was overridden)
- Top bar count leaves out items that failed to index, matching the library (58,395 not 58,403)
- suggest_seams compares places by distance first, so a street in Salem is not a new place from Salem
- View enter animation fills backwards only, so sections don't keep a translate after it ends
- Transition picker is no longer cut off: fixed on body, flips above the seam when needed
- Audio panel names searched music by title, not its uuid filename
- Viewer makes the page behind it inert, so VoiceOver stays in the dialog
- Timeline clips named by place and date, audio tracks no longer called Voiceover
- Asks show what you typed, not the creative-direction block sent to Claude; settings labels capitalised
- Library tiles have VoiceOver labels (kind, length, date)
- Reference links over 15 min use their first 15 min instead of failing

## v0.14.1 (2026-11-10)

- Long location tags widen before shrinking in 9:16 (28 px -> 37 px at 1080)
- Music tracks show their title, not the uuid filename
- Project header shows clip count only (summed clip lengths said 1:28 for a 57 s edit)
- Exact playback works after an engine restart (preview file 404'd)

## v0.14.0 (2026-11-10)

- Colour match across clips and a finish layer (roadmap #10, X7 + X8)

## v0.13.0 (2026-11-10)

- Seam clip engine: zoom-through, light leak, luma fade, glitch (roadmap #8, X3)
- Music search v2: analysed shortlist, Jamendo and Incompetech sources, library search by feel
- Text pack: subpixel motion, easings, per-letter templates, motion blur, word-on-beat, text-behind, Photos tokens, text styles (roadmap #6)

## v0.12.0 (2026-11-10)

- suggest_seams: read-only per-seam facts and one suggested transition (roadmap #7, X4)
- Curated CC0 seam and text sounds, declick at hard cuts
- Auto reframe for 9:16: follow faces then saliency with a smoothed crop path

## v0.11.0 (2026-11-10)

- Face-aware text placement, 9:16 safe area, auto legibility backing
- Music map and auto fit: per-bar energy, sections, drop, fit track to edit via audio spec fields
- Eased motion and real whips (roadmap #1, X1 + X2)
- notes: editing power research (text, transitions, music roadmap)

## v0.10.0 (2026-11-10)

- Reuse unchanged final renders, fix Export for Resolve, deselect on outside click

## v0.9.0 (2026-11-10)

- Edit versions, titles lane, cached originals, versioned render names

## v0.8.0 (2026-11-10)

- Editor: FCP-style inspector sections, captions in inspector, growing Ask box

## v0.7.1 (2026-11-10)

- Fix: cap concurrent Apple Vision calls at 2; parallel find_moments deadlocked Vision's capacity queue and froze edits

## v0.7.0 (2026-11-10)

- Fable audit fixes: engine stops for good (process group, waits on the pid, then SIGKILL; busy engine not mistaken for stale); edits interrupted by a restart show as failed with Retry; request timeouts and cancellable iCloud analyze; Ask box never detached; drop the :has() rule that could hide the live panel in WebKit; pill Show works from any page; scoped #empty/#project; cheaper glow
- Collapsible brief, floating 'Claude is editing' pill with editor glow, app errors logged to engine.log

## v0.6.1 (2026-11-10)

- Fix: live edit panel restarts its poll if it dies and shows iCloud downloads; Library select circle selects; review tile caption no longer inherits the Library match dot

## v0.6.0 (2026-11-10)

- Appearance > Font: seven bundled font pairs for text and headings, remembered per device
- Live edit panel: phase, pipeline, running calls with clips and timers, failed steps with their error; yt-dlp finds Homebrew deno and ffmpeg

## v0.5.0 (2026-11-10)

- Ask: treat polite requests (can you make, I'd like a video of) as edits, not searches
- Creative direction composer on the home page; skill asks before it searches
- Creative direction questions grounded in the library; reference links via yt-dlp
- Skill: one workflow for the full editing kit
- Texts section in the project inspector: add at playhead, edit, style, remove
- Titles: animated text templates, bundled OFL fonts, film LUT looks
- Declare pyobjc-framework-vision (OCR, moments) instead of relying on osxphotos pulling it in
- Phase 4: style reference analysis, planner, MCP tools and skill section
- Skill: find moments before cutting
- moments: brief match from stored scene embeddings (1 fps frame embedding took 40 s per clip)
- Rhythm in the timeline, MCP tools and skill
- moments: routes and find_moments MCP tool
- moments: signals, fusion and tests (ffmpeg stats, Vision, SoundAnalysis)
- Beat sync, hits, motion, punch, shake, flash, speed ramps, more transitions, seam sounds
- Design: rhythm, motion, moments, titles, style references

## v0.4.0 (2026-11-10)

- Four more background arts: Harmonograph, Moire, Truchet, Lattice
- Library filters stick under the top bar while scrolling
- Project and album covers: first, middle and last clip as a collage

## v0.3.3 (2026-11-09)

- Copy project previews out of Photos once, so scrubbing stops prompting

## v0.3.2 (2026-11-09)

- Run the engine bundled in Kioku.app, venv in ~/Kioku

## v0.3.1 (2026-11-09)

- Replace a leftover engine from another version at launch; Month says to pick a year

## v0.3.0 (2026-11-09)

- Ask page: sections as cards with real headings
- Top bar: centre the window buttons in the 56px bar
- Library: year and month filters for browsing and search

## v0.2.1 (2026-11-09)

- Declare the Photos entitlement so the Photos prompt can appear

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
