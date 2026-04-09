from __future__ import annotations

import logging
from typing import Any, Literal

from v2.control.controller_position_execution import current_position_amount
from v2.control.controller_position_market import bars_held_for_management
from v2.control.controller_position_runner import (
    apply_breakeven_update,
    apply_extension_updates,
    apply_partial_reduce,
    apply_runner_lock,
    maybe_exit_runner_position,
)
from v2.control.controller_position_signal import (
    handle_signal_management,
)
from v2.control.runtime_utils import to_float, utcnow_iso

logger = logging.getLogger("v2.control.api")


def maybe_manage_open_position(
    controller: Any,
    *,
    symbol: str,
    row: dict[str, Any],
    plan: dict[str, Any],
    market_bar: dict[str, float] | None,
) -> tuple[bool, dict[str, Any]]:
    mark_price = to_float(row.get("markPrice"), default=0.0)
    if mark_price <= 0.0 and isinstance(market_bar, dict):
        mark_price = to_float(market_bar.get("close"), default=0.0)
    entry_price = to_float(plan.get("entry_price"), default=0.0)
    risk_per_unit = to_float(plan.get("risk_per_unit"), default=0.0)
    side = str(plan.get("side") or "").strip().upper()
    if mark_price <= 0.0 or entry_price <= 0.0 or risk_per_unit <= 0.0 or side not in {"LONG", "SHORT"}:
        return False, plan

    favorable_price = mark_price
    if isinstance(market_bar, dict):
        if side == "LONG":
            favorable_price = max(favorable_price, to_float(market_bar.get("high"), default=favorable_price))
        else:
            favorable_price = min(favorable_price, to_float(market_bar.get("low"), default=favorable_price))

    previous_best = to_float(plan.get("max_favorable_price"), default=entry_price)
    if side == "LONG":
        best_price = max(previous_best, favorable_price)
        max_favorable_r = max((best_price - entry_price) / risk_per_unit, 0.0)
        current_r = (mark_price - entry_price) / risk_per_unit
    else:
        best_price = min(previous_best, favorable_price)
        max_favorable_r = max((entry_price - best_price) / risk_per_unit, 0.0)
        current_r = (entry_price - mark_price) / risk_per_unit

    plan["max_favorable_price"] = float(best_price)
    plan["max_favorable_r"] = float(max_favorable_r)
    held_bars = bars_held_for_management(plan, market_bar)
    plan["held_bars"] = int(held_bars)
    plan["current_r"] = float(current_r)
    plan["last_evaluated_at"] = utcnow_iso()
    plan.setdefault("weak_reduce_stage", 0)
    plan.setdefault("runner_lock_stage", 0)
    plan.setdefault("entry_score", 0.0)

    position_amt = current_position_amount(row)
    position_side = str(row.get("positionSide") or "BOTH").strip().upper() or "BOTH"
    entry_side: Literal["BUY", "SELL"] = "BUY" if side == "LONG" else "SELL"
    exit_side: Literal["BUY", "SELL"] = "SELL" if side == "LONG" else "BUY"

    signal_result, plan = handle_signal_management(
        controller,
        symbol=symbol,
        plan=plan,
        side=side,
        position_amt=position_amt,
        position_side=position_side,
        entry_side=entry_side,
        exit_side=exit_side,
        entry_price=entry_price,
        held_bars=held_bars,
        current_r=current_r,
        max_favorable_r=max_favorable_r,
    )
    if signal_result == "exit":
        return True, plan
    if signal_result == "reduced":
        return False, plan

    plan = apply_runner_lock(
        controller,
        symbol=symbol,
        plan=plan,
        side=side,
        position_amt=position_amt,
        position_side=position_side,
        entry_price=entry_price,
        risk_per_unit=risk_per_unit,
        max_favorable_r=max_favorable_r,
        held_bars=held_bars,
    )

    reduced, plan = apply_partial_reduce(
        controller,
        symbol=symbol,
        plan=plan,
        side=side,
        position_amt=position_amt,
        position_side=position_side,
        entry_price=entry_price,
        risk_per_unit=risk_per_unit,
        current_r=current_r,
        held_bars=held_bars,
        max_favorable_r=max_favorable_r,
    )
    if reduced:
        return False, plan

    plan = apply_extension_updates(
        controller,
        symbol=symbol,
        plan=plan,
        side=side,
        position_amt=position_amt,
        position_side=position_side,
        entry_price=entry_price,
        risk_per_unit=risk_per_unit,
        held_bars=held_bars,
        max_favorable_r=max_favorable_r,
    )
    plan = apply_breakeven_update(
        controller,
        symbol=symbol,
        plan=plan,
        side=side,
        position_amt=position_amt,
        position_side=position_side,
        entry_price=entry_price,
        max_favorable_r=max_favorable_r,
        held_bars=held_bars,
    )
    return maybe_exit_runner_position(
        controller,
        symbol=symbol,
        plan=plan,
        current_r=current_r,
        held_bars=held_bars,
        max_favorable_r=max_favorable_r,
    )
