from __future__ import annotations

from v2.run import _BacktestExecutionModel, _Kline15m, _simulate_symbol_metrics


def test_simulate_symbol_metrics_supports_partial_then_runner_exit() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=100.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=101.4, low=100.8, close=101.1),
        _Kline15m(open_time_ms=4, close_time_ms=5, open=101.1, high=103.0, low=101.8, close=102.8),
        _Kline15m(open_time_ms=6, close_time_ms=7, open=102.8, high=103.1, low=101.0, close=101.2),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "score": 0.9},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "entry_family": "breakout",
                "regime": "TREND_UP",
                "sl_tp": {"take_profit": 101.2, "stop_loss": 99.0},
                "execution": {
                    "tp_partial_ratio": 0.25,
                    "tp_partial_price": 101.2,
                    "tp_partial_at_r": 1.2,
                    "move_stop_to_be_at_r": 1.0,
                    "runner_exit_mode": "trail_only",
                    "runner_trailing_atr_mult": 1.8,
                    "stalled_trend_timeout_bars": 32,
                    "stalled_volume_ratio_floor": 0.85,
                },
                "indicators": {"atr14_15m": 0.8, "volume_ratio_15m": 1.4, "close_30m": 102.0, "ema20_30m": 101.0},
            },
        },
        {
            "would_enter": False,
            "candidate": None,
            "size": None,
            "decision": {"indicators": {"atr14_15m": 0.8, "volume_ratio_15m": 1.3, "close_30m": 102.2, "ema20_30m": 101.2}},
        },
        {
            "would_enter": False,
            "candidate": None,
            "size": None,
            "decision": {"indicators": {"atr14_15m": 0.8, "volume_ratio_15m": 1.2, "close_30m": 102.8, "ema20_30m": 101.6}},
        },
        {
            "would_enter": False,
            "candidate": None,
            "size": None,
            "decision": {"indicators": {"atr14_15m": 0.8, "volume_ratio_15m": 1.0, "close_30m": 100.9, "ema20_30m": 101.7}},
        },
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=100.0,
        execution_model=_BacktestExecutionModel(fee_bps=0.0, slippage_bps=0.0, funding_bps_per_8h=0.0),
        min_expected_edge_over_roundtrip_cost=0.0,
        max_trades_per_day_per_symbol=100,
        reverse_cooldown_bars=0,
    )

    trade_events = metrics.get("trade_events", [])
    assert isinstance(trade_events, list)
    assert len(trade_events) == 1
    assert trade_events[0]["entry_family"] == "breakout"
    assert trade_events[0]["partial_taken"] is True
    assert trade_events[0]["reason"] in {"trail_stop", "structure_trail_exit"}


def test_simulate_symbol_metrics_uses_runner_reference_for_entry_rr_gate() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=100.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=101.4, low=100.8, close=101.1),
        _Kline15m(open_time_ms=4, close_time_ms=5, open=101.1, high=103.0, low=101.8, close=102.8),
        _Kline15m(open_time_ms=6, close_time_ms=7, open=102.8, high=103.1, low=101.0, close=101.2),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "score": 0.9},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "score": 0.9,
                "entry_family": "breakout",
                "regime": "TREND_UP",
                "sl_tp": {
                    "take_profit": 101.2,
                    "take_profit_final": 102.2,
                    "stop_loss": 99.0,
                },
                "execution": {
                    "tp_partial_ratio": 0.25,
                    "tp_partial_price": 101.2,
                    "tp_partial_at_r": 1.2,
                    "reward_risk_reference_r": 1.95,
                    "move_stop_to_be_at_r": 1.0,
                    "runner_exit_mode": "trail_only",
                    "runner_trailing_atr_mult": 1.8,
                    "stalled_trend_timeout_bars": 32,
                    "stalled_volume_ratio_floor": 0.85,
                },
                "indicators": {
                    "atr14_15m": 0.8,
                    "volume_ratio_15m": 1.4,
                    "close_30m": 102.0,
                    "ema20_30m": 101.0,
                },
            },
        },
        {
            "would_enter": False,
            "candidate": None,
            "size": None,
            "decision": {"indicators": {"atr14_15m": 0.8, "volume_ratio_15m": 1.3, "close_30m": 102.2, "ema20_30m": 101.2}},
        },
        {
            "would_enter": False,
            "candidate": None,
            "size": None,
            "decision": {"indicators": {"atr14_15m": 0.8, "volume_ratio_15m": 1.2, "close_30m": 102.8, "ema20_30m": 101.6}},
        },
        {
            "would_enter": False,
            "candidate": None,
            "size": None,
            "decision": {"indicators": {"atr14_15m": 0.8, "volume_ratio_15m": 1.0, "close_30m": 100.9, "ema20_30m": 101.7}},
        },
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=100.0,
        execution_model=_BacktestExecutionModel(fee_bps=0.0, slippage_bps=0.0, funding_bps_per_8h=0.0),
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.6,
        min_reward_risk_ratio=1.8,
        max_trades_per_day_per_symbol=100,
        reverse_cooldown_bars=0,
    )

    assert metrics["total_trades"] == 1
    assert metrics["entry_block_distribution"].get("reward_risk_block", 0) == 0
