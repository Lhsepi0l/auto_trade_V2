# Repository Guidelines

## Project Structure & Module Organization
Core code lives in `v2/`, which is the active runtime package.
- `v2/run.py`: main entrypoint for runtime, ops actions, and control HTTP.
- `v2/config/`, `v2/engine/`, `v2/exchange/`, `v2/risk/`, `v2/tpsl/`: trading logic and state flow.
- `v2/discord_bot/`: Discord command and panel integration.
- `v2/tests/`: pytest suite for runtime, bot, ops, and exchange behavior.
- `v2/scripts/`: operational scripts (`preflight.sh`, `run_stack.sh`, `install_systemd_stack.sh`).
- `v2/docs/`: runbook and audit/checklist docs.
- Runtime artifacts: `data/` (SQLite) and `logs/` / `v2/logs/`.

## Build, Test, and Development Commands
- `python -m venv .venv && source .venv/bin/activate`: create and activate virtualenv.
- `pip install -e ".[dev]"`: install package with test/lint dependencies.
- `python -m ruff check v2 v2/tests`: lint and import-order checks.
- `python -m pytest -q`: run all tests (configured to use `v2/tests`).
- `python -m v2.run --profile normal --mode shadow --env testnet`: run locally in shadow mode.
- `python -m v2.run --deploy-prep --profile normal --mode shadow --env testnet --keep-reports 30`: run preflight plus smoke checks.

## Coding Style & Naming Conventions
Use Python 3.10+ conventions with 4-space indentation and type hints on public interfaces.
- Follow Ruff config in `pyproject.toml` (`line-length = 100`, rules `E,F,I,B,BLE`).
- Naming: `snake_case` for modules/functions, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Keep behavior config in `v2/config/config.yaml`; treat `.env` as secrets/infrastructure only.

## Testing Guidelines
Use `pytest` with test files named `test_*.py` under `v2/tests/`.
- Add or update tests with every logic change, especially for engine state, ops controls, and exchange adapters.
- Run focused tests during iteration, for example: `python -m pytest -q v2/tests/test_ops_controls.py`.
- Ensure lint and full test suite pass before opening a PR.

## Commit & Pull Request Guidelines
Recent history follows Conventional Commit style: `feat:`, `fix:`, `docs:`, `chore:`, `perf:`.
- Keep commits scoped and descriptive (one logical change per commit when possible).
- PRs should include: concise summary, linked issue (if any), risk/rollback notes, and exact validation commands run.
- For API/panel changes, include sample request/response or screenshots.
- Never commit secrets, API keys, or raw `.env` contents.

## Session Memory (2026-02)

### Recent Technical Outcomes
- Runtime stabilization work focused on reducing immediate tick contention (`tick_busy`) and transient balance fetch failures in `v2/control/api.py`.
- Tick trigger behavior was hardened with a coalescing path so user-triggered ticks can succeed by waiting for in-flight cycle completion when possible.
- Live balance retrieval now uses retry + cache fallback behavior to reduce noisy false-failure surfaces.
- Status and panel messaging were improved for operator clarity (Korean-first operational wording and clearer failure guidance).
- TP/SL bracket lifecycle is wired through v2 runtime flow (creation/recovery/cleanup), with Algo endpoint usage preserved.
- Runtime trailing profit-lock exits were added and integrated with bracket monitoring flow.
- Risk/runtime settings persistence was reinforced so panel-applied values survive restarts via runtime storage/state paths.
- Live execution now enforces Binance symbol filters before order submit (`stepSize`, `minQty`, `minNotional`) and includes Binance error code in failure reason (e.g. `live_order_failed:BinanceRESTError:-2019`).
- Live execution now pre-checks available USDT margin before market order submit and returns explicit local reason (`insufficient_available_margin:required=...,available=...`) to reduce repeated opaque exchange rejects.
- Scheduler tick interval and status notify interval were decoupled: `/scheduler/interval` updates `scheduler_tick_sec`, while `notify_interval_sec` remains independently configurable.
- TP/SL bracket algo payload now quantizes quantity/triggerPrice using exchange symbol filters to prevent `/fapi/v1/algoOrder` precision rejects (`-1111`).
- Runtime sizing control now syncs panel risk config into kernel notional sizing (`capital_mode`, `margin_budget_usdt` / `capital_usdt`, `margin_use_pct`, `max_position_notional_usdt`) instead of relying on static fallback behavior.
- Status snapshot and Discord immediate-tick response now prefer live positions and display position side + unrealized PnL for operator visibility.
- Status loop behavior was fixed so alerts continue to emit on `notify_interval_sec` cadence during RUNNING state (no silent skip while engine is active).
- Kernel `no_candidate` responses now preserve selector-provided detailed reasons instead of collapsing everything to a generic token.
- `StrategyPackV1CandidateSelector` now captures per-symbol candidate-drop reasons and exposes compact multi-symbol summaries (`no_candidate_multi:...`) for operator debugging.
- Discord immediate-tick and status messaging now localize `no_entry:*` reasons (e.g. `no_entry:donchian` -> `돈치안 진입 조건 미충족`) to reduce false bug alarms during normal wait states.
- Added focused regression coverage for reason propagation/localization paths in `v2/tests/test_strategy_selector_multi_symbol.py`, `v2/tests/test_discord_panel.py`, and `v2/tests/test_control_api.py`.
- Validation habit used in this phase: targeted `ruff` + focused `pytest` for touched modules before push.

### Collaboration Preferences (Observed)
- Default response language for this operator should be Korean.
- User prefers direct action: patch -> verify -> commit/push cadence with minimal conversational delay.
- For urgent runtime issues, prioritize root-cause fix over theoretical explanation.
- Keep explanations practical and panel/operator-centric; include what changed and how to verify quickly.
- Treat Discord panel "즉시 판단" as a live trading action in production mode (it can place real orders immediately when signal conditions are met).
- When anxious about regressions, user prefers immediate reassurance backed by quick critical-path test reruns and explicit pass/fail evidence.

### Security and Ops Notes
- `.env` remains secrets-only; runtime behavior flags stay in `v2/config/config.yaml` or runtime risk config storage.
- Never place conditional TP/SL on legacy order endpoint in v2 bracket flow; keep Algo order path.
- If credentials are pasted in chat/logs, treat them as compromised and rotate immediately; never store raw tokens in repo docs.
- For external web access to control surfaces, prefer VPN-only access (or strict reverse-proxy auth) over direct public port exposure.
- Avoid using very small scheduler intervals (e.g. 1s) in live mode except for short supervised diagnostics; rapid loops can create operational noise and burst actions.
- For live troubleshooting, keep `notify_interval_sec` and `scheduler_tick_sec` configured independently; do not assume one controls the other.
- `no_candidate` + `no_entry:*` should be interpreted as a strategy wait condition (conditions unmet), not as an execution-path failure.
- For operator sanity checks, compare immediate-tick reason and status reason together; they should match semantically even if formatting differs.

### Next Planned Direction
- Build a minimal read-first web dashboard backed by existing control/status endpoints.
- Add visual trade markers and event traceability (`decision -> execution -> fill -> Discord`) with consistent IDs for operator verification.
- Expose structured no-candidate reason counters (by symbol/reason) in status/dashboard surfaces for faster live diagnosis.

### Session References (2026-02 latest)
- Primary deep-work session: `ses_3758d5f45ffeQWNqxcFXrLc8nP` (entry suppression -> live execution rejects -> bracket precision -> status/panel fixes end-to-end).
- Earlier context-heavy thread: `ses_37e7e459affelpF6A52mP3pHeB` (baseline runtime/panel stabilization and ops workflow).
- Reason-visibility subthreads: `ses_36f1a5337ffeT74yHDcA7bqu1B`, `ses_36f1a4c5dffeyCNXkxL9t3106S`, `ses_36f0f61f9ffe5AGdfBltIV0TD7`.
