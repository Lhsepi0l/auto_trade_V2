from __future__ import annotations

from typing import Any

JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
JSONPayload = dict[str, JSONValue]

PRESETS: list[str] = ["conservative", "normal", "aggressive"]
PROFILE_KEYS: list[str] = ["recovery_safe", "balanced_20x", "aggressive_50x"]


def build_profile_payload(name: str, budget_usdt: float | None) -> JSONPayload:
    profiles: dict[str, JSONPayload] = {
        "recovery_safe": {
            "capital_mode": "MARGIN_BUDGET_USDT",
            "margin_use_pct": "0.8",
            "max_leverage": "20",
            "max_exposure_pct": "0.5",
            "max_notional_pct": "300",
            "per_trade_risk_pct": "15",
            "score_conf_threshold": "0.3",
            "score_gap_threshold": "0.15",
            "daily_loss_limit_pct": "-0.03",
            "dd_limit_pct": "-0.12",
            "cooldown_hours": "6",
        },
        "balanced_20x": {
            "capital_mode": "MARGIN_BUDGET_USDT",
            "margin_use_pct": "0.9",
            "max_leverage": "20",
            "max_exposure_pct": "null",
            "max_notional_pct": "1000",
            "per_trade_risk_pct": "50",
            "score_conf_threshold": "0.2",
            "score_gap_threshold": "0.1",
            "daily_loss_limit_pct": "-0.05",
            "dd_limit_pct": "-0.2",
            "cooldown_hours": "2",
        },
        "aggressive_50x": {
            "capital_mode": "MARGIN_BUDGET_USDT",
            "margin_use_pct": "0.9",
            "max_leverage": "50",
            "max_exposure_pct": "null",
            "max_notional_pct": "2000",
            "per_trade_risk_pct": "100",
            "score_conf_threshold": "0.1",
            "score_gap_threshold": "0.1",
            "daily_loss_limit_pct": "-0.15",
            "dd_limit_pct": "-0.35",
            "cooldown_hours": "0",
        },
    }
    payload = dict(profiles[name])
    if budget_usdt is not None:
        payload["margin_budget_usdt"] = str(float(budget_usdt))
    return payload


def profile_summary_lines(name: str, risk: dict[str, Any]) -> list[str]:
    return [
        f"profile={name}",
        f"max_leverage={risk.get('max_leverage')}",
        f"margin_budget_usdt={risk.get('margin_budget_usdt')}",
        f"max_notional_pct={risk.get('max_notional_pct')}",
        f"per_trade_risk_pct={risk.get('per_trade_risk_pct')}",
        f"score_conf_threshold={risk.get('score_conf_threshold')}",
        f"daily_loss_limit_pct={risk.get('daily_loss_limit_pct')}",
    ]
