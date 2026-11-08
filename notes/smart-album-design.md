# Smart album maker: design and plan

Ask: "like i describe what photos i want in a particular album, it searches and presents a review screen where i can easily remove photos. and if i think somethings are missing i can ask it to search specifically again."

Status: design only, no code written. Date 2026-11-08.

## 0. Recommendation in one paragraph

Build a **draft** (not an album yet) that lives in two new tables. A short, tool-less Claude call (Sonnet by default, setting) turns the description into 4 to 8 search queries. The **engine runs those searches in-process** (free, ~0.4 s each), fuses them, and stores candidates with the query that found them. The user reviews a plain big grid: one click or key X removes or restores, removed items are tombstoned so "Search again" never brings them back. "Search again for ..." runs the same pipeline with the new instruction and marks new tiles. Save turns the kept items into a normal album (or, for "refine existing album", writes the diff back). Checking pictures with `look_at` is a **phase 2, opt-in, capped** step that only looks at borderline hits and only marks them removed (restorable), never deletes. Almost all new code lives in `engine/albums.py`, `engine/test_albums.py`, `app/src/review.ts`, `app/src/review.css`.

## 1. Who turns the description into results

Verified against the running engine (read-only curl):

- `/search?q=beach photos march 2024&k=300` takes 0.37 s and returns 300 hits, 229 of them keyword hits. `/search?q=photos&k=300` returns 155 keyword hits. Cause: every `meta` string contains the literal token `photo` (see `Beach ; Cloudy ; ... ; photo ; Myrtle Beach, ... ; March 2024`), and FTS uses porter stemming, so "photos" matches a large share of the library. So a sentence typed straight into search is noisy: `fts_query` ORs every non-stopword.
- Meta vocabulary is Apple labels + place string + "Month YYYY". A good query is a few concrete words in that vocabulary. A sentence like "my trip to Japan last spring" needs translating to "Tokyo", "Kyoto", "April 2025", "Temple", "Cherry Blossom".

Options:

| Option | Cost | Quality | Verdict |
|---|---|---|---|
| A. Plain `search(description)` | free, 0.4 s | noisy on compound or date phrasing, "photo" junk | Only as fallback |
| B. Full Claude agent (search_media + look_at over everything, like Ask) | minutes, many tokens; hundreds of thumbnails | best precision | No: expensive, slow, wrong tool for "I'll review it myself" |
| C. Claude plans queries only, engine runs them | one ~2k token call (~3 to 8 s on Sonnet, less on Haiku), searches free | good recall, fixes vocabulary and dates | **Default** |
| D. C plus Claude looks at borderline hits only | + up to ~36 thumbnails per round, 30 to 60 s | cuts obvious junk before the user sees it | Phase 2, opt-in checkbox |

Why C is the lazy-correct core: the human review screen is the precision step the user asked for. Claude's job is recall and translation, which needs no images and no MCP round trips. Running searches in-process also avoids 8 tool-call turns (each turn re-sends context).

Planner details:
- `claude -p` with `--tools ""`, **no MCP config**, `--output-format json`, prompt asks for JSON only: `{"name": "...", "queries": [{"q": "...", "kind": ""|"image"|"video", "why": "short label"}]}`.
- Prompt carries: today's date (so "last summer" resolves), 3 random sample `meta` strings from the library (teaches the vocabulary), and rules: concrete words, no "photo/photos/picture/video/image" (they match everything), one idea per query, include place names and "Month YYYY" forms when dates are implied, 4 to 8 queries, plus for search-again: the original description, the new instruction, and queries already used ("do not repeat").
- Parse with pydantic; on bad JSON retry once with the error text; on second failure or missing `claude`, fall back to A (single query = description with generic words stripped) and tell the user in the draft's status line ("Claude unavailable, used plain search").
- Optional small core fix (separate, recommended): add `photo`, `photos`, `picture`, `pictures`, `image`, `images` to `STOP` in `core.fts_query`. It helps normal search too. This touches `core.py` (one line). Not required for this feature because the planner is told to avoid them; the fallback strips them itself.
- Model: new setting `album_model`, default `sonnet` (date and place reasoning matters; cost is tiny: one short call per round). Haiku selectable. Not gated by `agent_slot` (see 4).

Fusion in the engine (no model involved):
- For each query call `Index.search(q, k=60, kind)` (floor logic already drops weak visual-only hits).
- Per uuid keep the best query (highest per-query rank score) as `query`, count how many queries hit it (`hits`), score = max RRF + 0.25 * (hits - 1) * max RRF. Sort desc. Cap 150 new items per round, 400 per draft. Skip uuids already in the draft in any state (kept or removed).

Phase 2 verify (opt-in, "Double-check doubtful ones" checkbox, off by default):
- Borderline = found by exactly one query, `keyword` false (visual-only), and in the lower half of the round by score. Cap 36 per round.
- One `claude -p` with `--allowedTools mcp__safelight__look_at`, `--tools ""`, `--strict-mcp-config` (same flags as `run_claude`), Sonnet. Prompt: description + list of borderline uuids, "call look_at in batches of 6, in parallel, return JSON `{"drop": [{"uuid": "...", "reason": "..."}]}` for pictures that clearly do not match; keep anything uncertain". Takes `agent_slot` because it is heavy.
- Dropped items get `state='removed', by='claude', note=reason`: shown under "Show removed" with the reason, one click restores. Never deleted, so a wrong call costs the user one click.
- Cost bound: 36 images ~ one medium turn; it is a button the user opts into per round, not automatic.

## 2. Draft state

Reuse **collections?** No. `collections.kind` has a CHECK constraint (`album` or `project`) in `core.SCHEMA`, and altering a CHECK in SQLite means a table rebuild. The sidebar and `syncCollections` would also show drafts as albums. Use separate tables created by `albums.py` itself (`create table if not exists` run through `library.conn()` on first use, so **`core.py` stays untouched**).

```
album_drafts(id integer primary key, name text not null, description text not null,
             album_id integer references collections(id) on delete set null,   -- set when refining an existing album
             status text not null default 'open' check(status in ('open','saved')),
             round integer not null default 0, created real, updated real)

draft_items(draft_id integer references album_drafts(id) on delete cascade, uuid text not null,
            state text not null default 'in' check(state in ('in','removed')),
            by text not null default 'search' check(by in ('search','user','claude')),
            query text, why text, note text, round integer not null, score real, hits integer,
            primary key(draft_id, uuid))

draft_runs(id integer primary key, draft_id integer references album_drafts(id) on delete cascade,
           round integer, prompt text, model text, queries text,   -- JSON
           status text, error text, added integer, started real, finished real)
```

- Removed items are rows with `state='removed'`: that is the tombstone. The search-again insert is `insert or ignore`, so a removed uuid can never be re-added, and a user restoring one flips state back.
- Why-it-matched: `query` (the query string that scored best) and `why` (the planner's short label, e.g. "Cherry blossoms"). Shown on the tile.
- "New" = `round == draft.round and round > 1` (the badge disappears when the next round starts).
- Existing-album refine: draft created with `album_id`; seeded with current album items as `state='in', round=0, why='Already in album'`. Removing one tombstones it. Save reconciles: insert kept uuids not in the album, delete album items whose uuid is `removed`. Same tables, same endpoints, same screen.
- Job state is in memory (like `projects.jobs`), keyed by draft id. A draft row with a run `status='running'` but no live job after an engine restart is reported as `failed: "Stopped because the engine restarted"` on the next GET and the run row is closed. Candidates already stored are kept.
- Save is guarded: only `status='open'` drafts, in one transaction (create album, insert items ordered by `assets.created`, mark draft `saved`). A second Save returns the existing album id (no duplicate). After save the draft rows are deleted (the album is the artifact). Discard = hard delete rows, photos untouched, confirm inline.

## 3. Review screen UX

New view `{type: "draft", id}` rendered by `app/src/review.ts` into a new `<div id="review">` beside `#project`. Styled with CSS variables from the existing pastel themes (`--surface`, `--accent`, `--accent-fill`, `--danger`, `--line`) in `app/src/review.css`, imported by review.ts (so `styles.css` is untouched). Reuses classes `.tile`, `.dur`, `button.primary`, `button.ghost`, `.confirm`, `.ask-out`.

Entry points:
- Sidebar Albums header gets a second small button (sparkle) "Smart album". Click opens an inline textarea ("Describe the photos you want, e.g. dinners with Priya in 2025"), Enter creates the draft and navigates to it.
- New sidebar group "Drafts" (only when any open draft exists), each with kept count and a spinner dot while searching.
- On an album page, header button "Refine with search" (creates a draft with `album_id`).

Layout (top to bottom):
1. Title (editable, prefilled from planner `name`), pill "Smart album draft", description line (muted).
2. Status line: `118 kept · 9 removed · 14 new` and, while running, the step text ("Planning searches", "Searching “kyoto temple” (+12)", "Checking 30 doubtful photos").
3. Search-again row: input "Missing something? e.g. also the night photos, or include Osaka" + button "Search again". Checkbox "Double-check doubtful ones" (phase 2).
4. Buttons right: **Save as album** (primary, opens the name field inline, prefilled), **Discard** (ghost danger, inline confirm).
5. Grid: a plain CSS grid of up to 400 tiles (no virtualization; `img loading="lazy"` plus the existing `/thumb` endpoint; this avoids touching `grid.ts` whose click and selection model is hardwired to open or select).

Tile behavior:
- Click toggles removed/kept. Removed = dimmed, grayscale, a visible "x" badge, stays in place (no layout jump), so a mistaken click is one click to undo.
- Magnifier button in the corner, or Space, opens the existing `Viewer` (review.ts gets an `openViewer(items, i)` callback from main.ts; viewer shows full image and info).
- Bottom caption on hover and keyboard focus shows `why` (and the query on tooltip); key `W` pins captions on for all. New tiles get a small "New" dot in the accent color.
- Sorted by relevance (best first) with weakest at the end, which is where junk collects; a "By date" toggle reorders client-side. Save writes albums in date order.

Keyboard (only while the review view is active and focus is not in an input):
- Arrows move a visible focus ring over tiles (roving tabindex), Home/End.
- `X`, Delete, or Backspace toggles the focused tile and advances to the next kept tile. `Space` opens viewer. `U` undo last toggle (stack in memory). `R` toggles "Show removed only". `W` captions. `/` focuses the search-again input. `Cmd+Enter` Save. `Esc` leaves input focus.
- Shift+click a range to toggle a run at once (same as grid semantics); bulk send is one PATCH.

Persistence: each toggle is optimistic locally and sent as `PATCH /drafts/{id}/items`. If it fails the tile reverts and a toast shows (existing `toast`/`fail` helpers).

Accessibility: tiles are `<button aria-pressed>`, removed state also exposed in the label ("removed"), the status line is `aria-live="polite"`, focus ring uses the existing focus style.

Empty and error states: zero results ("Nothing matched. Try describing it differently."), planner fallback notice, engine unreachable (same wording as `showError`).

## 4. Engine endpoints, MCP, job, prompt, settings

New file `engine/albums.py` with `router`, included from `server.py`.

| Method and path | Purpose |
|---|---|
| `POST /drafts` `{description, album_id?}` | create draft, start round 1, 202 with draft |
| `GET /drafts` | open drafts: id, name, description, kept, status, updated, album_id |
| `GET /drafts/{id}` | draft + items (uuid, state, by, query, why, note, round, score, kind, duration, created, meta) + job snapshot + last runs |
| `GET /drafts/{id}/job` | light status for polling (status, step, note, added, error) |
| `POST /drafts/{id}/search` `{prompt, verify?}` | start next round; 409 if one is running |
| `PATCH /drafts/{id}/items` `{uuids, state}` | bulk keep or remove (user toggles) |
| `PATCH /drafts/{id}` `{name}` | rename |
| `POST /drafts/{id}/save` `{name?}` | create album (or reconcile onto `album_id`), returns `{collection_id}` |
| `DELETE /drafts/{id}` | discard |

Job runner: a ~15 line local copy of the `start_job` idea (dict keyed by draft id, lock, thread, same "failed with readable message" handling) inside `albums.py`. We do not import `projects.start_job`, because it is keyed `(collection id, kind)` and saves `ask_runs` rows for kind "ask", and `projects.py` is about to change.

Slots: the planner is one short process and is **not** queued behind `agent_slot` (a 30 minute video edit holding both slots would otherwise freeze a 5 s planner). Only the phase 2 verify call takes a slot. This means `albums.py` imports `projects.agent_slot`, `projects.release_slot`, `projects.claude_bin`, `projects.mcp_config` (phase 2 only for the last three). **Coupling to flag to the other team:** keep those four names stable, or tell us when they move.

Search access: `albums.py` must not import `server` (circular). `server.py` sets `albums.search = lambda q, k, kind: fresh_index().search(q, k, kind)` once at startup. Tests assign a fake.

MCP: **no changes needed for the app flow** (the planner uses no tools; the verify call uses the existing `look_at`). Optional later for Claude Code users: `create_collection` + `add_to_collection` already let Claude Code build an album by hand; a `create_album_draft` tool is not worth it now. This keeps `mcp_server.py` off our file list.

System prompt: a new section constant in `albums.py` (about 25 lines), not a SKILL.md. The skill file is the video editing workflow, shared with the in-app editor, and a different purpose; a second skill would only be useful if Claude Code users want to say "make me an album of X" from the terminal, in which case existing `search_media` + `create_collection(kind="album")` + `add_to_collection` already cover it. Defer.

Settings: add `album_model: Literal["opus","sonnet","haiku"] = "sonnet"` to `Settings` in `settings.py` (one line; the Settings dialog is schema driven and falls back to a generated label, so no `main.ts` edit needed, though a label entry in `LABELS` is a nice touch). `ask_minutes` is reused as the timeout for planner and verify processes (kill timer, same pattern as `run_claude`). `max_agents` is reused for verify only. `test_settings.py` PUTs a full settings body: because the new field has a default, existing tests keep passing.

Logging: `logging.getLogger("safelight.albums")`, one line per round (queries, counts, model, seconds), no photo content.

## 5. Test plan

`engine/test_albums.py`, same style as `test_library.py`: `os.environ["CS_DB"]` to a temp db, `FastAPI()` + `include_router(albums.router)` + `TestClient`, plain asserts, no network, no real `claude`.

Fakes:
- `albums.search = fake_search` returning canned hit dicts per query (including overlapping uuids across queries, a keyword-only hit, a visual-only hit).
- `albums.plan = fake_plan` (the planner function is a module-level callable so it can be swapped) returning fixed queries, or raising to test fallback, or returning bad JSON via a fake subprocess to test the parse-and-retry path (parse function tested directly with strings).
- Jobs run in a thread: tests wait on `GET /drafts/{id}/job` until not running (bounded loop).

Asserts:
1. Create draft: 202; after job, N items `state='in'`, each has `query` and `why`; fused ordering puts a multi-query hit above a single-query one; duplicates across queries stored once.
2. Remove then search again: PATCH remove A; run search that returns A again plus new B; A stays removed, B is added with `round == 2`, A's `by` stays `user`.
3. Restore: PATCH A to `in`; A kept; counts correct.
4. 409 when a job is running (fake search blocks on an Event).
5. Planner failure: fake plan raises; draft still gets plain-search fallback results and a status note; planner bad JSON retried once then fallback.
6. Caps: 150 new per round and 400 per draft enforced.
7. Save new album: album exists with only `in` items, ordered by `assets.created`; draft marked saved/removed; second Save returns same album id, no duplicate items; `removed` never present. Photos table untouched (row count and meta unchanged).
8. Refine existing album: draft seeded with album items; remove one, add one via search; Save: album has the diff applied (removed gone, new present, others keep their `pos`).
9. Restart simulation: insert a `draft_runs` row `running`, clear in-memory jobs, GET returns `failed` with the restart message and closes the run.
10. Validation: empty description 422, unknown uuid in PATCH ignored or 422 (choose 422), unknown draft 404, state value invalid 422, `album_id` of a project collection 422.
11. Verify (phase 2) with fake verify callable: returned drops become `removed, by='claude', note=reason`, uuids not in the request are ignored, restore works.

Frontend: no test framework exists in `app/`; manual check list in the PR (tab through tiles, X, U, search again marks new, save, discard) and one Playwright pass later if wanted. Run `npm run build` (tsc) to catch type errors.

## 6. File ownership and collisions

Another team is touching `engine/render.py` (new), `engine/projects.py` (render/export area), `engine/mcp_server.py`, `app/src/main.ts` (project panel).

New files (zero conflict):
- `engine/albums.py`
- `engine/test_albums.py`
- `app/src/review.ts`
- `app/src/review.css`

Shared files we must touch, minimal and located:

| File | Edit | Where | Conflict risk |
|---|---|---|---|
| `engine/server.py` | `import albums`, `app.include_router(albums.router)`, `albums.search = ...` | next to the other `include_router` lines (~137 to 140) | low, other team unlikely |
| `engine/settings.py` | add `album_model` field | `Settings` class | low |
| `app/src/main.ts` | (a) extend `View` union with `{type:"draft"; id:number}` (line ~28); (b) in `go()` add a `draft` branch that hides `#grid` and `#project` and calls `review.show(id)`, ~10 lines (lines ~114 to 160); (c) call `review.loadDrafts()` from `syncCollections` and `go` for the sidebar group; (d) `renderHead`: "Refine with search" button for albums (~lines 174 to 197); (e) `markNav` key for drafts; (f) `document` keydown early-return when review view active (or review.ts adds its own listener and main's `Cmd+A`/`Esc` handlers ignore draft view) | Medium: `main.ts` is shared, but our hunks are in `go`, `renderHead`, the `View` type and the keydown block, none in the project panel (`renderProject`, `clipRow`, `pollJob`, `renderRuns`, lines ~211 to 540) |
| `app/src/index.html` | `<div id="review" hidden>`, a Smart album button in the Albums group head (line ~21), a Drafts group | sidebar and main | low |
| `app/src/api.ts` | `Draft`, `DraftItem` types only (append) | end of file | low (append only). Could instead put types in review.ts and skip this file |
| `core.py` | optional STOP tweak only | `STOP` | none, optional |

Not touched: `engine/projects.py`, `engine/mcp_server.py`, `engine/render.py`, `engine/library.py`, `engine/core.py` (schema), `grid.ts`, `viewer.ts`, `styles.css`, `skills/`.

Tell the other team: keep `projects.agent_slot`, `release_slot`, `claude_bin`, `mcp_config` names, and avoid reformatting `go()` and `renderHead` in `main.ts`. To cut `main.ts` conflicts further, we can expose a single `registerView(type, show)` hook later; not worth it now.

Merge order suggestion: land the `main.ts` and `index.html` hooks as one small commit early.

## 7. Risks, open questions, estimate

Risks:
- Planner quality on vague descriptions ("good photos of the kids"): recall is bounded by Apple labels and CLIP; the review screen and search-again are the safety net. Mitigation: show the queries it ran (tooltip on status line) so the user can say "also X".
- 400 lazy `<img>` thumbnails: `/thumb` makes thumbs on demand (issue #3) so a long scroll triggers generation; fine for <=400 locally, and the cap keeps it bounded.
- Click-to-remove accident rate: mitigated by in-place dimming, `U` undo, and Save showing the kept count.
- `claude -p` JSON compliance: handled with parse, one retry, plain-search fallback. No `--json-schema` assumed because its availability across CLI versions is unverified.
- iCloud-only items (54k of 58k): thumbs and search work (proxies/posters already indexed); only verify and viewer video play may need downloads. Verify uses stills (`look_at`) so no iCloud video fetch.
- Index staleness: `fresh_index()` reloads every 30 s while indexing (issue #2 blocks a search briefly); a round's 8 searches may each wait on a reload. Acceptable.
- Thread safety: `Index.search` is already used concurrently by requests; calling it from the job thread is the same pattern.
- Coupling to `projects.agent_slot` (see 4).

Open questions for the user:
1. Should Save order be date ascending (my default) or relevance order?
2. Should "Double-check doubtful ones" default on for the first round after seeing how long it takes in practice?
3. Cap sizes (150 per round, 400 per draft) acceptable? A big album like "all of 2025" would hit the cap; maybe offer "Load more" later.
4. Allow videos in smart albums by default (search returns posters/scenes)? Default yes, planner may set `kind` per query; the review tile shows the duration badge.
5. Keep saved-draft history? Default no.

Estimate (one focused dev, includes tests, excludes review time):
- Engine `albums.py` + tests (phase 1, no verify): 0.75 to 1 day.
- `review.ts` + `review.css` + main/index hooks: 1 to 1.25 days.
- Refine existing album: 0.25 day (seeding + reconcile + header button).
- Phase 2 verify with `look_at`: 0.5 day plus a tuning pass on real photos.
- Total: about 2.5 days phase 1 + refine, 3 days with verify. Smallest shippable slice: phase 1 only (about 2 days), which already satisfies the whole ask.
