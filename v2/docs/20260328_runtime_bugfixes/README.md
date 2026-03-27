# 2026-03-28 Runtime Bugfixes

## Summary

This document records the runtime fixes completed on 2026-03-28 for two live-operation issues:

1. TP/SL brackets could disappear after entry even though the position remained open.
2. Operator-configured leverage could be silently capped, causing orders to keep entering at an older lower leverage such as `10x`.

## Issues

### 1. TP/SL disappearance after entry

Observed behavior:
- Position entry succeeded.
- One TP/SL leg could temporarily disappear from open algo-order polling.
- The runtime treated the missing leg as an immediate TP/SL fill and cleaned the bracket state too early.

Root cause:
- The bracket poller in `v2/control/api.py` assumed `one leg missing == filled`.
- Live placement in `v2/tpsl/brackets.py` was not fully atomic; if one bracket leg succeeded and the second failed, a partial/orphaned state could remain.

Fix:
- The poller now requires either:
  - confirmed flat position, or
  - recent matching fill evidence
  before it treats a single missing leg as a real TP/SL exit.
- If the position is still open, the runtime repairs TP/SL from the persisted position-management plan instead of cleaning it.
- Live bracket placement now cancels any already accepted leg and marks bracket state `CLEANED` if the second leg fails.

### 2. Symbol leverage not applied as requested

Observed behavior:
- The operator entered leverage `N`.
- Runtime accepted the setting in the UI/control layer.
- Actual execution still used a lower effective leverage, commonly the previous runtime max such as `10x`.

Root cause:
- Symbol leverage was resolved with `min(symbol_leverage, max_leverage)`.
- When `max_leverage` stayed lower than the explicit symbol leverage, the requested value was silently capped.
- Discord modal also blocked higher symbol leverage before the backend could reconcile the intended value.

Fix:
- `set_symbol_leverage(...)` in `v2/control/api.py` now treats explicit symbol leverage as operator intent.
- If requested symbol leverage is above current runtime `max_leverage`, the runtime lifts `max_leverage` to the requested value before syncing kernel overrides.
- Discord symbol leverage modal no longer pre-blocks this path and now lets the backend apply the authoritative runtime update.

## Files Changed

- `v2/control/api.py`
- `v2/tpsl/brackets.py`
- `v2/discord_bot/views/panel.py`
- `v2/tests/test_control_api.py`
- `v2/tests/test_tpsl_brackets.py`
- `v2/tests/test_discord_panel.py`
- `AGENTS.md`

## Verification

Executed during this fix:

```bash
python -m pytest -q v2/tests/test_tpsl_brackets.py v2/tests/test_control_api.py -k 'bracket'
python -m pytest -q v2/tests/test_tpsl_brackets.py
python -m pytest -q v2/tests/test_control_api.py -k 'position_management or bracket or trailing'
python -m pytest -q v2/tests/test_control_api.py -k 'control_api_contract or persists_risk_config_across_restart or restores_kernel_runtime_overrides_after_restart or symbol_leverage_lifts_runtime_max'
python -m pytest -q v2/tests/test_discord_panel.py -k 'symbol_leverage_modal'
python -m ruff check v2/control/api.py v2/tpsl/brackets.py v2/discord_bot/views/panel.py v2/tests/test_control_api.py v2/tests/test_tpsl_brackets.py v2/tests/test_discord_panel.py
```

## Operator Checks After Deploy

1. Set symbol leverage to a value above the current runtime max, for example `12`.
2. Confirm `/status` or operator panel shows leverage `12`.
3. Trigger a supervised entry and confirm the exchange-side leverage reflects the requested value.
4. After entry, confirm two bracket algo orders remain present together.
5. If one bracket leg is temporarily missing while the position is still open, confirm runtime repairs the bracket instead of treating it as a completed exit.

## Rollback Note

- If rollback is needed, revert this commit on `migration/web-operator-panel` and redeploy with `git pull --ff-only` to the prior revision.
- Rolling back removes both the TP/SL safety hardening and the leverage-intent fix, so rollback should be used only if a new regression is confirmed.
