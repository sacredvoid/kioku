# Safelight

A local Mac app for your Photos library: search photos and videos by what's in them, organize with your own tags, albums and video projects, and have Claude cut videos for you. Everything runs on your Mac. Photos is only ever read, never changed.

- `engine/`: Python. Indexes the library with EmbeddingGemma 2, plus hybrid search (picture + Apple's labels, people, places, your tags), tags, albums, projects, scene detection, export to Resolve (FCP XML), and an MCP server for Claude.
- `app/`: Tauri 2 + TypeScript UI.
- `skills/safelight-editor/`: Claude Code skill. Turns a prompt into a cut video project.
- `WORK.md`: progress tracker.

## Run

```sh
cd engine && uv sync
uv run index.py --since 2026-08-01      # index recent items first (resumable)
uv run status.py --watch                # progress
cd ../app && npm install && npm run tauri dev
```

Downloading iCloud videos needs Photos access for whatever app starts the engine (System Settings > Privacy & Security > Photos).

## Claude

```sh
claude mcp add -s user safelight -- uv run --directory "$PWD/engine" mcp_server.py
ln -s "$PWD/skills/safelight-editor" ~/.claude/skills/safelight-editor
```

## Privacy

`data/` holds everything personal (the index, thumbnails, video previews, audio, tags, albums) and is git-ignored. Albums and projects store pointers to Photos items, not copies.
