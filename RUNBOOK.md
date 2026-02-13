# Auto-Trader Runbook

## Before Live
1. Confirm `TRADING_DRY_RUN=false` and `DRY_RUN_STRICT=false` in runtime env.
2. Confirm Binance key permissions:
   - USD-M Futures enabled
   - Read + Trade allowed
   - IP whitelist configured (recommended)
3. Confirm risk/budget config is explicitly set (do not rely on defaults):
   - `capital_mode`, `capital_pct` or `capital_usdt`/`margin_budget_usdt`
   - `margin_use_pct`, `max_position_notional_usdt`, `max_exposure_pct`
   - `max_leverage`, `daily_loss_limit_pct`, `dd_limit_pct`
4. Call `/status` and verify:
   - `engine_state.state` is `STOPPED` before final checks
   - `safe_mode=false`
   - `ws_connected=true` and recent `last_ws_event_ts`
   - `capital_snapshot.blocked=false` for target symbol
5. Start with minimal size and monitor first full cycle.

## If Stuck
1. Check `/status`:
   - `safe_mode`
   - `last_error`
   - `last_ws_event_ts`
   - `capital_snapshot.block_reason`
2. If `safe_mode=true`:
   - verify network/WS path
   - wait for reconnect + reconcile
   - entries are expected to be blocked until healthy
3. Force reconcile path:
   - restart engine process (startup reconcile runs before normal loop)
4. Emergency flatten:
   - call `/panic` (idempotent)
   - verify position = flat and open orders = none

## If WS Down (Expected Behavior)
1. After disconnect threshold, engine enters safe mode automatically.
2. New entries are blocked (`ws_down_safe_mode`).
3. Close actions remain allowed:
   - `/trade/close`
   - `/trade/close_all`
   - `/panic`
4. On reconnect success:
   - reconcile runs
   - safe mode clears automatically

## Quick Operational Checks
1. `/health` returns `{"ok": true}`.
2. `/status` includes:
   - `ws_connected`
   - `listenKey_last_keepalive_ts`
   - `last_ws_event_ts`
   - `safe_mode`
3. `order_records` has no duplicate `client_order_id`.
