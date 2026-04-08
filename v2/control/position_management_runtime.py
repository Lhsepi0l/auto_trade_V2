from __future__ import annotations

import time
from typing import Any

from v2.common.async_bridge import run_async_blocking as _run_async_blocking
from v2.kernel.contracts import KernelCycleResult
from v2.management import (
    PositionLifecycleEvent,
    PositionManagementSpec,
    advance_position_lifecycle,
    normalize_management_policy,
)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def plan_management_policy(plan: dict[str, Any] | None) -> str:
    if not isinstance(plan, dict):
        return ""
    return str(plan.get("management_policy") or "").strip().lower()


def plan_uses_runner_management(plan: dict[str, Any] | None) -> bool:
    policy = normalize_management_policy(plan_management_policy(plan))
    if policy is not None:
        return policy == "tp1_runner"
    return PositionManagementSpec.from_plan(plan).uses_runner_management()


def build_position_management_plan(*, cycle: KernelCycleResult) -> dict[str, Any] | None:
    candidate = cycle.candidate
    if candidate is None or cycle.state != "executed":
        return None
    execution_hints = (
        dict(candidate.execution_hints)
        if isinstance(candidate.execution_hints, dict)
        else None
    )
    if not execution_hints:
        return None
    symbol = str(candidate.symbol or "").strip().upper()
    if not symbol:
        return None
    entry_price = _to_float(candidate.entry_price, default=0.0)
    stop_price = _to_float(candidate.stop_price_hint, default=0.0)
    if entry_price <= 0.0 or stop_price <= 0.0:
        return None
    risk_per_unit = abs(entry_price - stop_price)
    if risk_per_unit <= 0.0:
        return None
    spec = PositionManagementSpec.from_execution_hints(execution_hints)
    lifecycle_state = advance_position_lifecycle(None, PositionLifecycleEvent.ENTRY_RECORDED)
    return {
        "symbol": symbol,
        "side": "LONG" if str(getattr(candidate, "side", "")).strip().upper() == "BUY" else "SHORT",
        "management_policy": spec.management_policy,
        "lifecycle_state": lifecycle_state,
        "entry_score": float(_to_float(getattr(candidate, "score", 0.0), default=0.0)),
        "entry_regime": str(getattr(candidate, "regime_hint", "") or "").strip().upper() or None,
        "entry_price": float(entry_price),
        "stop_price": float(stop_price),
        "take_profit_price": _to_float(candidate.take_profit_hint, default=0.0),
        "risk_per_unit": float(risk_per_unit),
        "volatility_frac": (
            float(_to_float(getattr(candidate, "volatility_hint", 0.0), default=0.0)) / float(entry_price)
            if _to_float(getattr(candidate, "volatility_hint", 0.0), default=0.0) > 0.0
            else float(risk_per_unit) / float(entry_price)
        ),
        "entry_time_ms": int(time.time() * 1000),
        "created_at": _utcnow_iso(),
        "alpha_id": getattr(candidate, "alpha_id", None),
        "entry_family": getattr(candidate, "entry_family", None),
        "max_favorable_price": float(entry_price),
        "max_favorable_r": 0.0,
        "progress_extension_applied": False,
        "selective_extension_activated": False,
        "breakeven_protection_armed": False,
        "partial_reduce_done": False,
        "weak_reduce_stage": 0,
        "runner_lock_stage": 0,
        **spec.to_execution_hints(),
    }


def mark_runner_activated(plan: dict[str, Any]) -> dict[str, Any]:
    updated = dict(plan)
    updated["lifecycle_state"] = advance_position_lifecycle(
        updated.get("lifecycle_state"),
        PositionLifecycleEvent.TP1_COMPLETED,
    )
    return updated


def mark_exit_requested(plan: dict[str, Any]) -> dict[str, Any]:
    updated = dict(plan)
    updated["lifecycle_state"] = advance_position_lifecycle(
        updated.get("lifecycle_state"),
        PositionLifecycleEvent.EXIT_REQUESTED,
    )
    return updated


def handle_take_profit_rearm(
    controller: Any,
    *,
    symbol: str,
    filled_client_id: str,
    management_plan: dict[str, Any],
) -> bool:
    if not plan_uses_runner_management(management_plan):
        return False
    confirmed_row, confirmed_ok = controller._confirm_live_position_row(symbol=symbol)
    if not confirmed_ok:
        return False
    if not isinstance(confirmed_row, dict):
        return False
    updated_plan = mark_runner_activated(management_plan)
    _ = controller._repair_position_bracket_from_plan(
        symbol=symbol,
        position_row=confirmed_row,
        plan=dict(updated_plan),
        reason="take_profit_rearm",
    )
    tp_fill = controller._latest_recent_fill_for_client_id(
        symbol=symbol,
        client_id=filled_client_id,
    )
    reduced_qty = _to_float((tp_fill or {}).get("qty"), default=0.0)
    remaining_qty = abs(_to_float(confirmed_row.get("positionAmt"), default=0.0))
    controller._log_event(
        "position_reduced",
        symbol=symbol,
        reason="take_profit_rearmed",
        reduced_qty=round(float(reduced_qty), 8) if reduced_qty > 0.0 else None,
        remaining_qty=round(float(remaining_qty), 8),
        realized_pnl=controller._resolve_symbol_realized_pnl(symbol=symbol),
        event_time=_utcnow_iso(),
    )
    state = controller._load_position_management_state()
    state[str(symbol).strip().upper()] = dict(updated_plan)
    controller._save_position_management_state(state)
    return True


def maybe_handle_fill_event_fast_path(controller: Any, *, event: dict[str, Any]) -> bool:
    if controller.cfg.mode != "live":
        return False
    if str(event.get("e") or "") != "ORDER_TRADE_UPDATE":
        return False
    order = event.get("o")
    if not isinstance(order, dict):
        return False
    if str(order.get("x") or "").strip().upper() != "TRADE":
        return False
    symbol = str(order.get("s") or "").strip().upper()
    client_id = str(order.get("c") or "").strip()
    if not symbol or not client_id:
        return False
    tracked = {
        str(row.get("symbol") or "").strip().upper(): row
        for row in controller._list_tracked_brackets()
    }
    tracked_row = tracked.get(symbol)
    if not isinstance(tracked_row, dict):
        return False
    tp_id = str(tracked_row.get("tp_order_client_id") or "").strip()
    if client_id != tp_id:
        return False
    management_state = controller._load_position_management_state()
    management_plan = management_state.get(symbol)
    if not isinstance(management_plan, dict) or not plan_uses_runner_management(management_plan):
        return False
    if not controller._has_recent_fill_for_client_id(symbol=symbol, client_id=client_id):
        return False
    try:
        _ = _run_async_blocking(
            lambda s=symbol, cid=client_id: controller._bracket_service.on_leg_filled(
                symbol=s,
                filled_client_algo_id=cid,
            ),
            timeout_sec=8.0,
        )
    except Exception:  # noqa: BLE001
        return False
    return handle_take_profit_rearm(
        controller,
        symbol=symbol,
        filled_client_id=client_id,
        management_plan=management_plan,
    )
