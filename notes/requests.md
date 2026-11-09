# Sam's requests and bugs (session 2026-11-09)

Status words: **live** = on main and running in the installed app; **built** = committed on a branch, not merged;
**open** = not fixed; **waiting** = needs Sam's decision. Update this file whenever something changes.

## Live (on main)
| # | Request / bug | Where |
|---|---|---|
| 1 | Better "loading engine" screen (was a pulsing dot) | main 2999f56: harmonograph boot loader |
| 2 | 3D loaders per status, mini/big; sweep of where statuses show | main 7b58a13..5674afd, notes/status-design.md |
| 3 | Move index out of the repo to ~/Kioku, owner-only | main 1bd8ca1 |
| 4 | Encrypt the index, keep the key safe | main 1bd8ca1: SQLCipher, key in login Keychain (Sam saved a copy) |

## Merged to main 2026-11-09 (9938eac), in the DMG; live once Sam installs it
| # | Request / bug | Commit | Verified |
|---|---|---|---|
| 5 | Buttons/icons invisible (play, prev/next, Add clips, nav) | f91766b: CSP img-src lacked data: | cause confirmed; not yet seen in the installed app |
| 6 | Nav underline too subtle / misaligned | f91766b | not yet seen in the installed app |
| 7 | Better theme switcher; themes from creative-code-lab art, not the old art | 0e25625: Appearance popover, 4 arts | WebKit screenshots |
| 8 | Brief box: unclear, looked editable | 3821a6d: labeled, read-only until Edit | WebKit screenshots |
| 9 | App not using its space | 35d871d: wider layout, card grids | WebKit at 2400px and 900px |
| 10 | Settings pops up unlike other tabs | d43de36: Settings is a page, Cmd+, | WebKit screenshots |
| 11 | Dark band left after switching Dark to Light | d43de36: html/body paint full viewport | WebKit, 100ms after flip |
| 12 | No clear Claude working / done state in project view | d535fb6 | typecheck only, no screenshot (engine hung) |

## Open bugs
| # | Bug | Known so far |
|---|---|---|
| 13 | Clip previews broken ("?" in player, blank tiles) | FIXED: macOS blocked Photos reads after reinstalling an ad-hoc signed app; Sam granted Full Disk Access; 8168a14 signs every build with a stable local identity so the grant survives rebuilds (one last grant needed for the first signed build). |
| 14 | Engine hung (no response, 2026-11-09 14:24) | Deadlock inside bundled SQLite 3.51.1 (SQLCipher): one thread closing a WAL connection, one opening, both waiting on SQLite's file-lock mutex. FIXED 189431b: pooled connections, so nothing opens or closes at runtime (1.17M checkouts on 16 connections, no stall). Engine picks it up on next app launch. |
| 15 | Render/download status sits at the bottom of the project page; must scroll to see it | FIXED 9938eac: status stack above the player, scrolls into view on start (WebKit checked) |
| 16 | "Downloading originals: 0 of 10 · 0%" did not move | likely 13 and/or 14 (both fixed); retest after installing |

## Added later
| # | Request / bug | Status |
|---|---|---|
| 20 | App attaches to a frozen leftover engine (port open, no answer) | open: check /health, offer restart |
| 21 | How to chop an audio track | built 3faee96: Cut start/end here (I/O), End with video; in the 15:25 DMG; not checked in the installed app |

## Waiting on Sam
| # | Decision | Ask |
|---|---|---|
| 17 | Free internet audio (Claude adds it, Sam browses/plays in app); mostly free music, sometimes own music | MERGED to main: Openverse music (default) + sound effects, My music (~/Music), MCP search_sounds/add_sound, credits + credits.txt; default CC0 + CC BY, NC switchable in Settings. Verified: 12 engine tests, live Openverse search + download + waveform, WebKit (mocked). Not verified: Add at playhead in the installed app; drag onto a lane not built; ogg/flac results skipped. |
| 18 | Stable code signing so macOS permissions survive rebuilds | DONE 8168a14: "Kioku Local Signing" cert in login Keychain |
| 19 | Delete old plaintext kioku/data/ (1.4 GB) | "delete old data" |
