# Repository Guidelines

## Project Structure & Module Organization
`main.py` is the application entry point and coordinates queueing, speech events, and playback. Core Python modules live at the repo root: `music_service.py` serves audio over HTTP, `player_control.py` sends device commands, `music_search.py` wraps search behavior, and `config.py` holds runtime settings. `music_search_core/` contains reusable indexing, models, search, and JSON storage code. `src/` contains the Rust `open_xiaoai_server` extension built with PyO3/Maturin. Deployment files include `ecosystem.config.cjs`; generated runtime data such as `cache/` and `logs/` should stay uncommitted.

## Build, Test, and Development Commands
`uv run main.py` starts the service and rebuilds the Rust extension when `src/*.rs` changes. `cargo check` is the fastest way to validate Rust changes. `cargo test` should be used for Rust unit or integration tests when present. `pm2 start ecosystem.config.cjs` runs the app in the background; `pm2 logs XiaoAiMusic` tails logs. Before runtime testing, confirm `ffprobe -version` works and update `config.py` with valid `music_dirs` and `http.base_url`.

## Coding Style & Naming Conventions
Use 4-space indentation in Python and keep existing type hints on public functions. Follow `snake_case` for functions, variables, and modules, and `PascalCase` for classes like `MusicSearcher` or `LocalMusicHttpServer`. Keep imports explicit rather than wildcard-based. In Rust, follow `rustfmt` defaults, keep modules/functions in `snake_case`, and keep PyO3 bridge code in `src/` thin and focused.

## Testing Guidelines
There is no committed Python `tests/` directory yet, so new pure-logic features should add targeted `pytest` tests under `tests/test_*.py`. Keep Rust unit tests close to the module they cover or add integration tests under `tests/`. At minimum, run `cargo check` and do a manual `uv run main.py` smoke test for changes that affect event handling, indexing, or playback.

## Commit & Pull Request Guidelines
Recent history favors short, imperative subjects, for example `pm2`, `update ignore`, and `Update README.md`. Keep commits focused on one change and avoid mixing config edits with refactors. Pull requests should state the user-visible effect, note any `config.py` or deployment changes, link related issues, and include logs or screenshots when behavior changes are observable through PM2, HTTP serving, or device playback.

## Configuration & Security Tips
Do not commit personal LAN addresses, absolute music library paths, or generated index/log files. Treat `config.py` as environment-specific and verify the XiaoAi client can reach `http.base_url` and connect on port `4399`.

## Rules
- Make the smallest change that fixes the issue.
- Always show a plan before editing.
- Before finishing: run tests (or explain why you cannot).
- Never commit secrets (.env, keys) or credentials.