from __future__ import annotations

from v2.config.loader import EffectiveConfig

LOCAL_BACKTEST_INITIAL_CAPITAL_USDT = 30.0
_VOL_TARGET_STRATEGIES = frozenset(
    {
        "ra_2026_alpha_v2",
    }
)
_LEGACY_PORTFOLIO_BACKTEST_STRATEGIES: frozenset[str] = frozenset()


def _locked_local_backtest_initial_capital(_requested: float | None = None) -> float:
    return float(LOCAL_BACKTEST_INITIAL_CAPITAL_USDT)


def _resolve_market_intervals(cfg: EffectiveConfig) -> list[str]:
    intervals = ["10m", "15m", "30m", "1h", "4h"]
    configured = getattr(getattr(cfg.behavior, "exchange", None), "market_intervals", None)
    if isinstance(configured, list) and configured:
        normalized: list[str] = []
        for value in configured:
            interval = str(value).strip()
            if interval:
                normalized.append(interval)
        if normalized:
            intervals = normalized

    if "15m" not in intervals:
        intervals.insert(0, "15m")

    seen: set[str] = set()
    ordered: list[str] = []
    for interval in intervals:
        if interval in seen:
            continue
        seen.add(interval)
        ordered.append(interval)
    return ordered


def _is_vol_target_backtest_strategy(strategy_name: str) -> bool:
    normalized = str(strategy_name).strip()
    return normalized in _VOL_TARGET_STRATEGIES
