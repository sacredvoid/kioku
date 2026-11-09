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

## Built on feat/theme-art (not merged, not in the installed app yet)
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
| 13 | Clip previews broken ("?" in player, blank tiles) | Cause: macOS blocked Photos library reads after reinstalling the ad-hoc signed app. Thumbs load again after restart (200). Recurs on every rebuild until signing is stable (see 18). |
| 14 | Engine hung (no response, 2026-11-09 14:24) | Deadlock inside bundled SQLite 3.51.1 (SQLCipher): one thread closing a WAL connection, one opening, both waiting on SQLite's file-lock mutex. Restarted 14:5x. Root cause not fixed. |
| 15 | Render/download status sits at the bottom of the project page; must scroll to see it | open |
| 16 | "Downloading originals: 0 of 10 · 0%" did not move | likely 13 and/or 14; retest after both |

## Waiting on Sam
| # | Decision | Ask |
|---|---|---|
| 17 | Free internet audio (Claude adds it, Sam browses/plays in app) | plan done (Openverse); license default: "CC0 + CC-BY" or "include NC too" |
| 18 | Stable code signing so macOS permissions survive rebuilds | "sign it" (creates a local signing cert in Keychain) |
| 19 | Delete old plaintext kioku/data/ (1.4 GB) | "delete old data" |
