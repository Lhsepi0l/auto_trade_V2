from __future__ import annotations

from typing import Any

from v2.control.controller_events import dispatch_webpush_notification
from v2.control.mutating_responses import (
    build_trade_close_all_response,
    build_trade_close_response,
)
from v2.control.runtime_utils import to_float, utcnow_iso
from v2.notify.runtime_events import build_position_close_notification


async def close_position(
    controller: Any,
    *,
    symbol: str,
    notify_reason: str = "forced_close",
) -> dict[str, Any]:
    position_state = controller.state_store.get().current_position.get(symbol.upper())
    close_qty = abs(to_float(getattr(position_state, "position_amt", 0.0), default=0.0))
    controller._log_event("flatten_requested", action="close_position", symbol=symbol.upper())
    result = await controller.ops.flatten(symbol=symbol, latch_ops_mode=False)
    notification = build_position_close_notification(
        symbol=result.symbol,
        reason=notify_reason,
        context=controller._notification_context(),
    )
    _ = controller.notifier.send_notification(notification)
    dispatch_webpush_notification(controller, notification)
    controller._log_event(
        "position_closed",
        symbol=result.symbol,
        reason=notify_reason,
        closed_qty=round(float(close_qty), 8),
        realized_pnl=controller._resolve_symbol_realized_pnl(symbol=result.symbol),
        event_time=utcnow_iso(),
    )
    controller._report_stats["closes"] += 1
    return build_trade_close_response(flatten_result=result)


async def close_all(controller: Any, *, notify_reason: str = "forced_close") -> dict[str, Any]:
    controller._log_event("flatten_requested", action="close_all")
    symbols: set[str] = set()
    live_positions, _live_rows, live_ok, _live_error = controller._fetch_live_positions()
    if live_ok:
        for sym, position_amt in live_positions.items():
            if abs(to_float(position_amt, default=0.0)) <= 0.0:
                continue
            symbols.add(str(sym).strip().upper())
    else:
        symbols = set(
            controller._risk.get("universe_symbols") or [controller.cfg.behavior.exchange.default_symbol]
        )
        for sym in controller.state_store.get().current_position.keys():
            symbols.add(sym)
    details: list[dict[str, Any]] = []
    for symbol in sorted({str(s).upper() for s in symbols if str(s).strip()}):
        details.append(await close_position(controller, symbol=symbol, notify_reason=notify_reason))
    return build_trade_close_all_response(results=details)
