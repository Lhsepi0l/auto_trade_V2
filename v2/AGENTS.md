# V2 Runtime Guide

## Current Posture
- `v2/` is the only active runtime surface.
- Operations are web-first through `/operator`.
- Discord runtime surfaces are removed. Do not reintroduce Discord bot/panel codepaths.
- Behavior config lives in `v2/config/config.yaml`. `.env` is secrets/infrastructure only.

## Core Map
```text
v2/
|- run.py                # entrypoint: runtime / control-http / local-backtest
|- config/               # effective config loader + profile inheritance
|- control/              # runtime controller, control API, readiness, runtime helpers
|- operator/             # operator read-models, actions, presets, form defaults
|- operator_web/         # /operator web routes + assets
|- kernel/               # candidate selection, sizing, execution composition
|- strategies/           # active strategy implementations
|- exchange/             # REST + market/user stream adapters
|- engine/               # state store + journal/reconcile flow
|- tpsl/                 # bracket planning and lifecycle
|- storage/              # sqlite schema and runtime markers
|- notify/               # ntfy/webpush notifier surfaces
|- runtime/              # boot / serve wiring
|- backtest/             # local replay, event tape, research helpers
|- docs/                 # active docs + archived history
```

## Where To Look
| Task | Location | Notes |
|---|---|---|
| Runtime boot / HTTP serve | `v2/run.py`, `v2/runtime/serve.py` | mode/env/profile wiring |
| Effective config | `v2/config/loader.py` | YAML + env merge |
| Runtime controller shell | `v2/control/api.py` | thin orchestration shell |
| Control helper boundaries | `v2/control/controller_*.py` | lifecycle / risk / status / positions / readiness |
| Shared control helpers | `v2/control/runtime_utils.py` | float/bool/pct/time helpers + async bridge alias |
| Position management contract | `v2/management/*` | shared runner lifecycle/spec |
| Kernel composition | `v2/kernel/kernel.py`, `v2/kernel/defaults.py` | decision/sizing/execution |
| Active strategy | `v2/strategies/ra_2026_alpha_v2*.py` | main live family |
| Research strategy | `v2/strategies/ebc_v1_continuation.py` | research scaffold only |
| Web operator | `v2/operator/service.py`, `v2/operator_web/router.py` | current operator surface |
| Brackets / TP-SL | `v2/tpsl/brackets.py`, `v2/control/controller_brackets.py` | algo-only lifecycle |
| Backtest research | `v2/backtest/*` | event tape, setup queue, local runner |

## Current Design Decisions
- Position management principle is explicit `tp1_runner`, not hidden partial-TP inference.
- Control internals should depend on `runtime_utils.py`, not on `v2.control.api` as a utility bag.
- Web Push and ntfy are valid notify surfaces. Discord webhook/provider is not.
- `v2/docs/archive/` holds historical notes; active guidance should stay in `v2/docs/` root.
- Generated runtime/report artifacts should not be tracked. Keep repo noise low.

## Do Not Regress
- Do not add behavior flags back into `.env`.
- Do not reintroduce Discord bot/panel/fallback paths.
- Do not put generated `.venv`, `*.egg-info`, `__pycache__`, or generated report artifacts back under tracking.
- Do not use legacy conditional-order paths for TP/SL; keep algo bracket flow.
- Do not grow `api.py` by moving helper logic back into it.

## Verify
```bash
python -m ruff check v2 v2/tests
python -m pytest -q
python -m v2.run --deploy-prep --profile ra_2026_alpha_v2_expansion_live_candidate --mode shadow --env testnet --keep-reports 30
```

## Archive Boundary
- Active docs: `v2/docs/*.md`
- Historical docs: `v2/docs/archive/**`
- Research archive: `output/research/**`
- Local-only archive bucket: `archive/**`
