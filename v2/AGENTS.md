# V2 RUNTIME GUIDE

## OVERVIEW
`v2/` is the new runtime architecture: config SSOT, exchange adapters, state/journal, TP/SL brackets, and ops controls.

## STRUCTURE
```text
v2/
|- run.py       # CLI entry + ops actions/http
|- config/      # YAML loader and profile inheritance
|- exchange/    # REST + market/user stream adapters
|- engine/      # state store + journal replay
|- ops/         # pause/resume/safe/flatten controls
|- tpsl/        # bracket planning and lifecycle
|- storage/     # runtime sqlite schema access
```

## WHERE TO LOOK
| Task | Location | Notes |
|---|---|---|
| Boot/profile/env wiring | `v2/run.py` | mode/env/profile and ops CLI |
| Effective config | `v2/config/loader.py` | profile inheritance + validation |
| Exchange API integration | `v2/exchange/rest_client.py` | signed requests, open/algo ops |
| User stream resilience | `v2/exchange/user_ws.py` | keepalive + reconnect + reorder |
| State SSOT and replay | `v2/engine/state.py` | journal idempotency + reconcile |
| Bracket TP/SL | `v2/tpsl/brackets.py` | algo-only TP/SL lifecycle |
| Ops controls | `v2/ops/control.py` | flatten verification sequence |

## CONVENTIONS
- `.env` is secrets-only for v2; behavior config must come from `v2/config/config.yaml`.
- Shadow mode must run without keys; live mode requires keys.
- Keep state transitions journaled/idempotent (`EngineStateStore` + `JournalWriter`).
- Use Algo endpoints for conditional TP/SL in v2 flow.

## ANTI-PATTERNS
- Do not put runtime behavior flags back into `.env` for v2.
- Do not send conditional TP/SL on legacy order endpoint in v2 bracket flow.
- Do not skip final verification step in flatten (`position==0`, no regular/algo open orders).

## VERIFY
```bash
python -m pytest -q tests/v2
python -m ruff check v2 tests/v2
python -m v2.run --mode shadow
python -m v2.run --mode shadow --ops-action flatten --ops-symbol BTCUSDT
```
