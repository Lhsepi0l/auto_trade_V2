from __future__ import annotations

from typing import Any, Literal, cast

from v2.control.controller_position_state import clear_position_management_state
from v2.control.position_management_runtime import mark_exit_requested
from v2.control.runtime_utils import run_async_blocking, to_float, utcnow_iso


def reduce_only_market(
    controller: Any,
    *,
    symbol: str,
    side: Literal["BUY", "SELL"],
    quantity: float,
    position_side: str,
    timeout_sec: float = 15.0,
) -> bool:
    if quantity <= 0.0 or controller.rest_client is None:
        return False
    if not hasattr(controller.rest_client, "place_reduce_only_market_order"):
        return False
    rest_client_any: Any = controller.rest_client
    _ = run_async_blocking(
        lambda s=symbol, side_out=side, qty=quantity, ps=position_side: (
            rest_client_any.place_reduce_only_market_order(
                symbol=s,
                side=side_out,
                quantity=qty,
                position_side=cast(Literal["BOTH", "LONG", "SHORT"], ps),
            )
        ),
        timeout_sec=timeout_sec,
    )
    return True


def exit_managed_position(
    controller: Any,
    *,
    symbol: str,
    plan: dict[str, Any],
    notify_reason: str,
    event_reason: str,
    held_bars: int,
    max_favorable_r: float,
    extra_log_fields: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    updated_plan = mark_exit_requested(plan)
    clear_position_management_state(controller, symbol=symbol)
    _ = run_async_blocking(
        lambda s=symbol: controller.close_position(symbol=s, notify_reason=notify_reason),
        timeout_sec=30.0,
    )
    payload = {
        "symbol": symbol,
        "reason": event_reason,
        "held_bars": int(held_bars),
        "max_favorable_r": round(float(max_favorable_r), 4),
    }
    if isinstance(extra_log_fields, dict):
        payload.update(extra_log_fields)
    controller._log_event("position_management_exit", **payload)
    return True, updated_plan


def log_position_reduced(
    controller: Any,
    *,
    symbol: str,
    reason: str,
    reduced_qty: float,
    remaining_qty: float,
    current_r: float,
    stage: int | None = None,
    side: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "symbol": symbol,
        "reason": reason,
        "reduced_qty": round(float(reduced_qty), 8),
        "remaining_qty": round(float(remaining_qty), 8),
        "current_r": round(float(current_r), 4),
        "event_time": utcnow_iso(),
    }
    if stage is not None:
        payload["stage"] = int(stage)
    if side is not None:
        payload["side"] = side
    controller._log_event("position_reduced", **payload)


def log_management_update(
    controller: Any,
    *,
    symbol: str,
    reason: str,
    held_bars: int,
    max_favorable_r: float,
    extra_fields: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "symbol": symbol,
        "reason": reason,
        "held_bars": int(held_bars),
        "max_favorable_r": round(float(max_favorable_r), 4),
    }
    if isinstance(extra_fields, dict):
        payload.update(extra_fields)
    controller._log_event("position_management_update", **payload)


def current_position_amount(row: dict[str, Any]) -> float:
    return abs(to_float(row.get("positionAmt"), default=0.0))
