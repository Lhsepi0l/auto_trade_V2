from __future__ import annotations

import json

from v2.backtest.event_tape import (
    EventTapeRow,
    _forward_outcomes,
    _hypothetical_expansion_fields,
    _parse_horizons,
    _signed_close_return_bps,
    _summarize_event_tape,
    build_event_tape_report,
)
from v2.backtest.snapshots import _Kline15m
from v2.strategies.ra_2026_alpha_v2 import RA2026AlphaV2
from v2.tests.test_ra_2026_alpha_v2 import _market_for_borderline_expansion


def _kline(*, open_time_ms: int, open_: float, high: float, low: float, close: float) -> _Kline15m:
    return _Kline15m(
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + 899_999,
        open=float(open_),
        high=float(high),
        low=float(low),
        close=float(close),
        volume=1000.0,
    )


def test_signed_close_return_bps_supports_long_and_short() -> None:
    assert round(_signed_close_return_bps(entry_close=100.0, future_close=101.0, side="LONG"), 6) == 100.0
    assert round(_signed_close_return_bps(entry_close=100.0, future_close=99.0, side="SHORT"), 6) == 101.010101


def test_forward_outcomes_computes_returns_and_excursions() -> None:
    rows = [
        _kline(open_time_ms=0, open_=100.0, high=100.5, low=99.5, close=100.0),
        _kline(open_time_ms=900_000, open_=100.0, high=102.0, low=99.0, close=101.0),
        _kline(open_time_ms=1_800_000, open_=101.0, high=103.0, low=98.0, close=99.0),
    ]

    long_outcomes = _forward_outcomes(candles_15m=rows, idx=0, side="LONG", horizons=(1, 2))
    short_outcomes = _forward_outcomes(candles_15m=rows, idx=0, side="SHORT", horizons=(1, 2))

    assert long_outcomes["forward_close_return_bps_1"] == 100.0
    assert long_outcomes["forward_close_return_bps_2"] == -100.0
    assert long_outcomes["forward_mfe_bps"] == 300.0
    assert long_outcomes["forward_mae_bps"] == -200.0
    assert round(float(short_outcomes["forward_close_return_bps_1"]), 6) == -99.009901
    assert round(float(short_outcomes["forward_close_return_bps_2"]), 6) == 101.010101


def test_summarize_event_tape_groups_reasons() -> None:
    rows = [
        EventTapeRow(
            open_time_ms=0,
            close_time_ms=1,
            open_time_utc="2026-01-01T00:00:00Z",
            close_time_utc="2026-01-01T00:14:59Z",
            event_state="candidate",
            decision_reason="entry_signal",
            alpha_reason="entry_signal",
            alpha_id="alpha_expansion",
            side_hint="LONG",
            regime="TREND_UP",
            bias_side="LONG",
            score=0.8,
            vol_ratio_15m=1.1,
            spread_estimate_bps=3.0,
            expected_move_frac=0.01,
            required_move_frac=0.008,
            edge_ratio=1.25,
            range_atr=1.2,
            body_ratio=0.5,
            favored_close_long=0.8,
            favored_close_short=0.2,
            width_expansion_frac=0.1,
            breakout_distance_atr_long=0.3,
            breakout_distance_atr_short=0.0,
            quality_score_v2=0.8,
            forward_window_bars=16,
            forward_close_return_bps_4=50.0,
            forward_close_return_bps_8=60.0,
            forward_close_return_bps_12=70.0,
            forward_close_return_bps_16=80.0,
            forward_mfe_bps=120.0,
            forward_mae_bps=-30.0,
        ),
        EventTapeRow(
            open_time_ms=2,
            close_time_ms=3,
            open_time_utc="2026-01-01T00:15:00Z",
            close_time_utc="2026-01-01T00:29:59Z",
            event_state="blocked",
            decision_reason="trigger_missing",
            alpha_reason="trigger_missing",
            alpha_id="alpha_expansion",
            side_hint="LONG",
            regime="TREND_UP",
            bias_side="LONG",
            score=0.0,
            vol_ratio_15m=0.9,
            spread_estimate_bps=4.0,
            expected_move_frac=0.009,
            required_move_frac=0.01,
            edge_ratio=0.9,
            range_atr=0.8,
            body_ratio=0.3,
            favored_close_long=0.6,
            favored_close_short=0.4,
            width_expansion_frac=0.03,
            breakout_distance_atr_long=0.0,
            breakout_distance_atr_short=0.0,
            quality_score_v2=0.0,
            forward_window_bars=16,
            forward_close_return_bps_4=10.0,
            forward_close_return_bps_8=5.0,
            forward_close_return_bps_12=-2.0,
            forward_close_return_bps_16=-5.0,
            forward_mfe_bps=40.0,
            forward_mae_bps=-25.0,
        ),
    ]

    summary = _summarize_event_tape(rows, horizons=(4, 8, 12, 16))

    assert summary["count"] == 2
    assert summary["candidate_count"] == 1
    assert summary["reason_counts"][0][0] in {"entry_signal", "trigger_missing"}
    assert summary["candidate_avg_close_return_bps_16"] == 80.0


def test_parse_horizons_dedupes_and_defaults() -> None:
    assert _parse_horizons("4,8,8,16") == (4, 8, 16)
    assert _parse_horizons("") == (4, 8, 12, 16)


def test_build_event_tape_report_writes_artifacts(monkeypatch, tmp_path) -> None:
    sample_rows = [
        EventTapeRow(
            open_time_ms=0,
            close_time_ms=1,
            open_time_utc="2026-01-01T00:00:00Z",
            close_time_utc="2026-01-01T00:14:59Z",
            event_state="candidate",
            decision_reason="entry_signal",
            alpha_reason="entry_signal",
            alpha_id="alpha_expansion",
            side_hint="LONG",
            regime="TREND_UP",
            bias_side="LONG",
            score=0.8,
            vol_ratio_15m=1.2,
            spread_estimate_bps=3.0,
            expected_move_frac=0.01,
            required_move_frac=0.008,
            edge_ratio=1.25,
            range_atr=1.0,
            body_ratio=0.5,
            favored_close_long=0.8,
            favored_close_short=0.2,
            width_expansion_frac=0.1,
            breakout_distance_atr_long=0.3,
            breakout_distance_atr_short=0.0,
            quality_score_v2=0.82,
            forward_window_bars=16,
            forward_close_return_bps_4=25.0,
            forward_close_return_bps_8=35.0,
            forward_close_return_bps_12=45.0,
            forward_close_return_bps_16=55.0,
            forward_mfe_bps=90.0,
            forward_mae_bps=-20.0,
        )
    ]

    monkeypatch.setattr(
        "v2.backtest.event_tape._build_event_tape_rows",
        lambda **kwargs: sample_rows,
    )

    report, json_path, md_path, csv_path = build_event_tape_report(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        symbol="BTCUSDT",
        start_utc="2025-03-28T00:00:00Z",
        end_utc="2025-03-29T00:00:00Z",
        horizons=(4, 8, 12, 16),
        report_dir=str(tmp_path),
    )

    assert report["summary"]["count"] == 1
    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"]["candidate_count"] == 1
    assert "Alpha Expansion Event Tape" in md_path.read_text(encoding="utf-8")
    assert "forward_close_return_bps_16" in csv_path.read_text(encoding="utf-8")


def test_hypothetical_expansion_fields_smoke() -> None:
    strategy = RA2026AlphaV2(params={"enabled_alphas": ["alpha_expansion"]})
    fields = _hypothetical_expansion_fields(
        market=_market_for_borderline_expansion(),
        symbol="BTCUSDT",
        cfg=strategy._cfg,
    )

    assert "hypothetical_cost_subreason" in fields
    assert "hypothetical_quality_score_v2" in fields
