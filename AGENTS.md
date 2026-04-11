# Repository Guidelines

## Project Structure & Module Organization
Active runtime code lives in `v2/`.

Top-level map:
- `v2/`: active runtime package
- `local_backtest/`: convenience shell wrappers for local replay runs
- `data/`: SQLite/runtime state
- `logs/`, `v2/logs/`: runtime logs
- `output/research/`: research/backtest archive outputs
- `archive/`: local-only archive bucket, not active runtime

`v2/` map:
- `v2/run.py`: main entrypoint for runtime, local backtest, and control HTTP
- `v2/config/`: config loader and profile inheritance
- `v2/control/`: runtime controller shell plus lifecycle/risk/status/position helpers
- `v2/kernel/`: decision, sizing, execution composition
- `v2/strategies/`: active strategy implementations
- `v2/operator/`, `v2/operator_web/`: operator read models and `/operator` web surface
- `v2/exchange/`, `v2/storage/`, `v2/tpsl/`: exchange/state/bracket plumbing
- `v2/backtest/`: replay/event-tape/research helpers
- `v2/management/`: shared position-management spec and lifecycle
- `v2/notify/`: webpush notifications
- `v2/docs/`: active docs
- `v2/docs/archive/`: historical docs only
- `v2/tests/`: pytest suite

## Build, Test, and Development Commands
- `python -m venv .venv && source .venv/bin/activate`: create and activate virtualenv
- `pip install -e ".[dev]"`: install package with dev dependencies
- `python -m ruff check v2 v2/tests`: lint/import-order checks
- `python -m pytest -q`: full test suite
- `python -m v2.run --profile ra_2026_alpha_v2_expansion_live_candidate --mode shadow --env testnet`: local shadow run
- `python -m v2.run --deploy-prep --profile ra_2026_alpha_v2_expansion_live_candidate --mode shadow --env testnet --keep-reports 30`: preflight + smoke path
- `bash local_backtest/run_local_backtest.sh`: one-command local replay wrapper

## Coding Style & Naming Conventions
Use Python 3.10+ conventions with 4-space indentation and type hints on public interfaces.
- Follow Ruff config in `pyproject.toml` (`line-length = 100`, rules `E,F,I,B,BLE`)
- Naming: `snake_case` for modules/functions, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants
- Keep behavior config in `v2/config/config.yaml`; treat `.env` as secrets/infrastructure only
- Prefer deletion over addition, and prefer existing helpers/patterns before new abstractions

## Engineering Standard
- Always write code with optimization in mind first: avoid unnecessary I/O, repeated computation, duplicate branching, and hidden work
- Never accept messy or tangled code when a clearer boundary or smaller design will solve the same problem
- Avoid hardcoding unless the value is a true domain constant that is unlikely to change; prefer config, typed contracts, and explicit helper boundaries
- Favor simple, explicit, composable code over clever patches, incidental abstractions, and “just make it work” glue
- Write as if the code will be reviewed by a 10+ year senior cryptocurrency quant trading engineer: correctness, risk awareness, runtime efficiency, and maintainability all matter at the same time
- If a change makes the code harder to reason about, keep refactoring until the structure reads cleanly

## Testing Guidelines
Use `pytest` with test files named `test_*.py` under `v2/tests/`.
- Add or update tests with every logic change, especially around control/runtime, exchange adapters, and strategy gating
- Run focused tests during iteration, then finish with full `ruff` and full `pytest -q`
- For cleanup/refactor work, lock behavior first with regression tests when the path is not already well covered

## Commit & Pull Request Guidelines
Recent history follows Conventional Commit style: `feat:`, `fix:`, `docs:`, `chore:`, `perf:`, `refactor:`.
- Keep commits scoped and descriptive
- PRs should include: concise summary, risk/rollback notes, and exact validation commands run
- For operator/API changes, include sample request/response or screenshots when useful
- Never commit secrets, API keys, raw `.env`, generated `.venv`, `*.egg-info`, `__pycache__`, or generated report artifacts

## Current Project Memory

### Runtime Posture
- `v2/` is the only active runtime surface
- Operations are web-first through `/operator`
- Discord runtime surfaces are fully removed; do not reintroduce Discord bot/panel/webhook codepaths
- Notifications are centered on `webpush`

### Current Architecture
- `v2/control/api.py` is now a thin orchestration shell, not a utility dump
- Control responsibilities are split into explicit helper modules such as lifecycle, status, readiness, risk, brackets, and position submodules
- Internal control modules should share helpers via `v2/control/runtime_utils.py`, not by importing `v2.control.api`
- Position management uses shared contracts under `v2/management/`
- The active management principle is explicit `tp1_runner`
- `v2/strategies/ra_2026_alpha_v2*.py` is the main live strategy family

### Critical Recent Outcomes
- Live runtime is aligned around one explicit management contract: `tp1_runner`
- TP leg fills must preserve and re-arm remaining runner management when position size remains open
- Normal `close_position` paths must not latch ops mode; `paused/safe_mode` latch is for real panic/flatten flows only
- Shared position-management contracts are reused by both live control and backtest/replay paths
- Web operator is the single active control surface; docs and scripts should assume web-first operation
- `v2/control/api.py` was deliberately de-spaghettified into helper modules; future work should continue pushing logic outward, not pull it back in
- Generated Python/runtime artifacts are intentionally untracked; keep worktrees and deploys clean
- Root README and `v2/README.md` are now the first-stop navigation docs for humans opening the repo cold

### Active Operational Profiles
Keep active operational profiles limited to:
- `ra_2026_alpha_v2_expansion`
- `ra_2026_alpha_v2_expansion_verified_candidate`
- `ra_2026_alpha_v2_expansion_verified_q070`
- `ra_2026_alpha_v2_expansion_candidate`
- `ra_2026_alpha_v2_expansion_live_candidate`
- `ra_2026_alpha_v2_expansion_champion_candidate`

### Design Decisions To Preserve
- Keep the repository in the current alpha-only final state around `ra_2026_alpha_v2`
- Do not reintroduce removed legacy/operator surfaces such as `v2/clean_room`, `v2/web_panel`, nested `v2/v2`, or Discord runtime paths
- Do not move helper logic back into `v2/control/api.py`
- Do not add behavior flags back into `.env`
- Do not use legacy conditional-order paths for TP/SL; keep algo bracket flow
- Treat `v2/docs/archive/**` and `output/research/**` as archive/research only, not active runtime surfaces

### Collaboration Preferences
- Default response language is Korean
- User prefers direct action: patch -> verify -> summarize
- User expects optimization-conscious changes by default
- User prefers blunt yes/no conclusions first during runtime incidents
- User wants exact commands/paths instead of abstract guidance
- User expects AGENTS/session memory to be refreshed when the operating reality changes materially

### Git / Deploy Workflow
- Git remote standard is GitHub `origin`
- Working branch is `migration/web-operator-panel`
- Stable reference branch is `main`
- Server is pull-only: do not edit or commit directly on the server
- Deploy by pushing to GitHub, then using `git pull --ff-only` on the server
- Production deploy tags are preferred, for example `prod-YYYYMMDD-a`

### Security & Ops Notes
- Keep control HTTP non-public; prefer localhost bind plus VPN/SSH tunnel/reverse-proxy auth
- If credentials are exposed in chat/logs, rotate them immediately
- Treat `.env` as secrets/infrastructure only
- Keep `notify_interval_sec` and `scheduler_tick_sec` conceptually separate
- Interpret `no_candidate` / `no_entry:*` as strategy wait states, not execution failures
- Current kill behavior is operator-invoked (`panic`/flatten); do not assume automatic account kill-switch enforcement unless explicitly implemented
- Keep post-restart checks simple: `readyz`, runtime status, and control bind on `127.0.0.1:8101`

### Local Backtest / Research Notes
- Heavy historical replay belongs on the workstation, not the production Pi/server
- `local_backtest/` is the quick launcher surface; `v2/backtest/` is the underlying implementation surface
- Research outputs should stay local under `output/research/**` and must not quietly become active runtime dependencies

### Verification Habit
For substantive changes, finish with:
```bash
python -m ruff check v2 v2/tests
python -m pytest -q
```

For runtime/deploy changes, also use:
```bash
python -m v2.run --deploy-prep --profile ra_2026_alpha_v2_expansion_live_candidate --mode shadow --env testnet --keep-reports 30
```

### Archive Policy
- Active docs: `v2/docs/*.md`
- Historical docs: `v2/docs/archive/**`
- Research outputs: `output/research/**`
- Local-only archive bucket: `archive/**`

Detailed historical session logs were intentionally removed from this file.
Use git history and archived docs/reports when old incident context is truly needed.
