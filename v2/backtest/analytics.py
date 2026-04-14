from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from v2.backtest.common import _to_float


@dataclass
class _OpenTrade:
    symbol: str
    side: str
    entry_price: float
    quantity: float
    initial_quantity: float
    entry_fee: float
    entry_notional: float
    margin_used: float
    max_loss_cap: float | None
    tp: float | None
    sl: float | None
    initial_risk_abs: float | None
    time_stop_bars: int | None = None
    progress_check_bars: int = 0
    progress_min_mfe_r: float = 0.0
    progress_extend_trigger_r: float = 0.0
    progress_extend_bars: int = 0
    reverse_exit_min_r: float = 0.0
    allow_reverse_exit: bool = True
    effective_leverage: float | None = None
    regime: str | None = None
    entry_tick: int = 0
    entry_time_ms: int = 0
    stop_exit_cooldown_bars: int = 0
    loss_streak_trigger: int = 0
    loss_streak_cooldown_bars: int = 0
    profit_exit_cooldown_bars: int = 0
    alpha_id: str | None = None
    entry_family: str | None = None
    entry_tier: str | None = None
    regime_lost_exit_required: bool = False
    tp_partial_ratio: float = 0.0
    tp_partial_price: float | None = None
    tp_partial_at_r: float = 0.0
    break_even_move_r: float = 0.0
    runner_exit_mode: str | None = None
    runner_trailing_atr_mult: float = 0.0
    stalled_trend_timeout_bars: int | None = None
    stalled_volume_ratio_floor: float = 0.0
    entry_quality_score_v2: float = 0.0
    entry_regime_strength: float = 0.0
    entry_bias_strength: float = 0.0
    quality_exit_applied: bool = False
    selective_extension_proof_bars: int = 0
    selective_extension_min_mfe_r: float = 0.0
    selective_extension_min_regime_strength: float = 0.0
    selective_extension_min_bias_strength: float = 0.0
    selective_extension_min_quality_score_v2: float = 0.0
    selective_extension_time_stop_bars: int = 0
    selective_extension_take_profit_r: float = 0.0
    selective_extension_move_stop_to_be_at_r: float = 0.0
    selective_extension_activated: bool = False
    selective_extension_activation_tick: int | None = None
    selective_extension_tp_applied: bool = False
    selective_extension_protection_applied: bool = False
    partial_taken: bool = False
    runner_stop: float | None = None
    peak_price: float | None = None
    trough_price: float | None = None
    realized_gross_pnl: float = 0.0
    realized_exit_fees: float = 0.0
    realized_funding_pnl: float = 0.0


@dataclass(frozen=True)
class _BacktestExecutionModel:
    fee_bps: float = 0.0
    slippage_bps: float = 0.0
    funding_bps_per_8h: float = 0.0

    def _slippage_ratio(self) -> float:
        return max(float(self.slippage_bps), 0.0) / 10000.0

    def _fee_ratio(self) -> float:
        return max(float(self.fee_bps), 0.0) / 10000.0

    def filled_entry_price(self, *, side: str, raw_price: float) -> float:
        slip = self._slippage_ratio()
        if side == "BUY":
            return float(raw_price) * (1.0 + slip)
        return float(raw_price) * (1.0 - slip)

    def filled_exit_price(self, *, side: str, raw_price: float) -> float:
        slip = self._slippage_ratio()
        if side == "BUY":
            return float(raw_price) * (1.0 - slip)
        return float(raw_price) * (1.0 + slip)

    def fee(self, *, notional: float) -> float:
        return max(float(notional), 0.0) * self._fee_ratio()

    def funding_pnl(
        self,
        *,
        side: str,
        entry_time_ms: int,
        exit_time_ms: int,
        notional: float,
    ) -> float:
        interval_ms = 8 * 60 * 60 * 1000
        held_ms = max(int(exit_time_ms) - int(entry_time_ms), 0)
        periods = held_ms // interval_ms
        if periods <= 0:
            return 0.0
        rate = max(float(self.funding_bps_per_8h), 0.0) / 10000.0
        funding_amount = max(float(notional), 0.0) * rate * float(periods)
        if side == "BUY":
            return -funding_amount
        return funding_amount


def _calc_max_drawdown_pct(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        if value > peak:
            peak = value
            continue
        if peak <= 0:
            continue
        drawdown = (value - peak) / peak
        if drawdown < worst:
            worst = drawdown
    return abs(worst) * 100.0


def _sort_trade_events_for_stats(trade_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        trade_events,
        key=lambda item: (
            int(item.get("exit_time_ms") or 0),
            int(item.get("entry_time_ms") or 0),
            int(item.get("exit_tick") or 0),
            int(item.get("entry_tick") or 0),
        ),
    )


def _calc_trade_event_drawdown_pct(
    *, trade_events: list[dict[str, Any]], initial_equity: float
) -> float:
    equity = float(initial_equity)
    equity_curve = [equity]
    for item in _sort_trade_events_for_stats(trade_events):
        equity += float(item.get("pnl") or 0.0)
        equity_curve.append(equity)
    return _calc_max_drawdown_pct(equity_curve)


def _top_reason_counts(source: Counter[str], *, limit: int = 5) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for reason, count in sorted(source.items(), key=lambda item: (-item[1], item[0]))[
        : max(int(limit), 1)
    ]:
        items.append({"reason": str(reason), "count": int(count)})
    return items


def _summarize_alpha_stats(
    *,
    trade_events: list[dict[str, Any]],
    alpha_block_distribution: dict[str, Counter[str]],
    initial_capital: float,
) -> dict[str, dict[str, Any]]:
    alpha_ids: set[str] = set(alpha_block_distribution.keys())
    alpha_trade_events: dict[str, list[dict[str, Any]]] = {}
    for item in trade_events:
        alpha_id = str(item.get("alpha_id") or "").strip()
        if not alpha_id:
            continue
        alpha_ids.add(alpha_id)
        alpha_trade_events.setdefault(alpha_id, []).append(dict(item))

    payload: dict[str, dict[str, Any]] = {}
    for alpha_id in sorted(alpha_ids):
        events = _sort_trade_events_for_stats(alpha_trade_events.get(alpha_id, []))
        gross_profit = sum(max(0.0, float(item.get("pnl") or 0.0)) for item in events)
        gross_loss = abs(sum(min(0.0, float(item.get("pnl") or 0.0)) for item in events))
        net_profit = sum(float(item.get("pnl") or 0.0) for item in events)
        trades = len(events)
        wins = sum(1 for item in events if float(item.get("pnl") or 0.0) > 0.0)
        losses = sum(1 for item in events if float(item.get("pnl") or 0.0) < 0.0)
        profit_factor = None if gross_loss <= 0.0 else round(gross_profit / gross_loss, 6)
        payload[alpha_id] = {
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "net_profit": round(net_profit, 6),
            "gross_profit": round(gross_profit, 6),
            "gross_loss": round(gross_loss, 6),
            "profit_factor": profit_factor,
            "max_drawdown_pct": round(
                _calc_trade_event_drawdown_pct(
                    trade_events=events,
                    initial_equity=float(initial_capital),
                ),
                6,
            ),
            "block_top": _top_reason_counts(alpha_block_distribution.get(alpha_id, Counter())),
        }
    return payload


def _risk_pct_from_levels(
    *,
    signal_side: str,
    entry_fill: float,
    stop_loss: float | None,
) -> float:
    if stop_loss is None or stop_loss <= 0.0 or entry_fill <= 0.0:
        return 0.0
    if signal_side == "BUY":
        return max((entry_fill - float(stop_loss)) / entry_fill, 0.0)
    return max((float(stop_loss) - entry_fill) / entry_fill, 0.0)


def _reward_pct_from_levels(
    *,
    signal_side: str,
    entry_fill: float,
    take_profit: float | None,
) -> float:
    if take_profit is None or take_profit <= 0.0 or entry_fill <= 0.0:
        return 0.0
    if signal_side == "BUY":
        return max((float(take_profit) - entry_fill) / entry_fill, 0.0)
    return max((entry_fill - float(take_profit)) / entry_fill, 0.0)


def _entry_reward_and_risk_pct(
    *,
    signal_side: str,
    entry_fill: float,
    sl_tp: dict[str, Any] | None,
    execution_hints: dict[str, Any] | None,
) -> tuple[float, float]:
    stop_loss = _to_float((sl_tp or {}).get("stop_loss")) if isinstance(sl_tp, dict) else None
    risk_pct = _risk_pct_from_levels(
        signal_side=signal_side,
        entry_fill=float(entry_fill),
        stop_loss=stop_loss,
    )
    reward_reference_r = (
        _to_float(execution_hints.get("reward_risk_reference_r"))
        if isinstance(execution_hints, dict)
        else None
    )
    if reward_reference_r is not None and reward_reference_r > 0.0 and risk_pct > 0.0:
        return float(reward_reference_r) * risk_pct, risk_pct

    take_profit_final = (
        _to_float((sl_tp or {}).get("take_profit_final")) if isinstance(sl_tp, dict) else None
    )
    if take_profit_final is not None and take_profit_final > 0.0:
        return (
            _reward_pct_from_levels(
                signal_side=signal_side,
                entry_fill=float(entry_fill),
                take_profit=float(take_profit_final),
            ),
            risk_pct,
        )

    take_profit = _to_float((sl_tp or {}).get("take_profit")) if isinstance(sl_tp, dict) else None
    return (
        _reward_pct_from_levels(
            signal_side=signal_side,
            entry_fill=float(entry_fill),
            take_profit=take_profit,
        ),
        risk_pct,
    )
