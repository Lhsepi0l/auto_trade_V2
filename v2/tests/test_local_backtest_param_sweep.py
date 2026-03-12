from __future__ import annotations

import pytest

from local_backtest.param_sweep import (
    YearResult,
    _build_cases,
    _build_parser,
    _objective,
    _parse_float_list,
    _parse_int_list,
    _year_gate_reasons,
)


def _parse_args(*argv: str):
    parser = _build_parser()
    return parser.parse_args(list(argv))


def test_parse_float_list_deduplicates() -> None:
    values = _parse_float_list("0.4,0.42,0.4, 0.5")
    assert values == [0.4, 0.42, 0.5]


def test_parse_int_list_deduplicates() -> None:
    values = _parse_int_list("1,2,1, 3")
    assert values == [1, 2, 3]


def test_parser_defaults_use_quick_mode() -> None:
    args = _parse_args()
    assert args.action == "sweep"
    assert args.profile == "ra_2026_alpha_v2_expansion_live_candidate"
    assert args.years == "1"
    assert args.verify_years == "3"
    assert args.preselect_top_k == 6
    assert args.max_cases == 12
    assert args.alpha_squeeze_percentile_values == "0.40"
    assert args.alpha_expansion_buffer_values == "2.0"
    assert args.alpha_expansion_body_ratio_values == "0.0"
    assert args.alpha_expansion_close_location_values == "0.0"
    assert args.alpha_expansion_range_values == "1.0"
    assert args.alpha_min_volume_values == "1.0"
    assert args.alpha_take_profit_r_values == "2.0"
    assert args.alpha_time_stop_values == "24"
    assert args.alpha_trend_adx_values == "14.0"
    assert args.alpha_expected_move_cost_values == "2.0"
    assert args.fb_failed_break_buffer_values == "4.0"
    assert args.fb_wick_ratio_values == "1.25"
    assert args.fb_take_profit_r_values == "1.6"
    assert args.fb_time_stop_values == "8"
    assert args.cbr_squeeze_percentile_values == "0.20"
    assert args.cbr_breakout_buffer_values == "3.0"
    assert args.cbr_take_profit_r_values == "2.2"
    assert args.cbr_time_stop_values == "14"
    assert args.cbr_trend_adx_values == "14.0"
    assert args.cbr_ema_gap_trend_values == "0.0030"
    assert args.cbr_breakout_min_range_values == "0.90"
    assert args.cbr_breakout_min_volume_values == "1.0"
    assert args.sfd_reclaim_sweep_buffer_values == "3.0"
    assert args.sfd_reclaim_wick_ratio_values == "1.2"
    assert args.sfd_drive_breakout_range_values == "0.90"
    assert args.sfd_take_profit_r_values == "1.5"
    assert args.pfd_premium_z_values == "1.8,2.2"
    assert args.pfd_funding_24h_values == "0.00020,0.00030"
    assert args.pfd_reclaim_buffer_atr_values == "0.10,0.20"
    assert args.pfd_take_profit_r_values == "1.6,2.0"


def test_parser_accepts_gate_action() -> None:
    args = _parse_args("--action", "gate")
    assert args.action == "gate"


def test_build_cases_rejects_risky_loss_cap_without_flag() -> None:
    args = _parse_args("--max-trade-margin-loss-fraction-values", "15,30")
    with pytest.raises(ValueError):
        _build_cases(args)


def test_build_cases_accepts_risky_loss_cap_with_flag() -> None:
    args = _parse_args(
        "--allow-risky-loss-cap",
        "--max-trade-margin-loss-fraction-values",
        "15,30",
        "--score-values",
        "0.42",
        "--rr-values",
        "2.0",
        "--max-trades-values",
        "1",
        "--reverse-cooldown-values",
        "30",
        "--margin-use-values",
        "10",
        "--daily-loss-values",
        "2.5",
        "--drawdown-margin-scale-min-values",
        "35",
        "--fb-failed-break-buffer-values",
        "4.0",
        "--fb-wick-ratio-values",
        "1.25",
        "--fb-take-profit-r-values",
        "1.6",
        "--fb-time-stop-values",
        "8",
        "--cbr-squeeze-percentile-values",
        "0.20",
        "--cbr-breakout-buffer-values",
        "3.0",
        "--cbr-take-profit-r-values",
        "2.2",
        "--cbr-time-stop-values",
        "14",
        "--cbr-trend-adx-values",
        "14.0",
        "--cbr-ema-gap-trend-values",
        "0.0030",
        "--cbr-breakout-min-range-values",
        "0.90",
        "--cbr-breakout-min-volume-values",
        "1.0",
    )
    cases = _build_cases(args)
    assert len(cases) == 2
    assert sorted({case.max_trade_margin_loss_fraction_pct for case in cases}) == [15.0, 30.0]
    assert {case.fb_failed_break_buffer_bps for case in cases} == {4.0}
    assert {case.fb_wick_ratio_min for case in cases} == {1.25}
    assert {case.fb_take_profit_r for case in cases} == {1.6}
    assert {case.fb_time_stop_bars for case in cases} == {8}
    assert {case.cbr_squeeze_percentile_max for case in cases} == {0.20}
    assert {case.cbr_breakout_buffer_bps for case in cases} == {3.0}
    assert {case.cbr_take_profit_r for case in cases} == {2.2}
    assert {case.cbr_time_stop_bars for case in cases} == {14}
    assert {case.cbr_trend_adx_min_4h for case in cases} == {14.0}
    assert {case.cbr_ema_gap_trend_min_frac_4h for case in cases} == {0.0030}
    assert {case.cbr_breakout_min_range_atr for case in cases} == {0.90}
    assert {case.cbr_breakout_min_volume_ratio for case in cases} == {1.0}
    assert {case.sfd_reclaim_sweep_buffer_bps for case in cases} == {3.0}
    assert {case.sfd_reclaim_wick_ratio_min for case in cases} == {1.2}
    assert {case.sfd_drive_breakout_range_atr_min for case in cases} == {0.90}
    assert {case.sfd_take_profit_r for case in cases} == {1.5}
    assert {case.pfd_premium_z_min for case in cases} == {1.8}
    assert {case.pfd_funding_24h_min for case in cases} == {0.00020}
    assert {case.pfd_reclaim_buffer_atr for case in cases} == {0.10}
    assert {case.pfd_take_profit_r for case in cases} == {1.6}


def test_build_cases_supports_alpha_bounded_sweep_axes() -> None:
    args = _parse_args(
        "--profile",
        "ra_2026_alpha_v2_expansion",
        "--max-cases",
        "0",
        "--score-values",
        "0.42",
        "--rr-values",
        "2.0",
        "--max-trades-values",
        "1",
        "--reverse-cooldown-values",
        "30",
        "--margin-use-values",
        "10",
        "--daily-loss-values",
        "2.5",
        "--drawdown-margin-scale-min-values",
        "35",
        "--max-trade-margin-loss-fraction-values",
        "30",
        "--alpha-squeeze-percentile-values",
        "0.35,0.40",
        "--alpha-expansion-buffer-values",
        "1.0,2.0",
        "--alpha-expansion-body-ratio-values",
        "0.0,0.2",
        "--alpha-expansion-close-location-values",
        "0.0,0.6",
        "--alpha-expansion-range-values",
        "0.9,1.1",
        "--alpha-min-volume-values",
        "1.0,1.2",
        "--alpha-take-profit-r-values",
        "2.0",
        "--alpha-time-stop-values",
        "24",
        "--alpha-trend-adx-values",
        "14.0",
        "--alpha-expected-move-cost-values",
        "2.0",
        "--fb-failed-break-buffer-values",
        "4.0,6.0",
        "--cbr-breakout-buffer-values",
        "3.0,5.0",
        "--sfd-reclaim-sweep-buffer-values",
        "3.0,5.0",
        "--pfd-premium-z-values",
        "1.8,2.2",
    )
    cases = _build_cases(args)
    assert len(cases) == 64
    assert {case.alpha_squeeze_percentile_max for case in cases} == {0.35, 0.40}
    assert {case.alpha_expansion_buffer_bps for case in cases} == {1.0, 2.0}
    assert {case.alpha_expansion_body_ratio_min for case in cases} == {0.0, 0.2}
    assert {case.alpha_expansion_close_location_min for case in cases} == {0.0, 0.6}
    assert {case.alpha_expansion_range_atr_min for case in cases} == {0.9, 1.1}
    assert {case.alpha_min_volume_ratio for case in cases} == {1.0, 1.2}
    assert {case.fb_failed_break_buffer_bps for case in cases} == {4.0}
    assert {case.cbr_breakout_buffer_bps for case in cases} == {3.0}
    assert {case.sfd_reclaim_sweep_buffer_bps for case in cases} == {3.0}
    assert {case.pfd_premium_z_min for case in cases} == {1.8}


def test_build_cases_locks_non_alpha_strategy_axes_for_alpha_profile_sweep() -> None:
    args = _parse_args("--profile", "ra_2026_alpha_v2_expansion", "--max-cases", "0")
    cases = _build_cases(args)
    assert len(cases) == 16
    assert {case.alpha_squeeze_percentile_max for case in cases} == {0.40}
    assert {case.alpha_expansion_buffer_bps for case in cases} == {2.0}
    assert {case.alpha_expansion_body_ratio_min for case in cases} == {0.0}
    assert {case.alpha_expansion_close_location_min for case in cases} == {0.0}
    assert {case.alpha_expansion_range_atr_min for case in cases} == {1.0}
    assert {case.alpha_min_volume_ratio for case in cases} == {1.0}
    assert {case.alpha_take_profit_r for case in cases} == {2.0}
    assert {case.alpha_time_stop_bars for case in cases} == {24}
    assert {case.alpha_trend_adx_min_4h for case in cases} == {14.0}
    assert {case.alpha_expected_move_cost_mult for case in cases} == {2.0}
    assert {case.fb_failed_break_buffer_bps for case in cases} == {4.0}
    assert {case.cbr_breakout_buffer_bps for case in cases} == {3.0}
    assert {case.sfd_reclaim_sweep_buffer_bps for case in cases} == {3.0}
    assert {case.pfd_premium_z_min for case in cases} == {1.8}


def test_year_gate_reasons_detects_failures() -> None:
    result = YearResult(
        year=3,
        report_path="/tmp/report.json",
        status="ok",
        net_profit=-1.0,
        profit_factor=1.1,
        max_drawdown_pct=40.0,
        total_trades=2500,
        fee_to_trade_gross_pct=80.0,
        reasons=(),
    )
    reasons = _year_gate_reasons(
        result=result,
        min_pf=1.2,
        max_dd_pct=35.0,
        max_trades_per_year=2200,
        max_fee_to_trade_gross_pct=70.0,
        min_net_profit=0.0,
    )
    assert "pf<1.20" in reasons
    assert "dd>35.00" in reasons
    assert "trades>2200" in reasons
    assert "fee_trade>70.00" in reasons
    assert "net<0.00" in reasons


def test_objective_rewards_stronger_case() -> None:
    weak = (
        YearResult(
            year=1,
            report_path=None,
            status="ok",
            net_profit=1.0,
            profit_factor=1.1,
            max_drawdown_pct=30.0,
            total_trades=300,
            fee_to_trade_gross_pct=60.0,
            reasons=(),
        ),
        YearResult(
            year=3,
            report_path=None,
            status="ok",
            net_profit=2.0,
            profit_factor=1.1,
            max_drawdown_pct=32.0,
            total_trades=320,
            fee_to_trade_gross_pct=62.0,
            reasons=(),
        ),
    )
    strong = (
        YearResult(
            year=1,
            report_path=None,
            status="ok",
            net_profit=10.0,
            profit_factor=1.5,
            max_drawdown_pct=20.0,
            total_trades=220,
            fee_to_trade_gross_pct=55.0,
            reasons=(),
        ),
        YearResult(
            year=3,
            report_path=None,
            status="ok",
            net_profit=20.0,
            profit_factor=1.7,
            max_drawdown_pct=25.0,
            total_trades=240,
            fee_to_trade_gross_pct=58.0,
            reasons=(),
        ),
    )
    assert _objective(strong) > _objective(weak)
