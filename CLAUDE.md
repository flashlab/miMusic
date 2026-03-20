# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

XiaoAi Music (小爱音箱免费播放本地歌曲) - A local music server for XiaoAi smart speakers that enables playing local music files without requiring Xiaomi account login. The project integrates with flashed XiaoAi speakers via the open-xiaoai client.

## Architecture

### Hybrid Python/Rust Implementation

- **Python Layer**: Main application logic, music search, HTTP server, and player control
- **Rust Layer**: WebSocket server (`open_xiaoai_server`) compiled as a Python extension module via PyO3/Maturin
  - Handles real-time bidirectional communication with XiaoAi speaker client
  - Provides async Python bindings for WebSocket events and shell command execution

### Core Components

1. **main.py**: Application entry point and orchestration
   - Event-driven architecture listening to XiaoAi speaker events via WebSocket
   - Manages play queue, song timers, and automatic track progression
   - Implements reply interrupt mechanism to prevent XiaoAi from speaking over music commands
   - Handles voice commands: play, stop, refresh index, random play
   - Supports interrupt whitelist (e.g., volume commands don't clear queue)

2. **music_search_core/**: Music indexing and search engine
   - `indexer.py`: Scans directories, extracts metadata via ffprobe, caches by mtime/size
   - `search_engine.py`: Keyword matching across title/artist/album/filename
   - `store.py`: Persists index to JSON for fast startup
   - `models.py`: Data structures for indexed songs

3. **music_service.py**: HTTP file server
   - Serves local audio files to XiaoAi speaker over LAN
   - Supports HTTP Range requests for seeking
   - Hex-encodes file paths in URLs for security

4. **player_control.py**: XiaoAi device control via shell commands
   - `speak_text()`: TTS playback
   - `play_music_url()`: Stream audio from URL
   - `stop_playback()`: Pause current playback

5. **open_xiaoai_server (Rust)**: WebSocket server in `src/`
   - Connects to XiaoAi client on port 4399
   - Exposes Python API: `register_fn()`, `start_server()`, `run_shell()`

### Key Mechanisms

- **Reply Interrupt System**: When user issues a voice command, the system arms a time window to intercept and stop XiaoAi's default responses, preventing them from interfering with music playback
- **Whitelist Auto-Resume**: Volume/sound commands don't clear the queue; playback auto-resumes after configurable delay
- **Queue Management**: Async lock-protected queue with automatic timer-based track progression
- **Index Caching**: Reuses metadata for unchanged files (by size/mtime) to speed up refresh

## Development Commands

### Prerequisites

- **ffprobe** (required): Used to extract audio metadata and duration
  ```bash
  # macOS
  brew install ffmpeg

  # Ubuntu/Debian
  sudo apt install -y ffmpeg
  ```

- **uv**: Python package manager (handles Rust compilation automatically)

### Running

```bash
# Development run
uv run main.py

# Background with PM2 (recommended)
mkdir -p logs
pm2 start ecosystem.config.cjs
pm2 logs XiaoAiMusic
pm2 restart XiaoAiMusic
pm2 stop XiaoAiMusic

# Background with nohup
mkdir -p logs
nohup uv run main.py > logs/app.log 2>&1 &
echo $! > logs/app.pid
```

### Configuration

Edit `config.py` before running:
- `music_dirs`: Local music directories (required)
- `http.base_url`: Server URL accessible to XiaoAi speaker (e.g., `http://192.168.11.18:18080`)
- `search.max_results`: Playlist size (default 20)
- `search.refresh_interval_sec`: Auto-refresh interval (0 = disabled)
- `commands.*_keywords`: Voice command triggers
- `commands.interrupt_whitelist_keywords`: Commands that don't clear queue
- `commands.auto_resume_delay_sec`: Delay before resuming after whitelist command

### Building Rust Extension

The Rust extension is automatically compiled by `uv` when running the project. Manual rebuild:

```bash
# uv handles maturin build automatically
uv run main.py  # triggers rebuild if Rust files changed
```

## Code Patterns

### Async/Threading Model

- Main event loop runs in asyncio
- Music indexing runs in thread pool (CPU-bound)
- HTTP server runs in daemon threads
- Rust WebSocket server bridges to Python asyncio via `pyo3-async-runtimes`

### Locking Strategy

- `local_music_lock`: Protects play queue and current song state
- `index_refresh_lock`: Prevents concurrent index rebuilds
- `reply_interrupt_lock`: Serializes reply interrupt operations

### Voice Command Flow

1. User speaks → XiaoAi client sends ASR event via WebSocket
2. `on_event()` parses event, extracts text
3. Command detection: stop/play/refresh/random
4. For play commands: arm reply interrupt, search index, build queue, start playback
5. Reply interrupt window: if XiaoAi tries to speak, immediately stop it

## Important Notes

- The project requires a flashed XiaoAi speaker running the open-xiaoai client connected to port 4399
- All file paths use forward slashes (Unix-style) even on Windows due to bash shell
- The HTTP server must be accessible from the XiaoAi speaker's network
- Index file is saved to `cache/music_index.json` by default
- Supported audio formats: mp3, flac, wav, m4a, aac, ogg

## Collaboration rules
- Explain first, then edit.
- Prefer small, reviewable diffs.