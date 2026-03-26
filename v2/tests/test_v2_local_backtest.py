from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from v2.config.loader import load_effective_config
from v2.run import (
    _aggregate_klines_to_interval,
    _BacktestExecutionModel,
    _build_half_year_window_summaries,
    _build_local_backtest_portfolio_rows,
    _build_replay_cycle_record,
    _cache_file_for_klines,
    _calc_max_drawdown_pct,
    _cbr_research_gate,
    _cleanup_local_backtest_artifacts,
    _fb_research_gate,
    _FundingRateRow,
    _HistoricalSnapshotProvider,
    _Kline15m,
    _klines_csv_has_volume_column,
    _load_cached_klines_for_range,
    _local_backtest_profile_alpha_overrides,
    _lsr_research_gate,
    _merge_local_backtest_profile_alpha_overrides,
    _mr_research_gate,
    _parse_symbols,
    _pfd_research_gate,
    _run_local_backtest,
    _sfd_research_gate,
    _simulate_portfolio_metrics,
    _simulate_symbol_metrics,
    _summarize_alpha_stats,
    _write_klines_csv,
    _write_local_backtest_markdown,
)
from v2.tpsl import BracketConfig, BracketPlanner


def test_parse_symbols_deduplicates_and_normalizes() -> None:
    symbols = _parse_symbols(" btcusdt,ETHUSDT, ,BTCUSDT ")
    assert symbols == ["BTCUSDT", "ETHUSDT"]


def test_build_local_backtest_portfolio_rows_emits_ranked_candidates(monkeypatch) -> None:
    import v2.run as run_module

    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_candidate", mode="shadow")

    class _FakeStrategy:
        name = "ra_2026_alpha_v2"

        def __init__(self) -> None:
            self.runtime_params: dict[str, object] = {}

        def set_runtime_params(self, **kwargs):  # type: ignore[no-untyped-def]
            self.runtime_params = dict(kwargs)

        def decide(self, snapshot):  # type: ignore[no-untyped-def]
            symbol = str(snapshot.get("symbol") or "").upper()
            if symbol == "BTCUSDT":
                return {
                    "symbol": symbol,
                    "intent": "LONG",
                    "side": "BUY",
                    "score": 0.91,
                    "alpha_id": "alpha_expansion",
                    "entry_family": "expansion",
                    "reason": "entry_signal",
                    "entry_price": 101.0,
                    "risk_per_trade_pct": 0.01,
                    "max_effective_leverage": 3.0,
                    "indicators": {
                        "atr14_15m": 1.5,
                        "spread_estimate_bps": 8.0,
                    },
                    "execution": {
                        "time_stop_bars": 18,
                    },
                }
            return {
                "symbol": symbol,
                "intent": "NONE",
                "side": "NONE",
                "reason": "volume_missing",
                "alpha_blocks": {"alpha_expansion": "volume_missing"},
            }

    fake_strategy = _FakeStrategy()
    monkeypatch.setattr(
        run_module,
        "_build_strategy_selector",
        lambda **_kwargs: fake_strategy,
    )

    candles_by_symbol = {
        "BTCUSDT": {
            "15m": [
                _Kline15m(
                    open_time_ms=0,
                    close_time_ms=15 * 60 * 1000,
                    open=100.0,
                    high=102.0,
                    low=99.0,
                    close=101.0,
                    volume=1200.0,
                )
            ]
        },
        "ETHUSDT": {
            "15m": [
                _Kline15m(
                    open_time_ms=0,
                    close_time_ms=15 * 60 * 1000,
                    open=200.0,
                    high=202.0,
                    low=198.0,
                    close=201.0,
                    volume=900.0,
                )
            ]
        },
    }

    rows, state_counter = _build_local_backtest_portfolio_rows(
        cfg=cfg,
        candles_by_symbol=candles_by_symbol,
        premium_by_symbol=None,
        funding_by_symbol=None,
        market_intervals=["15m"],
        strategy_runtime_params={"enabled_alphas": ["alpha_expansion"]},
    )

    assert fake_strategy.runtime_params == {"enabled_alphas": ["alpha_expansion"]}
    assert state_counter["dry_run"] == 1
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == "dry_run"
    assert row["reason"] == "would_execute"
    assert row["ranked_candidates"][0]["symbol"] == "BTCUSDT"
    assert row["ranked_candidates"][0]["alpha_id"] == "alpha_expansion"
    assert row["candles"]["BTCUSDT"]["close"] == 101.0
    assert row["decisions"]["BTCUSDT"]["side"] == "BUY"
    assert row["decisions"]["BTCUSDT"]["execution"]["time_stop_bars"] == 18
    assert row["decisions"]["ETHUSDT"]["reason"] == "volume_missing"


def test_local_backtest_profile_alpha_overrides_maps_expansion_profiles() -> None:
    assert _local_backtest_profile_alpha_overrides("ra_2026_alpha_v2_expansion") == {
        "enabled_alphas": ["alpha_expansion"]
    }
    assert _local_backtest_profile_alpha_overrides("ra_2026_alpha_v2_expansion_verified_candidate") == {
        "enabled_alphas": ["alpha_expansion"],
        "squeeze_percentile_threshold": 0.30,
        "expansion_buffer_bps": 2.0,
        "expansion_range_atr_min": 0.7,
        "expansion_body_ratio_min": 0.18,
        "expansion_close_location_min": 0.35,
        "expansion_width_expansion_min": 0.02,
        "min_volume_ratio_15m": 0.9,
        "take_profit_r": 2.0,
        "time_stop_bars": 18,
        "trend_adx_min_4h": 14.0,
        "expected_move_cost_mult": 1.6,
    }
    assert _local_backtest_profile_alpha_overrides("ra_2026_alpha_v2_expansion_verified_q070") == {
        "enabled_alphas": ["alpha_expansion"],
        "squeeze_percentile_threshold": 0.35,
        "expansion_buffer_bps": 1.5,
        "expansion_range_atr_min": 0.7,
        "expansion_body_ratio_min": 0.18,
        "expansion_close_location_min": 0.35,
        "expansion_width_expansion_min": 0.02,
        "min_volume_ratio_15m": 0.8,
        "take_profit_r": 2.0,
        "time_stop_bars": 18,
        "trend_adx_min_4h": 14.0,
        "expected_move_cost_mult": 1.6,
        "expansion_quality_score_v2_min": 0.70,
    }
    assert _local_backtest_profile_alpha_overrides("ra_2026_alpha_v2_expansion_champion_candidate") == {
        "enabled_alphas": ["alpha_expansion"],
        "squeeze_percentile_threshold": 0.30,
        "expansion_buffer_bps": 2.0,
        "expansion_range_atr_min": 0.7,
        "expansion_body_ratio_min": 0.18,
        "expansion_close_location_min": 0.35,
        "expansion_width_expansion_min": 0.02,
        "min_volume_ratio_15m": 0.9,
        "take_profit_r": 2.0,
        "time_stop_bars": 18,
        "trend_adx_min_4h": 14.0,
        "expected_move_cost_mult": 1.6,
        "expansion_quality_score_v2_min": 0.70,
    }
    assert _local_backtest_profile_alpha_overrides("ra_2026_alpha_v2_expansion_candidate") == {
        "enabled_alphas": ["alpha_expansion"],
        "squeeze_percentile_threshold": 0.30,
        "expansion_buffer_bps": 2.0,
        "expansion_range_atr_min": 0.7,
        "expansion_body_ratio_min": 0.18,
        "expansion_close_location_min": 0.35,
        "expansion_width_expansion_min": 0.02,
        "min_volume_ratio_15m": 0.9,
        "take_profit_r": 2.0,
        "time_stop_bars": 18,
        "trend_adx_min_4h": 14.0,
        "expected_move_cost_mult": 1.6,
    }
    assert _local_backtest_profile_alpha_overrides("ra_2026_alpha_v2_expansion_live_candidate") == {
        "enabled_alphas": ["alpha_expansion"],
        "squeeze_percentile_threshold": 0.30,
        "expansion_buffer_bps": 2.0,
        "expansion_range_atr_min": 0.7,
        "expansion_body_ratio_min": 0.18,
        "expansion_close_location_min": 0.35,
        "expansion_width_expansion_min": 0.02,
        "min_volume_ratio_15m": 0.9,
        "take_profit_r": 2.0,
        "time_stop_bars": 18,
        "trend_adx_min_4h": 14.0,
        "expected_move_cost_mult": 1.6,
    }
    assert _local_backtest_profile_alpha_overrides("ra_2026_alpha_v2") == {}


def test_local_backtest_cli_alpha_overrides_win_over_profile_defaults() -> None:
    merged = _merge_local_backtest_profile_alpha_overrides(
        profile_name="ra_2026_alpha_v2_expansion_verified_q070",
        active_strategy_name="ra_2026_alpha_v2",
        strategy_runtime_params={
            "expansion_buffer_bps": 1.5,
            "expansion_body_ratio_min": 0.30,
            "expansion_close_location_min": 0.55,
            "expansion_width_expansion_min": 0.03,
            "min_volume_ratio_15m": 0.90,
        },
    )

    assert merged["enabled_alphas"] == ["alpha_expansion"]
    assert merged["squeeze_percentile_threshold"] == 0.35
    assert merged["expansion_quality_score_v2_min"] == 0.70
    assert merged["expansion_buffer_bps"] == 1.5
    assert merged["expansion_body_ratio_min"] == 0.30
    assert merged["expansion_close_location_min"] == 0.55
    assert merged["expansion_width_expansion_min"] == 0.03
    assert merged["min_volume_ratio_15m"] == 0.90


def test_calc_max_drawdown_pct() -> None:
    value = _calc_max_drawdown_pct([100.0, 110.0, 99.0, 120.0, 108.0])
    assert round(value, 2) == 10.0


def test_simulate_symbol_metrics_closes_with_take_profit() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=103.0, low=99.5, close=102.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 102.0, "stop_loss": 99.0}},
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=10000.0,
    )

    assert metrics["total_trades"] == 1
    assert metrics["wins"] == 1
    assert metrics["losses"] == 0
    assert metrics["net_profit"] == 2.0
    assert metrics["final_equity"] == 10002.0


def test_simulate_symbol_metrics_progress_time_stop_cuts_stalled_trade_early() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.2, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.3, low=99.8, close=100.1),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "alpha_id": "alpha_expansion"},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "alpha_id": "alpha_expansion",
                "sl_tp": {"take_profit": 104.0, "stop_loss": 99.0},
                "execution": {
                    "time_stop_bars": 10,
                    "progress_check_bars": 1,
                    "progress_min_mfe_r": 0.5,
                },
            },
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=10000.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.0,
    )

    trade_events = metrics["trade_events"]
    assert len(trade_events) == 1
    assert trade_events[0]["reason"] == "progress_time_stop"


def test_simulate_symbol_metrics_extends_time_stop_for_fast_progress_trade() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.2, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.8, low=99.9, close=100.4),
        _Kline15m(open_time_ms=4, close_time_ms=5, open=100.4, high=100.6, low=100.1, close=100.5),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "alpha_id": "alpha_expansion"},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "alpha_id": "alpha_expansion",
                "sl_tp": {"take_profit": 104.0, "stop_loss": 99.0},
                "execution": {
                    "time_stop_bars": 1,
                    "progress_extend_trigger_r": 0.5,
                    "progress_extend_bars": 1,
                },
            },
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=10000.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.0,
    )

    trade_events = metrics["trade_events"]
    assert len(trade_events) == 1
    assert trade_events[0]["reason"] == "time_stop"
    assert trade_events[0]["exit_tick"] == 2


def test_simulate_symbol_metrics_selective_extension_extends_time_stop_for_proven_trend_trade() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.1, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.8, low=99.9, close=100.5),
        _Kline15m(open_time_ms=4, close_time_ms=5, open=100.5, high=100.7, low=100.2, close=100.4),
        _Kline15m(open_time_ms=6, close_time_ms=7, open=100.4, high=100.6, low=100.1, close=100.3),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "alpha_id": "alpha_expansion"},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "alpha_id": "alpha_expansion",
                "sl_tp": {"take_profit": 104.0, "stop_loss": 99.0},
                "execution": {
                    "time_stop_bars": 1,
                    "entry_quality_score_v2": 0.82,
                    "entry_regime_strength": 0.60,
                    "entry_bias_strength": 0.60,
                    "selective_extension_proof_bars": 1,
                    "selective_extension_min_mfe_r": 0.5,
                    "selective_extension_min_regime_strength": 0.55,
                    "selective_extension_min_bias_strength": 0.55,
                    "selective_extension_time_stop_bars": 3,
                },
            },
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=10000.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.0,
    )

    trade_events = metrics["trade_events"]
    assert len(trade_events) == 1
    assert trade_events[0]["reason"] == "time_stop"
    assert trade_events[0]["exit_tick"] == 3
    assert trade_events[0]["selective_extension_activated"] is True


def test_simulate_symbol_metrics_selective_extension_skips_trade_without_early_proof() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.1, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.3, low=99.9, close=100.1),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "alpha_id": "alpha_expansion"},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "alpha_id": "alpha_expansion",
                "sl_tp": {"take_profit": 104.0, "stop_loss": 99.0},
                "execution": {
                    "time_stop_bars": 1,
                    "entry_quality_score_v2": 0.82,
                    "entry_regime_strength": 0.60,
                    "entry_bias_strength": 0.60,
                    "selective_extension_proof_bars": 1,
                    "selective_extension_min_mfe_r": 0.5,
                    "selective_extension_min_regime_strength": 0.55,
                    "selective_extension_min_bias_strength": 0.55,
                    "selective_extension_time_stop_bars": 3,
                },
            },
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=10000.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.0,
    )

    trade_events = metrics["trade_events"]
    assert len(trade_events) == 1
    assert trade_events[0]["reason"] == "time_stop"
    assert trade_events[0]["exit_tick"] == 1
    assert trade_events[0]["selective_extension_activated"] is False


def test_simulate_symbol_metrics_selective_extension_can_extend_take_profit_with_protection() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.1, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.8, low=99.9, close=100.5),
        _Kline15m(open_time_ms=4, close_time_ms=5, open=100.5, high=102.2, low=100.1, close=101.7),
        _Kline15m(open_time_ms=6, close_time_ms=7, open=101.7, high=102.6, low=100.8, close=102.4),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "alpha_id": "alpha_expansion"},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "alpha_id": "alpha_expansion",
                "sl_tp": {"take_profit": 102.0, "stop_loss": 99.0},
                "execution": {
                    "time_stop_bars": 4,
                    "entry_quality_score_v2": 0.84,
                    "entry_regime_strength": 0.62,
                    "entry_bias_strength": 0.61,
                    "selective_extension_proof_bars": 1,
                    "selective_extension_min_mfe_r": 0.75,
                    "selective_extension_min_regime_strength": 0.55,
                    "selective_extension_min_bias_strength": 0.55,
                    "selective_extension_time_stop_bars": 4,
                    "selective_extension_take_profit_r": 2.5,
                    "selective_extension_move_stop_to_be_at_r": 0.75,
                },
            },
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=10000.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.0,
    )

    trade_events = metrics["trade_events"]
    assert len(trade_events) == 1
    assert trade_events[0]["reason"] == "take_profit"
    assert trade_events[0]["exit_tick"] == 3
    assert round(float(trade_events[0]["exit_price"]), 2) == 102.50
    assert trade_events[0]["selective_extension_activated"] is True
    assert trade_events[0]["selective_extension_tp_applied"] is True
    assert trade_events[0]["selective_extension_protection_applied"] is True


def test_simulate_symbol_metrics_applies_fees_and_slippage() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=103.0, low=99.5, close=102.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 102.0, "stop_loss": 99.0}},
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=10000.0,
        execution_model=_BacktestExecutionModel(
            fee_bps=10.0, slippage_bps=10.0, funding_bps_per_8h=0.0
        ),
    )

    assert metrics["total_fees"] > 0.0
    assert metrics["gross_trade_pnl"] > metrics["net_profit"]


def test_simulate_symbol_metrics_tracks_alpha_stats_and_block_top() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.2, low=99.8, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=102.5, low=99.8, close=102.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "alpha_id": "alpha_breakout"},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "alpha_id": "alpha_breakout",
                "alpha_blocks": {
                    "alpha_breakout": "entry_signal",
                    "alpha_pullback": "bias_missing",
                    "alpha_expansion": "trigger_missing",
                },
                "sl_tp": {"take_profit": 102.0, "stop_loss": 99.0},
            },
        },
        {
            "would_enter": False,
            "candidate": None,
            "size": None,
            "decision": {
                "alpha_blocks": {
                    "alpha_breakout": "trigger_missing",
                    "alpha_pullback": "bias_missing",
                    "alpha_expansion": "volume_missing",
                }
            },
        },
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=10000.0,
    )

    alpha_stats = metrics.get("alpha_stats")
    assert isinstance(alpha_stats, dict)
    assert alpha_stats["alpha_breakout"]["trades"] == 1
    assert alpha_stats["alpha_breakout"]["net_profit"] == 2.0
    assert alpha_stats["alpha_pullback"]["trades"] == 0
    assert alpha_stats["alpha_pullback"]["block_top"][0]["reason"] == "bias_missing"


def test_build_half_year_window_summaries_splits_trade_events() -> None:
    start_dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
    mid_dt = datetime(2025, 3, 1, tzinfo=timezone.utc)
    late_dt = datetime(2025, 9, 1, tzinfo=timezone.utc)
    end_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    reports = [
        {
            "trade_events": [
                {"exit_time_ms": int(mid_dt.timestamp() * 1000), "pnl": 3.0},
                {"exit_time_ms": int(late_dt.timestamp() * 1000), "pnl": -1.0},
            ]
        }
    ]

    windows = _build_half_year_window_summaries(
        symbol_reports=reports,
        start_ms=int(start_dt.timestamp() * 1000),
        end_ms=int(end_dt.timestamp() * 1000),
        total_initial_capital=30.0,
    )

    assert len(windows) == 2
    assert windows[0]["trades"] == 1
    assert windows[0]["net_profit"] == 3.0
    assert windows[1]["trades"] == 1
    assert windows[1]["net_profit"] == -1.0


def test_simulate_symbol_metrics_applies_fixed_leverage_position_sizing() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=103.0, low=99.5, close=102.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0},
            "size": {"qty": 1.0, "leverage": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 102.0, "stop_loss": 99.0}},
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    baseline = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
    )
    leveraged = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        fixed_leverage=30.0,
    )

    assert leveraged["gross_trade_pnl"] > baseline["gross_trade_pnl"]
    assert leveraged["net_profit"] > baseline["net_profit"]


def test_simulate_symbol_metrics_caps_loss_by_margin_used() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=100.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.0, low=1.0, close=1.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0},
            "size": {"qty": 1.0, "leverage": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 120.0, "stop_loss": 1.0}},
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        fixed_leverage=30.0,
        fixed_leverage_margin_use_pct=0.20,
        execution_model=_BacktestExecutionModel(
            fee_bps=0.0, slippage_bps=0.0, funding_bps_per_8h=0.0
        ),
    )

    assert metrics["net_profit"] >= -6.0


def test_simulate_symbol_metrics_scales_down_position_after_drawdown() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=100.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.0, low=99.0, close=99.0),
        _Kline15m(open_time_ms=4, close_time_ms=5, open=100.0, high=100.0, low=100.0, close=100.0),
        _Kline15m(open_time_ms=6, close_time_ms=7, open=100.0, high=103.0, low=100.0, close=102.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0},
            "size": {"qty": 1.0, "leverage": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 120.0, "stop_loss": 99.0}},
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0},
            "size": {"qty": 1.0, "leverage": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 102.0, "stop_loss": 99.0}},
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        fixed_leverage=30.0,
        fixed_leverage_margin_use_pct=0.20,
        execution_model=_BacktestExecutionModel(
            fee_bps=0.0,
            slippage_bps=0.0,
            funding_bps_per_8h=0.0,
        ),
        reverse_cooldown_bars=0,
        daily_loss_limit_pct=1.0,
        equity_floor_pct=0.0,
        drawdown_scale_start_pct=0.0,
        drawdown_scale_end_pct=0.10,
        drawdown_margin_scale_min=0.5,
    )

    trade_events = metrics.get("trade_events", [])
    assert isinstance(trade_events, list)
    assert len(trade_events) == 2
    assert float(trade_events[1]["quantity"]) < float(trade_events[0]["quantity"])


def test_simulate_symbol_metrics_blocks_reverse_before_min_hold() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=101.0, low=99.0, close=100.2),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 120.0, "stop_loss": 90.0}},
        },
        {
            "would_enter": True,
            "candidate": {"side": "SELL", "entry_price": 100.2},
            "size": {"qty": 1.0},
            "decision": {"side": "SELL", "sl_tp": {"take_profit": 95.0, "stop_loss": 105.0}},
        },
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        reverse_min_hold_bars=10,
        reverse_cooldown_bars=0,
        min_expected_edge_over_roundtrip_cost=0.0,
        max_trades_per_day_per_symbol=100,
    )

    assert metrics["total_trades"] == 1
    assert metrics["entry_block_distribution"].get("reverse_hold_block", 0) >= 1


def test_simulate_symbol_metrics_blocks_entries_after_stoploss_streak_trigger() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.5, low=99.5, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.2, low=98.8, close=99.0),
        _Kline15m(open_time_ms=4, close_time_ms=5, open=99.0, high=99.4, low=98.7, close=99.1),
        _Kline15m(open_time_ms=6, close_time_ms=7, open=99.1, high=99.6, low=98.9, close=99.2),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "score": 0.8},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 120.0, "stop_loss": 99.0}},
        },
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 99.0, "score": 0.8},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 120.0, "stop_loss": 98.0}},
        },
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 99.1, "score": 0.8},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 120.0, "stop_loss": 98.0}},
        },
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 99.2, "score": 0.8},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 120.0, "stop_loss": 98.0}},
        },
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.0,
        reverse_min_hold_bars=0,
        reverse_cooldown_bars=0,
        stoploss_streak_trigger=1,
        stoploss_cooldown_bars=3,
    )

    assert metrics["total_trades"] == 1
    assert metrics["entry_block_distribution"].get("stoploss_cooldown_block", 0) >= 2


def test_simulate_symbol_metrics_blocks_entries_after_any_loss_cooldown() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.5, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.2, low=98.8, close=99.0),
        _Kline15m(open_time_ms=4, close_time_ms=5, open=99.0, high=99.3, low=98.7, close=99.1),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "score": 0.8},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 120.0, "stop_loss": 99.0}},
        },
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 99.0, "score": 0.8},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 120.0, "stop_loss": 98.0}},
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.0,
        reverse_min_hold_bars=0,
        reverse_cooldown_bars=0,
        stoploss_streak_trigger=0,
        stoploss_cooldown_bars=0,
        loss_cooldown_bars=3,
    )

    assert metrics["total_trades"] == 1
    assert metrics["entry_block_distribution"].get("loss_cooldown_block", 0) >= 1


def test_simulate_symbol_metrics_blocks_entry_when_edge_below_roundtrip_cost() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.1, low=99.9, close=100.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 100.05, "stop_loss": 99.0}},
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        execution_model=_BacktestExecutionModel(
            fee_bps=20.0, slippage_bps=10.0, funding_bps_per_8h=0.0
        ),
        min_expected_edge_over_roundtrip_cost=1.2,
    )

    assert metrics["total_trades"] == 0
    assert metrics["entry_block_distribution"].get("edge_cost_block", 0) >= 1


def test_simulate_symbol_metrics_blocks_low_reward_risk_ratio() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.1, low=99.9, close=100.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "score": 0.8},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "score": 0.8,
                "sl_tp": {"take_profit": 100.2, "stop_loss": 99.8},
            },
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.0,
        min_reward_risk_ratio=2.0,
    )

    assert metrics["total_trades"] == 0
    assert metrics["entry_block_distribution"].get("reward_risk_block", 0) >= 1


def test_simulate_symbol_metrics_blocks_when_reward_risk_missing() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.1, low=99.9, close=100.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "score": 0.8},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "score": 0.8,
                "sl_tp": {"take_profit": 101.0},
            },
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.0,
        min_reward_risk_ratio=1.0,
    )

    assert metrics["total_trades"] == 0
    assert metrics["entry_block_distribution"].get("reward_risk_missing_block", 0) >= 1


def test_simulate_symbol_metrics_stops_entries_after_daily_loss_limit() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=100.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.0, low=89.0, close=90.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 120.0, "stop_loss": 90.0}},
        },
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 90.0},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 100.0, "stop_loss": 80.0}},
        },
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        daily_loss_limit_pct=0.10,
        equity_floor_pct=0.0,
    )

    assert metrics["total_trades"] == 1
    assert metrics["entry_block_distribution"].get("daily_loss_stop_block", 0) >= 1


def test_simulate_symbol_metrics_blocks_entries_after_equity_floor() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.0, low=100.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.0, low=95.0, close=96.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 120.0, "stop_loss": 96.0}},
        },
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 96.0},
            "size": {"qty": 1.0},
            "decision": {"side": "BUY", "sl_tp": {"take_profit": 110.0, "stop_loss": 90.0}},
        },
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        daily_loss_limit_pct=1.0,
        equity_floor_pct=0.90,
    )

    assert metrics["total_trades"] == 1
    assert metrics["entry_block_distribution"].get("equity_floor_block", 0) >= 1


def test_simulate_symbol_metrics_blocks_low_score_signals() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=101.0, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=102.0, low=98.0, close=101.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "score": 0.15},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "score": 0.15,
                "sl_tp": {"take_profit": 120.0, "stop_loss": 95.0},
            },
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.4,
    )

    assert metrics["total_trades"] == 0
    assert metrics["entry_block_distribution"].get("score_quality_block", 0) >= 1


def test_simulate_symbol_metrics_blocks_reverse_exit_when_profit_too_small() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=101.0, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.2, low=99.8, close=100.1),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "score": 0.9},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "score": 0.9,
                "sl_tp": {"take_profit": 120.0, "stop_loss": 80.0},
            },
        },
        {
            "would_enter": True,
            "candidate": {"side": "SELL", "entry_price": 100.1, "score": 0.9},
            "size": {"qty": 1.0},
            "decision": {
                "side": "SELL",
                "score": 0.9,
                "sl_tp": {"take_profit": 80.0, "stop_loss": 120.0},
            },
        },
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.0,
        reverse_min_hold_bars=0,
        reverse_cooldown_bars=0,
        reverse_exit_min_profit_pct=0.005,
        reverse_exit_min_signal_score=0.0,
    )

    assert metrics["total_trades"] == 1
    assert metrics["entry_block_distribution"].get("reverse_profit_block", 0) >= 1


def test_simulate_symbol_metrics_blocks_reverse_exit_when_score_is_low() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=101.0, low=99.0, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=106.0, low=99.0, close=105.0),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "score": 0.9},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "score": 0.9,
                "sl_tp": {"take_profit": 120.0, "stop_loss": 80.0},
            },
        },
        {
            "would_enter": True,
            "candidate": {"side": "SELL", "entry_price": 105.0, "score": 0.2},
            "size": {"qty": 1.0},
            "decision": {
                "side": "SELL",
                "score": 0.2,
                "sl_tp": {"take_profit": 80.0, "stop_loss": 120.0},
            },
        },
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.0,
        reverse_min_hold_bars=0,
        reverse_cooldown_bars=0,
        reverse_exit_min_profit_pct=0.0,
        reverse_exit_min_signal_score=0.5,
    )

    assert metrics["total_trades"] == 1
    assert metrics["entry_block_distribution"].get("reverse_score_block", 0) >= 1


def test_write_local_backtest_markdown_generates_readable_summary(tmp_path: Path) -> None:
    payload = {
        "generated_at": "2026-02-28T00:00:00+00:00",
        "backtest": {"symbols": ["BTCUSDT", "ETHUSDT"], "years": 3},
        "summary": {
            "total_initial_capital": 60.0,
            "total_final_equity": 72.34,
            "total_net_profit": 12.34,
            "gross_profit": 18.2,
            "gross_loss": 5.86,
            "total_return_pct": 20.5,
            "win_rate_pct": 55.0,
            "profit_factor": 1.8,
            "max_drawdown_pct": 8.2,
            "total_trades": 10,
            "alpha_stats": {
                "alpha_breakout": {
                    "net_profit": 7.5,
                    "profit_factor": 1.7,
                    "trades": 4,
                    "max_drawdown_pct": 5.0,
                    "block_top": [{"reason": "trigger_missing", "count": 12}],
                }
            },
            "window_slices_6m": [
                {
                    "label": "2025-01-01 ~ 2025-06-30",
                    "net_profit": 6.1,
                    "profit_factor": 1.6,
                    "trades": 4,
                    "max_drawdown_pct": 4.2,
                }
            ],
            "research_gate": {
                "track": "mr-fast-kill",
                "verdict": "KEEP",
                "checks": [
                    {
                        "name": "slice_6m",
                        "verdict": "KEEP",
                        "metrics": {
                            "net_profit": 6.1,
                            "profit_factor": 1.6,
                            "max_drawdown_pct": 4.2,
                            "trades": 40,
                            "fee_to_trade_gross_pct": None,
                        },
                        "reasons": [],
                    }
                ],
            },
        },
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "net_profit": 7.0,
                "total_return_pct": 11.5,
                "win_rate_pct": 60.0,
                "total_trades": 5,
                "max_drawdown_pct": 6.0,
            }
        ],
    }
    json_path = tmp_path / "local_backtest_20260228_000000.json"
    json_path.write_text("{}", encoding="utf-8")

    md_path = _write_local_backtest_markdown(report_payload=payload, target_json=json_path)
    rendered = md_path.read_text(encoding="utf-8")

    assert md_path.name.endswith(".md")
    assert "# 로컬 백테스트 리포트" in rendered
    assert "| 총 수익액 (USDT) | 18.20 |" in rendered
    assert "| 총 손실액 (USDT) | 5.86 |" in rendered
    assert "## 연구 게이트" in rendered
    assert "| slice_6m | KEEP | 6.10 | 1.600 | 4.20 | 40 | - | - |" in rendered
    assert "## 알파별 요약" in rendered
    assert "| alpha_breakout | 7.50 | 1.700 | 4 | 5.00 | trigger_missing: 12 |" in rendered
    assert "## 6개월 구간 요약" in rendered
    assert "| 2025-01-01 ~ 2025-06-30 | 6.10 | 1.600 | 4 | 4.20 |" in rendered
    assert "| BTCUSDT | 7.00 | 11.50 | 60.00 | 5 | 6.00 |" in rendered


def test_run_local_backtest_alpha_report_includes_runtime_params(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import v2.run as run_module

    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    report_dir = tmp_path / "reports"
    cache_root = report_dir / "_cache"
    start_ms = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
    symbols = ["BTCUSDT"]

    for symbol in symbols:
        for interval, interval_ms in {
            "15m": 900_000,
            "10m": 600_000,
            "30m": 1_800_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
        }.items():
            cache_file = _cache_file_for_klines(
                cache_root=cache_root,
                symbol=symbol,
                interval=interval,
                years=1,
            )
            _write_klines_csv(
                path=cache_file,
                symbol=symbol,
                rows=[
                    _Kline15m(
                        open_time_ms=start_ms,
                        close_time_ms=start_ms + interval_ms - 1,
                        open=100.0,
                        high=101.0,
                        low=99.0,
                        close=100.5,
                        volume=1000.0,
                    )
                ],
            )

    def _fake_symbol_replay(**kwargs):
        assert kwargs["alpha_squeeze_percentile_max"] == 0.35
        assert kwargs["alpha_expansion_buffer_bps"] == 1.5
        assert kwargs["alpha_expansion_body_ratio_min"] == 0.25
        assert kwargs["alpha_expansion_close_location_min"] == 0.6
        assert kwargs["alpha_expansion_width_expansion_min"] == 0.2
        assert kwargs["alpha_expansion_break_distance_atr_min"] == 0.15
        assert kwargs["alpha_expansion_breakout_efficiency_min"] == 0.18
        assert kwargs["alpha_expansion_breakout_stability_score_min"] == 0.64
        assert kwargs["alpha_expansion_breakout_stability_edge_score_min"] == 0.61
        assert kwargs["alpha_expansion_quality_score_min"] == 0.62
        assert kwargs["alpha_expansion_quality_score_v2_min"] == 0.67
        assert kwargs["alpha_expansion_range_atr_min"] == 0.9
        assert kwargs["alpha_min_volume_ratio"] == 1.1
        assert kwargs["alpha_take_profit_r"] == 2.4
        assert kwargs["alpha_time_stop_bars"] == 18
        assert kwargs["alpha_trend_adx_min_4h"] == 12.0
        assert kwargs["alpha_trend_adx_max_4h"] == 32.0
        assert kwargs["alpha_trend_adx_rising_lookback_4h"] == 2
        assert kwargs["alpha_trend_adx_rising_min_delta_4h"] == 0.4
        assert kwargs["alpha_expected_move_cost_mult"] == 1.6
        return {
            "symbol": "BTCUSDT",
            "skipped": False,
            "report": {
                "symbol": "BTCUSDT",
                "initial_capital": 30.0,
                "final_equity": 31.8,
                "net_profit": 1.8,
                "total_return_pct": 6.0,
                "total_trades": 1,
                "wins": 1,
                "losses": 0,
                "win_rate_pct": 100.0,
                "gross_profit": 2.1,
                "gross_loss": 0.0,
                "gross_trade_pnl": 2.0,
                "total_fees": 0.2,
                "total_funding_pnl": 0.0,
                "profit_factor": 2.0,
                "max_drawdown_pct": 1.5,
                "entry_block_distribution": {"volume_missing": 2},
                "alpha_stats": {
                    "alpha_expansion": {
                        "trades": 1,
                        "wins": 1,
                        "losses": 0,
                        "net_profit": 1.8,
                        "gross_profit": 2.1,
                        "gross_loss": 0.0,
                        "profit_factor": 2.0,
                        "max_drawdown_pct": 1.5,
                        "block_top": [{"reason": "volume_missing", "count": 2}],
                    }
                },
                "alpha_block_distribution": {"alpha_expansion": {"volume_missing": 2}},
                "trade_events": [
                    {
                        "symbol": "BTCUSDT",
                        "alpha_id": "alpha_expansion",
                        "entry_family": "expansion",
                        "gross_pnl": 2.0,
                        "entry_fee": 0.1,
                        "exit_fee": 0.1,
                        "pnl": 1.8,
                        "entry_time_ms": start_ms,
                        "exit_time_ms": start_ms + 900_000,
                        "entry_tick": 0,
                        "exit_tick": 1,
                    }
                ],
            },
            "state_distribution": {"dry_run": 1},
            "total_cycles": 1,
        }

    monkeypatch.setattr(run_module, "_run_local_backtest_symbol_replay_worker", _fake_symbol_replay)

    report_path = report_dir / "alpha_report.json"
    rc = _run_local_backtest(
        cfg,
        symbols=symbols,
        years=1,
        initial_capital=30.0,
        fee_bps=4.0,
        slippage_bps=2.0,
        funding_bps_per_8h=0.5,
        margin_use_pct=10.0,
        replay_workers=0,
        reverse_min_hold_bars=0,
        reverse_cooldown_bars=0,
        min_expected_edge_multiple=2.0,
        min_reward_risk_ratio=1.0,
        max_trades_per_day=4,
        daily_loss_limit_pct=3.0,
        equity_floor_pct=50.0,
        max_trade_margin_loss_fraction=30.0,
        min_signal_score=0.4,
        reverse_exit_min_profit_pct=0.4,
        reverse_exit_min_signal_score=0.6,
        drawdown_scale_start_pct=12.0,
        drawdown_scale_end_pct=32.0,
        drawdown_margin_scale_min=35.0,
        stoploss_streak_trigger=2,
        stoploss_cooldown_bars=24,
        loss_cooldown_bars=30,
        alpha_squeeze_percentile_max=0.35,
        alpha_expansion_buffer_bps=1.5,
        alpha_expansion_body_ratio_min=0.25,
        alpha_expansion_close_location_min=0.6,
        alpha_expansion_width_expansion_min=0.2,
        alpha_expansion_break_distance_atr_min=0.15,
        alpha_expansion_breakout_efficiency_min=0.18,
        alpha_expansion_breakout_stability_score_min=0.64,
        alpha_expansion_breakout_stability_edge_score_min=0.61,
        alpha_expansion_quality_score_min=0.62,
        alpha_expansion_quality_score_v2_min=0.67,
        alpha_expansion_range_atr_min=0.9,
        alpha_min_volume_ratio=1.1,
        alpha_take_profit_r=2.4,
        alpha_time_stop_bars=18,
        alpha_trend_adx_min_4h=12.0,
        alpha_trend_adx_max_4h=32.0,
        alpha_trend_adx_rising_lookback_4h=2,
        alpha_trend_adx_rising_min_delta_4h=0.4,
        alpha_expected_move_cost_mult=1.6,
        fb_failed_break_buffer_bps=4.0,
        fb_wick_ratio_min=1.25,
        fb_take_profit_r=1.6,
        fb_time_stop_bars=8,
        cbr_squeeze_percentile_max=0.20,
        cbr_breakout_buffer_bps=3.0,
        cbr_take_profit_r=2.2,
        cbr_time_stop_bars=14,
        cbr_trend_adx_min_4h=14.0,
        cbr_ema_gap_trend_min_frac_4h=0.0030,
        cbr_breakout_min_range_atr=0.90,
        cbr_breakout_min_volume_ratio=1.0,
        sfd_reclaim_sweep_buffer_bps=3.0,
        sfd_reclaim_wick_ratio_min=1.2,
        sfd_drive_breakout_range_atr_min=0.90,
        sfd_take_profit_r=1.5,
        pfd_premium_z_min=2.2,
        pfd_funding_24h_min=0.00030,
        pfd_reclaim_buffer_atr=0.20,
        pfd_take_profit_r=2.0,
        offline=True,
        fetch_sleep_sec=0.0,
        backtest_start_ms=start_ms,
        backtest_end_ms=end_ms,
        report_dir=str(report_dir),
        report_path=str(report_path),
    )

    assert rc == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    backtest = payload["backtest"]
    summary = payload["summary"]
    assert backtest["strategy_name"] == "ra_2026_alpha_v2"
    assert backtest["alpha_squeeze_percentile_max"] == 0.35
    assert backtest["alpha_expansion_buffer_bps"] == 1.5
    assert backtest["alpha_expansion_body_ratio_min"] == 0.25
    assert backtest["alpha_expansion_close_location_min"] == 0.6
    assert backtest["alpha_expansion_width_expansion_min"] == 0.2
    assert backtest["alpha_expansion_break_distance_atr_min"] == 0.15
    assert backtest["alpha_expansion_breakout_efficiency_min"] == 0.18
    assert backtest["alpha_expansion_breakout_stability_score_min"] == 0.64
    assert backtest["alpha_expansion_breakout_stability_edge_score_min"] == 0.61
    assert backtest["alpha_expansion_quality_score_min"] == 0.62
    assert backtest["alpha_expansion_quality_score_v2_min"] == 0.67
    assert backtest["alpha_expansion_range_atr_min"] == 0.9
    assert backtest["alpha_min_volume_ratio"] == 1.1
    assert backtest["alpha_take_profit_r"] == 2.4
    assert backtest["alpha_time_stop_bars"] == 18
    assert backtest["alpha_trend_adx_min_4h"] == 12.0
    assert backtest["alpha_trend_adx_max_4h"] == 32.0
    assert backtest["alpha_trend_adx_rising_lookback_4h"] == 2
    assert backtest["alpha_trend_adx_rising_min_delta_4h"] == 0.4
    assert backtest["alpha_expected_move_cost_mult"] == 1.6
    assert "alpha_expansion" in summary["alpha_stats"]
    assert "strategy_meta" not in summary


def test_build_half_year_window_summaries_includes_fee_efficiency() -> None:
    window = _build_half_year_window_summaries(
        symbol_reports=[
            {
                "trade_events": [
                    {
                        "exit_time_ms": int(datetime(2025, 3, 1, tzinfo=timezone.utc).timestamp() * 1000),
                        "gross_pnl": 2.0,
                        "entry_fee": 0.3,
                        "exit_fee": 0.2,
                        "pnl": 1.5,
                    },
                    {
                        "exit_time_ms": int(datetime(2025, 4, 1, tzinfo=timezone.utc).timestamp() * 1000),
                        "gross_pnl": -0.5,
                        "entry_fee": 0.1,
                        "exit_fee": 0.1,
                        "pnl": -0.7,
                    },
                ]
            }
        ],
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_initial_capital=30.0,
    )

    assert len(window) == 1
    assert window[0]["trades"] == 2
    assert window[0]["net_profit"] == 0.8
    assert window[0]["fee_to_trade_gross_pct"] == 46.666667


def test_mr_research_gate_requires_slice_and_full_year_pass() -> None:
    gate = _mr_research_gate(
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_net_profit=9.0,
        profit_factor=1.35,
        max_drawdown_pct=14.0,
        total_trades=65,
        fee_to_trade_gross_pct=55.0,
        window_slices_6m=[
            {
                "label": "2025-01-01 ~ 2025-07-01",
                "net_profit": 3.5,
                "profit_factor": 1.2,
                "trades": 32,
                "max_drawdown_pct": 8.0,
            }
        ],
    )

    assert gate["verdict"] == "KEEP"
    checks = gate["checks"]
    assert isinstance(checks, list)
    assert checks[0]["name"] == "slice_6m"
    assert checks[1]["name"] == "full_1y"

    failed = _mr_research_gate(
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_net_profit=4.0,
        profit_factor=1.10,
        max_drawdown_pct=18.0,
        total_trades=18,
        fee_to_trade_gross_pct=90.0,
        window_slices_6m=[
            {
                "label": "2025-01-01 ~ 2025-07-01",
                "net_profit": -1.0,
                "profit_factor": 0.9,
                "trades": 12,
                "max_drawdown_pct": 14.0,
            }
        ],
    )

    assert failed["verdict"] == "KILL"


def test_fb_research_gate_requires_slice_and_full_year_pass() -> None:
    gate = _fb_research_gate(
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_net_profit=9.5,
        profit_factor=1.34,
        max_drawdown_pct=14.5,
        total_trades=96,
        fee_to_trade_gross_pct=54.0,
        window_slices_6m=[
            {
                "label": "2025-01-01 ~ 2025-07-01",
                "net_profit": 3.8,
                "profit_factor": 1.22,
                "trades": 44,
                "max_drawdown_pct": 7.0,
                "fee_to_trade_gross_pct": 60.0,
            }
        ],
    )

    assert gate["verdict"] == "KEEP"
    checks = gate["checks"]
    assert isinstance(checks, list)
    assert checks[0]["name"] == "slice_6m"
    assert checks[1]["name"] == "full_1y"

    failed = _fb_research_gate(
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_net_profit=4.0,
        profit_factor=1.10,
        max_drawdown_pct=18.0,
        total_trades=25,
        fee_to_trade_gross_pct=90.0,
        window_slices_6m=[
            {
                "label": "2025-01-01 ~ 2025-07-01",
                "net_profit": -0.5,
                "profit_factor": 0.95,
                "trades": 20,
                "max_drawdown_pct": 13.0,
                "fee_to_trade_gross_pct": 72.0,
            }
        ],
    )

    assert failed["track"] == "fb-fast-kill"
    assert failed["verdict"] == "KILL"


def test_cbr_research_gate_requires_slice_and_full_year_pass() -> None:
    gate = _cbr_research_gate(
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_net_profit=8.5,
        profit_factor=1.28,
        max_drawdown_pct=13.2,
        total_trades=62,
        fee_to_trade_gross_pct=54.0,
        window_slices_6m=[
            {
                "label": "2025-01-01 ~ 2025-07-01",
                "net_profit": 2.8,
                "profit_factor": 1.18,
                "trades": 32,
                "max_drawdown_pct": 8.5,
                "fee_to_trade_gross_pct": 62.0,
            }
        ],
    )

    assert gate["track"] == "cbr-fast-kill"
    assert gate["verdict"] == "KEEP"

    failed = _cbr_research_gate(
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_net_profit=-1.0,
        profit_factor=0.98,
        max_drawdown_pct=18.0,
        total_trades=18,
        fee_to_trade_gross_pct=90.0,
        window_slices_6m=[
            {
                "label": "2025-01-01 ~ 2025-07-01",
                "net_profit": -0.4,
                "profit_factor": 1.0,
                "trades": 20,
                "max_drawdown_pct": 13.0,
                "fee_to_trade_gross_pct": 75.0,
            }
        ],
    )

    assert failed["track"] == "cbr-fast-kill"
    assert failed["verdict"] == "KILL"


def test_lsr_research_gate_requires_slice_and_full_year_pass() -> None:
    gate = _lsr_research_gate(
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_net_profit=8.2,
        profit_factor=1.26,
        max_drawdown_pct=11.8,
        total_trades=58,
        fee_to_trade_gross_pct=54.0,
        window_slices_6m=[
            {
                "label": "2025-01-01 ~ 2025-07-01",
                "net_profit": 2.6,
                "profit_factor": 1.18,
                "trades": 30,
                "max_drawdown_pct": 7.5,
                "fee_to_trade_gross_pct": 62.0,
            }
        ],
    )

    assert gate["track"] == "lsr-fast-kill"
    assert gate["verdict"] == "KEEP"

    failed = _lsr_research_gate(
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_net_profit=-0.4,
        profit_factor=1.02,
        max_drawdown_pct=15.0,
        total_trades=18,
        fee_to_trade_gross_pct=88.0,
        window_slices_6m=[
            {
                "label": "2025-01-01 ~ 2025-07-01",
                "net_profit": -0.1,
                "profit_factor": 1.04,
                "trades": 19,
                "max_drawdown_pct": 13.0,
                "fee_to_trade_gross_pct": 75.0,
            }
        ],
    )

    assert failed["track"] == "lsr-fast-kill"
    assert failed["verdict"] == "KILL"


def test_sfd_research_gate_requires_slice_and_full_year_pass() -> None:
    gate = _sfd_research_gate(
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_net_profit=12.5,
        profit_factor=1.36,
        max_drawdown_pct=10.5,
        total_trades=110,
        fee_to_trade_gross_pct=58.0,
        window_slices_6m=[
            {
                "label": "2025-01-01 ~ 2025-07-01",
                "net_profit": 2.8,
                "profit_factor": 1.20,
                "trades": 52,
                "max_drawdown_pct": 8.0,
                "fee_to_trade_gross_pct": 64.0,
            }
        ],
    )

    assert gate["track"] == "sfd-session-core"
    assert gate["verdict"] == "KEEP"
    checks = gate["checks"]
    assert isinstance(checks, list)
    assert checks[0]["name"] == "slice_6m"
    assert checks[1]["name"] == "full_1y"

    failed = _sfd_research_gate(
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_net_profit=-0.4,
        profit_factor=0.98,
        max_drawdown_pct=13.5,
        total_trades=22,
        fee_to_trade_gross_pct=88.0,
        window_slices_6m=[
            {
                "label": "2025-01-01 ~ 2025-07-01",
                "net_profit": -0.4,
                "profit_factor": 0.98,
                "trades": 22,
                "max_drawdown_pct": 13.5,
                "fee_to_trade_gross_pct": 88.0,
            }
        ],
    )

    assert failed["track"] == "sfd-session-core"
    assert failed["verdict"] == "KILL"


def test_pfd_research_gate_requires_slice_and_full_year_pass() -> None:
    gate = _pfd_research_gate(
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_net_profit=13.0,
        profit_factor=1.38,
        max_drawdown_pct=10.0,
        total_trades=95,
        fee_to_trade_gross_pct=52.0,
        window_slices_6m=[
            {
                "label": "2025-01-01 ~ 2025-07-01",
                "net_profit": 3.2,
                "profit_factor": 1.22,
                "trades": 42,
                "max_drawdown_pct": 7.5,
                "fee_to_trade_gross_pct": 55.0,
            }
        ],
    )

    assert gate["track"] == "pfd-crowding-core"
    assert gate["verdict"] == "KEEP"
    checks = gate["checks"]
    assert isinstance(checks, list)
    assert checks[0]["name"] == "slice_6m"
    assert checks[1]["name"] == "full_1y"

    failed = _pfd_research_gate(
        start_ms=int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        end_ms=int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp() * 1000),
        total_net_profit=-0.3,
        profit_factor=1.01,
        max_drawdown_pct=12.8,
        total_trades=18,
        fee_to_trade_gross_pct=88.0,
        window_slices_6m=[
            {
                "label": "2025-01-01 ~ 2025-07-01",
                "net_profit": -0.3,
                "profit_factor": 1.01,
                "trades": 18,
                "max_drawdown_pct": 12.8,
                "fee_to_trade_gross_pct": 88.0,
            }
        ],
    )

    assert failed["track"] == "pfd-crowding-core"
    assert failed["verdict"] == "KILL"


def test_historical_snapshot_provider_includes_premium_and_funding_context() -> None:
    candle = _Kline15m(
        open_time_ms=299 * 900_000,
        close_time_ms=(300 * 900_000) - 1,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1500.0,
    )
    premium_rows = [
        _Kline15m(
            open_time_ms=idx * 900_000,
            close_time_ms=((idx + 1) * 900_000) - 1,
            open=-0.0010 + (idx * 0.00001),
            high=-0.0008 + (idx * 0.00001),
            low=-0.0012 + (idx * 0.00001),
            close=-0.0010 + (idx * 0.00001),
            volume=0.0,
        )
        for idx in range(300)
    ]
    funding_rows = [
        _FundingRateRow(
            funding_time_ms=idx * 8 * 60 * 60 * 1000,
            funding_rate=-0.00005 - (idx * 0.00001),
        )
        for idx in range(10)
    ]

    provider = _HistoricalSnapshotProvider(
        symbol="BTCUSDT",
        candles_15m=[candle],
        premium_rows_15m=premium_rows,
        funding_rows=funding_rows,
    )
    snapshot = provider()
    market = snapshot["market"]

    assert market["premium"]["close_15m"] is not None
    assert market["premium"]["zscore_24h"] is not None
    assert market["premium"]["zscore_3d"] is not None
    assert market["funding"]["last"] is not None
    assert market["funding"]["sum_24h"] is not None
    assert market["funding"]["sum_3d"] is not None


def test_historical_snapshot_provider_includes_10m_30m_1h_4h_context() -> None:
    candles_15m = [
        _Kline15m(
            open_time_ms=900_000,
            close_time_ms=1_799_999,
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1200,
        ),
        _Kline15m(
            open_time_ms=1_800_000,
            close_time_ms=2_699_999,
            open=100,
            high=102,
            low=98,
            close=101,
            volume=1400,
        ),
    ]
    candles_10m = [
        _Kline15m(
            open_time_ms=600_000,
            close_time_ms=1_199_999,
            open=99,
            high=100,
            low=98,
            close=99.5,
            volume=700,
        ),
        _Kline15m(
            open_time_ms=1_200_000,
            close_time_ms=1_799_999,
            open=99.5,
            high=101,
            low=99,
            close=100,
            volume=800,
        ),
    ]
    candles_30m = [
        _Kline15m(
            open_time_ms=0,
            close_time_ms=1_799_999,
            open=98,
            high=101,
            low=97,
            close=100,
            volume=1500,
        )
    ]
    candles_1h = [
        _Kline15m(
            open_time_ms=0,
            close_time_ms=3_599_999,
            open=97,
            high=102,
            low=96,
            close=101,
            volume=2300,
        )
    ]
    candles_4h = [
        _Kline15m(
            open_time_ms=0,
            close_time_ms=14_399_999,
            open=95,
            high=103,
            low=94,
            close=102,
            volume=6400,
        )
    ]

    provider = _HistoricalSnapshotProvider(
        symbol="BTCUSDT",
        candles_15m=candles_15m,
        candles_10m=candles_10m,
        candles_30m=candles_30m,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
    )
    snapshot = provider()
    market = snapshot.get("market", {})

    assert isinstance(market, dict)
    assert len(market.get("10m", [])) >= 1
    assert len(market.get("30m", [])) >= 1
    assert len(market.get("1h", [])) == 0
    assert len(market.get("4h", [])) == 0
    assert len(market.get("15m", [])) == 1
    assert market["15m"][0]["volume"] == 1200.0


def test_cleanup_local_backtest_artifacts_removes_csv_and_sqlite(tmp_path: Path) -> None:
    csv_path = tmp_path / "backtest_btcusdt_15m_20260228_000000.csv"
    sqlite_path = tmp_path / "backtest_btcusdt_20260228_000000.sqlite3"
    report_path = tmp_path / "local_backtest_20260228_000000.json"

    csv_path.write_text("symbol,open\n", encoding="utf-8")
    sqlite_path.write_text("sqlite", encoding="utf-8")
    report_path.write_text("{}", encoding="utf-8")

    removed = _cleanup_local_backtest_artifacts(tmp_path)

    assert removed == 2
    assert not csv_path.exists()
    assert not sqlite_path.exists()
    assert report_path.exists()


def test_load_cached_klines_for_range_returns_cached_rows(tmp_path: Path) -> None:
    cache_file = _cache_file_for_klines(
        cache_root=tmp_path / "_cache",
        symbol="BTCUSDT",
        interval="15m",
        years=3,
    )
    rows = [
        _Kline15m(
            open_time_ms=0,
            close_time_ms=899_999,
            open=1.0,
            high=1.1,
            low=0.9,
            close=1.05,
            volume=11.0,
        ),
        _Kline15m(
            open_time_ms=900_000,
            close_time_ms=1_799_999,
            open=1.05,
            high=1.2,
            low=1.0,
            close=1.1,
            volume=12.0,
        ),
        _Kline15m(
            open_time_ms=1_800_000,
            close_time_ms=2_699_999,
            open=1.1,
            high=1.3,
            low=1.05,
            close=1.2,
            volume=13.0,
        ),
        _Kline15m(
            open_time_ms=2_700_000,
            close_time_ms=3_599_999,
            open=1.2,
            high=1.35,
            low=1.1,
            close=1.25,
            volume=14.0,
        ),
    ]
    _write_klines_csv(path=cache_file, symbol="BTCUSDT", rows=rows)

    out = _load_cached_klines_for_range(
        path=cache_file,
        interval="15m",
        start_ms=0,
        end_ms=3_600_000,
    )

    assert _klines_csv_has_volume_column(cache_file) is True
    assert len(out) == 4
    assert out[0].volume == 11.0


def test_legacy_kline_cache_without_volume_column_is_detected(tmp_path: Path) -> None:
    cache_file = _cache_file_for_klines(
        cache_root=tmp_path / "_cache",
        symbol="BTCUSDT",
        interval="15m",
        years=1,
    )
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        "symbol,open_time_ms,open,high,low,close,close_time_ms\n"
        "BTCUSDT,0,1.0,1.1,0.9,1.05,899999\n",
        encoding="utf-8",
    )

    rows = _load_cached_klines_for_range(
        path=cache_file,
        interval="15m",
        start_ms=0,
        end_ms=900_000,
    )

    assert _klines_csv_has_volume_column(cache_file) is False
    assert len(rows) == 1
    assert rows[0].volume == 0.0


def test_aggregate_5m_rows_to_10m_rows() -> None:
    rows = [
        _Kline15m(
            open_time_ms=0,
            close_time_ms=299_999,
            open=1.0,
            high=2.0,
            low=0.8,
            close=1.5,
            volume=10.0,
        ),
        _Kline15m(
            open_time_ms=300_000,
            close_time_ms=599_999,
            open=1.5,
            high=2.5,
            low=1.2,
            close=2.2,
            volume=20.0,
        ),
        _Kline15m(
            open_time_ms=600_000,
            close_time_ms=899_999,
            open=2.2,
            high=2.6,
            low=2.0,
            close=2.4,
            volume=30.0,
        ),
    ]

    out = _aggregate_klines_to_interval(rows, target_interval_ms=600_000)

    assert len(out) == 2
    assert out[0].open_time_ms == 0
    assert out[0].close_time_ms == 599_999
    assert out[0].open == 1.0
    assert out[0].high == 2.5
    assert out[0].low == 0.8
    assert out[0].close == 2.2
    assert out[0].volume == 30.0


def test_build_replay_cycle_record_uses_candidate_entry_for_sl_tp() -> None:
    cycle = SimpleNamespace(
        candidate=SimpleNamespace(
            symbol="BTCUSDT",
            side="BUY",
            score=0.9,
            entry_price=100.0,
            reason="entry_adaptive_long",
        ),
        risk=SimpleNamespace(allow=True, reason="allow", max_notional=None),
        size=SimpleNamespace(qty=0.1, notional=10.0, leverage=30.0, reason="size_ok"),
        execution=SimpleNamespace(ok=True, order_id="shadow-1", reason="planned_execution"),
        state="dry_run",
        reason="would_execute",
    )
    decision = {
        "decision": {
            "intent": "LONG",
            "side": "BUY",
            "score": 0.9,
            "regime": "BULL",
            "allowed_side": "LONG",
            "reason": "entry_adaptive_long",
        }
    }
    planner = BracketPlanner(cfg=BracketConfig(take_profit_pct=0.02, stop_loss_pct=0.01))

    row = _build_replay_cycle_record(
        tick=1,
        cycle=cycle,
        decision=decision,
        symbol="BTCUSDT",
        bracket_planner=planner,
    )

    assert row["would_enter"] is True
    assert row["decision"]["sl_tp"] is not None


def test_build_replay_cycle_record_builds_decision_from_flat_payload() -> None:
    cycle = SimpleNamespace(
        candidate=SimpleNamespace(
            symbol="ETHUSDT",
            side="SELL",
            score=0.8,
            entry_price=200.0,
            reason="entry_adaptive_short",
        ),
        risk=SimpleNamespace(allow=True, reason="allow", max_notional=None),
        size=SimpleNamespace(qty=0.1, notional=10.0, leverage=30.0, reason="size_ok"),
        execution=SimpleNamespace(ok=True, order_id="shadow-2", reason="planned_execution"),
        state="dry_run",
        reason="would_execute",
    )
    flat_decision = {
        "intent": "SHORT",
        "side": "SELL",
        "score": 0.8,
        "reason": "entry_adaptive_short",
    }
    planner = BracketPlanner(cfg=BracketConfig(take_profit_pct=0.02, stop_loss_pct=0.01))

    row = _build_replay_cycle_record(
        tick=2,
        cycle=cycle,
        decision=flat_decision,
        symbol="ETHUSDT",
        bracket_planner=planner,
    )

    assert row["decision"]["side"] == "SELL"
    assert row["decision"]["intent"] == "SHORT"
    assert row["decision"]["sl_tp"] is not None


def test_simulate_symbol_metrics_respects_allow_reverse_exit_false() -> None:
    candles = [
        _Kline15m(open_time_ms=0, close_time_ms=1, open=100.0, high=100.5, low=99.5, close=100.0),
        _Kline15m(open_time_ms=2, close_time_ms=3, open=100.0, high=100.4, low=99.8, close=100.1),
        _Kline15m(open_time_ms=4, close_time_ms=5, open=100.1, high=101.0, low=100.0, close=100.9),
    ]
    rows = [
        {
            "would_enter": True,
            "candidate": {"side": "BUY", "entry_price": 100.0, "score": 0.8, "alpha_id": "alpha_breakout"},
            "size": {"qty": 1.0},
            "decision": {
                "side": "BUY",
                "alpha_id": "alpha_breakout",
                "sl_tp": {"take_profit": 104.0, "stop_loss": 99.0},
                "execution": {"allow_reverse_exit": False},
            },
        },
        {
            "would_enter": True,
            "candidate": {"side": "SELL", "entry_price": 100.1, "score": 0.9, "alpha_id": "alpha_expansion"},
            "size": {"qty": 1.0},
            "decision": {
                "side": "SELL",
                "alpha_id": "alpha_expansion",
                "sl_tp": {"take_profit": 96.0, "stop_loss": 101.0},
                "execution": {"allow_reverse_exit": False},
            },
        },
        {"would_enter": False, "candidate": None, "size": None, "decision": {}},
    ]

    metrics = _simulate_symbol_metrics(
        symbol="BTCUSDT",
        rows=rows,
        candles_15m=candles,
        initial_capital=30.0,
        min_expected_edge_over_roundtrip_cost=0.0,
        min_signal_score=0.0,
        reverse_min_hold_bars=0,
        reverse_cooldown_bars=0,
    )

    assert metrics["total_trades"] == 1
    assert metrics["entry_block_distribution"].get("reverse_disabled_block", 0) >= 1


def test_summarize_alpha_stats_includes_block_top() -> None:
    trade_events = [
        {"alpha_id": "alpha_breakout", "pnl": 3.0, "exit_time_ms": 1, "exit_tick": 1, "entry_time_ms": 0, "entry_tick": 0},
        {"alpha_id": "alpha_breakout", "pnl": -1.0, "exit_time_ms": 3, "exit_tick": 3, "entry_time_ms": 2, "entry_tick": 2},
        {"alpha_id": "alpha_pullback", "pnl": 2.0, "exit_time_ms": 5, "exit_tick": 5, "entry_time_ms": 4, "entry_tick": 4},
    ]

    alpha_stats = _summarize_alpha_stats(
        trade_events=trade_events,
        alpha_block_distribution={
            "alpha_breakout": {"trigger_missing": 5, "volume_missing": 2},
            "alpha_pullback": {"bias_missing": 3},
        },
        initial_capital=30.0,
    )

    assert alpha_stats["alpha_breakout"]["trades"] == 2
    assert alpha_stats["alpha_breakout"]["block_top"][0]["reason"] == "trigger_missing"
    assert alpha_stats["alpha_pullback"]["net_profit"] == 2.0


def test_build_half_year_window_summaries_slices_trade_events() -> None:
    start_dt = datetime(2023, 1, 1, tzinfo=timezone.utc)
    mid_dt = datetime(2023, 7, 1, tzinfo=timezone.utc)
    end_dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
    windows = _build_half_year_window_summaries(
        symbol_reports=[
            {
                "trade_events": [
                    {"pnl": 2.0, "exit_time_ms": int(start_dt.timestamp() * 1000) + 1},
                    {"pnl": -1.0, "exit_time_ms": int(mid_dt.timestamp() * 1000) + 1},
                ]
            }
        ],
        start_ms=int(start_dt.timestamp() * 1000),
        end_ms=int(end_dt.timestamp() * 1000),
        total_initial_capital=30.0,
    )

    assert len(windows) == 2
    assert windows[0]["trades"] == 1
    assert windows[1]["trades"] == 1


def test_simulate_portfolio_metrics_tracks_shared_slots_and_bucket_blocks() -> None:
    rows = [
        {
            "tick": 0,
            "open_time": 0,
            "state": "dry_run",
            "reason": "would_execute",
            "ranked_candidates": [
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "score": 0.90,
                    "portfolio_bucket": "majors",
                    "alpha_id": "alpha_breakout",
                    "entry_family": "breakout",
                    "entry_price": 100.0,
                    "risk_per_trade_pct": 0.012,
                    "max_effective_leverage": 10.0,
                },
                {
                    "symbol": "ETHUSDT",
                    "side": "BUY",
                    "score": 0.89,
                    "portfolio_bucket": "majors",
                    "alpha_id": "alpha_breakout",
                    "entry_family": "breakout",
                    "entry_price": 100.0,
                    "risk_per_trade_pct": 0.012,
                    "max_effective_leverage": 10.0,
                },
                {
                    "symbol": "SOLUSDT",
                    "side": "BUY",
                    "score": 0.88,
                    "portfolio_bucket": "alts",
                    "alpha_id": "alpha_expansion",
                    "entry_family": "expansion",
                    "entry_price": 100.0,
                    "risk_per_trade_pct": 0.012,
                    "max_effective_leverage": 10.0,
                },
            ],
            "decisions": {
                "BTCUSDT": {
                    "regime": "TREND_UP",
                    "alpha_id": "alpha_breakout",
                    "entry_family": "breakout",
                    "sl_tp": {"take_profit": 102.0, "stop_loss": 99.0},
                    "execution": {"time_stop_bars": 12, "stop_exit_cooldown_bars": 0},
                },
                "ETHUSDT": {
                    "regime": "TREND_UP",
                    "alpha_id": "alpha_breakout",
                    "entry_family": "breakout",
                    "sl_tp": {"take_profit": 102.0, "stop_loss": 99.0},
                    "execution": {"time_stop_bars": 12, "stop_exit_cooldown_bars": 0},
                },
                "SOLUSDT": {
                    "regime": "TREND_UP",
                    "alpha_id": "alpha_expansion",
                    "entry_family": "expansion",
                    "sl_tp": {"take_profit": 102.0, "stop_loss": 99.0},
                    "execution": {"time_stop_bars": 12, "stop_exit_cooldown_bars": 0},
                },
            },
            "candles": {
                "BTCUSDT": {
                    "open_time_ms": 0.0,
                    "close_time_ms": 1.0,
                    "open": 100.0,
                    "high": 100.5,
                    "low": 99.5,
                    "close": 100.0,
                    "volume": 1000.0,
                },
                "ETHUSDT": {
                    "open_time_ms": 0.0,
                    "close_time_ms": 1.0,
                    "open": 100.0,
                    "high": 100.4,
                    "low": 99.6,
                    "close": 100.0,
                    "volume": 1000.0,
                },
                "SOLUSDT": {
                    "open_time_ms": 0.0,
                    "close_time_ms": 1.0,
                    "open": 100.0,
                    "high": 100.6,
                    "low": 99.4,
                    "close": 100.0,
                    "volume": 1000.0,
                },
            },
        },
        {
            "tick": 1,
            "open_time": 2,
            "state": "no_candidate",
            "reason": "no_candidate",
            "ranked_candidates": [],
            "decisions": {},
            "candles": {
                "BTCUSDT": {
                    "open_time_ms": 2.0,
                    "close_time_ms": 3.0,
                    "open": 100.0,
                    "high": 102.5,
                    "low": 99.8,
                    "close": 102.0,
                    "volume": 1000.0,
                },
                "SOLUSDT": {
                    "open_time_ms": 2.0,
                    "close_time_ms": 3.0,
                    "open": 100.0,
                    "high": 102.5,
                    "low": 99.8,
                    "close": 102.0,
                    "volume": 1000.0,
                },
            },
        },
    ]

    metrics = _simulate_portfolio_metrics(
        rows=rows,
        initial_capital=30.0,
        execution_model=_BacktestExecutionModel(fee_bps=0.0, slippage_bps=0.0, funding_bps_per_8h=0.0),
        fixed_leverage_margin_use_pct=0.10,
        max_open_positions=2,
        max_new_entries_per_tick=2,
        reverse_cooldown_bars=0,
        max_trades_per_day_per_symbol=20,
        drawdown_scale_start_pct=0.12,
        drawdown_scale_end_pct=0.32,
        drawdown_margin_scale_min=0.35,
    )

    assert metrics["total_trades"] == 2
    assert metrics["bucket_block_distribution"]["majors"] == 1
    assert metrics["portfolio_open_slots_usage"]["2"] >= 1
    assert metrics["capital_utilization"]["max_pct"] > 0.0
