from __future__ import annotations

from typing import Any


def build_state_response(*, runtime_state: Any) -> dict[str, Any]:
    return {
        "state": runtime_state.status,
        "updated_at": runtime_state.last_transition_at,
    }


def build_panic_response(
    *,
    runtime_state: Any,
    flatten_result: Any,
) -> dict[str, Any]:
    return {
        "engine_state": build_state_response(runtime_state=runtime_state),
        "panic_result": {
            "ok": True,
            "canceled_orders_ok": True,
            "close_ok": True,
            "errors": [],
            "closed_symbol": flatten_result.symbol,
            "closed_qty": abs(flatten_result.position_amt),
        },
    }


def build_set_value_response(
    *,
    key: str,
    requested_value: str,
    applied_value: Any,
    risk_config: dict[str, Any],
) -> dict[str, Any]:
    return {
        "key": key,
        "requested_value": requested_value,
        "applied_value": applied_value,
        "summary": f"Applied {key}={applied_value}",
        "risk_config": risk_config,
    }


def build_tick_scheduler_response(
    *,
    ok: bool,
    tick_sec: float,
    snapshot: dict[str, Any],
    error: str | None,
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "tick_sec": float(tick_sec),
        "snapshot": dict(snapshot),
        "error": error,
    }


def build_trade_close_response(
    *,
    flatten_result: Any,
) -> dict[str, Any]:
    return {
        "symbol": flatten_result.symbol,
        "detail": {
            "open_regular_orders": flatten_result.open_regular_orders,
            "open_algo_orders": flatten_result.open_algo_orders,
            "position_amt": flatten_result.position_amt,
            "paused": flatten_result.paused,
            "safe_mode": flatten_result.safe_mode,
        },
    }


def build_trade_close_all_response(*, results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"symbol": "ALL", "detail": {"results": results}}


def build_clear_cooldown_response(
    *,
    day: str,
    risk_config: dict[str, Any],
) -> dict[str, Any]:
    return {
        "day": day,
        "daily_realized_pnl": float(risk_config.get("daily_realized_pnl") or 0.0),
        "equity_peak": float(risk_config.get("runtime_equity_peak_usdt") or 0.0),
        "daily_pnl_pct": float(risk_config.get("daily_realized_pct") or 0.0) * 100.0,
        "drawdown_pct": float(risk_config.get("dd_used_pct") or 0.0) * 100.0,
        "lose_streak": int(risk_config.get("lose_streak") or 0),
        "cooldown_until": risk_config.get("cooldown_until"),
        "last_block_reason": risk_config.get("last_block_reason"),
        "last_strategy_block_reason": risk_config.get("last_strategy_block_reason"),
        "last_auto_risk_reason": risk_config.get("last_auto_risk_reason"),
    }
