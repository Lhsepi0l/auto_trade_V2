from __future__ import annotations

from typing import Any


def _portfolio_research_gate(
    *,
    years: int,
    total_net_profit: float,
    profit_factor: float | None,
    max_drawdown_pct: float,
    fee_to_trade_gross_pct: float | None,
) -> dict[str, Any]:
    if int(years) >= 3:
        net_threshold = 18.0
    elif int(years) <= 1:
        net_threshold = 12.0
    else:
        net_threshold = 15.0
    pf_threshold = 1.8
    max_drawdown_threshold = 18.0
    fee_to_trade_gross_threshold = 60.0
    passed = (
        float(total_net_profit) >= net_threshold
        and (profit_factor is not None and float(profit_factor) >= pf_threshold)
        and float(max_drawdown_pct) <= max_drawdown_threshold
        and (
            fee_to_trade_gross_pct is not None
            and float(fee_to_trade_gross_pct) <= fee_to_trade_gross_threshold
        )
    )
    return {
        "track": "profit-max-research",
        "verdict": "GO" if passed else "NO-GO",
        "thresholds": {
            "net_profit_usdt": net_threshold,
            "profit_factor": pf_threshold,
            "max_drawdown_pct": max_drawdown_threshold,
            "fee_to_trade_gross_pct": fee_to_trade_gross_threshold,
        },
    }
