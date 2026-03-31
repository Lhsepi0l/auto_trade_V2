from __future__ import annotations

from v2.backtest.block_samples import (
    CostSample,
    TriggerSample,
    VolumeSample,
    _summarize_cost_samples,
    _summarize_trigger_samples,
    _summarize_volume_samples,
)


def test_summarize_trigger_samples_counts_near_buffer_and_readiness() -> None:
    samples = [
        TriggerSample(
            bias_side="LONG",
            close_price=100.0,
            long_gap_bps=-4.0,
            short_gap_bps=None,
            range_atr=1.2,
            body_ratio=0.5,
            favored_close_long=0.8,
            favored_close_short=0.2,
            width_expansion_frac=0.1,
            breakout_distance_atr_long=0.0,
            breakout_distance_atr_short=0.0,
            body_ready=True,
            close_ready=True,
            short_close_ready=False,
            width_ready=True,
            squeeze_ready=True,
        ),
        TriggerSample(
            bias_side="SHORT",
            close_price=90.0,
            long_gap_bps=None,
            short_gap_bps=-7.0,
            range_atr=1.0,
            body_ratio=0.4,
            favored_close_long=0.3,
            favored_close_short=0.7,
            width_expansion_frac=0.05,
            breakout_distance_atr_long=0.0,
            breakout_distance_atr_short=0.0,
            body_ready=True,
            close_ready=False,
            short_close_ready=True,
            width_ready=True,
            squeeze_ready=True,
        ),
    ]

    summary = _summarize_trigger_samples(samples, sample_limit=2)

    assert summary["count"] == 2
    assert summary["aggregate"]["body_ready"] == 2
    assert summary["aggregate"]["close_ready"] == 2
    assert summary["aggregate"]["long_gap_lt_5bps"] == 1
    assert summary["aggregate"]["short_gap_lt_8bps"] == 1
    assert summary["aggregate"]["long_gap8_body_close_width"] == 1
    assert summary["aggregate"]["short_gap8_body_close_width"] == 1
    assert summary["near_buffer_candidates"]["count"] == 2
    assert summary["near_buffer_candidates"]["long_count"] == 1
    assert summary["near_buffer_candidates"]["short_count"] == 1
    assert len(summary["setup_candidates"]["top_rules"]) >= 1
    assert len(summary["top_samples"]) == 2


def test_summarize_volume_samples_tracks_near_gate_distribution() -> None:
    samples = [
        VolumeSample(
            bias_side="LONG",
            vol_ratio=0.85,
            min_volume_ratio=0.9,
            expected_move_frac=0.003,
            required_move_frac=0.002,
            spread_bps=1.5,
        ),
        VolumeSample(
            bias_side="LONG",
            vol_ratio=0.78,
            min_volume_ratio=0.9,
            expected_move_frac=0.0025,
            required_move_frac=0.002,
            spread_bps=1.5,
        ),
    ]

    summary = _summarize_volume_samples(samples, sample_limit=2)

    assert summary["count"] == 2
    assert summary["aggregate"]["vol_ge_084"] == 1
    assert summary["aggregate"]["vol_ge_080"] == 1
    assert summary["aggregate"]["vol_ge_075"] == 2
    assert summary["stats"]["avg_vol_ratio"] == 0.815
    assert summary["top_samples"][0]["vol_ratio"] == 0.85


def test_summarize_cost_samples_tracks_edge_pressure() -> None:
    samples = [
        CostSample(
            side="LONG",
            spread_bps=1.5,
            max_spread_bps=8.0,
            stop_distance_frac=0.004,
            min_stop_distance_frac=0.002,
            expected_move_frac=0.0024,
            required_move_frac=0.0025,
            edge_ratio=0.96,
            likely_subreason="edge_shortfall",
        ),
        CostSample(
            side="SHORT",
            spread_bps=1.5,
            max_spread_bps=8.0,
            stop_distance_frac=0.001,
            min_stop_distance_frac=0.002,
            expected_move_frac=0.0015,
            required_move_frac=0.0025,
            edge_ratio=0.60,
            likely_subreason="stop_distance_too_small",
        ),
    ]

    summary = _summarize_cost_samples(samples, sample_limit=2)

    assert summary["count"] == 2
    assert summary["aggregate"]["edge_shortfall"] == 1
    assert summary["aggregate"]["stop_distance_too_small"] == 1
    assert summary["aggregate"]["edge_ratio_ge_095"] == 1
    assert summary["stats"]["max_edge_ratio"] == 0.96
