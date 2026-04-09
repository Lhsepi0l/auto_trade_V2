from __future__ import annotations

import logging
from typing import Any, Literal

from v2.control.controller_position_execution import (
    exit_managed_position,
    log_position_reduced,
    reduce_only_market,
)
from v2.control.controller_position_tpsl import replace_management_bracket
from v2.control.position_management_runtime import mark_runner_activated
from v2.control.runtime_utils import clamp, to_float

logger = logging.getLogger("v2.control.api")


def dynamic_weak_reduce_ratio(
    plan: dict[str, Any],
    *,
    stage: int,
    signal_view: dict[str, Any] | None = None,
) -> float:
    alpha_id = str(plan.get("alpha_id") or "").strip().lower()
    regime = str((signal_view or {}).get("regime") or plan.get("entry_regime") or "").strip().upper()
    stage_one = 0.25
    stage_two = 0.50
    if alpha_id == "alpha_expansion":
        stage_one = 0.20
        stage_two = 0.35
    elif alpha_id == "alpha_breakout":
        stage_one = 0.30
        stage_two = 0.50
    elif alpha_id == "alpha_pullback":
        stage_one = 0.25
        stage_two = 0.45

    if regime in {"UNKNOWN", "SIDEWAYS", "NONE"}:
        stage_one *= 1.1
        stage_two *= 1.1

    ratio = stage_one if stage <= 1 else stage_two
    return clamp(float(ratio), 0.15, 0.60)


def inspect_position_signal(controller: Any, *, symbol: str) -> dict[str, Any] | None:
    probe = getattr(controller.kernel, "probe_market_data", None)
    snapshot = None
    if callable(probe):
        try:
            snapshot = probe()
        except Exception:  # noqa: BLE001
            logger.exception("position_signal_probe_failed symbol=%s", symbol)
            return None
    inspector = getattr(controller.kernel, "inspect_symbol_decision", None)
    if not callable(inspector):
        return None
    try:
        decision = inspector(symbol=symbol, snapshot=snapshot)
    except Exception:  # noqa: BLE001
        logger.exception("position_signal_inspect_failed symbol=%s", symbol)
        return None
    if not isinstance(decision, dict):
        return None
    intent = str(decision.get("intent") or "").strip().upper()
    state = "candidate" if intent in {"LONG", "SHORT"} else "blocked"
    side = "LONG" if intent == "LONG" else "SHORT" if intent == "SHORT" else "NONE"
    return {
        "state": state,
        "side": side,
        "reason": str(decision.get("reason") or "").strip(),
        "score": to_float(decision.get("score"), default=0.0),
        "regime_strength": to_float(decision.get("regime_strength"), default=0.0),
        "bias_strength": to_float(decision.get("bias_strength"), default=0.0),
        "regime": str(decision.get("regime") or "").strip().upper() or None,
        "alpha_id": str(decision.get("alpha_id") or "").strip() or None,
    }


def handle_signal_management(
    controller: Any,
    *,
    symbol: str,
    plan: dict[str, Any],
    side: str,
    position_amt: float,
    position_side: str,
    entry_side: Literal["BUY", "SELL"],
    exit_side: Literal["BUY", "SELL"],
    entry_price: float,
    held_bars: int,
    current_r: float,
    max_favorable_r: float,
) -> tuple[str, dict[str, Any]]:
    signal_view = inspect_position_signal(controller, symbol=symbol)
    if signal_view is None:
        return "none", plan

    signal_state = str(signal_view.get("state") or "").strip().lower()
    signal_reason = str(signal_view.get("reason") or "").strip().lower()
    current_signal_side = str(signal_view.get("side") or "").strip().upper()
    current_score = to_float(signal_view.get("score"), default=0.0)
    current_regime_strength = to_float(signal_view.get("regime_strength"), default=0.0)
    current_bias_strength = to_float(signal_view.get("bias_strength"), default=0.0)
    entry_score = max(to_float(plan.get("entry_score"), default=0.0), 0.0)

    if current_signal_side and current_signal_side not in {side} and signal_state == "candidate":
        _exited, updated_plan = exit_managed_position(
            controller,
            symbol=symbol,
            plan=plan,
            notify_reason="signal_flip_close",
            event_reason="signal_flip_close",
            held_bars=held_bars,
            max_favorable_r=max_favorable_r,
            extra_log_fields={"signal_side": current_signal_side},
        )
        return "exit", updated_plan

    if signal_state != "candidate" and signal_reason in {"regime_missing", "bias_missing"}:
        _exited, updated_plan = exit_managed_position(
            controller,
            symbol=symbol,
            plan=plan,
            notify_reason="regime_bias_lost_close",
            event_reason="regime_bias_lost_close",
            held_bars=held_bars,
            max_favorable_r=max_favorable_r,
            extra_log_fields={"signal_reason": signal_reason},
        )
        return "exit", updated_plan

    weak_reduce_stage = int(to_float(plan.get("weak_reduce_stage"), default=0.0))
    score_weak = current_score > 0.0 and entry_score > 0.0 and current_score < max(entry_score * 0.7, 0.52)
    structure_weak = (
        current_regime_strength > 0.0
        and current_regime_strength < max(to_float(plan.get("entry_regime_strength"), default=0.0) * 0.7, 0.45)
    ) or (
        current_bias_strength > 0.0
        and current_bias_strength < max(to_float(plan.get("entry_bias_strength"), default=0.0) * 0.7, 0.45)
    )
    blocked_weak = signal_state != "candidate" and signal_reason in {
        "volume_missing",
        "trigger_missing",
        "quality_score_v2_missing",
        "quality_score_missing",
        "short_overextension_risk",
        "breakout_efficiency_missing",
        "breakout_stability_missing",
        "breakout_stability_edge_missing",
    }
    if position_amt > 0.0 and weak_reduce_stage == 0 and (score_weak or structure_weak or blocked_weak):
        reduce_qty = max(position_amt * dynamic_weak_reduce_ratio(plan, stage=1, signal_view=signal_view), 0.0)
        if reduce_qty > 0.0 and reduce_only_market(
            controller,
            symbol=symbol,
            side=exit_side,
            quantity=reduce_qty,
            position_side=position_side,
        ):
            plan["weak_reduce_stage"] = 1
            plan["breakeven_protection_armed"] = True
            remaining_qty = max(position_amt - reduce_qty, 0.0)
            if remaining_qty > 0.0 and to_float(plan.get("take_profit_price"), default=0.0) > 0.0:
                replace_management_bracket(
                    controller,
                    symbol=symbol,
                    entry_side=entry_side,
                    position_side=position_side,
                    quantity=remaining_qty,
                    take_profit_price=to_float(plan.get("take_profit_price"), default=0.0),
                    stop_loss_price=float(entry_price),
                    reason="weakness_reduce_reprice",
                )
                plan["stop_price"] = float(entry_price)
            updated_plan = mark_runner_activated(plan)
            log_position_reduced(
                controller,
                symbol=symbol,
                reason="signal_weakness_reduce",
                stage=1,
                reduced_qty=reduce_qty,
                remaining_qty=remaining_qty,
                current_r=current_r,
            )
            return "reduced", updated_plan

    severe_score_weak = current_score > 0.0 and entry_score > 0.0 and current_score < max(entry_score * 0.55, 0.45)
    severe_structure_weak = (
        current_regime_strength > 0.0
        and current_regime_strength < max(to_float(plan.get("entry_regime_strength"), default=0.0) * 0.55, 0.35)
    ) or (
        current_bias_strength > 0.0
        and current_bias_strength < max(to_float(plan.get("entry_bias_strength"), default=0.0) * 0.55, 0.35)
    )
    if position_amt > 0.0 and weak_reduce_stage == 1 and (severe_score_weak or severe_structure_weak or blocked_weak):
        reduce_qty = max(position_amt * dynamic_weak_reduce_ratio(plan, stage=2, signal_view=signal_view), 0.0)
        if reduce_qty > 0.0 and reduce_only_market(
            controller,
            symbol=symbol,
            side=exit_side,
            quantity=reduce_qty,
            position_side=position_side,
        ):
            plan["weak_reduce_stage"] = 2
            remaining_qty = max(position_amt - reduce_qty, 0.0)
            if remaining_qty > 0.0 and to_float(plan.get("take_profit_price"), default=0.0) > 0.0:
                replace_management_bracket(
                    controller,
                    symbol=symbol,
                    entry_side=entry_side,
                    position_side=position_side,
                    quantity=remaining_qty,
                    take_profit_price=to_float(plan.get("take_profit_price"), default=0.0),
                    stop_loss_price=to_float(plan.get("stop_price"), default=entry_price),
                    reason="weakness_reduce_reprice",
                )
            updated_plan = mark_runner_activated(plan)
            log_position_reduced(
                controller,
                symbol=symbol,
                reason="signal_weakness_reduce",
                stage=2,
                reduced_qty=reduce_qty,
                remaining_qty=remaining_qty,
                current_r=current_r,
            )
            return "reduced", updated_plan

    return "none", plan
