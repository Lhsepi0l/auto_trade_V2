from __future__ import annotations

from collections import Counter
from typing import Any

from v2.backtest.analytics import (
    _BacktestExecutionModel,
    _calc_max_drawdown_pct,
    _entry_reward_and_risk_pct,
    _OpenTrade,
    _summarize_alpha_stats,
)
from v2.backtest.common import _candidate_from_payload, _to_float, _to_int
from v2.backtest.snapshots import _Kline15m
from v2.kernel import Candidate
from v2.kernel.portfolio import (
    PortfolioRoutingConfig,
    portfolio_bucket_for_symbol,
    route_ranked_candidates,
)
from v2.management import PositionManagementSpec


def _simulate_symbol_metrics(
    *,
    symbol: str,
    rows: list[dict[str, Any]],
    candles_15m: list[_Kline15m],
    initial_capital: float,
    execution_model: _BacktestExecutionModel | None = None,
    fixed_leverage: float | None = None,
    fixed_leverage_margin_use_pct: float = 0.20,
    reverse_min_hold_bars: int = 8,
    reverse_cooldown_bars: int = 6,
    min_expected_edge_over_roundtrip_cost: float = 1.2,
    min_reward_risk_ratio: float = 0.0,
    max_trades_per_day_per_symbol: int = 12,
    daily_loss_limit_pct: float = 0.04,
    equity_floor_pct: float = 0.35,
    max_trade_margin_loss_fraction: float = 0.45,
    min_signal_score: float = 0.35,
    reverse_exit_min_profit_pct: float = 0.004,
    reverse_exit_min_signal_score: float = 0.60,
    default_reverse_exit_min_r: float = 0.0,
    default_risk_per_trade_pct: float = 0.0,
    default_max_effective_leverage: float = 30.0,
    drawdown_scale_start_pct: float = 0.12,
    drawdown_scale_end_pct: float = 0.32,
    drawdown_margin_scale_min: float = 0.35,
    stoploss_streak_trigger: int = 3,
    stoploss_cooldown_bars: int = 20,
    loss_cooldown_bars: int = 0,
    max_peak_drawdown_pct: float | None = None,
) -> dict[str, Any]:
    model = execution_model or _BacktestExecutionModel()
    open_trade: _OpenTrade | None = None
    realized_pnl = 0.0
    trade_events: list[dict[str, Any]] = []
    equity_curve: list[float] = []
    total_fees = 0.0
    total_funding_pnl = 0.0
    gross_trade_pnl_total = 0.0
    last_exit_tick = -(10**9)
    last_exit_cooldown_bars = max(int(reverse_cooldown_bars), 0)
    day_trade_counter: dict[int, int] = {}
    entry_block_counter: Counter[str] = Counter()
    day_ms = 24 * 60 * 60 * 1000
    current_day_key: int | None = None
    day_start_equity = float(initial_capital)
    day_locked = False
    trading_locked = False
    trading_lock_reason: str | None = None
    effective_daily_loss_limit = max(float(daily_loss_limit_pct), 0.0)
    effective_equity_floor_pct = max(float(equity_floor_pct), 0.0)
    effective_margin_loss_fraction = max(0.0, min(float(max_trade_margin_loss_fraction), 1.0))
    effective_min_signal_score = max(float(min_signal_score), 0.0)
    effective_reverse_exit_min_profit_pct = max(float(reverse_exit_min_profit_pct), 0.0)
    effective_reverse_exit_min_signal_score = max(float(reverse_exit_min_signal_score), 0.0)
    effective_default_reverse_exit_min_r = max(float(default_reverse_exit_min_r), 0.0)
    effective_default_risk_per_trade_pct = max(float(default_risk_per_trade_pct), 0.0)
    effective_default_max_effective_leverage = max(float(default_max_effective_leverage), 1.0)
    effective_min_reward_risk_ratio = max(float(min_reward_risk_ratio), 0.0)
    effective_drawdown_scale_start_pct = max(float(drawdown_scale_start_pct), 0.0)
    effective_drawdown_scale_end_pct = max(float(drawdown_scale_end_pct), 0.0)
    effective_drawdown_margin_scale_min = max(
        0.05,
        min(float(drawdown_margin_scale_min), 1.0),
    )
    effective_stoploss_streak_trigger = max(int(stoploss_streak_trigger), 0)
    effective_stoploss_cooldown_bars = max(int(stoploss_cooldown_bars), 0)
    effective_loss_cooldown_bars = max(int(loss_cooldown_bars), 0)
    effective_max_peak_drawdown_pct = (
        max(float(max_peak_drawdown_pct), 0.0)
        if max_peak_drawdown_pct is not None
        else None
    )
    peak_realized_equity = float(initial_capital)
    stoploss_streak = 0
    consecutive_loss_count = 0
    stoploss_cooldown_until_tick = -(10**9)
    loss_cooldown_until_tick = -(10**9)
    loss_streak_cooldown_until_tick = -(10**9)

    def _record_no_candidate_reason(reason: str) -> None:
        normalized = str(reason or "").strip()
        if not normalized:
            return
        if normalized.startswith("no_candidate_multi:"):
            detail = normalized.split(":", 1)[1]
            for token in detail.split(";"):
                item = str(token).strip()
                if not item:
                    continue
                _, _, reason_part = item.partition(":")
                entry_block_counter[str(reason_part or "no_candidate").strip()] += 1
            return
        if normalized != "no_candidate":
            entry_block_counter[normalized] += 1

    def _move_stop_to_break_even(trade: _OpenTrade) -> None:
        if trade.side == "BUY":
            trade.sl = max(float(trade.sl or 0.0), float(trade.entry_price))
            return
        stop_candidates = [float(trade.entry_price)]
        if trade.sl is not None:
            stop_candidates.append(float(trade.sl))
        trade.sl = min(stop_candidates)

    def _apply_selective_extension(
        *,
        trade: _OpenTrade,
        tick: int,
        current_mfe_r: float,
        per_unit_risk: float,
    ) -> None:
        if trade.selective_extension_activated:
            return
        if int(trade.selective_extension_proof_bars) <= 0:
            return
        if (
            int(trade.selective_extension_time_stop_bars) <= 0
            and float(trade.selective_extension_take_profit_r) <= 0.0
            and float(trade.selective_extension_move_stop_to_be_at_r) <= 0.0
        ):
            return
        held_bars = int(tick) - int(trade.entry_tick)
        if held_bars > max(int(trade.selective_extension_proof_bars), 0):
            return
        if float(current_mfe_r) < float(trade.selective_extension_min_mfe_r):
            return
        if float(trade.entry_regime_strength) < float(trade.selective_extension_min_regime_strength):
            return
        if float(trade.entry_bias_strength) < float(trade.selective_extension_min_bias_strength):
            return
        if float(trade.entry_quality_score_v2) < float(trade.selective_extension_min_quality_score_v2):
            return

        trade.selective_extension_activated = True
        trade.selective_extension_activation_tick = int(tick)

        if int(trade.selective_extension_time_stop_bars) > 0:
            if trade.time_stop_bars is None:
                trade.time_stop_bars = int(trade.selective_extension_time_stop_bars)
            else:
                trade.time_stop_bars = max(
                    int(trade.time_stop_bars),
                    int(trade.selective_extension_time_stop_bars),
                )

        if float(trade.selective_extension_take_profit_r) > 0.0 and float(per_unit_risk) > 0.0:
            tp_candidate = (
                float(trade.entry_price)
                + (float(per_unit_risk) * float(trade.selective_extension_take_profit_r))
                if trade.side == "BUY"
                else float(trade.entry_price)
                - (float(per_unit_risk) * float(trade.selective_extension_take_profit_r))
            )
            if trade.side == "BUY":
                if trade.tp is None or float(tp_candidate) > float(trade.tp):
                    trade.tp = float(tp_candidate)
                    trade.selective_extension_tp_applied = True
            elif trade.tp is None or float(tp_candidate) < float(trade.tp):
                trade.tp = float(tp_candidate)
                trade.selective_extension_tp_applied = True

        be_trigger_r = float(trade.selective_extension_move_stop_to_be_at_r)
        if be_trigger_r > 0.0:
            if float(current_mfe_r) >= be_trigger_r:
                _move_stop_to_break_even(trade)
                trade.selective_extension_protection_applied = True
            elif float(trade.break_even_move_r) <= 0.0 or be_trigger_r < float(trade.break_even_move_r):
                trade.break_even_move_r = be_trigger_r

    def _realize_trade_slice(
        *,
        qty_to_close: float,
        candle: _Kline15m,
        exit_price: float,
    ) -> tuple[float, float, float, float, float]:
        if open_trade is None:
            return 0.0, 0.0, 0.0, 0.0, 0.0
        qty = max(min(float(qty_to_close), float(open_trade.quantity)), 0.0)
        if qty <= 0.0:
            return 0.0, 0.0, 0.0, 0.0, 0.0
        exit_fill = model.filled_exit_price(side=open_trade.side, raw_price=float(exit_price))
        gross_pnl = (
            (exit_fill - open_trade.entry_price) * qty
            if open_trade.side == "BUY"
            else (open_trade.entry_price - exit_fill) * qty
        )
        exit_notional = abs(exit_fill * qty)
        exit_fee = model.fee(notional=exit_notional)
        funding_base_notional = float(open_trade.entry_notional)
        if float(open_trade.initial_quantity) > 0.0:
            funding_base_notional *= qty / float(open_trade.initial_quantity)
        funding_pnl = model.funding_pnl(
            side=open_trade.side,
            entry_time_ms=open_trade.entry_time_ms,
            exit_time_ms=candle.close_time_ms,
            notional=funding_base_notional,
        )
        net_pnl = gross_pnl - exit_fee + funding_pnl
        return qty, exit_fill, gross_pnl, exit_fee, net_pnl

    def _close_partial(
        *,
        tick: int,
        candle: _Kline15m,
        exit_price: float,
        qty_to_close: float,
    ) -> None:
        nonlocal open_trade, realized_pnl, total_fees, total_funding_pnl, gross_trade_pnl_total
        if open_trade is None:
            return
        qty, exit_fill, gross_pnl, exit_fee, net_pnl = _realize_trade_slice(
            qty_to_close=qty_to_close,
            candle=candle,
            exit_price=exit_price,
        )
        if qty <= 0.0:
            return
        funding_pnl = net_pnl - gross_pnl + exit_fee
        realized_pnl += net_pnl
        total_fees += exit_fee
        total_funding_pnl += funding_pnl
        gross_trade_pnl_total += gross_pnl
        open_trade.realized_gross_pnl += gross_pnl
        open_trade.realized_exit_fees += exit_fee
        open_trade.realized_funding_pnl += funding_pnl
        open_trade.quantity = max(float(open_trade.quantity) - qty, 0.0)
        open_trade.partial_taken = True
        if open_trade.break_even_move_r > 0.0:
            if open_trade.side == "BUY":
                open_trade.sl = max(float(open_trade.sl or 0.0), float(open_trade.entry_price))
            else:
                stop_candidates = [float(open_trade.entry_price)]
                if open_trade.sl is not None:
                    stop_candidates.append(float(open_trade.sl))
                open_trade.sl = min(stop_candidates)

    def _close_trade(*, tick: int, candle: _Kline15m, exit_price: float, reason: str) -> None:
        nonlocal \
            open_trade, \
            realized_pnl, \
            total_fees, \
            total_funding_pnl, \
            gross_trade_pnl_total, \
            last_exit_tick, \
            last_exit_cooldown_bars, \
            stoploss_streak, \
            consecutive_loss_count, \
            stoploss_cooldown_until_tick, \
            loss_cooldown_until_tick, \
            loss_streak_cooldown_until_tick
        if open_trade is None:
            return
        qty = max(float(open_trade.quantity), 0.0)
        if qty <= 0.0:
            return
        qty, exit_fill, gross_pnl, exit_fee, net_slice = _realize_trade_slice(
            qty_to_close=qty,
            candle=candle,
            exit_price=exit_price,
        )
        funding_pnl = net_slice - gross_pnl + exit_fee
        gross_total = float(open_trade.realized_gross_pnl) + gross_pnl
        exit_fee_total = float(open_trade.realized_exit_fees) + exit_fee
        funding_total = float(open_trade.realized_funding_pnl) + funding_pnl
        partial_net_total = (
            float(open_trade.realized_gross_pnl)
            - float(open_trade.realized_exit_fees)
            + float(open_trade.realized_funding_pnl)
        )
        net_pnl = gross_total - exit_fee_total + funding_total
        if open_trade.max_loss_cap is not None:
            trade_floor = -float(open_trade.max_loss_cap) + float(open_trade.entry_fee)
            if net_pnl < trade_floor:
                net_pnl = trade_floor
        total_fees += exit_fee
        total_funding_pnl += funding_pnl
        gross_trade_pnl_total += gross_pnl
        realized_pnl += net_pnl - partial_net_total
        trade_events.append(
            {
                "symbol": symbol,
                "side": "LONG" if open_trade.side == "BUY" else "SHORT",
                "regime": open_trade.regime,
                "alpha_id": open_trade.alpha_id,
                "entry_tier": open_trade.entry_tier,
                "entry_price": open_trade.entry_price,
                "exit_price": exit_fill,
                "quantity": float(open_trade.initial_quantity),
                "gross_pnl": gross_total,
                "entry_fee": open_trade.entry_fee,
                "exit_fee": exit_fee_total,
                "funding_pnl": funding_total,
                "pnl": net_pnl,
                "entry_tick": open_trade.entry_tick,
                "exit_tick": tick,
                "entry_time_ms": open_trade.entry_time_ms,
                "exit_time_ms": candle.close_time_ms,
                "initial_risk_abs": open_trade.initial_risk_abs,
                "effective_leverage": open_trade.effective_leverage,
                "entry_family": open_trade.entry_family,
                "entry_quality_score_v2": open_trade.entry_quality_score_v2,
                "entry_regime_strength": open_trade.entry_regime_strength,
                "entry_bias_strength": open_trade.entry_bias_strength,
                "quality_exit_applied": bool(open_trade.quality_exit_applied),
                "time_stop_bars": open_trade.time_stop_bars,
                "progress_check_bars": open_trade.progress_check_bars,
                "progress_min_mfe_r": open_trade.progress_min_mfe_r,
                "progress_extend_trigger_r": open_trade.progress_extend_trigger_r,
                "progress_extend_bars": open_trade.progress_extend_bars,
                "selective_extension_activated": bool(open_trade.selective_extension_activated),
                "selective_extension_activation_tick": open_trade.selective_extension_activation_tick,
                "selective_extension_tp_applied": bool(open_trade.selective_extension_tp_applied),
                "selective_extension_protection_applied": bool(
                    open_trade.selective_extension_protection_applied
                ),
                "partial_taken": bool(open_trade.partial_taken),
                "reason": reason,
            }
        )
        exit_cooldown_bars = max(int(reverse_cooldown_bars), 0)
        if net_pnl > 0.0:
            stoploss_streak = 0
            consecutive_loss_count = 0
            exit_cooldown_bars = max(int(open_trade.profit_exit_cooldown_bars), 0)
        else:
            consecutive_loss_count += 1
            if reason == "stop_loss":
                stoploss_streak += 1
                exit_cooldown_bars = max(int(open_trade.stop_exit_cooldown_bars), 0)
                if (
                    effective_stoploss_streak_trigger > 0
                    and stoploss_streak >= effective_stoploss_streak_trigger
                    and effective_stoploss_cooldown_bars > 0
                ):
                    stoploss_cooldown_until_tick = max(
                        int(stoploss_cooldown_until_tick),
                        int(tick) + int(effective_stoploss_cooldown_bars),
                    )
            else:
                stoploss_streak = 0
            if (
                int(open_trade.loss_streak_trigger) > 0
                and consecutive_loss_count >= int(open_trade.loss_streak_trigger)
                and int(open_trade.loss_streak_cooldown_bars) > 0
            ):
                loss_streak_cooldown_until_tick = max(
                    int(loss_streak_cooldown_until_tick),
                    int(tick) + int(open_trade.loss_streak_cooldown_bars),
                )
        if net_pnl < 0.0 and effective_loss_cooldown_bars > 0:
            loss_cooldown_until_tick = max(
                int(loss_cooldown_until_tick),
                int(tick) + int(effective_loss_cooldown_bars),
            )
        last_exit_tick = int(tick)
        last_exit_cooldown_bars = max(int(exit_cooldown_bars), 0)
        open_trade = None

    for tick, row in enumerate(rows):
        candle = candles_15m[tick]
        high = float(candle.high)
        low = float(candle.low)
        close = float(candle.close)
        day_key = int(candle.open_time_ms // day_ms)
        if current_day_key != day_key:
            current_day_key = day_key
            day_start_equity = max(float(initial_capital) + realized_pnl, 0.0)
            day_locked = False

        current_mfe_r = 0.0
        per_unit_risk = 0.0
        if open_trade is not None:
            decision_now = row.get("decision") if isinstance(row.get("decision"), dict) else {}
            indicators_now = (
                decision_now.get("indicators")
                if isinstance(decision_now.get("indicators"), dict)
                else {}
            )
            atr_now = _to_float(indicators_now.get("atr14_15m"))
            volume_ratio_now = _to_float(indicators_now.get("volume_ratio_15m"))
            close_30m_now = _to_float(indicators_now.get("close_30m"))
            ema_30m_now = _to_float(indicators_now.get("ema20_30m"))
            close_1h_now = _to_float(indicators_now.get("close_1h"))
            ema_1h_now = _to_float(indicators_now.get("ema20_1h"))
            if open_trade.side == "BUY":
                open_trade.peak_price = max(float(open_trade.peak_price or open_trade.entry_price), high)
            else:
                baseline_trough = float(open_trade.trough_price or open_trade.entry_price)
                open_trade.trough_price = min(baseline_trough, low)

            if open_trade.initial_risk_abs is not None and open_trade.initial_quantity > 0.0:
                per_unit_risk = float(open_trade.initial_risk_abs) / max(
                    float(open_trade.initial_quantity),
                    1e-9,
                )
                if per_unit_risk > 0.0:
                    favorable_move = (
                        max(high - float(open_trade.entry_price), 0.0)
                        if open_trade.side == "BUY"
                        else max(float(open_trade.entry_price) - low, 0.0)
                    )
                    current_mfe_r = float(favorable_move / per_unit_risk)

            if open_trade.side == "BUY":
                effective_stop = float(open_trade.sl) if open_trade.sl is not None else None
                if open_trade.runner_stop is not None:
                    effective_stop = (
                        max(float(effective_stop), float(open_trade.runner_stop))
                        if effective_stop is not None
                        else float(open_trade.runner_stop)
                    )
                if effective_stop is not None and low <= float(effective_stop):
                    _close_trade(
                        tick=tick,
                        candle=candle,
                        exit_price=float(effective_stop),
                        reason="trail_stop" if open_trade.runner_stop is not None else "stop_loss",
                    )
                elif open_trade is not None:
                    _apply_selective_extension(
                        trade=open_trade,
                        tick=tick,
                        current_mfe_r=current_mfe_r,
                        per_unit_risk=per_unit_risk,
                    )
                    if (
                        not open_trade.partial_taken
                        and open_trade.tp_partial_price is not None
                        and open_trade.tp_partial_ratio > 0.0
                        and high >= float(open_trade.tp_partial_price)
                    ):
                        partial_qty = float(open_trade.initial_quantity) * float(open_trade.tp_partial_ratio)
                        _close_partial(
                            tick=tick,
                            candle=candle,
                            exit_price=float(open_trade.tp_partial_price),
                            qty_to_close=partial_qty,
                        )
                    elif (
                        open_trade.tp is not None
                        and high >= float(open_trade.tp)
                        and str(open_trade.runner_exit_mode or "").lower() != "trail_only"
                    ):
                        _close_trade(
                            tick=tick,
                            candle=candle,
                            exit_price=float(open_trade.tp),
                            reason="take_profit",
                        )
            elif open_trade is not None:
                effective_stop = float(open_trade.sl) if open_trade.sl is not None else None
                if open_trade.runner_stop is not None:
                    effective_stop = (
                        min(float(effective_stop), float(open_trade.runner_stop))
                        if effective_stop is not None
                        else float(open_trade.runner_stop)
                    )
                if effective_stop is not None and high >= float(effective_stop):
                    _close_trade(
                        tick=tick,
                        candle=candle,
                        exit_price=float(effective_stop),
                        reason="trail_stop" if open_trade.runner_stop is not None else "stop_loss",
                    )
                elif open_trade is not None:
                    _apply_selective_extension(
                        trade=open_trade,
                        tick=tick,
                        current_mfe_r=current_mfe_r,
                        per_unit_risk=per_unit_risk,
                    )
                    if (
                        not open_trade.partial_taken
                        and open_trade.tp_partial_price is not None
                        and open_trade.tp_partial_ratio > 0.0
                        and low <= float(open_trade.tp_partial_price)
                    ):
                        partial_qty = float(open_trade.initial_quantity) * float(open_trade.tp_partial_ratio)
                        _close_partial(
                            tick=tick,
                            candle=candle,
                            exit_price=float(open_trade.tp_partial_price),
                            qty_to_close=partial_qty,
                        )
                    elif (
                        open_trade.tp is not None
                        and low <= float(open_trade.tp)
                        and str(open_trade.runner_exit_mode or "").lower() != "trail_only"
                    ):
                        _close_trade(
                            tick=tick,
                            candle=candle,
                            exit_price=float(open_trade.tp),
                            reason="take_profit",
                        )

            if open_trade is not None and per_unit_risk > 0.0:
                if current_mfe_r >= float(open_trade.break_even_move_r):
                    _move_stop_to_break_even(open_trade)

                if (
                    open_trade.partial_taken
                    and str(open_trade.runner_exit_mode or "").lower() == "trail_only"
                    and atr_now is not None
                    and atr_now > 0.0
                ):
                    if open_trade.side == "BUY":
                        peak_price = float(open_trade.peak_price or high)
                        trail_candidate = peak_price - (
                            float(open_trade.runner_trailing_atr_mult) * float(atr_now)
                        )
                        open_trade.runner_stop = max(
                            float(open_trade.runner_stop or trail_candidate),
                            float(trail_candidate),
                            float(open_trade.entry_price),
                        )
                    else:
                        trough_price = float(open_trade.trough_price or low)
                        trail_candidate = trough_price + (
                            float(open_trade.runner_trailing_atr_mult) * float(atr_now)
                        )
                        current_runner = (
                            float(open_trade.runner_stop)
                            if open_trade.runner_stop is not None
                            else float(trail_candidate)
                        )
                        open_trade.runner_stop = min(
                            current_runner,
                            float(trail_candidate),
                            float(open_trade.entry_price),
                        )

                structure_lost = False
                if open_trade.side == "BUY":
                    structure_lost = bool(
                        (ema_30m_now is not None and close_30m_now is not None and close_30m_now < ema_30m_now)
                        or (ema_1h_now is not None and close_1h_now is not None and close_1h_now < ema_1h_now)
                    )
                else:
                    structure_lost = bool(
                        (ema_30m_now is not None and close_30m_now is not None and close_30m_now > ema_30m_now)
                        or (ema_1h_now is not None and close_1h_now is not None and close_1h_now > ema_1h_now)
                    )

                if open_trade.partial_taken and structure_lost:
                    _close_trade(
                        tick=tick,
                        candle=candle,
                        exit_price=close,
                        reason="structure_trail_exit",
                    )

            if open_trade is not None and open_trade.stalled_trend_timeout_bars is not None:
                held_bars = int(tick) - int(open_trade.entry_tick)
                if held_bars >= max(int(open_trade.stalled_trend_timeout_bars), 0):
                    progress_r = 0.0
                    if per_unit_risk > 0.0:
                        progress_move = (
                            max(close - float(open_trade.entry_price), 0.0)
                            if open_trade.side == "BUY"
                            else max(float(open_trade.entry_price) - close, 0.0)
                        )
                        progress_r = progress_move / per_unit_risk
                    if (
                        progress_r < 0.8
                        and (volume_ratio_now or 0.0) < float(open_trade.stalled_volume_ratio_floor or 0.0)
                    ):
                        structure_lost = False
                        if open_trade.side == "BUY":
                            structure_lost = bool(
                                ema_30m_now is not None and close_30m_now is not None and close_30m_now < ema_30m_now
                            )
                        else:
                            structure_lost = bool(
                                ema_30m_now is not None and close_30m_now is not None and close_30m_now > ema_30m_now
                            )
                        if structure_lost:
                            _close_trade(
                                tick=tick,
                                candle=candle,
                                exit_price=close,
                                reason="stalled_trend_exit",
                            )

            if open_trade is not None and open_trade.time_stop_bars is not None:
                held_bars = int(tick) - int(open_trade.entry_tick)
                if (
                    int(open_trade.progress_check_bars) > 0
                    and held_bars >= int(open_trade.progress_check_bars)
                    and float(open_trade.progress_min_mfe_r) > 0.0
                    and float(current_mfe_r) < float(open_trade.progress_min_mfe_r)
                ):
                    _close_trade(
                        tick=tick,
                        candle=candle,
                        exit_price=close,
                        reason="progress_time_stop",
                    )
                elif held_bars >= max(
                    int(open_trade.time_stop_bars),
                    0,
                ) + (
                    int(open_trade.progress_extend_bars)
                    if (
                        float(open_trade.progress_extend_trigger_r) > 0.0
                        and float(current_mfe_r) >= float(open_trade.progress_extend_trigger_r)
                    )
                    else 0
                ):
                    _close_trade(
                        tick=tick,
                        candle=candle,
                        exit_price=close,
                        reason="time_stop",
                    )

        realized_equity = float(initial_capital) + realized_pnl
        if (
            not trading_locked
            and effective_daily_loss_limit > 0.0
            and day_start_equity > 0.0
            and ((day_start_equity - realized_equity) / day_start_equity)
            >= effective_daily_loss_limit
        ):
            if open_trade is not None:
                _close_trade(
                    tick=tick,
                    candle=candle,
                    exit_price=close,
                    reason="daily_loss_stop",
                )
                realized_equity = float(initial_capital) + realized_pnl
            day_locked = True

        floor_equity = float(initial_capital) * effective_equity_floor_pct
        if (
            not trading_locked
            and effective_equity_floor_pct > 0.0
            and realized_equity <= floor_equity
        ):
            if open_trade is not None:
                _close_trade(
                    tick=tick,
                    candle=candle,
                    exit_price=close,
                    reason="equity_floor_stop",
                )
                realized_equity = float(initial_capital) + realized_pnl
            trading_locked = True
            trading_lock_reason = "equity_floor"

        if realized_equity > peak_realized_equity:
            peak_realized_equity = float(realized_equity)

        if (
            not trading_locked
            and effective_max_peak_drawdown_pct is not None
            and effective_max_peak_drawdown_pct > 0.0
            and peak_realized_equity > 0.0
        ):
            realized_drawdown = (
                (float(peak_realized_equity) - float(realized_equity))
                / float(peak_realized_equity)
            )
            if realized_drawdown >= effective_max_peak_drawdown_pct:
                if open_trade is not None:
                    _close_trade(
                        tick=tick,
                        candle=candle,
                        exit_price=close,
                        reason="peak_drawdown_kill",
                    )
                    realized_equity = float(initial_capital) + realized_pnl
                trading_locked = True
                trading_lock_reason = "peak_drawdown_kill"
                entry_block_counter["peak_drawdown_kill"] += 1

        candidate = row.get("candidate")
        decision = row.get("decision")
        size = row.get("size")
        cycle_state = str(row.get("state") or "")
        cycle_reason = str(row.get("reason") or "")
        would_enter = bool(row.get("would_enter"))

        if not would_enter and candidate is None and cycle_state == "no_candidate":
            _record_no_candidate_reason(cycle_reason)
            continue

        if would_enter and isinstance(candidate, dict):
            signal_side = str(candidate.get("side") or "")
            if signal_side not in {"BUY", "SELL"}:
                signal_side = str((decision or {}).get("side") or "")
            if signal_side in {"BUY", "SELL"}:
                if open_trade is not None and open_trade.side != signal_side:
                    if not bool(open_trade.allow_reverse_exit):
                        entry_block_counter["reverse_disabled_block"] += 1
                        continue
                    held_bars = int(tick) - int(open_trade.entry_tick)
                    if held_bars >= max(int(reverse_min_hold_bars), 0):
                        reverse_score = _to_float((candidate or {}).get("score"))
                        if reverse_score is None:
                            reverse_score = _to_float((decision or {}).get("score"))
                        if (
                            reverse_score is not None
                            and reverse_score < effective_reverse_exit_min_signal_score
                        ):
                            entry_block_counter["reverse_score_block"] += 1
                        else:
                            unrealized_pct = 0.0
                            unrealized_abs = 0.0
                            if open_trade.entry_price > 0:
                                if open_trade.side == "BUY":
                                    unrealized_abs = max(
                                        (close - float(open_trade.entry_price))
                                        * float(open_trade.quantity),
                                        0.0,
                                    )
                                    unrealized_pct = max(
                                        (close - float(open_trade.entry_price))
                                        / float(open_trade.entry_price),
                                        0.0,
                                    )
                                else:
                                    unrealized_abs = max(
                                        (float(open_trade.entry_price) - close)
                                        * float(open_trade.quantity),
                                        0.0,
                                    )
                                    unrealized_pct = max(
                                        (float(open_trade.entry_price) - close)
                                        / float(open_trade.entry_price),
                                        0.0,
                                    )
                            if unrealized_pct < effective_reverse_exit_min_profit_pct:
                                entry_block_counter["reverse_profit_block"] += 1
                            else:
                                required_r = max(
                                    float(open_trade.reverse_exit_min_r),
                                    effective_default_reverse_exit_min_r,
                                )
                                regime_lost = False
                                current_regime = str((decision or {}).get("regime") or "").strip().upper()
                                if open_trade.regime and current_regime and current_regime != str(open_trade.regime).upper():
                                    regime_lost = True
                                if bool((decision or {}).get("regime_lost")):
                                    regime_lost = True
                                if bool(getattr(open_trade, "regime_lost_exit_required", False)) and not regime_lost:
                                    entry_block_counter["reverse_regime_block"] += 1
                                    continue
                                if required_r > 0.0:
                                    initial_risk_abs = float(open_trade.initial_risk_abs or 0.0)
                                    if initial_risk_abs <= 0.0:
                                        entry_block_counter["reverse_r_missing_block"] += 1
                                        continue
                                    current_r = unrealized_abs / initial_risk_abs
                                    if current_r < required_r:
                                        entry_block_counter["reverse_r_block"] += 1
                                        continue
                                _close_trade(
                                    tick=tick,
                                    candle=candle,
                                    exit_price=close,
                                    reason="reverse_signal",
                                )
                    else:
                        entry_block_counter["reverse_hold_block"] += 1
                if open_trade is None:
                    if trading_locked:
                        if trading_lock_reason == "peak_drawdown_kill":
                            entry_block_counter["peak_drawdown_kill_block"] += 1
                        else:
                            entry_block_counter["equity_floor_block"] += 1
                        continue
                    if day_locked:
                        entry_block_counter["daily_loss_stop_block"] += 1
                        continue
                    if int(tick) < int(stoploss_cooldown_until_tick):
                        entry_block_counter["stoploss_cooldown_block"] += 1
                        continue
                    if int(tick) < int(loss_cooldown_until_tick):
                        entry_block_counter["loss_cooldown_block"] += 1
                        continue
                    if int(tick) < int(loss_streak_cooldown_until_tick):
                        entry_block_counter["loss_streak_cooldown_block"] += 1
                        continue

                    signal_score = _to_float((candidate or {}).get("score"))
                    if signal_score is None:
                        signal_score = _to_float((decision or {}).get("score"))
                    if signal_score is not None and signal_score < effective_min_signal_score:
                        entry_block_counter["score_quality_block"] += 1
                        continue

                    cooldown_bars = max(int(last_exit_cooldown_bars), 0)
                    if int(tick) - int(last_exit_tick) < cooldown_bars:
                        entry_block_counter["reverse_cooldown_block"] += 1
                        continue

                    if max(int(max_trades_per_day_per_symbol), 0) > 0 and day_trade_counter.get(
                        day_key, 0
                    ) >= int(max_trades_per_day_per_symbol):
                        entry_block_counter["daily_trade_cap_block"] += 1
                        continue

                    entry_price = _to_float((candidate or {}).get("entry_price"))
                    if entry_price is None:
                        entry_price = _to_float((decision or {}).get("entry_price"))
                    if entry_price is None or entry_price <= 0:
                        entry_price = close
                    qty = _to_float((size or {}).get("qty")) if isinstance(size, dict) else None
                    if qty is None or qty <= 0:
                        qty = 1.0
                    entry_fill = model.filled_entry_price(
                        side=signal_side, raw_price=float(entry_price)
                    )

                    sl_tp = (decision or {}).get("sl_tp") if isinstance(decision, dict) else None
                    tp = (
                        _to_float((sl_tp or {}).get("take_profit"))
                        if isinstance(sl_tp, dict)
                        else None
                    )
                    sl = (
                        _to_float((sl_tp or {}).get("stop_loss"))
                        if isinstance(sl_tp, dict)
                        else None
                    )
                    execution_hints = (
                        (decision or {}).get("execution") if isinstance(decision, dict) else None
                    )
                    management_spec = PositionManagementSpec.from_execution_hints(
                        execution_hints if isinstance(execution_hints, dict) else None
                    )
                    time_stop_bars: int | None = (
                        int(management_spec.time_stop_bars)
                        if int(management_spec.time_stop_bars) > 0
                        else None
                    )
                    progress_check_bars = int(management_spec.progress_check_bars)
                    progress_min_mfe_r = float(management_spec.progress_min_mfe_r)
                    progress_extend_trigger_r = float(management_spec.progress_extend_trigger_r)
                    progress_extend_bars = int(management_spec.progress_extend_bars)
                    stalled_trend_timeout_bars: int | None = None
                    stop_exit_cooldown_bars = max(
                        int(management_spec.stop_exit_cooldown_bars),
                        int(reverse_cooldown_bars),
                        0,
                    )
                    loss_streak_trigger_hint = int(management_spec.loss_streak_trigger)
                    loss_streak_cooldown_bars_hint = int(management_spec.loss_streak_cooldown_bars)
                    profit_exit_cooldown_bars = max(
                        int(management_spec.profit_exit_cooldown_bars),
                        int(reverse_cooldown_bars),
                        0,
                    )
                    tp_partial_ratio = float(management_spec.tp_partial_ratio)
                    tp_partial_price: float | None = None
                    tp_partial_at_r = float(management_spec.tp_partial_at_r)
                    break_even_move_r = float(management_spec.move_stop_to_be_at_r)
                    runner_exit_mode: str | None = None
                    runner_trailing_atr_mult = 0.0
                    stalled_volume_ratio_floor = 0.0
                    regime_lost_exit_required = True
                    allow_reverse_exit = bool(management_spec.allow_reverse_exit)
                    entry_quality_score_v2 = float(management_spec.entry_quality_score_v2)
                    entry_regime_strength = float(management_spec.entry_regime_strength)
                    entry_bias_strength = float(management_spec.entry_bias_strength)
                    quality_exit_applied = bool(management_spec.quality_exit_applied)
                    selective_extension_proof_bars = int(
                        management_spec.selective_extension_proof_bars
                    )
                    selective_extension_min_mfe_r = float(
                        management_spec.selective_extension_min_mfe_r
                    )
                    selective_extension_min_regime_strength = float(
                        management_spec.selective_extension_min_regime_strength
                    )
                    selective_extension_min_bias_strength = float(
                        management_spec.selective_extension_min_bias_strength
                    )
                    selective_extension_min_quality_score_v2 = float(
                        management_spec.selective_extension_min_quality_score_v2
                    )
                    selective_extension_time_stop_bars = int(
                        management_spec.selective_extension_time_stop_bars
                    )
                    selective_extension_take_profit_r = float(
                        management_spec.selective_extension_take_profit_r
                    )
                    selective_extension_move_stop_to_be_at_r = float(
                        management_spec.selective_extension_move_stop_to_be_at_r
                    )
                    alpha_id = str((decision or {}).get("alpha_id") or "").strip() or None
                    entry_family = str((decision or {}).get("entry_family") or "").strip() or None
                    if isinstance(execution_hints, dict):
                        hint_stalled_trend_timeout_bars = _to_int(
                            execution_hints.get("stalled_trend_timeout_bars")
                        )
                        if (
                            hint_stalled_trend_timeout_bars is not None
                            and hint_stalled_trend_timeout_bars > 0
                        ):
                            stalled_trend_timeout_bars = int(hint_stalled_trend_timeout_bars)
                        hint_tp_partial_price = _to_float(execution_hints.get("tp_partial_price"))
                        if hint_tp_partial_price is not None and hint_tp_partial_price > 0.0:
                            tp_partial_price = float(hint_tp_partial_price)
                        hint_runner_exit_mode = execution_hints.get("runner_exit_mode")
                        if hint_runner_exit_mode is not None:
                            runner_exit_mode = str(hint_runner_exit_mode).strip().lower() or None
                        hint_runner_trailing_atr_mult = _to_float(
                            execution_hints.get("runner_trailing_atr_mult")
                        )
                        if (
                            hint_runner_trailing_atr_mult is not None
                            and hint_runner_trailing_atr_mult > 0.0
                        ):
                            runner_trailing_atr_mult = float(hint_runner_trailing_atr_mult)
                        hint_stalled_volume_ratio_floor = _to_float(
                            execution_hints.get("stalled_volume_ratio_floor")
                        )
                        if (
                            hint_stalled_volume_ratio_floor is not None
                            and hint_stalled_volume_ratio_floor >= 0.0
                        ):
                            stalled_volume_ratio_floor = float(hint_stalled_volume_ratio_floor)
                    expected_edge_pct, risk_pct = _entry_reward_and_risk_pct(
                        signal_side=signal_side,
                        entry_fill=float(entry_fill),
                        sl_tp=sl_tp if isinstance(sl_tp, dict) else None,
                        execution_hints=execution_hints if isinstance(execution_hints, dict) else None,
                    )
                    roundtrip_cost_pct = (
                        (2.0 * max(float(model.fee_bps), 0.0))
                        + (2.0 * max(float(model.slippage_bps), 0.0))
                    ) / 10000.0
                    funding_buffer_pct = (
                        max(float(model.funding_bps_per_8h), 0.0) / 10000.0
                    ) * 0.25
                    min_edge_pct = (roundtrip_cost_pct + funding_buffer_pct) * max(
                        float(min_expected_edge_over_roundtrip_cost),
                        0.0,
                    )
                    if expected_edge_pct <= min_edge_pct:
                        entry_block_counter["edge_cost_block"] += 1
                        continue

                    if effective_min_reward_risk_ratio > 0.0:
                        reward_pct = float(expected_edge_pct)
                        if reward_pct <= 0.0 or risk_pct <= 0.0:
                            entry_block_counter["reward_risk_missing_block"] += 1
                            continue

                        reward_risk_ratio = reward_pct / risk_pct
                        if reward_risk_ratio < effective_min_reward_risk_ratio:
                            entry_block_counter["reward_risk_block"] += 1
                            continue

                    decision_risk_per_trade_pct = _to_float(
                        (decision or {}).get("risk_per_trade_pct")
                    )
                    if decision_risk_per_trade_pct is None:
                        decision_risk_per_trade_pct = effective_default_risk_per_trade_pct
                    decision_risk_per_trade_pct = min(
                        max(float(decision_risk_per_trade_pct), 0.0),
                        1.0,
                    )
                    decision_max_effective_leverage = _to_float(
                        (decision or {}).get("max_effective_leverage")
                    )
                    if decision_max_effective_leverage is None:
                        decision_max_effective_leverage = effective_default_max_effective_leverage
                    decision_max_effective_leverage = max(
                        float(decision_max_effective_leverage),
                        1.0,
                    )
                    decision_reverse_exit_min_r = _to_float(
                        (decision or {}).get("reverse_exit_min_r")
                    )
                    if decision_reverse_exit_min_r is None:
                        decision_reverse_exit_min_r = effective_default_reverse_exit_min_r
                    decision_reverse_exit_min_r = max(float(decision_reverse_exit_min_r), 0.0)
                    decision_regime = str((decision or {}).get("regime") or "").strip().upper()

                    margin_used = 0.0
                    max_loss_cap: float | None = None
                    effective_leverage_used: float | None = None
                    initial_risk_abs: float | None = None
                    entry_equity = float(initial_capital) + realized_pnl
                    if entry_equity <= 0:
                        continue

                    if decision_risk_per_trade_pct > 0.0:
                        if sl is None or sl <= 0 or entry_fill <= 0:
                            entry_block_counter["risk_size_missing_stop_block"] += 1
                            continue
                        risk_per_unit = abs(entry_fill - float(sl))
                        if risk_per_unit <= 0.0:
                            entry_block_counter["risk_size_invalid_stop_block"] += 1
                            continue
                        risk_budget = entry_equity * decision_risk_per_trade_pct
                        qty = risk_budget / risk_per_unit
                        max_notional = entry_equity * decision_max_effective_leverage
                        if max_notional > 0 and entry_fill > 0:
                            qty = min(qty, max_notional / entry_fill)
                        qty = max(float(qty), 0.0)
                        if qty <= 0.0:
                            entry_block_counter["risk_size_invalid_qty_block"] += 1
                            continue
                        entry_notional_est = abs(entry_fill * qty)
                        margin_used = (
                            entry_notional_est / decision_max_effective_leverage
                            if decision_max_effective_leverage > 0
                            else 0.0
                        )
                        effective_leverage_used = (
                            entry_notional_est / entry_equity if entry_equity > 0 else 0.0
                        )
                        initial_risk_abs = risk_per_unit * qty
                        max_loss_cap = initial_risk_abs
                        if effective_margin_loss_fraction > 0.0 and margin_used > 0.0:
                            max_loss_cap = min(
                                max_loss_cap,
                                margin_used * effective_margin_loss_fraction,
                            )
                    elif fixed_leverage is not None and float(fixed_leverage) > 0 and entry_fill > 0:
                        margin_ratio = max(0.01, min(float(fixed_leverage_margin_use_pct), 1.0))
                        if peak_realized_equity > 0 and entry_equity < peak_realized_equity:
                            drawdown_pct = max(
                                (float(peak_realized_equity) - float(entry_equity))
                                / float(peak_realized_equity),
                                0.0,
                            )
                            if drawdown_pct > effective_drawdown_scale_start_pct:
                                if (
                                    effective_drawdown_scale_end_pct
                                    <= effective_drawdown_scale_start_pct
                                ):
                                    margin_ratio *= effective_drawdown_margin_scale_min
                                elif drawdown_pct >= effective_drawdown_scale_end_pct:
                                    margin_ratio *= effective_drawdown_margin_scale_min
                                else:
                                    progress = (
                                        drawdown_pct - effective_drawdown_scale_start_pct
                                    ) / (
                                        effective_drawdown_scale_end_pct
                                        - effective_drawdown_scale_start_pct
                                    )
                                    scale = 1.0 - (
                                        progress * (1.0 - effective_drawdown_margin_scale_min)
                                    )
                                    margin_ratio *= max(
                                        effective_drawdown_margin_scale_min,
                                        min(scale, 1.0),
                                    )
                        margin_used = max(entry_equity * margin_ratio, 0.0)
                        qty = (margin_used * float(fixed_leverage)) / entry_fill
                        if effective_margin_loss_fraction > 0.0:
                            max_loss_cap = margin_used * effective_margin_loss_fraction
                        else:
                            max_loss_cap = margin_used
                        effective_leverage_used = float(fixed_leverage)
                        if sl is not None and sl > 0:
                            initial_risk_abs = abs(entry_fill - float(sl)) * float(qty)

                    if initial_risk_abs is None and sl is not None and sl > 0 and entry_fill > 0 and qty > 0:
                        initial_risk_abs = abs(entry_fill - float(sl)) * float(qty)

                    entry_notional = abs(entry_fill * float(qty))
                    entry_fee = model.fee(notional=entry_notional)
                    realized_pnl -= entry_fee
                    total_fees += entry_fee
                    open_trade = _OpenTrade(
                        symbol=symbol,
                        side=signal_side,
                        entry_price=float(entry_fill),
                        quantity=float(qty),
                        initial_quantity=float(qty),
                        entry_fee=float(entry_fee),
                        entry_notional=float(entry_notional),
                        margin_used=float(margin_used),
                        max_loss_cap=max_loss_cap,
                        tp=tp,
                        sl=sl,
                        initial_risk_abs=initial_risk_abs,
                        time_stop_bars=time_stop_bars,
                        progress_check_bars=progress_check_bars,
                        progress_min_mfe_r=progress_min_mfe_r,
                        progress_extend_trigger_r=progress_extend_trigger_r,
                        progress_extend_bars=progress_extend_bars,
                        reverse_exit_min_r=decision_reverse_exit_min_r,
                        allow_reverse_exit=allow_reverse_exit,
                        effective_leverage=effective_leverage_used,
                        regime=(decision_regime or None),
                        entry_tick=tick,
                        entry_time_ms=candle.open_time_ms,
                        stop_exit_cooldown_bars=stop_exit_cooldown_bars,
                        loss_streak_trigger=loss_streak_trigger_hint,
                        loss_streak_cooldown_bars=loss_streak_cooldown_bars_hint,
                        profit_exit_cooldown_bars=profit_exit_cooldown_bars,
                        alpha_id=alpha_id,
                        entry_family=entry_family,
                        entry_tier=str((decision or {}).get("entry_tier") or "").strip() or None,
                        regime_lost_exit_required=regime_lost_exit_required,
                        tp_partial_ratio=tp_partial_ratio,
                        tp_partial_price=tp_partial_price,
                        tp_partial_at_r=tp_partial_at_r,
                        break_even_move_r=break_even_move_r,
                        runner_exit_mode=runner_exit_mode,
                        runner_trailing_atr_mult=runner_trailing_atr_mult,
                        stalled_trend_timeout_bars=stalled_trend_timeout_bars,
                        stalled_volume_ratio_floor=stalled_volume_ratio_floor,
                        entry_quality_score_v2=entry_quality_score_v2,
                        entry_regime_strength=entry_regime_strength,
                        entry_bias_strength=entry_bias_strength,
                        quality_exit_applied=quality_exit_applied,
                        selective_extension_proof_bars=selective_extension_proof_bars,
                        selective_extension_min_mfe_r=selective_extension_min_mfe_r,
                        selective_extension_min_regime_strength=selective_extension_min_regime_strength,
                        selective_extension_min_bias_strength=selective_extension_min_bias_strength,
                        selective_extension_min_quality_score_v2=(
                            selective_extension_min_quality_score_v2
                        ),
                        selective_extension_time_stop_bars=selective_extension_time_stop_bars,
                        selective_extension_take_profit_r=selective_extension_take_profit_r,
                        selective_extension_move_stop_to_be_at_r=(
                            selective_extension_move_stop_to_be_at_r
                        ),
                        peak_price=float(entry_fill) if signal_side == "BUY" else None,
                        trough_price=float(entry_fill) if signal_side == "SELL" else None,
                    )
                    day_trade_counter[day_key] = int(day_trade_counter.get(day_key, 0)) + 1

        unrealized = 0.0
        if open_trade is not None:
            qty = max(float(open_trade.quantity), 0.0)
            if open_trade.side == "BUY":
                unrealized = (close - open_trade.entry_price) * qty
            else:
                unrealized = (open_trade.entry_price - close) * qty
        equity_curve.append(float(initial_capital) + realized_pnl + unrealized)

    if open_trade is not None and candles_15m:
        _close_trade(
            tick=len(candles_15m) - 1,
            candle=candles_15m[-1],
            exit_price=float(candles_15m[-1].close),
            reason="end_of_data",
        )
        equity_curve[-1] = float(initial_capital) + realized_pnl

    gross_profit = sum(max(0.0, float(item["pnl"])) for item in trade_events)
    gross_loss = abs(sum(min(0.0, float(item["pnl"])) for item in trade_events))
    wins = sum(1 for item in trade_events if float(item["pnl"]) > 0)
    losses = sum(1 for item in trade_events if float(item["pnl"]) < 0)
    total_trades = len(trade_events)
    final_equity = float(initial_capital) + realized_pnl
    total_return_pct = (
        ((final_equity - float(initial_capital)) / float(initial_capital)) * 100.0
        if float(initial_capital) > 0
        else 0.0
    )
    profit_factor = float("inf") if gross_loss <= 0 and gross_profit > 0 else 0.0
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    win_rate_pct = ((wins / total_trades) * 100.0) if total_trades > 0 else 0.0
    max_drawdown_pct = _calc_max_drawdown_pct(equity_curve)
    alpha_block_distribution: dict[str, Counter[str]] = {}
    for row in rows:
        decision = row.get("decision")
        if not isinstance(decision, dict):
            continue
        alpha_blocks = decision.get("alpha_blocks")
        if not isinstance(alpha_blocks, dict):
            continue
        for alpha_id, reason in alpha_blocks.items():
            alpha_name = str(alpha_id or "").strip()
            reason_name = str(reason or "").strip()
            if not alpha_name or not reason_name:
                continue
            alpha_block_distribution.setdefault(alpha_name, Counter())[reason_name] += 1
    alpha_stats = _summarize_alpha_stats(
        trade_events=trade_events,
        alpha_block_distribution=alpha_block_distribution,
        initial_capital=float(initial_capital),
    )

    return {
        "symbol": symbol,
        "candles_15m": len(candles_15m),
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate_pct, 4),
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "gross_trade_pnl": round(gross_trade_pnl_total, 6),
        "total_fees": round(total_fees, 6),
        "total_funding_pnl": round(total_funding_pnl, 6),
        "net_profit": round(realized_pnl, 6),
        "profit_factor": None if profit_factor == float("inf") else round(profit_factor, 6),
        "profit_factor_infinite": bool(profit_factor == float("inf")),
        "max_drawdown_pct": round(max_drawdown_pct, 6),
        "initial_capital": float(initial_capital),
        "final_equity": round(final_equity, 6),
        "total_return_pct": round(total_return_pct, 6),
        "entry_block_distribution": dict(entry_block_counter),
        "alpha_block_distribution": {
            alpha_id: dict(counter) for alpha_id, counter in alpha_block_distribution.items()
        },
        "alpha_stats": alpha_stats,
        "trading_lock_reason": trading_lock_reason,
        "trade_events": trade_events,
    }


def _simulate_portfolio_metrics(
    *,
    rows: list[dict[str, Any]],
    initial_capital: float,
    execution_model: _BacktestExecutionModel,
    fixed_leverage_margin_use_pct: float,
    max_open_positions: int,
    max_new_entries_per_tick: int,
    reverse_cooldown_bars: int,
    max_trades_per_day_per_symbol: int,
    min_expected_edge_over_roundtrip_cost: float = 0.0,
    min_reward_risk_ratio: float = 0.0,
    daily_loss_limit_pct: float = 0.0,
    equity_floor_pct: float = 0.0,
    max_trade_margin_loss_fraction: float = 1.0,
    min_signal_score: float = 0.0,
    drawdown_scale_start_pct: float,
    drawdown_scale_end_pct: float,
    drawdown_margin_scale_min: float,
) -> dict[str, Any]:
    open_trades: dict[str, _OpenTrade] = {}
    realized_pnl = 0.0
    total_fees = 0.0
    total_funding_pnl = 0.0
    gross_trade_pnl_total = 0.0
    trade_events: list[dict[str, Any]] = []
    equity_curve: list[float] = []
    entry_block_counter: Counter[str] = Counter()
    bucket_block_counter: Counter[str] = Counter()
    simultaneous_position_histogram: Counter[int] = Counter()
    portfolio_open_slots_usage: Counter[int] = Counter()
    state_distribution: Counter[str] = Counter()
    day_trade_counter: dict[tuple[int, str], int] = {}
    last_exit_tick_by_symbol: dict[str, int] = {}
    last_exit_cooldown_bars_by_symbol: dict[str, int] = {}
    capital_utilization_samples: list[float] = []
    peak_realized_equity = float(initial_capital)
    day_ms = 24 * 60 * 60 * 1000
    effective_min_expected_edge = max(float(min_expected_edge_over_roundtrip_cost), 0.0)
    effective_min_reward_risk_ratio = max(float(min_reward_risk_ratio), 0.0)
    effective_daily_loss_limit_pct = max(float(daily_loss_limit_pct), 0.0)
    effective_equity_floor_pct = max(float(equity_floor_pct), 0.0)
    effective_max_trade_margin_loss_fraction = max(
        0.0,
        min(float(max_trade_margin_loss_fraction), 1.0),
    )
    effective_min_signal_score = max(float(min_signal_score), 0.0)
    current_day_key: int | None = None
    day_start_equity = float(initial_capital)
    day_locked = False
    trading_locked = False
    trading_lock_reason: str | None = None

    def _record_reason(reason: str) -> None:
        normalized = str(reason or "").strip()
        if not normalized:
            return
        if normalized.startswith("no_candidate_multi:"):
            detail = normalized.split(":", 1)[1]
            for token in detail.split(";"):
                item = str(token).strip()
                if not item:
                    continue
                _, _, reason_part = item.partition(":")
                entry_block_counter[str(reason_part or "no_candidate").strip()] += 1
            return
        if normalized not in {"no_candidate", "would_execute"}:
            entry_block_counter[normalized] += 1

    def _drawdown_scale(entry_equity: float) -> float:
        if peak_realized_equity <= 0.0 or entry_equity >= peak_realized_equity:
            return 1.0
        dd_pct = max((peak_realized_equity - entry_equity) / peak_realized_equity, 0.0)
        if dd_pct <= float(drawdown_scale_start_pct):
            return 1.0
        if float(drawdown_scale_end_pct) <= float(drawdown_scale_start_pct):
            return float(drawdown_margin_scale_min)
        if dd_pct >= float(drawdown_scale_end_pct):
            return float(drawdown_margin_scale_min)
        progress = (dd_pct - float(drawdown_scale_start_pct)) / max(
            float(drawdown_scale_end_pct) - float(drawdown_scale_start_pct),
            1e-9,
        )
        return max(
            float(drawdown_margin_scale_min),
            1.0 - (progress * (1.0 - float(drawdown_margin_scale_min))),
        )

    def _close_trade(*, trade: _OpenTrade, candle: dict[str, float], tick: int, reason: str) -> None:
        nonlocal realized_pnl, total_fees, total_funding_pnl, gross_trade_pnl_total
        qty = max(float(trade.quantity), 0.0)
        if qty <= 0.0:
            return
        close_time_ms = int(candle.get("close_time_ms", 0.0))
        raw_exit_price = float(candle["close"])
        if reason == "stop_loss" and trade.sl is not None:
            raw_exit_price = float(trade.sl)
        elif reason == "take_profit" and trade.tp is not None:
            raw_exit_price = float(trade.tp)
        exit_fill = execution_model.filled_exit_price(side=trade.side, raw_price=raw_exit_price)
        gross_pnl = (
            (exit_fill - float(trade.entry_price)) * qty
            if trade.side == "BUY"
            else (float(trade.entry_price) - exit_fill) * qty
        )
        exit_notional = abs(exit_fill * qty)
        exit_fee = execution_model.fee(notional=exit_notional)
        funding_pnl = execution_model.funding_pnl(
            side=trade.side,
            entry_time_ms=int(trade.entry_time_ms),
            exit_time_ms=close_time_ms,
            notional=float(trade.entry_notional),
        )
        net_pnl = gross_pnl - exit_fee + funding_pnl
        if trade.max_loss_cap is not None:
            trade_floor = -float(trade.max_loss_cap) + float(trade.entry_fee)
            if net_pnl < trade_floor:
                net_pnl = trade_floor

        realized_pnl += net_pnl
        total_fees += exit_fee
        total_funding_pnl += funding_pnl
        gross_trade_pnl_total += gross_pnl
        trade_events.append(
            {
                "symbol": trade.symbol,
                "side": "LONG" if trade.side == "BUY" else "SHORT",
                "regime": trade.regime,
                "alpha_id": trade.alpha_id,
                "entry_tier": trade.entry_tier,
                "entry_price": trade.entry_price,
                "exit_price": exit_fill,
                "quantity": float(trade.initial_quantity),
                "gross_pnl": gross_pnl,
                "entry_fee": trade.entry_fee,
                "exit_fee": exit_fee,
                "funding_pnl": funding_pnl,
                "pnl": net_pnl,
                "entry_tick": trade.entry_tick,
                "exit_tick": tick,
                "entry_time_ms": trade.entry_time_ms,
                "exit_time_ms": close_time_ms,
                "initial_risk_abs": trade.initial_risk_abs,
                "effective_leverage": trade.effective_leverage,
                "entry_family": trade.entry_family,
                "partial_taken": False,
                "reason": reason,
            }
        )
        last_exit_tick_by_symbol[trade.symbol] = int(tick)
        last_exit_cooldown_bars_by_symbol[trade.symbol] = max(int(trade.stop_exit_cooldown_bars), 0)
        open_trades.pop(trade.symbol, None)

    def _liquidate_open_trades(*, candles: dict[str, Any], tick: int, reason: str) -> None:
        for symbol in list(open_trades.keys()):
            trade = open_trades.get(symbol)
            candle = candles.get(symbol)
            if trade is None or not isinstance(candle, dict):
                continue
            _close_trade(trade=trade, candle=candle, tick=tick, reason=reason)

    for row in rows:
        tick = int(row.get("tick", 0))
        state_distribution[str(row.get("state") or "unknown")] += 1
        candles = row.get("candles")
        if not isinstance(candles, dict):
            candles = {}
        day_key = int(float(row.get("open_time") or 0.0) // day_ms)
        if current_day_key != day_key:
            current_day_key = day_key
            day_start_equity = max(float(initial_capital) + realized_pnl, 0.0)
            day_locked = False

        for symbol in list(open_trades.keys()):
            trade = open_trades.get(symbol)
            candle = candles.get(symbol)
            if trade is None or not isinstance(candle, dict):
                continue
            high = float(candle.get("high", candle.get("close", trade.entry_price)))
            low = float(candle.get("low", candle.get("close", trade.entry_price)))
            close = float(candle.get("close", trade.entry_price))
            held_bars = int(tick) - int(trade.entry_tick)
            if trade.side == "BUY":
                if trade.sl is not None and low <= float(trade.sl):
                    _close_trade(trade=trade, candle=candle, tick=tick, reason="stop_loss")
                elif trade.tp is not None and high >= float(trade.tp):
                    _close_trade(trade=trade, candle=candle, tick=tick, reason="take_profit")
                elif trade.time_stop_bars is not None and held_bars >= int(trade.time_stop_bars):
                    _close_trade(
                        trade=trade,
                        candle={**candle, "close": close},
                        tick=tick,
                        reason="time_stop",
                    )
            else:
                if trade.sl is not None and high >= float(trade.sl):
                    _close_trade(trade=trade, candle=candle, tick=tick, reason="stop_loss")
                elif trade.tp is not None and low <= float(trade.tp):
                    _close_trade(trade=trade, candle=candle, tick=tick, reason="take_profit")
                elif trade.time_stop_bars is not None and held_bars >= int(trade.time_stop_bars):
                    _close_trade(
                        trade=trade,
                        candle={**candle, "close": close},
                        tick=tick,
                        reason="time_stop",
                    )

        entry_equity = float(initial_capital) + realized_pnl
        peak_realized_equity = max(float(peak_realized_equity), float(entry_equity))
        if (
            not day_locked
            and effective_daily_loss_limit_pct > 0.0
            and float(day_start_equity) > 0.0
            and float(entry_equity)
            <= float(day_start_equity) * (1.0 - float(effective_daily_loss_limit_pct))
        ):
            day_locked = True

        if (
            not trading_locked
            and effective_equity_floor_pct > 0.0
            and float(entry_equity) <= float(initial_capital) * float(effective_equity_floor_pct)
        ):
            _liquidate_open_trades(candles=candles, tick=tick, reason="equity_floor_stop")
            trading_locked = True
            trading_lock_reason = "equity_floor"
            entry_equity = float(initial_capital) + realized_pnl

        ranked_candidates = row.get("ranked_candidates")
        decisions = row.get("decisions")
        if not isinstance(ranked_candidates, list):
            ranked_candidates = []
        if not isinstance(decisions, dict):
            decisions = {}

        if not ranked_candidates:
            _record_reason(str(row.get("reason") or "no_candidate"))

        open_symbols = {str(symbol).strip().upper() for symbol in open_trades.keys()}
        ranked_objects: list[Candidate] = []
        for item in ranked_candidates:
            if isinstance(item, Candidate):
                ranked_objects.append(item)
                continue
            if isinstance(item, dict):
                candidate = _candidate_from_payload(item)
                if candidate is not None:
                    ranked_objects.append(candidate)
        routing = route_ranked_candidates(
            candidates=ranked_objects,
            open_symbols=open_symbols,
            allow_reentry=False,
            config=PortfolioRoutingConfig(
                max_open_positions=int(max_open_positions),
                max_new_entries_per_tick=int(max_new_entries_per_tick),
            ),
        )
        for blocked_candidate, blocked_reason in routing.blocked:
            entry_block_counter[blocked_reason] += 1
            if blocked_reason == "portfolio_bucket_cap":
                bucket_block_counter[
                    blocked_candidate.portfolio_bucket
                    or portfolio_bucket_for_symbol(blocked_candidate.symbol)
                ] += 1

        free_slots = len(routing.selected)
        for candidate in routing.selected:
            symbol = str(candidate.symbol).strip().upper()
            decision = decisions.get(symbol)
            if not isinstance(decision, dict):
                decision = {}

            cooldown_bars = int(last_exit_cooldown_bars_by_symbol.get(symbol, max(int(reverse_cooldown_bars), 0)))
            if int(tick) - int(last_exit_tick_by_symbol.get(symbol, -(10**9))) < cooldown_bars:
                entry_block_counter["reverse_cooldown_block"] += 1
                continue

            if max(int(max_trades_per_day_per_symbol), 0) > 0 and day_trade_counter.get(
                (day_key, symbol),
                0,
            ) >= int(max_trades_per_day_per_symbol):
                entry_block_counter["daily_trade_cap_block"] += 1
                continue

            if trading_locked:
                entry_block_counter[f"{trading_lock_reason or 'equity_floor'}_block"] += 1
                continue
            if day_locked:
                entry_block_counter["daily_loss_stop_block"] += 1
                continue

            signal_side = str(candidate.side or decision.get("side") or "NONE").upper()
            if signal_side not in {"BUY", "SELL"}:
                entry_block_counter["entry_side_missing_block"] += 1
                continue
            signal_score = float(candidate.portfolio_score or candidate.score)
            if signal_score < effective_min_signal_score:
                entry_block_counter["score_quality_block"] += 1
                continue

            entry_price = _to_float(candidate.entry_price)
            if entry_price is None or entry_price <= 0.0:
                entry_price = _to_float(decision.get("entry_price"))
            if entry_price is None or entry_price <= 0.0:
                candle = candles.get(symbol)
                if isinstance(candle, dict):
                    entry_price = _to_float(candle.get("close"))
            if entry_price is None or entry_price <= 0.0:
                entry_block_counter["price_unavailable"] += 1
                continue

            sl_tp = decision.get("sl_tp") if isinstance(decision.get("sl_tp"), dict) else {}
            tp = _to_float(sl_tp.get("take_profit"))
            sl = _to_float(sl_tp.get("stop_loss"))
            if sl is None or sl <= 0.0:
                entry_block_counter["risk_size_missing_stop_block"] += 1
                continue

            entry_fill = execution_model.filled_entry_price(side=signal_side, raw_price=float(entry_price))
            expected_edge_pct, risk_pct = _entry_reward_and_risk_pct(
                signal_side=signal_side,
                entry_fill=float(entry_fill),
                sl_tp=sl_tp if isinstance(sl_tp, dict) else None,
                execution_hints=decision.get("execution")
                if isinstance(decision.get("execution"), dict)
                else None,
            )
            roundtrip_cost_pct = (
                (2.0 * max(float(execution_model.fee_bps), 0.0))
                + (2.0 * max(float(execution_model.slippage_bps), 0.0))
            ) / 10000.0
            funding_buffer_pct = (
                max(float(execution_model.funding_bps_per_8h), 0.0) / 10000.0
            ) * 0.25
            min_edge_pct = (roundtrip_cost_pct + funding_buffer_pct) * effective_min_expected_edge
            if expected_edge_pct <= min_edge_pct:
                entry_block_counter["edge_cost_block"] += 1
                continue
            if effective_min_reward_risk_ratio > 0.0:
                if expected_edge_pct <= 0.0 or risk_pct <= 0.0:
                    entry_block_counter["reward_risk_missing_block"] += 1
                    continue
                reward_risk_ratio = expected_edge_pct / risk_pct
                if reward_risk_ratio < effective_min_reward_risk_ratio:
                    entry_block_counter["reward_risk_block"] += 1
                    continue
            risk_per_unit = abs(entry_fill - float(sl))
            if risk_per_unit <= 0.0:
                entry_block_counter["risk_size_invalid_stop_block"] += 1
                continue

            total_margin_used = sum(float(trade.margin_used) for trade in open_trades.values())
            available_margin = max(float(entry_equity) - float(total_margin_used), 0.0)
            if available_margin <= 0.0:
                entry_block_counter["capital_unavailable_block"] += 1
                continue

            risk_per_trade_pct = max(
                _to_float(candidate.risk_per_trade_pct)
                or _to_float(decision.get("risk_per_trade_pct"))
                or 0.0,
                0.0,
            )
            max_effective_leverage = max(
                _to_float(candidate.max_effective_leverage)
                or _to_float(decision.get("max_effective_leverage"))
                or 1.0,
                1.0,
            )
            scale = _drawdown_scale(float(entry_equity))
            risk_budget = float(entry_equity) * float(risk_per_trade_pct) * float(scale)
            qty = risk_budget / risk_per_unit if risk_budget > 0.0 else 0.0
            max_notional = float(entry_equity) * float(max_effective_leverage) * float(scale)
            if max_notional > 0.0:
                qty = min(qty, max_notional / float(entry_fill))
            qty = max(float(qty), 0.0)
            if qty <= 0.0:
                entry_block_counter["risk_size_invalid_qty_block"] += 1
                continue

            entry_notional = abs(float(entry_fill) * float(qty))
            margin_used = (
                entry_notional / float(max_effective_leverage) if float(max_effective_leverage) > 0.0 else 0.0
            )
            if margin_used > available_margin and margin_used > 0.0:
                scale_down = available_margin / margin_used
                qty *= scale_down
                entry_notional = abs(float(entry_fill) * float(qty))
                margin_used = available_margin
            if qty <= 0.0 or margin_used <= 0.0:
                entry_block_counter["capital_unavailable_block"] += 1
                continue

            entry_fee = execution_model.fee(notional=entry_notional)
            realized_pnl -= entry_fee
            total_fees += entry_fee
            max_loss_cap = float(risk_per_unit * qty)
            if effective_max_trade_margin_loss_fraction > 0.0:
                max_loss_cap = min(
                    max_loss_cap,
                    float(margin_used) * float(effective_max_trade_margin_loss_fraction),
                )
            open_trades[symbol] = _OpenTrade(
                symbol=symbol,
                side=signal_side,
                entry_price=float(entry_fill),
                quantity=float(qty),
                initial_quantity=float(qty),
                entry_fee=float(entry_fee),
                entry_notional=float(entry_notional),
                margin_used=float(margin_used),
                max_loss_cap=max_loss_cap,
                tp=tp,
                sl=sl,
                initial_risk_abs=float(risk_per_unit * qty),
                time_stop_bars=_to_int((decision.get("execution") or {}).get("time_stop_bars"))
                if isinstance(decision.get("execution"), dict)
                else None,
                progress_check_bars=_to_int((decision.get("execution") or {}).get("progress_check_bars"))
                if isinstance(decision.get("execution"), dict)
                else 0,
                progress_min_mfe_r=_to_float((decision.get("execution") or {}).get("progress_min_mfe_r"))
                if isinstance(decision.get("execution"), dict)
                else 0.0,
                progress_extend_trigger_r=_to_float(
                    (decision.get("execution") or {}).get("progress_extend_trigger_r")
                )
                if isinstance(decision.get("execution"), dict)
                else 0.0,
                progress_extend_bars=_to_int((decision.get("execution") or {}).get("progress_extend_bars"))
                if isinstance(decision.get("execution"), dict)
                else 0,
                effective_leverage=float(max_effective_leverage),
                regime=str(decision.get("regime") or "").strip().upper() or None,
                entry_tick=int(tick),
                entry_time_ms=int(float(row.get("open_time") or 0.0)),
                stop_exit_cooldown_bars=_to_int((decision.get("execution") or {}).get("stop_exit_cooldown_bars"))
                if isinstance(decision.get("execution"), dict)
                else max(int(reverse_cooldown_bars), 0),
                profit_exit_cooldown_bars=_to_int((decision.get("execution") or {}).get("profit_exit_cooldown_bars"))
                if isinstance(decision.get("execution"), dict)
                else 0,
                alpha_id=str(candidate.alpha_id or decision.get("alpha_id") or "").strip() or None,
                entry_family=str(candidate.entry_family or decision.get("entry_family") or "").strip()
                or None,
                entry_tier=str(decision.get("entry_tier") or "").strip() or None,
                entry_quality_score_v2=_to_float((decision.get("execution") or {}).get("entry_quality_score_v2"))
                if isinstance(decision.get("execution"), dict)
                else 0.0,
                entry_regime_strength=_to_float((decision.get("execution") or {}).get("entry_regime_strength"))
                if isinstance(decision.get("execution"), dict)
                else 0.0,
                entry_bias_strength=_to_float((decision.get("execution") or {}).get("entry_bias_strength"))
                if isinstance(decision.get("execution"), dict)
                else 0.0,
                quality_exit_applied=bool((decision.get("execution") or {}).get("quality_exit_applied"))
                if isinstance(decision.get("execution"), dict)
                else False,
                selective_extension_proof_bars=_to_int(
                    (decision.get("execution") or {}).get("selective_extension_proof_bars")
                )
                if isinstance(decision.get("execution"), dict)
                else 0,
                selective_extension_min_mfe_r=_to_float(
                    (decision.get("execution") or {}).get("selective_extension_min_mfe_r")
                )
                if isinstance(decision.get("execution"), dict)
                else 0.0,
                selective_extension_min_regime_strength=_to_float(
                    (decision.get("execution") or {}).get("selective_extension_min_regime_strength")
                )
                if isinstance(decision.get("execution"), dict)
                else 0.0,
                selective_extension_min_bias_strength=_to_float(
                    (decision.get("execution") or {}).get("selective_extension_min_bias_strength")
                )
                if isinstance(decision.get("execution"), dict)
                else 0.0,
                selective_extension_min_quality_score_v2=_to_float(
                    (decision.get("execution") or {}).get("selective_extension_min_quality_score_v2")
                )
                if isinstance(decision.get("execution"), dict)
                else 0.0,
                selective_extension_time_stop_bars=_to_int(
                    (decision.get("execution") or {}).get("selective_extension_time_stop_bars")
                )
                if isinstance(decision.get("execution"), dict)
                else 0,
                selective_extension_take_profit_r=_to_float(
                    (decision.get("execution") or {}).get("selective_extension_take_profit_r")
                )
                if isinstance(decision.get("execution"), dict)
                else 0.0,
                selective_extension_move_stop_to_be_at_r=_to_float(
                    (decision.get("execution") or {}).get("selective_extension_move_stop_to_be_at_r")
                )
                if isinstance(decision.get("execution"), dict)
                else 0.0,
            )
            open_symbols.add(symbol)
            day_trade_counter[(day_key, symbol)] = int(day_trade_counter.get((day_key, symbol), 0)) + 1
            free_slots -= 1

        unrealized = 0.0
        for symbol, trade in open_trades.items():
            candle = candles.get(symbol)
            if not isinstance(candle, dict):
                continue
            close = float(candle.get("close", trade.entry_price))
            qty = max(float(trade.quantity), 0.0)
            if trade.side == "BUY":
                unrealized += (close - float(trade.entry_price)) * qty
            else:
                unrealized += (float(trade.entry_price) - close) * qty
        current_equity = float(initial_capital) + realized_pnl + unrealized
        equity_curve.append(current_equity)
        simultaneous_position_histogram[len(open_trades)] += 1
        portfolio_open_slots_usage[len(open_trades)] += 1
        if entry_equity > 0.0:
            capital_utilization_samples.append(
                sum(float(trade.margin_used) for trade in open_trades.values()) / float(entry_equity)
            )

        for decision in decisions.values():
            if not isinstance(decision, dict):
                continue
            alpha_blocks = decision.get("alpha_blocks")
            if not isinstance(alpha_blocks, dict):
                continue
            for alpha_id, reason in alpha_blocks.items():
                alpha_name = str(alpha_id or "").strip()
                reason_name = str(reason or "").strip()
                if not alpha_name or not reason_name:
                    continue
                entry_block_counter[reason_name] += 0

    if open_trades and rows:
        last_row = rows[-1]
        candles = last_row.get("candles") if isinstance(last_row.get("candles"), dict) else {}
        for symbol in list(open_trades.keys()):
            trade = open_trades.get(symbol)
            candle = candles.get(symbol)
            if trade is None or not isinstance(candle, dict):
                continue
            _close_trade(trade=trade, candle=candle, tick=len(rows) - 1, reason="end_of_data")
        if equity_curve:
            equity_curve[-1] = float(initial_capital) + realized_pnl

    alpha_block_distribution: dict[str, Counter[str]] = {}
    for row in rows:
        decisions = row.get("decisions")
        if not isinstance(decisions, dict):
            continue
        for decision in decisions.values():
            if not isinstance(decision, dict):
                continue
            alpha_blocks = decision.get("alpha_blocks")
            if not isinstance(alpha_blocks, dict):
                continue
            for alpha_id, reason in alpha_blocks.items():
                alpha_name = str(alpha_id or "").strip()
                reason_name = str(reason or "").strip()
                if not alpha_name or not reason_name:
                    continue
                alpha_block_distribution.setdefault(alpha_name, Counter())[reason_name] += 1

    gross_profit = sum(max(0.0, float(item["pnl"])) for item in trade_events)
    gross_loss = abs(sum(min(0.0, float(item["pnl"])) for item in trade_events))
    wins = sum(1 for item in trade_events if float(item["pnl"]) > 0)
    losses = sum(1 for item in trade_events if float(item["pnl"]) < 0)
    total_trades = len(trade_events)
    final_equity = float(initial_capital) + realized_pnl
    total_return_pct = (
        ((final_equity - float(initial_capital)) / float(initial_capital)) * 100.0
        if float(initial_capital) > 0.0
        else 0.0
    )
    profit_factor = float("inf") if gross_loss <= 0 and gross_profit > 0 else 0.0
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    win_rate_pct = ((wins / total_trades) * 100.0) if total_trades > 0 else 0.0
    max_drawdown_pct = _calc_max_drawdown_pct(equity_curve)
    avg_capital_utilization = (
        sum(capital_utilization_samples) / len(capital_utilization_samples)
        if capital_utilization_samples
        else 0.0
    )
    max_capital_utilization = max(capital_utilization_samples) if capital_utilization_samples else 0.0
    alpha_stats = _summarize_alpha_stats(
        trade_events=trade_events,
        alpha_block_distribution=alpha_block_distribution,
        initial_capital=float(initial_capital),
    )
    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate_pct, 4),
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "gross_trade_pnl": round(gross_trade_pnl_total, 6),
        "total_fees": round(total_fees, 6),
        "total_funding_pnl": round(total_funding_pnl, 6),
        "net_profit": round(realized_pnl, 6),
        "profit_factor": None if profit_factor == float("inf") else round(profit_factor, 6),
        "profit_factor_infinite": bool(profit_factor == float("inf")),
        "max_drawdown_pct": round(max_drawdown_pct, 6),
        "initial_capital": float(initial_capital),
        "final_equity": round(final_equity, 6),
        "total_return_pct": round(total_return_pct, 6),
        "entry_block_distribution": dict(entry_block_counter),
        "bucket_block_distribution": {str(key): int(value) for key, value in bucket_block_counter.items()},
        "simultaneous_position_histogram": {
            str(key): int(value) for key, value in simultaneous_position_histogram.items()
        },
        "portfolio_open_slots_usage": {
            str(key): int(value) for key, value in portfolio_open_slots_usage.items()
        },
        "capital_utilization": {
            "avg_pct": round(avg_capital_utilization * 100.0, 6),
            "max_pct": round(max_capital_utilization * 100.0, 6),
        },
        "state_distribution": dict(state_distribution),
        "alpha_block_distribution": {
            alpha_id: dict(counter) for alpha_id, counter in alpha_block_distribution.items()
        },
        "alpha_stats": alpha_stats,
        "trading_lock_reason": trading_lock_reason,
        "trade_events": trade_events,
    }
