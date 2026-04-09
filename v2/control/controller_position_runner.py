from __future__ import annotations

from typing import Any, Literal

from v2.control.controller_position_execution import (
    exit_managed_position,
    log_management_update,
    log_position_reduced,
    reduce_only_market,
)
from v2.control.controller_position_tpsl import replace_management_bracket
from v2.control.position_management_runtime import mark_runner_activated
from v2.control.runtime_utils import to_float


def runner_lock_targets(plan: dict[str, Any]) -> list[tuple[float, float, int]]:
    volatility_frac = max(to_float(plan.get("volatility_frac"), default=0.0), 0.0)
    if volatility_frac >= 0.015:
        return [(1.6, 0.35, 1), (2.2, 0.75, 2)]
    if 0.0 < volatility_frac <= 0.006:
        return [(1.4, 0.65, 1), (1.9, 1.25, 2)]
    return [(1.5, 0.50, 1), (2.0, 1.00, 2)]


def apply_runner_lock(
    controller: Any,
    *,
    symbol: str,
    plan: dict[str, Any],
    side: str,
    position_amt: float,
    position_side: str,
    entry_price: float,
    risk_per_unit: float,
    max_favorable_r: float,
    held_bars: int,
) -> dict[str, Any]:
    runner_lock_stage = int(to_float(plan.get("runner_lock_stage"), default=0.0))
    if not bool(plan.get("partial_reduce_done")) or position_amt <= 0.0:
        return plan

    lock_target_r = 0.0
    target_stage = 0
    for trigger_r, target_r, stage in runner_lock_targets(plan):
        if max_favorable_r >= trigger_r and runner_lock_stage < stage:
            lock_target_r = target_r
            target_stage = stage
    if target_stage > 0:
        plan["runner_lock_stage"] = target_stage
    if lock_target_r <= 0.0:
        return plan

    locked_stop = (
        entry_price + (risk_per_unit * lock_target_r)
        if side == "LONG"
        else entry_price - (risk_per_unit * lock_target_r)
    )
    current_stop = to_float(plan.get("stop_price"), default=0.0)
    should_reprice = (side == "LONG" and locked_stop > current_stop) or (
        side == "SHORT" and (current_stop <= 0.0 or locked_stop < current_stop)
    )
    if not should_reprice:
        return plan

    replace_management_bracket(
        controller,
        symbol=symbol,
        entry_side="BUY" if side == "LONG" else "SELL",
        position_side=position_side,
        quantity=position_amt,
        take_profit_price=to_float(plan.get("take_profit_price"), default=0.0),
        stop_loss_price=locked_stop,
        reason="runner_lock_reprice",
    )
    plan["stop_price"] = float(locked_stop)
    log_management_update(
        controller,
        symbol=symbol,
        reason="runner_lock_reprice",
        held_bars=held_bars,
        max_favorable_r=max_favorable_r,
        extra_fields={
            "locked_r": lock_target_r,
            "lock_stage": int(target_stage),
            "volatility_frac": round(to_float(plan.get("volatility_frac"), default=0.0), 6),
        },
    )
    return plan


def apply_partial_reduce(
    controller: Any,
    *,
    symbol: str,
    plan: dict[str, Any],
    side: str,
    position_amt: float,
    position_side: str,
    entry_price: float,
    risk_per_unit: float,
    current_r: float,
    held_bars: int,
    max_favorable_r: float,
) -> tuple[bool, dict[str, Any]]:
    tp_partial_ratio = min(max(to_float(plan.get("tp_partial_ratio"), default=0.0), 0.0), 1.0)
    tp_partial_at_r = to_float(plan.get("tp_partial_at_r"), default=0.0)
    if not (
        position_amt > 0.0
        and tp_partial_ratio > 0.0
        and tp_partial_at_r > 0.0
        and not bool(plan.get("partial_reduce_done"))
        and current_r >= tp_partial_at_r
    ):
        return False, plan

    exit_side: Literal["BUY", "SELL"] = "SELL" if side == "LONG" else "BUY"
    reduce_qty = max(position_amt * tp_partial_ratio, 0.0)
    if reduce_qty <= 0.0 or not reduce_only_market(
        controller,
        symbol=symbol,
        side=exit_side,
        quantity=reduce_qty,
        position_side=position_side,
    ):
        return False, plan

    plan["partial_reduce_done"] = True
    plan["partial_reduce_qty"] = float(reduce_qty)
    plan["partial_reduce_at_r_done"] = float(current_r)
    plan["breakeven_protection_armed"] = True

    remaining_qty = max(position_amt - reduce_qty, 0.0)
    if remaining_qty > 0.0:
        reward_r = max(to_float(plan.get("reward_risk_reference_r"), default=0.0), 0.0)
        if reward_r > 0.0:
            take_profit_price = (
                entry_price + (risk_per_unit * reward_r)
                if side == "LONG"
                else entry_price - (risk_per_unit * reward_r)
            )
        else:
            take_profit_price = to_float(plan.get("take_profit_price"), default=0.0)
        stop_price = float(entry_price)
        replace_management_bracket(
            controller,
            symbol=symbol,
            entry_side="BUY" if side == "LONG" else "SELL",
            position_side=position_side,
            quantity=remaining_qty,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_price,
            reason="partial_reduce_reprice",
        )
        plan["stop_price"] = float(stop_price)
        if take_profit_price > 0.0:
            plan["take_profit_price"] = float(take_profit_price)

    updated_plan = mark_runner_activated(plan)
    log_position_reduced(
        controller,
        symbol=symbol,
        reason="partial_reduce_executed",
        side=exit_side,
        reduced_qty=reduce_qty,
        remaining_qty=remaining_qty,
        current_r=current_r,
    )
    log_management_update(
        controller,
        symbol=symbol,
        reason="partial_reduce_executed",
        held_bars=held_bars,
        max_favorable_r=max_favorable_r,
        extra_fields={
            "partial_reduce_qty": round(float(reduce_qty), 8),
            "partial_reduce_at_r": round(float(current_r), 4),
        },
    )
    return True, updated_plan


def apply_extension_updates(
    controller: Any,
    *,
    symbol: str,
    plan: dict[str, Any],
    side: str,
    position_amt: float,
    position_side: str,
    entry_price: float,
    risk_per_unit: float,
    held_bars: int,
    max_favorable_r: float,
) -> dict[str, Any]:
    extend_trigger_r = to_float(plan.get("progress_extend_trigger_r"), default=0.0)
    extend_bars = int(to_float(plan.get("progress_extend_bars"), default=0.0))
    if (
        extend_trigger_r > 0.0
        and extend_bars > 0
        and not bool(plan.get("progress_extension_applied"))
        and max_favorable_r >= extend_trigger_r
    ):
        current_time_stop = int(to_float(plan.get("current_time_stop_bars"), default=0.0))
        plan["current_time_stop_bars"] = current_time_stop + extend_bars
        plan["progress_extension_applied"] = True
        log_management_update(
            controller,
            symbol=symbol,
            reason="progress_extension_applied",
            held_bars=held_bars,
            max_favorable_r=max_favorable_r,
            extra_fields={"current_time_stop_bars": int(plan["current_time_stop_bars"])},
        )

    if bool(plan.get("selective_extension_activated")):
        return plan

    proof_bars = int(to_float(plan.get("selective_extension_proof_bars"), default=0.0))
    if not (
        proof_bars > 0
        and held_bars <= proof_bars
        and max_favorable_r >= to_float(plan.get("selective_extension_min_mfe_r"), default=0.0)
        and to_float(plan.get("entry_regime_strength"), default=0.0)
        >= to_float(plan.get("selective_extension_min_regime_strength"), default=0.0)
        and to_float(plan.get("entry_bias_strength"), default=0.0)
        >= to_float(plan.get("selective_extension_min_bias_strength"), default=0.0)
        and to_float(plan.get("entry_quality_score_v2"), default=0.0)
        >= to_float(plan.get("selective_extension_min_quality_score_v2"), default=0.0)
    ):
        return plan

    selective_time_stop = int(to_float(plan.get("selective_extension_time_stop_bars"), default=0.0))
    if selective_time_stop > int(to_float(plan.get("current_time_stop_bars"), default=0.0)):
        plan["current_time_stop_bars"] = selective_time_stop
    plan["selective_extension_activated"] = True
    extension_tp_r = to_float(plan.get("selective_extension_take_profit_r"), default=0.0)
    if position_amt > 0.0 and extension_tp_r > 0.0:
        take_profit_price = (
            entry_price + (risk_per_unit * extension_tp_r)
            if side == "LONG"
            else entry_price - (risk_per_unit * extension_tp_r)
        )
        stop_price = float(entry_price) if bool(plan.get("breakeven_protection_armed")) else to_float(
            plan.get("stop_price"),
            default=0.0,
        )
        if stop_price > 0.0:
            replace_management_bracket(
                controller,
                symbol=symbol,
                entry_side="BUY" if side == "LONG" else "SELL",
                position_side=position_side,
                quantity=position_amt,
                take_profit_price=take_profit_price,
                stop_loss_price=stop_price,
                reason="selective_extension_reprice",
            )
            plan["take_profit_price"] = float(take_profit_price)
            plan["stop_price"] = float(stop_price)
    log_management_update(
        controller,
        symbol=symbol,
        reason="selective_extension_activated",
        held_bars=held_bars,
        max_favorable_r=max_favorable_r,
        extra_fields={"current_time_stop_bars": int(to_float(plan.get("current_time_stop_bars"), default=0.0))},
    )
    return plan


def apply_breakeven_update(
    controller: Any,
    *,
    symbol: str,
    plan: dict[str, Any],
    side: str,
    position_amt: float,
    position_side: str,
    entry_price: float,
    max_favorable_r: float,
    held_bars: int,
) -> dict[str, Any]:
    breakeven_trigger_r = to_float(plan.get("selective_extension_move_stop_to_be_at_r"), default=0.0)
    if breakeven_trigger_r <= 0.0:
        breakeven_trigger_r = to_float(plan.get("move_stop_to_be_at_r"), default=0.0)
    if breakeven_trigger_r <= 0.0 or max_favorable_r < breakeven_trigger_r:
        return plan
    if bool(plan.get("breakeven_protection_armed")):
        return plan

    plan["breakeven_protection_armed"] = True
    if position_amt > 0.0 and to_float(plan.get("take_profit_price"), default=0.0) > 0.0:
        replace_management_bracket(
            controller,
            symbol=symbol,
            entry_side="BUY" if side == "LONG" else "SELL",
            position_side=position_side,
            quantity=position_amt,
            take_profit_price=to_float(plan.get("take_profit_price"), default=0.0),
            stop_loss_price=float(entry_price),
            reason="breakeven_reprice",
        )
        plan["stop_price"] = float(entry_price)
    log_management_update(
        controller,
        symbol=symbol,
        reason="breakeven_protection_armed",
        held_bars=held_bars,
        max_favorable_r=max_favorable_r,
    )
    return plan


def maybe_exit_runner_position(
    controller: Any,
    *,
    symbol: str,
    plan: dict[str, Any],
    current_r: float,
    held_bars: int,
    max_favorable_r: float,
) -> tuple[bool, dict[str, Any]]:
    if bool(plan.get("breakeven_protection_armed")) and current_r <= 0.0:
        return exit_managed_position(
            controller,
            symbol=symbol,
            plan=plan,
            notify_reason="management_breakeven_close",
            event_reason="management_breakeven_close",
            held_bars=held_bars,
            max_favorable_r=max_favorable_r,
        )

    progress_check_bars = int(to_float(plan.get("progress_check_bars"), default=0.0))
    progress_min_mfe_r = to_float(plan.get("progress_min_mfe_r"), default=0.0)
    if progress_check_bars > 0 and progress_min_mfe_r > 0.0 and held_bars >= progress_check_bars:
        if max_favorable_r < progress_min_mfe_r:
            return exit_managed_position(
                controller,
                symbol=symbol,
                plan=plan,
                notify_reason="progress_failed_close",
                event_reason="progress_failed_close",
                held_bars=held_bars,
                max_favorable_r=max_favorable_r,
                extra_log_fields={"progress_min_mfe_r": round(float(progress_min_mfe_r), 4)},
            )

    time_stop_bars = int(to_float(plan.get("current_time_stop_bars"), default=0.0))
    if time_stop_bars > 0 and held_bars >= time_stop_bars:
        return exit_managed_position(
            controller,
            symbol=symbol,
            plan=plan,
            notify_reason="time_stop_close",
            event_reason="time_stop_close",
            held_bars=held_bars,
            max_favorable_r=max_favorable_r,
            extra_log_fields={"time_stop_bars": int(time_stop_bars)},
        )

    return False, plan
