# V2 ENGINE GUIDE

## OVERVIEW
`v2/engine` owns runtime state, journal writes, reconciliation apply, and replay behavior.

## WHERE TO LOOK
| Concern | File |
|---|---|
| State model + event apply | `v2/engine/state.py` |
| Journal write helper | `v2/engine/journal.py` |
| Thin managers | `v2/engine/order_manager.py`, `v2/engine/position_manager.py` |

## CONVENTIONS
- Keep event IDs deterministic; dedupe must prevent duplicate order/fill persistence.
- Keep reconcile and WS event paths behaviorally aligned.
- Persist ops mode through `apply_ops_mode`; do not mutate operational state ad hoc.
- Replay must reconstruct state without writing duplicate storage rows.

## ANTI-PATTERNS
- Do not bypass journal write on state mutation paths.
- Do not introduce side effects in replay mode (`persist_storage=False` paths).
- Do not coerce malformed WS payloads into silent success; keep validation explicit.

## VERIFY
```bash
python -m pytest -q tests/v2/test_engine_state_ssot.py
python -m ruff check v2/engine
```
