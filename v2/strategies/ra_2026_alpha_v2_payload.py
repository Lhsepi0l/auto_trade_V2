from __future__ import annotations

import copy
from typing import Any

from v2.management import PositionManagementSpec, normalize_management_policy
from v2.strategies.alpha_shared import _to_float
from v2.strategies.base import DesiredPosition
from v2.strategies.ra_2026_alpha_v2_alpha import (
    ALPHA_ENTRY_FAMILY,
    AllowedSideName,
    AlphaId,
    RegimeName,
)
from v2.strategies.ra_2026_alpha_v2_params import RA2026AlphaV2Params


def _decision_none(
    *,
    symbol: str,
    reason: str,
    regime: RegimeName,
    allowed_side: AllowedSideName,
    indicators: dict[str, float],
    alpha_diagnostics: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = dict(
        DesiredPosition(
            symbol=symbol,
            intent="NONE",
            side="NONE",
            score=0.0,
            reason=reason,
            allowed_side=allowed_side,
            signals={},
            blocks=[reason],
            indicators=indicators,
        ).to_dict()
    )
    payload["regime"] = regime
    payload["allowed_side"] = allowed_side
    if alpha_diagnostics:
        payload["alpha_diagnostics"] = copy.deepcopy(alpha_diagnostics)
        payload["alpha_blocks"] = {
            str(alpha_id): str(meta.get("reason") or "").strip()
            for alpha_id, meta in alpha_diagnostics.items()
            if str(meta.get("state") or "").strip() != "candidate"
            and str(meta.get("reason") or "").strip()
        }
        payload["alpha_reject_metrics"] = {
            str(alpha_id): copy.deepcopy(meta.get("metrics") or {})
            for alpha_id, meta in alpha_diagnostics.items()
            if str(meta.get("state") or "").strip() != "candidate"
            and isinstance(meta.get("metrics"), dict)
            and meta.get("metrics")
        }
    return payload


def _build_entry_payload(
    *,
    alpha_id: AlphaId,
    side: AllowedSideName,
    entry_price: float,
    stop_price: float,
    stop_distance_frac: float,
    score: float,
    ctx: Any,
    cfg: RA2026AlphaV2Params,
    alpha_diagnostics: dict[str, dict[str, Any]],
    entry_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = entry_meta or {}
    quality_score_v2 = max(float(_to_float(meta.get("quality_score_v2")) or 0.0), 0.0)
    entry_tier = str(meta.get("entry_tier") or "").strip().upper()
    management_policy = normalize_management_policy(cfg.management_policy) or "tp1_runner"
    effective_take_profit_r = max(
        float(_to_float(meta.get("take_profit_r_override")) or float(cfg.take_profit_r)),
        0.5,
    )
    effective_time_stop_bars = max(
        int(_to_float(meta.get("time_stop_bars_override")) or int(cfg.time_stop_bars)),
        1,
    )
    quality_exit_applied = False
    if (
        float(cfg.quality_exit_score_threshold) > 0.0
        and float(quality_score_v2) >= float(cfg.quality_exit_score_threshold)
    ):
        if float(cfg.quality_exit_take_profit_r) > 0.0:
            effective_take_profit_r = max(float(cfg.quality_exit_take_profit_r), 0.5)
        if int(cfg.quality_exit_time_stop_bars) > 0:
            effective_time_stop_bars = int(cfg.quality_exit_time_stop_bars)
        quality_exit_applied = True
    take_profit = (
        float(entry_price)
        + (float(stop_distance_frac) * float(entry_price) * float(effective_take_profit_r))
        if side == "LONG"
        else float(entry_price)
        - (float(stop_distance_frac) * float(entry_price) * float(effective_take_profit_r))
    )
    tp_partial_ratio = 0.0
    tp_partial_at_r = 0.0
    move_stop_to_be_at_r = 0.0
    if management_policy == "tp1_runner":
        tp_partial_ratio = min(max(float(cfg.tp_partial_ratio), 0.0), 1.0)
        move_stop_to_be_at_r = max(float(cfg.move_stop_to_be_at_r), 0.0)
        if tp_partial_ratio > 0.0:
            tp_partial_floor = max(float(cfg.tp_partial_min_r), 0.0)
            tp_partial_cap = max(float(cfg.tp_partial_max_r), 0.0)
            if tp_partial_cap > 0.0:
                tp_partial_cap = min(tp_partial_cap, float(effective_take_profit_r))
            else:
                tp_partial_cap = float(effective_take_profit_r)
            if tp_partial_cap >= tp_partial_floor and tp_partial_cap > 0.0:
                derived_partial_r = float(effective_take_profit_r) * max(
                    float(cfg.tp_partial_target_frac),
                    0.0,
                )
                tp_partial_at_r = min(max(derived_partial_r, tp_partial_floor), tp_partial_cap)
    payload = dict(
        DesiredPosition(
            symbol=ctx.symbol,
            intent="LONG" if side == "LONG" else "SHORT",
            side="BUY" if side == "LONG" else "SELL",
            score=float(score),
            reason="entry_signal",
            entry_price=float(entry_price),
            stop_hint=float(stop_price),
            management_hint=management_policy,
            regime=ctx.regime,
            allowed_side=ctx.regime_side if alpha_id != "alpha_expansion" else ctx.bias_side,
            signals={str(alpha_id): True},
            blocks=[],
            indicators=dict(ctx.indicators),
        ).to_dict()
    )
    payload["alpha_id"] = alpha_id
    payload["entry_family"] = ALPHA_ENTRY_FAMILY[alpha_id]
    payload["stop_price_hint"] = float(stop_price)
    payload["stop_distance_frac"] = float(stop_distance_frac)
    payload["risk_per_trade_pct"] = float(cfg.risk_per_trade_pct)
    if float(cfg.max_effective_leverage) > 0.0:
        payload["max_effective_leverage"] = float(cfg.max_effective_leverage)
    payload["expected_move_frac"] = float(ctx.expected_move_frac)
    payload["required_move_frac"] = float(ctx.required_move_frac)
    payload["regime_strength"] = float(ctx.regime_strength)
    payload["bias_strength"] = float(ctx.bias_strength)
    payload["entry_quality_score_v2"] = float(quality_score_v2)
    if entry_tier:
        payload["entry_tier"] = entry_tier
    payload["alpha_diagnostics"] = copy.deepcopy(alpha_diagnostics)
    payload["alpha_blocks"] = {
        str(alpha_id_key): str(meta.get("reason") or "").strip()
        for alpha_id_key, meta in alpha_diagnostics.items()
        if str(meta.get("state") or "").strip() != "candidate"
        and str(meta.get("reason") or "").strip()
    }
    payload["alpha_reject_metrics"] = {
        str(alpha_id_key): copy.deepcopy(meta.get("metrics") or {})
        for alpha_id_key, meta in alpha_diagnostics.items()
        if str(alpha_id_key) != str(alpha_id)
        and str(meta.get("state") or "").strip() != "candidate"
        and isinstance(meta.get("metrics"), dict)
        and meta.get("metrics")
    }
    payload["sl_tp"] = {
        "take_profit": float(take_profit),
        "stop_loss": float(stop_price),
    }
    payload["execution"] = PositionManagementSpec(
        management_policy=str(management_policy),
        time_stop_bars=int(effective_time_stop_bars),
        stop_exit_cooldown_bars=int(cfg.stop_exit_cooldown_bars),
        profit_exit_cooldown_bars=int(cfg.profit_exit_cooldown_bars),
        progress_check_bars=int(cfg.progress_check_bars),
        progress_min_mfe_r=float(cfg.progress_min_mfe_r),
        progress_extend_trigger_r=float(cfg.progress_extend_trigger_r),
        progress_extend_bars=int(cfg.progress_extend_bars),
        loss_streak_trigger=0,
        loss_streak_cooldown_bars=0,
        reward_risk_reference_r=float(effective_take_profit_r),
        tp_partial_ratio=float(tp_partial_ratio),
        tp_partial_at_r=float(tp_partial_at_r),
        move_stop_to_be_at_r=float(move_stop_to_be_at_r),
        allow_reverse_exit=False,
        reduce_only_exit=True,
        entry_quality_score_v2=float(quality_score_v2),
        quality_exit_applied=bool(quality_exit_applied),
        entry_regime_strength=float(ctx.regime_strength),
        entry_bias_strength=float(ctx.bias_strength),
        selective_extension_proof_bars=int(cfg.selective_extension_proof_bars),
        selective_extension_min_mfe_r=float(cfg.selective_extension_min_mfe_r),
        selective_extension_min_regime_strength=float(cfg.selective_extension_min_regime_strength),
        selective_extension_min_bias_strength=float(cfg.selective_extension_min_bias_strength),
        selective_extension_min_quality_score_v2=float(
            cfg.selective_extension_min_quality_score_v2
        ),
        selective_extension_time_stop_bars=int(cfg.selective_extension_time_stop_bars),
        selective_extension_take_profit_r=float(cfg.selective_extension_take_profit_r),
        selective_extension_move_stop_to_be_at_r=float(
            cfg.selective_extension_move_stop_to_be_at_r
        ),
    ).to_execution_hints()
    if entry_tier:
        payload["execution"]["entry_tier"] = entry_tier
    drift_setup_open_time_ms = _to_float(meta.get("setup_open_time_ms"))
    if (
        alpha_id == "alpha_drift"
        and drift_setup_open_time_ms is not None
        and drift_setup_open_time_ms > 0.0
    ):
        payload["execution"]["drift_setup_open_time_ms"] = int(drift_setup_open_time_ms)
    return payload
