from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from v2.common.operator_labels import humanize_action_token, humanize_reason_token


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _state_label(raw_state: str) -> str:
    return {
        "RUNNING": "실행중",
        "PAUSED": "일시정지",
        "STOPPED": "중지",
        "KILLED": "강제중지",
    }.get(raw_state, raw_state or "-")


def _build_positions(positions: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, payload in sorted(positions.items()):
        if not isinstance(payload, dict):
            continue
        position_amt = _to_float(payload.get("position_amt"))
        rows.append(
            {
                "symbol": symbol,
                "position_amt": position_amt,
                "position_side": str(
                    payload.get("position_side") or ("LONG" if position_amt > 0 else "SHORT")
                ),
                "entry_price": _to_float(payload.get("entry_price")),
                "unrealized_pnl": _to_float(payload.get("unrealized_pnl")),
            }
        )
    return rows


def build_operator_console_payload(status: dict[str, Any]) -> dict[str, Any]:
    engine = status.get("engine_state", {}) if isinstance(status.get("engine_state"), dict) else {}
    scheduler = status.get("scheduler", {}) if isinstance(status.get("scheduler"), dict) else {}
    capital = (
        status.get("capital_snapshot", {}) if isinstance(status.get("capital_snapshot"), dict) else {}
    )
    risk_config = status.get("risk_config", {}) if isinstance(status.get("risk_config"), dict) else {}
    pnl = status.get("pnl", {}) if isinstance(status.get("pnl"), dict) else {}
    binance = status.get("binance", {}) if isinstance(status.get("binance"), dict) else {}
    live_readiness = (
        status.get("live_readiness", {}) if isinstance(status.get("live_readiness"), dict) else {}
    )
    health = status.get("health", {}) if isinstance(status.get("health"), dict) else {}
    watchdog = status.get("watchdog", {}) if isinstance(status.get("watchdog"), dict) else {}
    submission_recovery = (
        status.get("submission_recovery", {})
        if isinstance(status.get("submission_recovery"), dict)
        else {}
    )
    boot_recovery = (
        status.get("boot_recovery", {}) if isinstance(status.get("boot_recovery"), dict) else {}
    )

    state = str(engine.get("state") or "-")
    last_action = str(scheduler.get("last_action") or "-")
    last_reason = str(scheduler.get("last_decision_reason") or "-")
    last_error = scheduler.get("last_error")
    blocked_reason = (
        capital.get("block_reason")
        or pnl.get("last_strategy_block_reason")
        or pnl.get("last_auto_risk_reason")
    )
    stale_items: list[str] = []
    if bool(status.get("user_ws_stale")):
        stale_items.append("프라이빗 스트림 stale")
    if bool(status.get("market_data_stale")):
        stale_items.append("마켓 데이터 stale")
    if bool(status.get("recovery_required")):
        stale_items.append("복구 필요")
    if bool(status.get("state_uncertain")):
        stale_items.append("상태 불확실")

    balance = (
        binance.get("usdt_balance", {}) if isinstance(binance.get("usdt_balance"), dict) else {}
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "profile": status.get("profile"),
            "mode": status.get("mode"),
            "env": status.get("env"),
            "surface_label": (
                status.get("runtime_identity", {})
                if isinstance(status.get("runtime_identity"), dict)
                else {}
            ).get("surface_label"),
        },
        "engine": {
            "state": state,
            "state_label": _state_label(state),
            "updated_at": engine.get("updated_at"),
            "can_start": state in {"STOPPED", "PAUSED"},
            "can_pause": state == "RUNNING",
            "can_panic": state in {"RUNNING", "PAUSED"},
            "start_label": "재개" if state == "PAUSED" else "시작",
        },
        "health": {
            "ready": bool(health.get("ready")),
            "ready_label": "준비됨" if bool(health.get("ready")) else "미준비",
            "busy": last_reason == "tick_busy",
            "busy_reason": last_reason if last_reason == "tick_busy" else None,
            "busy_reason_label": (
                humanize_reason_token(last_reason) if last_reason == "tick_busy" else None
            ),
            "blocked": bool(capital.get("blocked")) or bool(blocked_reason),
            "blocked_reason": blocked_reason,
            "blocked_reason_label": (
                humanize_reason_token(str(blocked_reason)) if blocked_reason else None
            ),
            "stale": bool(stale_items),
            "stale_items": stale_items,
            "state_uncertain": bool(status.get("state_uncertain")),
            "recovery_required": bool(status.get("recovery_required")),
        },
        "scheduler": {
            "tick_sec": _to_float(scheduler.get("tick_sec")),
            "running": bool(scheduler.get("running")),
            "tick_started_at": scheduler.get("tick_started_at"),
            "tick_finished_at": scheduler.get("tick_finished_at"),
            "last_action": last_action,
            "last_action_label": humanize_action_token(last_action),
            "last_reason": last_reason,
            "last_reason_label": humanize_reason_token(last_reason),
            "last_error": last_error,
            "portfolio_slots": scheduler.get("portfolio_slots"),
            "can_tick": last_reason != "tick_busy",
        },
        "recent_result": {
            "last_action": last_action,
            "last_action_label": humanize_action_token(last_action),
            "last_reason": last_reason,
            "last_reason_label": humanize_reason_token(last_reason),
            "last_error": last_error,
            "blocked_reason": blocked_reason,
            "blocked_reason_label": (
                humanize_reason_token(str(blocked_reason)) if blocked_reason else None
            ),
            "tick_started_at": scheduler.get("tick_started_at"),
            "tick_finished_at": scheduler.get("tick_finished_at"),
            "busy": last_reason == "tick_busy",
            "stale": bool(status.get("user_ws_stale")) or bool(status.get("market_data_stale")),
        },
        "readiness": {
            "ready": bool(live_readiness.get("ready")),
            "summary": str(
                live_readiness.get("summary")
                or live_readiness.get("detail")
                or live_readiness.get("reason")
                or "-"
            ),
            "private_error": binance.get("private_error"),
            "private_error_detail": binance.get("private_error_detail"),
        },
        "recovery": {
            "state_uncertain": bool(status.get("state_uncertain")),
            "state_uncertain_reason": status.get("state_uncertain_reason"),
            "state_uncertain_reason_label": (
                humanize_reason_token(str(status.get("state_uncertain_reason")))
                if status.get("state_uncertain_reason")
                else None
            ),
            "recovery_required": bool(status.get("recovery_required")),
            "recovery_reason": status.get("recovery_reason"),
            "startup_reconcile_ok": status.get("startup_reconcile_ok"),
            "last_reconcile_at": status.get("last_reconcile_at"),
            "submission_recovery_ok": bool(health.get("submission_recovery_ok")),
            "submission_recovery": submission_recovery,
            "boot_recovery": boot_recovery,
            "watchdog": watchdog,
        },
        "controls": {
            "exec_mode_default": str(risk_config.get("exec_mode_default") or "MARKET").upper(),
            "scheduler_tick_sec": _to_float(scheduler.get("tick_sec")),
            "notify_interval_sec": int(_to_float(risk_config.get("notify_interval_sec"), default=30.0)),
        },
        "capital": {
            "available_usdt": _to_float(balance.get("available")),
            "wallet_usdt": _to_float(balance.get("wallet")),
            "budget_usdt": _to_float(capital.get("budget_usdt")),
            "notional_usdt": _to_float(capital.get("notional_usdt")),
            "leverage": _to_float(capital.get("leverage")),
            "source": balance.get("source"),
            "blocked": bool(capital.get("blocked")),
            "block_reason": capital.get("block_reason"),
            "block_reason_label": (
                humanize_reason_token(str(capital.get("block_reason")))
                if capital.get("block_reason")
                else None
            ),
        },
        "positions": _build_positions(
            binance.get("positions", {}) if isinstance(binance.get("positions"), dict) else {}
        ),
        "risk": {
            "daily_pnl_pct": _to_float(pnl.get("daily_pnl_pct")),
            "drawdown_pct": _to_float(pnl.get("drawdown_pct")),
            "lose_streak": int(_to_float(pnl.get("lose_streak"))),
            "cooldown_until": pnl.get("cooldown_until"),
            "last_auto_risk_reason": pnl.get("last_auto_risk_reason"),
            "last_auto_risk_reason_label": (
                humanize_reason_token(str(pnl.get("last_auto_risk_reason")))
                if pnl.get("last_auto_risk_reason")
                else None
            ),
        },
        "risk_forms": {
            "margin_budget": {
                "margin_budget_usdt": _to_float(risk_config.get("margin_budget_usdt"), default=0.0),
                "max_leverage": _to_float(risk_config.get("max_leverage"), default=0.0),
                "margin_use_pct": _to_float(risk_config.get("margin_use_pct"), default=1.0),
            },
            "risk_basic": {
                "max_leverage": _to_float(risk_config.get("max_leverage"), default=0.0),
                "max_exposure_pct": _to_float(risk_config.get("max_exposure_pct"), default=0.0),
                "max_notional_pct": _to_float(risk_config.get("max_notional_pct"), default=0.0),
                "per_trade_risk_pct": _to_float(risk_config.get("per_trade_risk_pct"), default=0.0),
            },
            "risk_advanced": {
                "daily_loss_limit_pct": _to_float(
                    risk_config.get("daily_loss_limit_pct"), default=0.0
                ),
                "dd_limit_pct": _to_float(risk_config.get("dd_limit_pct"), default=0.0),
                "min_hold_minutes": int(
                    _to_float(risk_config.get("min_hold_minutes"), default=0.0)
                ),
                "score_conf_threshold": _to_float(
                    risk_config.get("score_conf_threshold"), default=0.0
                ),
            },
            "notify_interval": {
                "notify_interval_sec": int(
                    _to_float(risk_config.get("notify_interval_sec"), default=30.0)
                ),
            },
        },
        "alpha": {
            "last_alpha_id": pnl.get("last_alpha_id"),
            "last_entry_family": pnl.get("last_entry_family"),
            "last_regime": pnl.get("last_regime"),
            "last_strategy_block_reason": pnl.get("last_strategy_block_reason"),
            "last_strategy_block_reason_label": (
                humanize_reason_token(str(pnl.get("last_strategy_block_reason")))
                if pnl.get("last_strategy_block_reason")
                else None
            ),
            "last_alpha_reject_focus": pnl.get("last_alpha_reject_focus"),
            "last_alpha_reject_metrics": dict(pnl.get("last_alpha_reject_metrics") or {}),
            "last_alpha_blocks": dict(pnl.get("last_alpha_blocks") or {}),
        },
    }
