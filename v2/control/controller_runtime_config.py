from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from v2.control.profile_policy import normalize_runtime_risk_config
from v2.control.runtime_utils import to_float


def alpha_v2_profile_params(controller: Any) -> dict[str, Any]:
    for entry in controller.cfg.behavior.strategies:
        if not bool(getattr(entry, "enabled", False)):
            continue
        if str(getattr(entry, "name", "")).strip() != "ra_2026_alpha_v2":
            continue
        params = getattr(entry, "params", None)
        if isinstance(params, dict):
            return dict(params)
        return {}
    return {}


def strategy_runtime_defaults(
    controller: Any,
    *,
    runtime_param_keys: tuple[str, ...],
    runtime_defaults: dict[str, Any],
) -> dict[str, Any]:
    strategy_params = alpha_v2_profile_params(controller)
    if not strategy_params:
        return {}
    defaults: dict[str, Any] = {}
    for key in runtime_param_keys:
        base = copy.deepcopy(runtime_defaults[key])
        defaults[key] = copy.deepcopy(strategy_params.get(key, base))
    return defaults


def strategy_runtime_snapshot(
    controller: Any,
    *,
    runtime_param_keys: tuple[str, ...],
    runtime_defaults: dict[str, Any],
) -> dict[str, Any]:
    defaults = strategy_runtime_defaults(
        controller,
        runtime_param_keys=runtime_param_keys,
        runtime_defaults=runtime_defaults,
    )
    if not defaults:
        return {}
    snapshot: dict[str, Any] = {}
    for key, default in defaults.items():
        snapshot[key] = copy.deepcopy(controller._risk.get(key, default))
    return snapshot


def profile_runtime_risk_overrides(controller: Any, *, sched_sec: int) -> dict[str, Any]:
    from v2.control.profile_policy import (
        profile_runtime_risk_overrides as _profile_runtime_risk_overrides,
    )

    return _profile_runtime_risk_overrides(controller, sched_sec=sched_sec)


def non_persistent_risk_keys(
    controller: Any,
    *,
    runtime_param_keys: tuple[str, ...],
    runtime_derived_risk_keys: tuple[str, ...],
) -> set[str]:
    keys = set(runtime_param_keys)
    keys.update(runtime_derived_risk_keys)
    sched_sec = int(
        to_float(
            controller._risk.get("scheduler_tick_sec"),
            default=float(controller.scheduler.tick_seconds),
        )
    )
    keys.update(profile_runtime_risk_overrides(controller, sched_sec=sched_sec).keys())
    return keys


def persistent_risk_config(
    controller: Any,
    *,
    runtime_param_keys: tuple[str, ...],
    runtime_derived_risk_keys: tuple[str, ...],
) -> dict[str, Any]:
    payload = normalize_runtime_risk_config(controller._risk)
    for key in non_persistent_risk_keys(
        controller,
        runtime_param_keys=runtime_param_keys,
        runtime_derived_risk_keys=runtime_derived_risk_keys,
    ):
        payload.pop(key, None)
    return payload


def strip_persisted_strategy_runtime_overrides(
    controller: Any,
    payload: dict[str, Any],
    *,
    runtime_param_keys: tuple[str, ...],
    runtime_derived_risk_keys: tuple[str, ...],
) -> tuple[dict[str, Any], bool]:
    stripped = dict(payload)
    changed = False
    for key in non_persistent_risk_keys(
        controller,
        runtime_param_keys=runtime_param_keys,
        runtime_derived_risk_keys=runtime_derived_risk_keys,
    ):
        if key not in stripped:
            continue
        stripped.pop(key, None)
        changed = True
    return stripped, changed


def migrate_legacy_strategy_runtime_defaults(
    controller: Any,
    payload: dict[str, Any],
    *,
    runtime_param_keys: tuple[str, ...],
    runtime_defaults: dict[str, Any],
    legacy_defaults: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    defaults = strategy_runtime_defaults(
        controller,
        runtime_param_keys=runtime_param_keys,
        runtime_defaults=runtime_defaults,
    )
    if not defaults:
        return dict(payload), False
    migrated = dict(payload)
    changed = False
    for key, legacy_default in legacy_defaults.items():
        if key not in migrated or key not in defaults:
            continue
        current = migrated.get(key)
        profile_default = defaults[key]
        if current == legacy_default and profile_default != legacy_default:
            migrated[key] = copy.deepcopy(profile_default)
            changed = True
    return migrated, changed


def freshness_defaults_for_scheduler_tick(*, sched_sec: int) -> dict[str, float]:
    sec = max(1, int(sched_sec))
    return {
        "user_ws_stale_sec": max(float(sec) * 4.0, 60.0),
        "market_data_stale_sec": max(float(sec) * 2.0, 30.0),
        "watchdog_interval_sec": float(sec),
    }


def migrate_persisted_scheduler_runtime_defaults(
    controller: Any,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    migrated = dict(payload)
    if "scheduler_tick_sec" not in migrated:
        return migrated, False

    current_tick = max(
        1,
        int(to_float(controller._risk.get("scheduler_tick_sec"), default=float(controller.scheduler.tick_seconds))),
    )
    persisted_tick = max(1, int(to_float(migrated.get("scheduler_tick_sec"), default=float(current_tick))))
    current_defaults = freshness_defaults_for_scheduler_tick(sched_sec=current_tick)
    next_defaults = freshness_defaults_for_scheduler_tick(sched_sec=persisted_tick)

    changed = False
    for key in ("user_ws_stale_sec", "market_data_stale_sec", "watchdog_interval_sec"):
        if key in migrated:
            current_value = to_float(migrated.get(key), default=current_defaults[key])
            if abs(current_value - current_defaults[key]) >= 1e-9:
                continue
        migrated[key] = next_defaults[key]
        changed = True
    return migrated, changed


def apply_scheduler_tick_change(controller: Any, *, sec: int) -> None:
    normalized = max(1, int(sec))
    previous = max(
        1,
        int(to_float(controller._risk.get("scheduler_tick_sec"), default=float(controller.scheduler.tick_seconds))),
    )
    prev_defaults = freshness_defaults_for_scheduler_tick(sched_sec=previous)
    next_defaults = freshness_defaults_for_scheduler_tick(sched_sec=normalized)

    controller.scheduler.tick_seconds = normalized
    controller._risk["scheduler_tick_sec"] = normalized

    for key in ("user_ws_stale_sec", "market_data_stale_sec", "watchdog_interval_sec"):
        current_value = to_float(controller._risk.get(key), default=prev_defaults[key])
        if abs(current_value - prev_defaults[key]) < 1e-9:
            controller._risk[key] = next_defaults[key]


def sync_kernel_runtime_overrides(
    controller: Any,
    *,
    runtime_param_keys: tuple[str, ...],
    runtime_defaults: dict[str, Any],
) -> None:
    symbols = controller._runtime_symbols()
    mapping = controller._runtime_symbol_leverage_map()
    target_margin = controller._runtime_target_margin()
    max_leverage = max(1.0, to_float(controller._risk.get("max_leverage"), default=1.0))

    fallback_notional = float(target_margin)
    if fallback_notional <= 0.0:
        fallback_notional = 10.0

    max_position_notional = to_float(controller._risk.get("max_position_notional_usdt"), default=0.0)
    capped_notional: float | None = float(max_position_notional) if max_position_notional > 0 else None
    if hasattr(controller.kernel, "set_universe_symbols"):
        controller.kernel.set_universe_symbols(symbols)  # type: ignore[attr-defined]
    if hasattr(controller.kernel, "set_symbol_leverage_map"):
        controller.kernel.set_symbol_leverage_map(  # type: ignore[attr-defined]
            mapping,
            max_leverage=max_leverage,
        )
    if hasattr(controller.kernel, "set_notional_config"):
        controller.kernel.set_notional_config(  # type: ignore[attr-defined]
            fallback_notional=fallback_notional,
            max_notional=capped_notional,
        )
    strategy_runtime = strategy_runtime_snapshot(
        controller,
        runtime_param_keys=runtime_param_keys,
        runtime_defaults=runtime_defaults,
    )
    if hasattr(controller.kernel, "set_strategy_runtime_params") and strategy_runtime:
        controller.kernel.set_strategy_runtime_params(  # type: ignore[attr-defined]
            **strategy_runtime,
        )
    if hasattr(controller.kernel, "set_runtime_context"):
        controller.kernel.set_runtime_context(  # type: ignore[attr-defined]
            daily_loss_limit_pct=controller._risk.get("daily_loss_limit_pct"),
            dd_limit_pct=controller._risk.get("dd_limit_pct"),
            daily_loss_used_pct=float(controller._risk.get("daily_loss_used_pct") or 0.0),
            dd_used_pct=float(controller._risk.get("dd_used_pct") or 0.0),
            lose_streak=int(controller._risk.get("lose_streak") or 0),
            cooldown_until=controller._risk.get("cooldown_until"),
            risk_score_min=controller._risk.get("risk_score_min"),
            spread_max_pct=controller._risk.get("spread_max_pct"),
            dd_scale_start_pct=controller._risk.get("dd_scale_start_pct"),
            dd_scale_max_pct=controller._risk.get("dd_scale_max_pct"),
            dd_scale_min_factor=controller._risk.get("dd_scale_min_factor"),
            recent_blocks=dict(controller._risk.get("recent_blocks") or {}),
        )


def initial_risk_config(
    controller: Any,
    *,
    runtime_param_keys: tuple[str, ...],
    runtime_defaults: dict[str, Any],
) -> dict[str, Any]:
    risk_cfg = controller.cfg.behavior.risk
    tpsl_cfg = controller.cfg.behavior.tpsl
    sched_sec = int(controller.cfg.behavior.scheduler.tick_seconds)
    freshness_defaults = freshness_defaults_for_scheduler_tick(sched_sec=sched_sec)
    day_key = datetime.now(timezone.utc).date().isoformat()
    config = {
        "risk_per_trade_pct": 10.0,
        "max_exposure_pct": float(risk_cfg.max_exposure_pct),
        "max_notional_pct": 1000.0,
        "max_leverage": float(risk_cfg.max_leverage),
        "daily_loss_limit_pct": float(risk_cfg.daily_loss_limit_pct),
        "dd_limit_pct": float(risk_cfg.dd_limit_pct),
        "daily_loss_used_pct": 0.0,
        "dd_used_pct": 0.0,
        "daily_realized_pnl": 0.0,
        "daily_realized_pct": 0.0,
        "lose_streak": 0,
        "cooldown_until": None,
        "risk_day": day_key,
        "recent_blocks": {},
        "daily_lock": False,
        "dd_lock": False,
        "runtime_equity_now_usdt": 0.0,
        "runtime_equity_peak_usdt": 0.0,
        "last_auto_risk_reason": None,
        "last_auto_risk_at": None,
        "auto_risk_enabled": True,
        "auto_pause_on_risk": True,
        "auto_safe_mode_on_risk": True,
        "auto_flatten_on_risk": True,
        "risk_score_min": None,
        "dd_scale_start_pct": 0.12,
        "dd_scale_max_pct": 0.32,
        "dd_scale_min_factor": 0.35,
        "lose_streak_n": 3,
        "cooldown_hours": 2,
        "min_hold_minutes": 0,
        "trend_enter_adx_4h": 22.0,
        "trend_exit_adx_4h": 18.0,
        "regime_hold_bars_4h": 2,
        "breakout_buffer_bps": 8.0,
        "breakout_bar_size_atr_max": 1.6,
        "min_volume_ratio_15m": 1.2,
        "range_enabled": False,
        "overheat_funding_abs": 0.0008,
        "overheat_long_short_ratio_cap": 1.8,
        "overheat_long_short_ratio_floor": 0.56,
        "exec_mode_default": "MARKET",
        "exec_limit_timeout_sec": 3.0,
        "exec_limit_retries": 2,
        "scheduler_tick_sec": sched_sec,
        "notify_interval_sec": sched_sec,
        "spread_max_pct": 0.5,
        "allow_market_when_wide_spread": False,
        "user_ws_stale_sec": freshness_defaults["user_ws_stale_sec"],
        "market_data_stale_sec": freshness_defaults["market_data_stale_sec"],
        "reconcile_max_age_sec": 300.0,
        "capital_mode": "MARGIN_BUDGET_USDT",
        "capital_pct": 1.0,
        "capital_usdt": 100.0,
        "margin_budget_usdt": 100.0,
        "margin_use_pct": 1.0,
        "max_position_notional_usdt": None,
        "fee_buffer_pct": 0.001,
        "universe_symbols": [controller.cfg.behavior.exchange.default_symbol],
        "enable_watchdog": False,
        "watchdog_interval_sec": freshness_defaults["watchdog_interval_sec"],
        "shock_1m_pct": 0.0,
        "shock_from_entry_pct": 0.0,
        "trailing_enabled": bool(tpsl_cfg.trailing_enabled),
        "trailing_mode": "PCT",
        "trail_arm_pnl_pct": float(tpsl_cfg.take_profit_pct * 100.0),
        "trail_distance_pnl_pct": float(tpsl_cfg.stop_loss_pct * 100.0),
        "trail_grace_minutes": 0,
        "atr_trail_timeframe": "1h",
        "atr_trail_k": 2.0,
        "atr_trail_min_pct": 0.6,
        "atr_trail_max_pct": 1.8,
        "tpsl_policy": "adaptive_regime",
        "tpsl_method": "percent",
        "tpsl_base_take_profit_pct": float(tpsl_cfg.take_profit_pct),
        "tpsl_base_stop_loss_pct": float(tpsl_cfg.stop_loss_pct),
        "tpsl_regime_mult_bull": 1.15,
        "tpsl_regime_mult_bear": 1.15,
        "tpsl_regime_mult_sideways": 0.9,
        "tpsl_regime_mult_unknown": 1.0,
        "tpsl_volatility_norm_enabled": False,
        "tpsl_atr_pct_ref": 0.01,
        "tpsl_vol_mult_min": 0.85,
        "tpsl_vol_mult_max": 1.2,
        "tpsl_tp_min_pct": 0.0025,
        "tpsl_tp_max_pct": 0.06,
        "tpsl_sl_min_pct": 0.0025,
        "tpsl_sl_max_pct": 0.03,
        "tpsl_rr_min": 0.8,
        "tpsl_rr_max": 3.0,
        "tpsl_tp_atr": 2.0,
        "tpsl_sl_atr": 1.0,
        "vol_shock_atr_mult_threshold": 0.0,
        "atr_mult_mean_window": 0,
        "symbol_leverage_map": {},
        "last_strategy_block_reason": None,
        "last_alpha_id": None,
        "last_entry_family": None,
        "last_regime": None,
        "last_alpha_blocks": {},
        "last_alpha_reject_focus": None,
        "last_alpha_reject_metrics": {},
        "overheat_state": {"blocked": False, "reason": None},
    }
    config.update(
        strategy_runtime_defaults(
            controller,
            runtime_param_keys=runtime_param_keys,
            runtime_defaults=runtime_defaults,
        )
    )
    config.update(profile_runtime_risk_overrides(controller, sched_sec=sched_sec))
    return config
