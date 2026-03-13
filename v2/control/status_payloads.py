from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from v2.control.api import RuntimeController


def _live_trading_enabled(controller: RuntimeController) -> bool:
    return controller.cfg.mode == "live" and str(controller.cfg.env) == "prod"


def _positions_payload(controller: RuntimeController) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    for symbol, position_amt, unrealized_pnl, entry_price in controller._status_positions_source():
        payload[symbol] = {
            "position_amt": position_amt,
            "entry_price": entry_price,
            "unrealized_pnl": unrealized_pnl,
            "position_side": "LONG" if position_amt > 0 else "SHORT",
        }
    return payload


def _config_summary_payload(controller: RuntimeController) -> dict[str, Any]:
    config_summary = controller._public_risk_config()
    config_summary["scheduler_tick_sec"] = float(controller.scheduler.tick_seconds)
    config_summary["scheduler_running"] = bool(controller._running)
    config_summary["scheduler_enabled"] = bool(controller._running)
    config_summary["active_strategy_timeframes"] = ["10m", "15m", "30m", "1h", "4h"]
    config_summary["strategy_runtime"] = {
        "trend_enter_adx_4h": controller._risk.get("trend_enter_adx_4h", 22.0),
        "trend_exit_adx_4h": controller._risk.get("trend_exit_adx_4h", 18.0),
        "regime_hold_bars_4h": controller._risk.get("regime_hold_bars_4h", 2),
        "breakout_buffer_bps": controller._risk.get("breakout_buffer_bps", 8.0),
        "breakout_bar_size_atr_max": controller._risk.get("breakout_bar_size_atr_max", 1.6),
        "min_volume_ratio_15m": controller._risk.get("min_volume_ratio_15m", 1.2),
        "range_enabled": controller._risk.get("range_enabled", False),
        "overheat_funding_abs": controller._risk.get("overheat_funding_abs", 0.0008),
        "overheat_long_short_ratio_cap": controller._risk.get("overheat_long_short_ratio_cap", 1.8),
        "overheat_long_short_ratio_floor": controller._risk.get("overheat_long_short_ratio_floor", 0.56),
    }
    config_summary["risk_runtime"] = {
        "daily_loss_used_pct": float(controller._risk.get("daily_loss_used_pct") or 0.0),
        "dd_used_pct": float(controller._risk.get("dd_used_pct") or 0.0),
        "lose_streak": int(controller._risk.get("lose_streak") or 0),
        "cooldown_until": controller._risk.get("cooldown_until"),
        "recent_blocks": dict(controller._risk.get("recent_blocks") or {}),
        "last_auto_risk_reason": controller._risk.get("last_auto_risk_reason"),
        "last_auto_risk_at": controller._risk.get("last_auto_risk_at"),
        "last_strategy_block_reason": controller._risk.get("last_strategy_block_reason"),
        "last_alpha_id": controller._risk.get("last_alpha_id"),
        "last_entry_family": controller._risk.get("last_entry_family"),
        "last_regime": controller._risk.get("last_regime"),
        "overheat_state": dict(controller._risk.get("overheat_state") or {}),
    }
    return config_summary


def build_status_snapshot(controller: RuntimeController) -> dict[str, Any]:
    state = controller.state_store.get()
    live_available_usdt, live_wallet_usdt, live_balance_source = controller._fetch_live_usdt_balance()
    freshness = controller._freshness_snapshot()
    gate = controller._gate_snapshot()
    controller._update_stale_transitions()
    live_trading_enabled = _live_trading_enabled(controller)
    positions_payload = _positions_payload(controller)
    _symbols, _mapping, effective_margin, leverage, expected_notional = (
        controller._runtime_budget_context()
    )
    available_usdt = live_available_usdt if live_available_usdt is not None else effective_margin
    wallet_usdt = live_wallet_usdt if live_wallet_usdt is not None else effective_margin
    config = controller._public_risk_config()

    return {
        "profile": controller.cfg.profile,
        "mode": controller.cfg.mode,
        "env": controller.cfg.env,
        "live_trading_enabled": bool(live_trading_enabled),
        "runtime_identity": {
            "profile": controller.cfg.profile,
            "mode": controller.cfg.mode,
            "env": controller.cfg.env,
            "live_trading_enabled": bool(live_trading_enabled),
            "surface_label": "실거래 활성" if live_trading_enabled else "모의/테스트 또는 비실거래",
        },
        "dry_run": controller.cfg.mode == "shadow",
        "dry_run_strict": False,
        "state_uncertain": bool(controller._state_uncertain),
        "state_uncertain_reason": controller._state_uncertain_reason,
        "startup_reconcile_ok": controller._startup_reconcile_ok,
        "last_reconcile_at": state.last_reconcile_at,
        "last_shutdown_marker": controller._last_shutdown_marker,
        "recovery_required": bool(controller._recovery_required),
        "recovery_reason": controller._recovery_reason,
        "dirty_restart_detected": bool(controller._dirty_restart_detected),
        "single_instance_lock_active": bool(controller._runtime_lock_active),
        "user_ws_stale": bool(freshness["user_ws_stale"]),
        "market_data_stale": bool(freshness["market_data_stale"]),
        "engine_state": {"state": state.status, "updated_at": state.last_transition_at},
        "risk_config": controller._public_risk_config(),
        "config": config,
        "config_summary": _config_summary_payload(controller),
        "scheduler": {
            "tick_sec": float(controller.scheduler.tick_seconds),
            "running": bool(controller._running),
            **dict(controller._last_cycle),
        },
        "watchdog": dict(controller._watchdog_state),
        "capital_snapshot": {
            "symbol": controller.cfg.behavior.exchange.default_symbol,
            "available_usdt": available_usdt,
            "budget_usdt": effective_margin,
            "used_margin": 0.0,
            "leverage": leverage,
            "notional_usdt": expected_notional,
            "mark_price": 0.0,
            "est_qty": 0.0,
            "blocked": not controller.ops.can_open_new_entries(),
            "block_reason": (
                controller._risk.get("last_strategy_block_reason")
                or controller._risk.get("last_auto_risk_reason")
                or ("ops_paused" if not controller.ops.can_open_new_entries() else None)
            ),
        },
        "binance": {
            "enabled_symbols": list(
                controller._risk.get("universe_symbols")
                or [controller.cfg.behavior.exchange.default_symbol]
            ),
            "positions": positions_payload,
            "usdt_balance": {
                "wallet": wallet_usdt,
                "available": available_usdt,
                "source": live_balance_source,
            },
            "startup_error": controller._state_uncertain_reason if controller.cfg.mode == "live" else None,
            "private_error": controller._last_balance_error,
            "private_error_detail": controller._last_balance_error_detail,
        },
        "pnl": {
            "daily_pnl_pct": float(controller._risk.get("daily_realized_pct") or 0.0) * 100.0,
            "drawdown_pct": float(controller._risk.get("dd_used_pct") or 0.0) * 100.0,
            "lose_streak": int(controller._risk.get("lose_streak") or 0),
            "cooldown_until": controller._risk.get("cooldown_until"),
            "daily_realized_pnl": float(controller._risk.get("daily_realized_pnl") or 0.0),
            "daily_loss_used_pct": float(controller._risk.get("daily_loss_used_pct") or 0.0),
            "recent_blocks": dict(controller._risk.get("recent_blocks") or {}),
            "last_strategy_block_reason": controller._risk.get("last_strategy_block_reason"),
            "last_alpha_id": controller._risk.get("last_alpha_id"),
            "last_entry_family": controller._risk.get("last_entry_family"),
            "last_regime": controller._risk.get("last_regime"),
            "overheat_state": dict(controller._risk.get("overheat_state") or {}),
            "last_auto_risk_reason": controller._risk.get("last_auto_risk_reason"),
        },
        "live_readiness": controller._live_readiness_snapshot(
            live_balance_source=live_balance_source,
            private_error=controller._last_balance_error,
        ),
        "user_stream": {
            "started": bool(controller._user_stream_started),
            "started_at": controller._user_stream_started_at,
            "last_event_at": controller._user_stream_last_event_at,
            "last_private_ok_at": controller._last_private_stream_ok_at,
            "last_disconnect_at": controller._user_stream_last_disconnect_at,
            "last_error": controller._user_stream_last_error,
            "age_sec": freshness["user_ws_age_sec"],
            "stale": freshness["user_ws_stale"],
        },
        "market_data": {
            "last_market_data_at": freshness["last_market_data_at"],
            "age_sec": freshness["market_data_age_sec"],
            "stale": freshness["market_data_stale"],
            "observer_stale": freshness["market_data_observer_stale"],
            "source_stale": freshness["market_data_source_stale"],
            "last_source_ok_at": freshness["last_market_data_source_ok_at"],
            "last_source_fail_at": freshness["last_market_data_source_fail_at"],
            "source_age_sec": freshness["market_data_source_age_sec"],
            "source_error": freshness["last_market_data_source_error"],
            "symbol_count": controller._market_data_state.get("last_market_symbol_count"),
        },
        "submission_recovery": gate["submission_recovery"],
        "boot_recovery": dict(controller._boot_recovery),
        "health": {
            "ready": bool(gate["ready"]),
            "single_instance_ok": bool(gate["single_instance_ok"]),
            "private_auth_ok": bool(gate["private_auth_ok"]),
            "submission_recovery_ok": bool(gate["submission_recovery_ok"]),
        },
        "last_error": controller._last_cycle.get("last_error"),
    }


def build_healthz_snapshot(controller: RuntimeController) -> dict[str, Any]:
    gate = controller._gate_snapshot()
    freshness = gate["freshness"]
    return {
        "ok": True,
        "live": True,
        "mode": controller.cfg.mode,
        "profile": controller.cfg.profile,
        "env": controller.cfg.env,
        "ready": bool(gate["ready"]),
        "state_uncertain": bool(gate["state_uncertain"]),
        "recovery_required": bool(gate["recovery_required"]),
        "submission_recovery_ok": bool(gate["submission_recovery_ok"]),
        "safe_mode": bool(gate["safe_mode"]),
        "paused": bool(gate["paused"]),
        "startup_reconcile_ok": controller._startup_reconcile_ok,
        "single_instance_ok": bool(gate["single_instance_ok"]),
        "user_ws_stale": bool(freshness["user_ws_stale"]),
        "market_data_stale": bool(freshness["market_data_stale"]),
    }


def build_readyz_snapshot(controller: RuntimeController) -> dict[str, Any]:
    gate = controller._gate_snapshot(probe_private_rest=True)
    freshness = gate["freshness"]
    return {
        "ready": bool(gate["ready"]),
        "mode": controller.cfg.mode,
        "profile": controller.cfg.profile,
        "env": controller.cfg.env,
        "single_instance_ok": bool(gate["single_instance_ok"]),
        "state_uncertain": bool(gate["state_uncertain"]),
        "state_uncertain_reason": controller._state_uncertain_reason,
        "recovery_required": bool(gate["recovery_required"]),
        "recovery_reason": controller._recovery_reason,
        "submission_recovery_ok": bool(gate["submission_recovery_ok"]),
        "submission_recovery": gate["submission_recovery"],
        "bracket_recovery_ok": bool(gate["bracket_recovery_ok"]),
        "bracket_recovery": gate["bracket_recovery"],
        "startup_reconcile_ok": controller._startup_reconcile_ok,
        "last_reconcile_at": freshness["last_reconcile_at"],
        "last_reconcile_age_sec": freshness["reconcile_age_sec"],
        "last_reconcile_max_age_sec": freshness["reconcile_max_age_sec"],
        "user_ws_stale": bool(freshness["user_ws_stale"]),
        "last_user_ws_event_at": freshness["last_user_ws_event_at"],
        "last_private_stream_ok_at": freshness["last_private_stream_ok_at"],
        "user_ws_age_sec": freshness["user_ws_age_sec"],
        "user_ws_stale_sec": freshness["user_ws_stale_sec"],
        "market_data_stale": bool(freshness["market_data_stale"]),
        "market_data_observer_stale": bool(freshness["market_data_observer_stale"]),
        "market_data_source_stale": bool(freshness["market_data_source_stale"]),
        "market_data_source_error": freshness["last_market_data_source_error"],
        "last_market_data_at": freshness["last_market_data_at"],
        "last_market_data_source_ok_at": freshness["last_market_data_source_ok_at"],
        "market_data_age_sec": freshness["market_data_age_sec"],
        "market_data_source_age_sec": freshness["market_data_source_age_sec"],
        "market_data_stale_sec": freshness["market_data_stale_sec"],
        "safe_mode": bool(gate["safe_mode"]),
        "paused": bool(gate["paused"]),
        "private_auth_ok": bool(gate["private_auth_ok"]),
        "private_error": controller._last_balance_error,
        "private_error_detail": controller._last_balance_error_detail,
    }
