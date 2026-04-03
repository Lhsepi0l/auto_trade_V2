from __future__ import annotations

from v2.kernel.contracts import KernelContext
from v2.strategies.alpha_shared import _Bar
from v2.strategies.ra_2026_alpha_v2 import (
    RA2026AlphaV2,
    RA2026AlphaV2CandidateSelector,
    RA2026AlphaV2Params,
    _common_cost_reason,
    _expansion_cost_near_pass_allowed,
    _SharedContext,
)


def _bars(
    closes: list[float],
    *,
    wick: float = 0.45,
    body_shift: float = 0.12,
    volume_base: float = 1000.0,
    volume_step: float = 4.0,
) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    prev_close = float(closes[0])
    for idx, close in enumerate(closes):
        close_v = float(close)
        open_v = prev_close - body_shift if close_v >= prev_close else prev_close + body_shift
        rows.append(
            {
                "open": float(open_v),
                "high": float(max(open_v, close_v) + wick),
                "low": float(min(open_v, close_v) - wick),
                "close": close_v,
                "volume": float(volume_base + (idx * volume_step)),
            }
        )
        prev_close = close_v
    return rows


def _market_for_breakout() -> dict[str, list[dict[str, float]]]:
    closes_4h = [100.0 + (idx * 1.4) for idx in range(220)]
    closes_1h = [220.0 + (idx * 0.22) for idx in range(90)]
    closes_15m = [300.0 + (idx * 0.08) for idx in range(75)] + [306.0, 306.1, 306.2, 306.3, 309.0]
    market = {
        "4h": _bars(closes_4h, wick=0.9, body_shift=0.35, volume_base=1800.0, volume_step=3.0),
        "1h": _bars(closes_1h, wick=0.6, body_shift=0.18, volume_base=1400.0, volume_step=2.0),
        "15m": _bars(closes_15m, wick=0.35, body_shift=0.10, volume_base=900.0, volume_step=6.0),
    }
    market["15m"][-1]["volume"] = 2200.0
    return market


def _market_for_pullback() -> dict[str, list[dict[str, float]]]:
    closes_4h = [100.0 + (idx * 1.2) for idx in range(220)]
    closes_1h = [220.0 + (idx * 0.18) for idx in range(90)]
    closes_15m = [300.0 + (idx * 0.10) for idx in range(72)] + [307.0, 306.1, 305.1, 304.7, 304.9, 305.4, 305.9, 306.4]
    market = {
        "4h": _bars(closes_4h, wick=0.8, body_shift=0.30, volume_base=1700.0, volume_step=2.5),
        "1h": _bars(closes_1h, wick=0.55, body_shift=0.16, volume_base=1350.0, volume_step=2.0),
        "15m": _bars(closes_15m, wick=0.30, body_shift=0.12, volume_base=950.0, volume_step=5.0),
    }
    market["15m"][-1]["volume"] = 2100.0
    return market


def _market_for_expansion() -> dict[str, list[dict[str, float]]]:
    closes_4h = [100.0 + ((idx % 4) * 0.05) for idx in range(220)]
    closes_1h = [220.0 + (idx * 0.18) for idx in range(90)]
    closes_15m = [300.0 + ((idx % 2) * 0.01) for idx in range(78)] + [300.0, 300.0, 303.5]
    market = {
        "4h": _bars(closes_4h, wick=0.25, body_shift=0.03, volume_base=1600.0, volume_step=0.0),
        "1h": _bars(closes_1h, wick=0.55, body_shift=0.14, volume_base=1300.0, volume_step=2.0),
        "15m": _bars(closes_15m, wick=0.05, body_shift=0.02, volume_base=700.0, volume_step=0.5),
    }
    market["15m"][-1]["high"] = 305.2
    market["15m"][-1]["low"] = 298.8
    market["15m"][-1]["volume"] = 2600.0
    return market


def _market_for_unstable_expansion() -> dict[str, list[dict[str, float]]]:
    market = _market_for_expansion()
    market["15m"][-1]["open"] = 300.2
    market["15m"][-1]["close"] = 303.5
    market["15m"][-1]["high"] = 306.4
    market["15m"][-1]["low"] = 299.8
    market["15m"][-1]["volume"] = 2600.0
    return market


def _market_for_borderline_expansion() -> dict[str, list[dict[str, float]]]:
    market = _market_for_expansion()
    for bar in market["15m"]:
        bar["volume"] = 2000.0
    market["15m"][-1]["volume"] = 1800.0
    market["15m"][-1]["open"] = 302.9
    market["15m"][-1]["close"] = 303.5
    market["15m"][-1]["high"] = 305.0
    market["15m"][-1]["low"] = 299.2
    return market


def _market_for_weak_expansion_after_relaxation() -> dict[str, list[dict[str, float]]]:
    market = _market_for_borderline_expansion()
    market["15m"][-1]["close"] = 300.4
    market["15m"][-1]["high"] = 301.0
    market["15m"][-1]["low"] = 299.9
    return market


def _market_for_short_expansion() -> dict[str, list[dict[str, float]]]:
    closes_4h = [400.0 - (idx * 0.7) for idx in range(220)]
    closes_1h = [250.0 - (idx * 0.10) for idx in range(90)]
    closes_15m = [300.0 + (((idx % 3) - 1) * 0.08) for idx in range(78)] + [300.0, 299.9, 298.7]
    market = {
        "4h": _bars(closes_4h, wick=0.6, body_shift=0.20, volume_base=1600.0, volume_step=2.0),
        "1h": _bars(closes_1h, wick=0.35, body_shift=0.12, volume_base=1300.0, volume_step=2.0),
        "15m": _bars(closes_15m, wick=0.35, body_shift=0.06, volume_base=900.0, volume_step=1.0),
    }
    market["15m"][-1]["volume"] = 2600.0
    return market


def _market_for_overextended_short_expansion() -> dict[str, list[dict[str, float]]]:
    market = _market_for_short_expansion()
    market["15m"][-1]["close"] = 298.2
    market["15m"][-1]["high"] = 298.95
    market["15m"][-1]["low"] = 297.95
    return market


def _market_for_drift() -> dict[str, list[dict[str, float]]]:
    closes_4h = [100.0 + (idx * 1.0) for idx in range(220)]
    closes_1h = [220.0 + (idx * 0.20) for idx in range(90)]
    closes_15m = [300.0 + ((idx % 2) * 0.01) for idx in range(79)] + [299.95]
    market = {
        "4h": _bars(closes_4h, wick=0.8, body_shift=0.30, volume_base=1700.0, volume_step=2.0),
        "1h": _bars(closes_1h, wick=0.55, body_shift=0.16, volume_base=1350.0, volume_step=2.0),
        "15m": _bars(closes_15m, wick=0.05, body_shift=0.02, volume_base=950.0, volume_step=4.0),
    }
    market["15m"][-1]["open"] = 297.90
    market["15m"][-1]["close"] = 299.45
    market["15m"][-1]["high"] = 300.65
    market["15m"][-1]["low"] = 297.75
    market["15m"][-1]["volume"] = 2400.0
    return market


def _market_for_failed_drift() -> dict[str, list[dict[str, float]]]:
    market = _market_for_drift()
    market["15m"][-1]["open"] = 298.80
    market["15m"][-1]["close"] = 300.25
    market["15m"][-1]["high"] = 300.70
    market["15m"][-1]["low"] = 298.60
    return market


def _market_for_drift_confirm() -> dict[str, list[dict[str, float]]]:
    closes_4h = [100.0 + (idx * 1.0) for idx in range(220)]
    closes_1h = [220.0 + (idx * 0.20) for idx in range(90)]
    closes_15m = [300.0 + ((idx % 2) * 0.01) for idx in range(79)] + [299.45, 300.20]
    market = {
        "4h": _bars(closes_4h, wick=0.8, body_shift=0.30, volume_base=1700.0, volume_step=2.0),
        "1h": _bars(closes_1h, wick=0.55, body_shift=0.16, volume_base=1350.0, volume_step=2.0),
        "15m": _bars(closes_15m, wick=0.05, body_shift=0.02, volume_base=950.0, volume_step=4.0),
    }
    market["15m"][-2]["open"] = 297.90
    market["15m"][-2]["close"] = 299.45
    market["15m"][-2]["high"] = 300.65
    market["15m"][-2]["low"] = 297.75
    market["15m"][-2]["volume"] = 2400.0
    market["15m"][-1]["open"] = 299.55
    market["15m"][-1]["close"] = 300.35
    market["15m"][-1]["high"] = 300.50
    market["15m"][-1]["low"] = 299.50
    market["15m"][-1]["volume"] = 2400.0
    return market


def _flat_market() -> dict[str, list[dict[str, float]]]:
    closes_4h = [100.0 + ((idx % 2) * 0.02) for idx in range(220)]
    closes_1h = [200.0 + ((idx % 2) * 0.01) for idx in range(90)]
    closes_15m = [300.0 + ((idx % 2) * 0.01) for idx in range(90)]
    return {
        "4h": _bars(closes_4h, wick=0.08, body_shift=0.01, volume_base=800.0, volume_step=0.0),
        "1h": _bars(closes_1h, wick=0.05, body_shift=0.01, volume_base=750.0, volume_step=0.0),
        "15m": _bars(closes_15m, wick=0.04, body_shift=0.01, volume_base=700.0, volume_step=0.0),
    }


def _sample_shared_context(*, expected_move_frac: float, required_move_frac: float, spread_bps: float) -> _SharedContext:
    bar = _Bar(open=100.0, high=101.0, low=99.0, close=100.5, volume=1500.0)
    return _SharedContext(
        symbol="BTCUSDT",
        candles_4h=[bar],
        candles_1h=[bar],
        candles_15m=[bar, bar],
        regime="TREND_UP",
        regime_side="LONG",
        regime_block_reason="",
        regime_strength=0.8,
        bias_side="LONG",
        bias_strength=0.7,
        atr_15m=1.0,
        ema_15m=100.0,
        vol_ratio_15m=1.2,
        current_bar=bar,
        prev_bar=bar,
        spread_estimate_bps=float(spread_bps),
        expected_move_frac=float(expected_move_frac),
        required_move_frac=float(required_move_frac),
        indicators={},
    )


def test_alpha_v2_breakout_signal_emits_breakout_alpha() -> None:
    strategy = RA2026AlphaV2(params={"enabled_alphas": ["alpha_breakout"]})

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_breakout()})

    assert decision["intent"] == "LONG"
    assert decision["side"] == "BUY"
    assert decision["alpha_id"] == "alpha_breakout"
    assert decision["entry_family"] == "breakout"
    assert decision["execution"]["time_stop_bars"] == 24


def test_alpha_v2_runtime_supported_symbols_override_allows_eth() -> None:
    strategy = RA2026AlphaV2(params={})
    strategy.set_runtime_params(supported_symbols=["BTCUSDT", "ETHUSDT"])

    decision = strategy.decide({"symbol": "ETHUSDT", "market": _flat_market()})

    assert decision["reason"] != "unsupported_symbol"


def test_alpha_v2_candidate_selector_syncs_strategy_supported_symbols() -> None:
    strategy = RA2026AlphaV2(params={})
    selector = RA2026AlphaV2CandidateSelector(
        strategy=strategy,
        symbols=["BTCUSDT"],
        snapshot_provider=lambda: {
            "symbols": {
                "ETHUSDT": _flat_market(),
            }
        },
    )

    selector.set_symbols(["ETHUSDT"])
    _ = selector.select(
        context=KernelContext(
            mode="shadow",
            profile="ra_2026_alpha_v2_expansion_verified_q070",
            symbol="BTCUSDT",
            tick=1,
            dry_run=True,
        )
    )
    decision = strategy.decide({"symbol": "ETHUSDT", "market": _flat_market()})

    assert decision["reason"] != "unsupported_symbol"


def test_alpha_v2_runtime_updates_preserve_supported_symbols() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "supported_symbols": ["ETHUSDT"],
        }
    )

    strategy.set_runtime_params(trend_adx_min_4h=18.0)

    assert strategy._cfg.supported_symbols == ("ETHUSDT",)
    decision = strategy.decide({"symbol": "ETHUSDT", "market": _flat_market()})
    assert decision["reason"] != "unsupported_symbol"


def test_alpha_v2_pullback_signal_emits_pullback_alpha() -> None:
    strategy = RA2026AlphaV2(params={"enabled_alphas": ["alpha_pullback"], "pullback_touch_atr_mult": 1.0})

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_pullback()})

    assert decision["intent"] == "LONG"
    assert decision["side"] == "BUY"
    assert decision["alpha_id"] == "alpha_pullback"
    assert decision["entry_family"] == "pullback"


def test_alpha_v2_expansion_signal_emits_expansion_alpha() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.8,
            "expansion_range_atr_min": 0.5,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.35,
            "expansion_close_location_min": 0.65,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 1.0,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_expansion()})

    assert decision["intent"] == "LONG"
    assert decision["side"] == "BUY"
    assert decision["alpha_id"] == "alpha_expansion"
    assert decision["entry_family"] == "expansion"


def test_alpha_v2_expansion_accepts_borderline_momentum_breakout() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.01,
            "expansion_range_atr_min": 0.7,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.18,
            "expansion_close_location_min": 0.35,
            "expansion_width_expansion_min": 0.02,
            "min_volume_ratio_15m": 0.90,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 1.0,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_borderline_expansion()})

    assert decision["intent"] == "LONG"
    assert decision["alpha_id"] == "alpha_expansion"


def test_alpha_v2_expansion_still_rejects_weak_breakout_after_relaxation() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.01,
            "expansion_range_atr_min": 0.7,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.18,
            "expansion_close_location_min": 0.35,
            "expansion_width_expansion_min": 0.02,
            "min_volume_ratio_15m": 0.90,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 1.0,
        }
    )

    market = _market_for_weak_expansion_after_relaxation()
    market["15m"][-1]["volume"] = 1500.0

    decision = strategy.decide({"symbol": "BTCUSDT", "market": market})

    assert decision["intent"] == "NONE"
    assert decision["alpha_blocks"]["alpha_expansion"] == "volume_missing"


def test_alpha_v2_expansion_accepts_borderline_volume_after_q070_tuning() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.35,
            "expansion_range_atr_min": 0.7,
            "expansion_buffer_bps": 2.0,
            "expansion_body_ratio_min": 0.18,
            "expansion_close_location_min": 0.35,
            "expansion_width_expansion_min": 0.02,
            "min_volume_ratio_15m": 0.8,
            "expansion_quality_score_v2_min": 0.70,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 1.0,
        }
    )

    market = _market_for_borderline_expansion()
    market["15m"][-1]["volume"] = 1600.0

    decision = strategy.decide({"symbol": "BTCUSDT", "market": market})

    assert decision["intent"] == "LONG"
    assert decision["alpha_id"] == "alpha_expansion"


def test_alpha_v2_expansion_short_signal_survives_moderate_breakout_distance() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.35,
            "expansion_range_atr_min": 0.7,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.18,
            "expansion_close_location_min": 0.35,
            "expansion_width_expansion_min": 0.02,
            "min_volume_ratio_15m": 0.8,
            "expansion_quality_score_v2_min": 0.62,
            "expansion_short_break_distance_atr_max": 1.3,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 1.0,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_short_expansion()})

    assert decision["intent"] == "SHORT"
    assert decision["side"] == "SELL"
    assert decision["alpha_id"] == "alpha_expansion"


def test_alpha_v2_expansion_rejects_overextended_short_chase() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.35,
            "expansion_range_atr_min": 0.7,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.18,
            "expansion_close_location_min": 0.35,
            "expansion_width_expansion_min": 0.02,
            "min_volume_ratio_15m": 0.8,
            "expansion_quality_score_v2_min": 0.62,
            "expansion_short_break_distance_atr_max": 1.3,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 1.0,
        }
    )

    decision = strategy.decide(
        {"symbol": "BTCUSDT", "market": _market_for_overextended_short_expansion()}
    )

    assert decision["intent"] == "NONE"
    assert decision["alpha_blocks"]["alpha_expansion"] == "short_overextension_risk"


def test_alpha_v2_drift_signal_emits_drift_alpha() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_drift"],
            "min_volume_ratio_15m": 0.9,
            "drift_range_atr_min": 1.2,
            "drift_body_ratio_min": 0.5,
            "drift_close_location_max": 0.6,
            "drift_long_width_expansion_min": 0.1,
            "drift_long_edge_ratio_min": 1.1,
            "drift_bias_rsi_long_min": 45.0,
            "drift_bias_rsi_short_max": 55.0,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 1.6,
        }
    )

    setup = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_drift(), "open_time": 1})
    confirm = strategy.decide(
        {
            "symbol": "BTCUSDT",
            "market": _market_for_drift_confirm(),
            "open_time": 1 + (15 * 60 * 1000),
        }
    )

    assert setup["intent"] == "NONE"
    assert setup["alpha_blocks"]["alpha_drift"] == "trigger_missing"
    assert confirm["intent"] == "LONG"
    assert confirm["alpha_id"] == "alpha_drift"
    assert confirm["entry_family"] == "drift"
    assert confirm["execution"]["reward_risk_reference_r"] == 1.8
    assert confirm["execution"]["time_stop_bars"] == 16


def test_alpha_v2_drift_signal_survives_verified_q070_like_profile_mix() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion", "alpha_drift"],
            "squeeze_percentile_threshold": 0.35,
            "expansion_buffer_bps": 1.5,
            "expansion_range_atr_min": 0.7,
            "expansion_body_ratio_min": 0.18,
            "expansion_close_location_min": 0.35,
            "expansion_width_expansion_min": 0.02,
            "expansion_quality_score_v2_min": 0.70,
            "expansion_short_break_distance_atr_max": 1.3,
            "min_volume_ratio_15m": 0.8,
            "expected_move_cost_mult": 1.6,
            "drift_side_mode": "LONG",
            "drift_range_atr_min": 1.2,
            "drift_body_ratio_min": 0.5,
            "drift_close_location_max": 0.6,
            "drift_long_width_expansion_min": 0.1,
            "drift_long_edge_ratio_min": 1.1,
            "drift_bias_rsi_long_min": 45.0,
            "drift_setup_expiry_bars": 8,
            "drift_take_profit_r": 1.8,
            "drift_time_stop_bars": 16,
            "min_stop_distance_frac": 0.0005,
        }
    )

    setup = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_drift(), "open_time": 1})
    confirm = strategy.decide(
        {
            "symbol": "BTCUSDT",
            "market": _market_for_drift_confirm(),
            "open_time": 1 + (15 * 60 * 1000),
        }
    )

    assert setup["intent"] == "NONE"
    assert confirm["intent"] == "LONG"
    assert confirm["alpha_id"] == "alpha_drift"


def test_alpha_v2_drift_rejects_wrong_close_location() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_drift"],
            "min_volume_ratio_15m": 0.9,
            "drift_range_atr_min": 1.2,
            "drift_body_ratio_min": 0.5,
            "drift_close_location_max": 0.6,
            "drift_long_width_expansion_min": 0.1,
            "drift_long_edge_ratio_min": 1.1,
            "drift_bias_rsi_long_min": 45.0,
            "drift_bias_rsi_short_max": 55.0,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 0.5,
            "min_expected_move_floor": 0.0001,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_failed_drift()})

    assert decision["intent"] == "NONE"
    assert decision["alpha_blocks"]["alpha_drift"] == "trigger_missing"


def test_alpha_v2_drift_queue_expires_without_confirm() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_drift"],
            "drift_side_mode": "LONG",
            "drift_setup_expiry_bars": 2,
        }
    )

    _ = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_drift(), "open_time": 1})
    expired = strategy.decide(
        {
            "symbol": "BTCUSDT",
            "market": _market_for_drift_confirm(),
            "open_time": 1 + (3 * 15 * 60 * 1000),
        }
    )

    assert expired["intent"] == "NONE"
    assert expired["alpha_blocks"]["alpha_drift"] == "trigger_missing"


def test_alpha_v2_expansion_emits_progress_aware_exit_hints() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.8,
            "expansion_range_atr_min": 0.5,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.35,
            "expansion_close_location_min": 0.65,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 1.0,
            "progress_check_bars": 6,
            "progress_min_mfe_r": 0.35,
            "progress_extend_trigger_r": 1.0,
            "progress_extend_bars": 6,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_expansion()})

    assert decision["intent"] == "LONG"
    assert decision["execution"]["progress_check_bars"] == 6
    assert decision["execution"]["progress_min_mfe_r"] == 0.35
    assert decision["execution"]["progress_extend_trigger_r"] == 1.0
    assert decision["execution"]["progress_extend_bars"] == 6
    assert float(decision["execution"]["entry_quality_score_v2"]) > 0.0


def test_alpha_v2_expansion_emits_selective_extension_hints() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.8,
            "expansion_range_atr_min": 0.5,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.35,
            "expansion_close_location_min": 0.65,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 1.0,
            "selective_extension_proof_bars": 6,
            "selective_extension_min_mfe_r": 0.75,
            "selective_extension_min_regime_strength": 0.55,
            "selective_extension_min_bias_strength": 0.55,
            "selective_extension_min_quality_score_v2": 0.78,
            "selective_extension_time_stop_bars": 24,
            "selective_extension_take_profit_r": 2.35,
            "selective_extension_move_stop_to_be_at_r": 0.75,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_expansion()})

    assert decision["intent"] == "LONG"
    execution = decision["execution"]
    assert execution["selective_extension_proof_bars"] == 6
    assert execution["selective_extension_min_mfe_r"] == 0.75
    assert execution["selective_extension_min_regime_strength"] == 0.55
    assert execution["selective_extension_min_bias_strength"] == 0.55
    assert execution["selective_extension_min_quality_score_v2"] == 0.78
    assert execution["selective_extension_time_stop_bars"] == 24
    assert execution["selective_extension_take_profit_r"] == 2.35
    assert execution["selective_extension_move_stop_to_be_at_r"] == 0.75
    assert float(execution["entry_regime_strength"]) >= 0.0
    assert float(execution["entry_bias_strength"]) >= 0.0
    assert float(execution["entry_quality_score_v2"]) > 0.0


def test_alpha_v2_expansion_applies_quality_conditioned_exit_for_high_quality_entry() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.8,
            "expansion_range_atr_min": 0.5,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.35,
            "expansion_close_location_min": 0.65,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 1.0,
            "time_stop_bars": 18,
            "take_profit_r": 2.0,
            "quality_exit_score_threshold": 0.1,
            "quality_exit_take_profit_r": 2.4,
            "quality_exit_time_stop_bars": 24,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_expansion()})

    assert decision["intent"] == "LONG"
    assert decision["execution"]["time_stop_bars"] == 24
    assert decision["execution"]["reward_risk_reference_r"] == 2.4
    assert decision["execution"]["quality_exit_applied"] is True


def test_alpha_v2_expansion_rejects_low_quality_score() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.8,
            "expansion_range_atr_min": 0.5,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.35,
            "expansion_close_location_min": 0.65,
            "expansion_quality_score_min": 0.99,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 7.0,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_expansion()})

    assert decision["intent"] == "NONE"
    assert decision["alpha_blocks"]["alpha_expansion"] == "quality_score_missing"


def test_alpha_v2_expansion_rejects_low_breakout_efficiency() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.8,
            "expansion_range_atr_min": 0.5,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.35,
            "expansion_close_location_min": 0.65,
            "expansion_breakout_efficiency_min": 1.0,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 1.0,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_expansion()})

    assert decision["intent"] == "NONE"
    assert decision["alpha_blocks"]["alpha_expansion"] == "breakout_efficiency_missing"


def test_alpha_v2_expansion_rejects_low_breakout_stability_score() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.8,
            "expansion_range_atr_min": 0.5,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.1,
            "expansion_close_location_min": 0.1,
            "expansion_breakout_stability_score_min": 0.75,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 1.0,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_unstable_expansion()})

    assert decision["intent"] == "NONE"
    assert decision["alpha_blocks"]["alpha_expansion"] == "breakout_stability_missing"


def test_alpha_v2_expansion_rejects_low_breakout_stability_edge_score() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.8,
            "expansion_range_atr_min": 0.5,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.1,
            "expansion_close_location_min": 0.1,
            "expansion_breakout_stability_edge_score_min": 0.70,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 7.0,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_unstable_expansion()})

    assert decision["intent"] == "NONE"
    assert decision["alpha_blocks"]["alpha_expansion"] == "breakout_stability_edge_missing"


def test_alpha_v2_expansion_rejects_low_quality_score_v2() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.8,
            "expansion_range_atr_min": 0.5,
            "expansion_buffer_bps": 0.0,
            "expansion_body_ratio_min": 0.1,
            "expansion_close_location_min": 0.1,
            "expansion_quality_score_v2_min": 0.75,
            "min_stop_distance_frac": 0.0005,
            "expected_move_cost_mult": 7.0,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_unstable_expansion()})

    assert decision["intent"] == "NONE"
    assert decision["alpha_blocks"]["alpha_expansion"] == "quality_score_v2_missing"


def test_expansion_cost_near_pass_allows_high_quality_edge_shortfall() -> None:
    cfg = RA2026AlphaV2Params.from_params(
        {
            "expansion_quality_score_v2_min": 0.62,
            "min_stop_distance_frac": 0.0005,
            "max_spread_bps": 12.0,
        }
    )
    ctx = _sample_shared_context(
        expected_move_frac=0.0097,
        required_move_frac=0.01,
        spread_bps=4.0,
    )
    stop_distance_frac = 0.002

    assert _common_cost_reason(stop_distance_frac=stop_distance_frac, ctx=ctx, cfg=cfg) == "cost_missing"
    assert (
        _expansion_cost_near_pass_allowed(
            stop_distance_frac=stop_distance_frac,
            ctx=ctx,
            cfg=cfg,
            quality_score_v2=0.74,
        )
        is True
    )


def test_expansion_cost_near_pass_rejects_low_quality_edge_shortfall() -> None:
    cfg = RA2026AlphaV2Params.from_params(
        {
            "expansion_quality_score_v2_min": 0.62,
            "min_stop_distance_frac": 0.0005,
            "max_spread_bps": 12.0,
        }
    )
    ctx = _sample_shared_context(
        expected_move_frac=0.0097,
        required_move_frac=0.01,
        spread_bps=4.0,
    )

    assert (
        _expansion_cost_near_pass_allowed(
            stop_distance_frac=0.002,
            ctx=ctx,
            cfg=cfg,
            quality_score_v2=0.65,
        )
        is False
    )


def test_alpha_v2_breakout_accepts_breakout_on_neutral_candle_body() -> None:
    market = _market_for_breakout()
    market["15m"][-1]["open"] = 309.4
    market["15m"][-1]["close"] = 309.0

    strategy = RA2026AlphaV2(params={"enabled_alphas": ["alpha_breakout"]})

    decision = strategy.decide({"symbol": "BTCUSDT", "market": market})

    assert decision["intent"] == "LONG"
    assert decision["alpha_id"] == "alpha_breakout"


def test_alpha_v2_pullback_accepts_reclaim_on_neutral_candle_body() -> None:
    market = _market_for_pullback()
    market["15m"][-1]["open"] = 306.8
    market["15m"][-1]["close"] = 306.4

    strategy = RA2026AlphaV2(
        params={"enabled_alphas": ["alpha_pullback"], "pullback_touch_atr_mult": 1.0}
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": market})

    assert decision["intent"] == "LONG"
    assert decision["alpha_id"] == "alpha_pullback"


def test_alpha_v2_pullback_still_rejects_shallow_noise_after_window_expansion() -> None:
    market = _market_for_pullback()
    market["15m"][-1]["high"] = market["15m"][-2]["high"] - 0.05
    market["15m"][-1]["close"] = market["15m"][-2]["close"] + 0.02

    strategy = RA2026AlphaV2(
        params={"enabled_alphas": ["alpha_pullback"], "pullback_touch_atr_mult": 0.25}
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": market})

    assert decision["intent"] == "NONE"
    assert decision["alpha_blocks"]["alpha_pullback"] == "trigger_missing"


def test_alpha_v2_breakout_surfaces_adx_window_regime_block() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_breakout"],
            "trend_adx_min_4h": 1.0,
            "trend_adx_max_4h": 0.1,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_breakout()})

    assert decision["intent"] == "NONE"
    assert decision["alpha_blocks"]["alpha_breakout"] == "regime_adx_window_missing"


def test_alpha_v2_breakout_surfaces_adx_rising_regime_block() -> None:
    strategy = RA2026AlphaV2(
        params={
            "enabled_alphas": ["alpha_breakout"],
            "trend_adx_min_4h": 1.0,
            "trend_adx_rising_lookback_4h": 1,
            "trend_adx_rising_min_delta_4h": 100.0,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_breakout()})

    assert decision["intent"] == "NONE"
    assert decision["alpha_blocks"]["alpha_breakout"] == "regime_adx_rising_missing"


def test_alpha_v2_surfaces_decomposed_block_reasons() -> None:
    strategy = RA2026AlphaV2(params={})

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _flat_market()})

    assert decision["intent"] == "NONE"
    assert decision["reason"] == "regime_missing"
    alpha_blocks = decision.get("alpha_blocks")
    assert isinstance(alpha_blocks, dict)
    assert alpha_blocks["alpha_breakout"] == "regime_missing"
    assert alpha_blocks["alpha_pullback"] == "regime_missing"
    assert alpha_blocks["alpha_expansion"] in {"bias_missing", "trigger_missing"}


def test_alpha_v2_surfaces_numeric_reject_metrics() -> None:
    market = _market_for_borderline_expansion()
    market["15m"][-1]["volume"] = 1500.0
    strategy = RA2026AlphaV2(
        params={"enabled_alphas": ["alpha_expansion"], "min_volume_ratio_15m": 1.0}
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": market})

    metrics = decision["alpha_reject_metrics"]["alpha_expansion"]
    assert decision["alpha_blocks"]["alpha_expansion"] == "volume_missing"
    assert metrics["vol_ratio_15m"] < metrics["min_volume_ratio_15m"]
