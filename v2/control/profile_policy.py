from __future__ import annotations

from typing import Any

CANONICAL_LIVE_PROFILE = "ra_2026_alpha_v2_expansion_verified_q070"
PRIVATE_REST_UNSAFE_ERRORS = {
    "rest_client_unavailable",
    "balance_fetch_timeout",
    "balance_auth_failed",
    "balance_rate_limited",
    "balance_fetch_failed",
    "balance_payload_invalid",
    "usdt_asset_missing",
}
_RUNTIME_RISK_KEY_ALIASES = {
    "per_trade_risk_pct": "risk_per_trade_pct",
    "trend_enter_adx_4h": "trend_adx_min_4h",
}


def normalize_runtime_risk_key(key: Any) -> str:
    text = str(key or "").strip()
    return _RUNTIME_RISK_KEY_ALIASES.get(text, text)


def normalize_runtime_risk_config(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, Any] = {}
    for raw_key, value in payload.items():
        key = normalize_runtime_risk_key(raw_key)
        if not key:
            continue
        normalized[key] = value
    return normalized


def serialize_runtime_risk_config(payload: Any) -> dict[str, Any]:
    normalized = normalize_runtime_risk_config(payload)
    if "risk_per_trade_pct" in normalized:
        normalized["per_trade_risk_pct"] = normalized["risk_per_trade_pct"]
    return normalized


def profile_runtime_risk_overrides(controller: Any, *, sched_sec: int) -> dict[str, Any]:
    profile = str(controller.cfg.profile or "").strip().lower()
    profile_overrides: dict[str, dict[str, Any]] = {
        CANONICAL_LIVE_PROFILE: {
            "max_leverage": 5.0,
            "daily_loss_limit_pct": -0.015,
            "dd_limit_pct": -0.12,
            "risk_score_min": 0.60,
            "spread_max_pct": 0.35,
            "margin_use_pct": 0.10,
            "universe_symbols": ["BTCUSDT"],
            "enable_watchdog": True,
            "watchdog_interval_sec": max(5, min(int(sched_sec), 15)),
            "lose_streak_n": 2,
            "cooldown_hours": 4,
        },
    }
    return dict(profile_overrides.get(profile, {}))


def build_live_readiness_snapshot(
    controller: Any,
    *,
    live_balance_source: str | None = None,
    private_error: str | None = None,
) -> dict[str, Any]:
    from v2.control import api as api_module

    _normalize_pct = api_module._normalize_pct
    _to_bool = api_module._to_bool
    _to_float = api_module._to_float

    gate = controller._gate_snapshot(probe_private_rest=True)
    strategies = [
        str(entry.name).strip()
        for entry in controller.cfg.behavior.strategies
        if bool(getattr(entry, "enabled", False))
    ]
    symbols = controller._runtime_symbols()
    max_leverage = max(
        1.0,
        _to_float(
            controller._risk.get("max_leverage"),
            default=float(controller.cfg.behavior.risk.max_leverage),
        ),
    )
    margin_use_pct = max(0.0, _to_float(controller._risk.get("margin_use_pct"), default=1.0))
    daily_limit = _normalize_pct(controller._risk.get("daily_loss_limit_pct"), default=0.02)
    dd_limit = _normalize_pct(controller._risk.get("dd_limit_pct"), default=0.15)
    tick_sec = max(
        1.0,
        _to_float(
            controller._risk.get("scheduler_tick_sec"),
            default=float(controller.scheduler.tick_seconds),
        ),
    )
    profile_name = str(controller.cfg.profile or "").strip()
    mode_name = str(controller.cfg.mode or "").strip()
    checks: dict[str, dict[str, Any]] = {}
    readiness_specs: dict[str, dict[str, Any]] = {
        CANONICAL_LIVE_PROFILE: {
            "target": "alpha_expansion_verified_q070",
            "strategy": ["ra_2026_alpha_v2"],
            "symbols": ["BTCUSDT"],
            "margin_pass": 0.10,
            "margin_warn": 0.15,
            "leverage_pass": 5.0,
            "leverage_warn": 8.0,
            "daily_pass": 0.015,
            "daily_warn": 0.02,
            "dd_pass": 0.12,
            "dd_warn": 0.15,
        },
    }
    readiness_spec = readiness_specs.get(
        profile_name,
        readiness_specs[CANONICAL_LIVE_PROFILE],
    )

    def _set_check(name: str, *, status: str, detail: Any) -> None:
        checks[name] = {"status": status, "detail": detail}

    _set_check(
        "profile",
        status="pass" if profile_name in readiness_specs else "warn",
        detail=profile_name,
    )
    _set_check(
        "mode",
        status="pass" if mode_name == "live" else "warn",
        detail=mode_name,
    )
    _set_check(
        "single_instance",
        status="pass" if gate["single_instance_ok"] else "fail",
        detail=bool(gate["single_instance_ok"]),
    )
    _set_check(
        "dirty_recovery",
        status="pass" if not gate["recovery_required"] else "fail",
        detail={
            "dirty_restart_detected": bool(controller._dirty_restart_detected),
            "recovery_required": bool(gate["recovery_required"]),
            "recovery_reason": controller._recovery_reason,
        },
    )
    _set_check(
        "state_uncertain",
        status="pass" if not gate["state_uncertain"] else "fail",
        detail={
            "state_uncertain": bool(gate["state_uncertain"]),
            "reason": controller._state_uncertain_reason,
        },
    )
    _set_check(
        "strategy",
        status="pass" if strategies == readiness_spec["strategy"] else "fail",
        detail=strategies,
    )
    _set_check(
        "symbols",
        status="pass" if symbols == readiness_spec["symbols"] else "warn",
        detail=symbols,
    )
    _set_check(
        "margin_use_pct",
        status="pass"
        if margin_use_pct <= float(readiness_spec["margin_pass"])
        else "warn"
        if margin_use_pct <= float(readiness_spec["margin_warn"])
        else "fail",
        detail=round(float(margin_use_pct) * 100.0, 2),
    )
    _set_check(
        "max_leverage",
        status="pass"
        if max_leverage <= float(readiness_spec["leverage_pass"])
        else "warn"
        if max_leverage <= float(readiness_spec["leverage_warn"])
        else "fail",
        detail=float(max_leverage),
    )
    _set_check(
        "daily_loss_limit_pct",
        status="pass"
        if 0.0 < daily_limit <= float(readiness_spec["daily_pass"])
        else "warn"
        if daily_limit <= float(readiness_spec["daily_warn"])
        else "fail",
        detail=round(float(daily_limit) * 100.0, 2),
    )
    _set_check(
        "dd_limit_pct",
        status="pass"
        if 0.0 < dd_limit <= float(readiness_spec["dd_pass"])
        else "warn"
        if dd_limit <= float(readiness_spec["dd_warn"])
        else "fail",
        detail=round(float(dd_limit) * 100.0, 2),
    )
    auto_risk_ok = _to_bool(controller._risk.get("auto_risk_enabled"), default=True) and _to_bool(
        controller._risk.get("auto_safe_mode_on_risk"),
        default=True,
    )
    if mode_name == "live":
        auto_risk_ok = auto_risk_ok and _to_bool(
            controller._risk.get("auto_flatten_on_risk"),
            default=True,
        )
    _set_check(
        "auto_risk_circuit",
        status="pass" if auto_risk_ok else "fail",
        detail={
            "auto_risk_enabled": _to_bool(controller._risk.get("auto_risk_enabled"), default=True),
            "auto_safe_mode_on_risk": _to_bool(
                controller._risk.get("auto_safe_mode_on_risk"),
                default=True,
            ),
            "auto_flatten_on_risk": _to_bool(
                controller._risk.get("auto_flatten_on_risk"),
                default=True,
            ),
        },
    )
    _set_check(
        "scheduler_tick_sec",
        status="pass" if 15.0 <= tick_sec <= 60.0 else "warn",
        detail=float(tick_sec),
    )
    _set_check(
        "startup_reconcile",
        status="pass" if gate["last_reconcile_ok"] else "fail",
        detail={
            "startup_reconcile_ok": controller._startup_reconcile_ok,
            "last_reconcile_at": gate["freshness"]["last_reconcile_at"],
            "age_sec": gate["freshness"]["reconcile_age_sec"],
            "max_age_sec": gate["freshness"]["reconcile_max_age_sec"],
        },
    )
    _set_check(
        "submit_recovery",
        status="pass" if gate["submission_recovery_ok"] else "fail",
        detail=gate["submission_recovery"],
    )
    _set_check(
        "bracket_recovery",
        status="pass" if gate["bracket_recovery_ok"] else "fail",
        detail=gate["bracket_recovery"],
    )
    _set_check(
        "user_ws_freshness",
        status="pass" if gate["user_ws_ok"] else "fail",
        detail={
            "started": bool(controller._user_stream_started),
            "last_event_at": gate["freshness"]["last_user_ws_event_at"],
            "last_private_ok_at": gate["freshness"]["last_private_stream_ok_at"],
            "age_sec": gate["freshness"]["user_ws_age_sec"],
            "stale": gate["freshness"]["user_ws_stale"],
            "stale_sec": gate["freshness"]["user_ws_stale_sec"],
        },
    )
    _set_check(
        "market_data_freshness",
        status="pass" if gate["market_data_ok"] else "fail",
        detail={
            "last_market_data_at": gate["freshness"]["last_market_data_at"],
            "last_market_data_source_ok_at": gate["freshness"]["last_market_data_source_ok_at"],
            "age_sec": gate["freshness"]["market_data_age_sec"],
            "source_age_sec": gate["freshness"]["market_data_source_age_sec"],
            "stale": gate["freshness"]["market_data_stale"],
            "observer_stale": gate["freshness"]["market_data_observer_stale"],
            "source_stale": gate["freshness"]["market_data_source_stale"],
            "source_error": gate["freshness"]["last_market_data_source_error"],
            "stale_sec": gate["freshness"]["market_data_stale_sec"],
        },
    )
    _set_check(
        "ops_mode",
        status="pass" if (not gate["paused"] and not gate["safe_mode"]) else "fail",
        detail={"paused": gate["paused"], "safe_mode": gate["safe_mode"]},
    )

    balance_status = "pass"
    balance_detail: Any = live_balance_source or "fallback"
    if mode_name != "live":
        balance_status = "warn"
    elif private_error:
        balance_status = "fail"
        balance_detail = {
            "error": private_error,
            "detail": controller._last_balance_error_detail,
            "source": live_balance_source or "fallback",
        }
    elif not gate["private_auth_ok"]:
        balance_status = "fail"
        balance_detail = {
            "error": controller._last_balance_error or "private_auth_unavailable",
            "detail": controller._last_balance_error_detail,
            "source": live_balance_source or "fallback",
        }
    _set_check(
        "exchange_private",
        status=balance_status,
        detail=balance_detail,
    )

    statuses = [row["status"] for row in checks.values()]
    overall = "ready"
    if "fail" in statuses:
        overall = "blocked"
    elif "warn" in statuses:
        overall = "caution"

    return {
        "target": str(readiness_spec["target"]),
        "overall": overall,
        "ready": bool(gate["ready"]),
        "profile": profile_name,
        "mode": mode_name,
        "enabled_symbols": symbols,
        "checks": checks,
    }
