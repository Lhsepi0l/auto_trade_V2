from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from v2.backtest.analytics import _BacktestExecutionModel, _summarize_alpha_stats
from v2.backtest.cache_loader import (
    _klines_csv_has_volume_column,
    _load_cached_funding_for_range,
    _load_cached_klines_for_range,
    _load_cached_premium_for_range,
    _read_funding_csv_rows,
    _read_klines_csv_rows,
    _write_klines_csv,
)
from v2.backtest.cache_paths import (
    _cache_file_for_funding,
    _cache_file_for_klines,
    _cache_file_for_premium,
    _interval_to_ms,
)
from v2.backtest.common import _to_float
from v2.backtest.decision_types import _ReplayDecision
from v2.backtest.downloader import _fetch_klines_15m, _fetch_klines_interval
from v2.backtest.metrics import _simulate_portfolio_metrics, _simulate_symbol_metrics
from v2.backtest.orchestration import (
    _build_local_backtest_cycle_input,
    _build_local_backtest_portfolio_rows,
)
from v2.backtest.policy import (
    _LEGACY_PORTFOLIO_BACKTEST_STRATEGIES,
    _VOL_TARGET_STRATEGIES,
    _is_vol_target_backtest_strategy,
    _locked_local_backtest_initial_capital,
    _resolve_backtest_base_interval,
    _resolve_market_intervals,
)
from v2.backtest.providers import _HistoricalSnapshotProvider
from v2.backtest.reporting import _cleanup_local_backtest_artifacts, _write_local_backtest_markdown
from v2.backtest.research_policy import _portfolio_research_gate
from v2.backtest.runtime_deps import get_local_backtest_symbol_replay_worker
from v2.backtest.snapshots import _FundingRateRow, _Kline15m
from v2.backtest.summaries import _build_half_year_window_summaries, _format_utc_iso
from v2.config.loader import EffectiveConfig, EnvName, ModeName, load_effective_config
from v2.engine import EngineStateStore
from v2.exchange import BackoffPolicy, BinanceRESTClient
from v2.kernel import build_default_kernel
from v2.storage import RuntimeStorage
from v2.tpsl import BracketConfig, BracketPlanner


def _local_backtest_strategy_runtime_params(
    *,
    active_strategy_name: str,
    enabled_alphas_override: str | None = None,
    alpha_squeeze_percentile_max: float | None,
    alpha_expansion_buffer_bps: float | None,
    alpha_expansion_range_atr_min: float | None,
    alpha_expansion_body_ratio_min: float | None,
    alpha_expansion_close_location_min: float | None,
    alpha_expansion_width_expansion_min: float | None,
    alpha_expansion_break_distance_atr_min: float | None,
    alpha_expansion_breakout_efficiency_min: float | None,
    alpha_expansion_breakout_stability_score_min: float | None,
    alpha_expansion_breakout_stability_edge_score_min: float | None,
    alpha_expansion_quality_score_min: float | None,
    alpha_expansion_quality_score_v2_min: float | None,
    alpha_min_volume_ratio: float | None,
    alpha_take_profit_r: float | None,
    alpha_time_stop_bars: int | None,
    alpha_trend_adx_min_4h: float | None,
    alpha_trend_adx_max_4h: float | None,
    alpha_trend_adx_rising_lookback_4h: int | None,
    alpha_trend_adx_rising_min_delta_4h: float | None,
    alpha_expected_move_cost_mult: float | None,
    drift_side_mode: str | None,
    drift_take_profit_r: float | None,
    drift_time_stop_bars: int | None,
    fb_failed_break_buffer_bps: float,
    fb_wick_ratio_min: float,
    fb_take_profit_r: float,
    fb_time_stop_bars: int,
    cbr_squeeze_percentile_max: float,
    cbr_breakout_buffer_bps: float,
    cbr_take_profit_r: float,
    cbr_time_stop_bars: int,
    cbr_trend_adx_min_4h: float,
    cbr_ema_gap_trend_min_frac_4h: float,
    cbr_breakout_min_range_atr: float,
    cbr_breakout_min_volume_ratio: float,
    sfd_reclaim_sweep_buffer_bps: float,
    sfd_reclaim_wick_ratio_min: float,
    sfd_drive_breakout_range_atr_min: float,
    sfd_take_profit_r: float,
    pfd_premium_z_min: float,
    pfd_funding_24h_min: float,
    pfd_reclaim_buffer_atr: float,
    pfd_take_profit_r: float,
) -> dict[str, Any]:
    if str(active_strategy_name).startswith("ra_2026_alpha_v2"):
        params: dict[str, Any] = {}
        if alpha_squeeze_percentile_max is not None:
            params["squeeze_percentile_threshold"] = min(
                max(float(alpha_squeeze_percentile_max), 0.05),
                0.95,
            )
        if alpha_expansion_buffer_bps is not None:
            params["expansion_buffer_bps"] = max(float(alpha_expansion_buffer_bps), 0.0)
        if alpha_expansion_range_atr_min is not None:
            params["expansion_range_atr_min"] = max(float(alpha_expansion_range_atr_min), 0.0)
        if alpha_expansion_body_ratio_min is not None:
            params["expansion_body_ratio_min"] = min(
                max(float(alpha_expansion_body_ratio_min), 0.0),
                1.0,
            )
        if alpha_expansion_close_location_min is not None:
            params["expansion_close_location_min"] = min(
                max(float(alpha_expansion_close_location_min), 0.0),
                1.0,
            )
        if alpha_expansion_width_expansion_min is not None:
            params["expansion_width_expansion_min"] = max(
                float(alpha_expansion_width_expansion_min),
                0.0,
            )
        if alpha_expansion_break_distance_atr_min is not None:
            params["expansion_break_distance_atr_min"] = max(
                float(alpha_expansion_break_distance_atr_min),
                0.0,
            )
        if alpha_expansion_breakout_efficiency_min is not None:
            params["expansion_breakout_efficiency_min"] = max(
                float(alpha_expansion_breakout_efficiency_min),
                0.0,
            )
        if alpha_expansion_breakout_stability_score_min is not None:
            params["expansion_breakout_stability_score_min"] = min(
                max(float(alpha_expansion_breakout_stability_score_min), 0.0),
                1.0,
            )
        if alpha_expansion_breakout_stability_edge_score_min is not None:
            params["expansion_breakout_stability_edge_score_min"] = min(
                max(float(alpha_expansion_breakout_stability_edge_score_min), 0.0),
                1.0,
            )
        if alpha_expansion_quality_score_min is not None:
            params["expansion_quality_score_min"] = min(
                max(float(alpha_expansion_quality_score_min), 0.0),
                1.0,
            )
        if alpha_min_volume_ratio is not None:
            params["min_volume_ratio_15m"] = max(float(alpha_min_volume_ratio), 0.0)
        if alpha_take_profit_r is not None:
            params["take_profit_r"] = max(float(alpha_take_profit_r), 0.5)
        if alpha_time_stop_bars is not None:
            params["time_stop_bars"] = max(int(alpha_time_stop_bars), 1)
        if alpha_trend_adx_min_4h is not None:
            params["trend_adx_min_4h"] = max(float(alpha_trend_adx_min_4h), 0.0)
        if alpha_trend_adx_max_4h is not None:
            params["trend_adx_max_4h"] = max(float(alpha_trend_adx_max_4h), 0.0)
        if alpha_trend_adx_rising_lookback_4h is not None:
            params["trend_adx_rising_lookback_4h"] = max(int(alpha_trend_adx_rising_lookback_4h), 0)
        if alpha_trend_adx_rising_min_delta_4h is not None:
            params["trend_adx_rising_min_delta_4h"] = max(
                float(alpha_trend_adx_rising_min_delta_4h),
                0.0,
            )
        if alpha_expected_move_cost_mult is not None:
            params["expected_move_cost_mult"] = max(float(alpha_expected_move_cost_mult), 0.1)
        if drift_side_mode is not None:
            params["drift_side_mode"] = str(drift_side_mode).strip().upper() or "BOTH"
        if drift_take_profit_r is not None:
            params["drift_take_profit_r"] = max(float(drift_take_profit_r), 0.5)
        if drift_time_stop_bars is not None:
            params["drift_time_stop_bars"] = max(int(drift_time_stop_bars), 1)
        if enabled_alphas_override is not None and str(enabled_alphas_override).strip():
            params["enabled_alphas"] = [
                token.strip().lower()
                for token in str(enabled_alphas_override).split(",")
                if token.strip()
            ]
        if alpha_expansion_quality_score_v2_min is not None:
            params["expansion_quality_score_v2_min"] = min(
                max(float(alpha_expansion_quality_score_v2_min), 0.0),
                1.0,
            )
        return params
    return {}


def _local_backtest_profile_alpha_overrides(
    profile_name: str,
    *,
    config_path: str | None = None,
) -> dict[str, Any]:
    try:
        cfg = load_effective_config(
            profile=str(profile_name),
            mode="shadow",
            env="prod",
            env_file_path=".env",
            config_path=config_path,
        )
    except Exception:
        return {}

    for entry in cfg.behavior.strategies:
        if not bool(getattr(entry, "enabled", False)):
            continue
        if not str(getattr(entry, "name", "")).strip().startswith("ra_2026_alpha_v2"):
            continue
        params = getattr(entry, "params", None)
        if isinstance(params, dict):
            return dict(params)
        return {}
    return {}


def _merge_local_backtest_profile_alpha_overrides(
    *,
    profile_name: str,
    active_strategy_name: str,
    strategy_runtime_params: dict[str, Any],
    config_path: str | None = None,
) -> dict[str, Any]:
    merged = {}
    if str(active_strategy_name).startswith("ra_2026_alpha_v2"):
        merged.update(
            _local_backtest_profile_alpha_overrides(
                profile_name,
                config_path=config_path,
            )
        )
    merged.update({key: value for key, value in strategy_runtime_params.items() if value is not None})
    return merged


def _run_local_backtest_symbol_replay_worker(
    *,
    symbol: str,
    profile: str,
    mode: ModeName,
    env: EnvName,
    years: int,
    start_ms: int,
    end_ms: int,
    config_path: str | None,
    cache_root: str,
    sqlite_path: str,
    initial_capital: float,
    execution_model: _BacktestExecutionModel,
    fixed_leverage: float,
    fixed_leverage_margin_use_pct: float,
    reverse_min_hold_bars: int,
    reverse_cooldown_bars: int,
    min_expected_edge_over_roundtrip_cost: float,
    min_reward_risk_ratio: float,
    max_trades_per_day_per_symbol: int,
    daily_loss_limit_pct: float,
    equity_floor_pct: float,
    max_trade_margin_loss_fraction: float,
    min_signal_score: float,
    enabled_alphas_override: str | None = None,
    reverse_exit_min_profit_pct: float,
    reverse_exit_min_signal_score: float,
    default_reverse_exit_min_r: float,
    default_risk_per_trade_pct: float,
    default_max_effective_leverage: float,
    drawdown_scale_start_pct: float,
    drawdown_scale_end_pct: float,
    drawdown_margin_scale_min: float,
    stoploss_streak_trigger: int,
    stoploss_cooldown_bars: int,
    loss_cooldown_bars: int,
    alpha_squeeze_percentile_max: float | None,
    alpha_expansion_buffer_bps: float | None,
    alpha_expansion_range_atr_min: float | None,
    alpha_expansion_body_ratio_min: float | None,
    alpha_expansion_close_location_min: float | None,
    alpha_expansion_width_expansion_min: float | None,
    alpha_expansion_break_distance_atr_min: float | None,
    alpha_expansion_breakout_efficiency_min: float | None,
    alpha_expansion_breakout_stability_score_min: float | None,
    alpha_expansion_breakout_stability_edge_score_min: float | None,
    alpha_expansion_quality_score_min: float | None,
    alpha_expansion_quality_score_v2_min: float | None,
    alpha_min_volume_ratio: float | None,
    alpha_take_profit_r: float | None,
    alpha_time_stop_bars: int | None,
    alpha_trend_adx_min_4h: float | None,
    alpha_trend_adx_max_4h: float | None,
    alpha_trend_adx_rising_lookback_4h: int | None,
    alpha_trend_adx_rising_min_delta_4h: float | None,
    alpha_expected_move_cost_mult: float | None,
    drift_side_mode: str | None,
    drift_take_profit_r: float | None,
    drift_time_stop_bars: int | None,
    fb_failed_break_buffer_bps: float,
    fb_wick_ratio_min: float,
    fb_take_profit_r: float,
    fb_time_stop_bars: int,
    cbr_squeeze_percentile_max: float,
    cbr_breakout_buffer_bps: float,
    cbr_take_profit_r: float,
    cbr_time_stop_bars: int,
    cbr_trend_adx_min_4h: float,
    cbr_ema_gap_trend_min_frac_4h: float,
    cbr_breakout_min_range_atr: float,
    cbr_breakout_min_volume_ratio: float,
    sfd_reclaim_sweep_buffer_bps: float,
    sfd_reclaim_wick_ratio_min: float,
    sfd_drive_breakout_range_atr_min: float,
    sfd_take_profit_r: float,
    pfd_premium_z_min: float,
    pfd_funding_24h_min: float,
    pfd_reclaim_buffer_atr: float,
    pfd_take_profit_r: float,
    market_intervals: list[str],
    max_peak_drawdown_pct: float | None,
) -> dict[str, Any]:
    cfg = load_effective_config(
        profile=profile,
        mode=mode,
        env=env,
        env_file_path=".env",
        config_path=config_path,
    )
    cache_dir = Path(cache_root)

    def _load_interval_cached_with_fallback(interval: str) -> list[_Kline15m]:
        cache_path = _cache_file_for_klines(
            cache_root=cache_dir,
            symbol=symbol,
            interval=interval,
            years=years,
        )
        strict_rows = _load_cached_klines_for_range(
            path=cache_path,
            interval=interval,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        if strict_rows:
            return strict_rows
        raw_rows = _read_klines_csv_rows(cache_path)
        if not raw_rows:
            return []
        fallback_rows = [
            row
            for row in raw_rows
            if int(row.open_time_ms) >= int(start_ms) and int(row.open_time_ms) < int(end_ms)
        ]
        if fallback_rows:
            return fallback_rows
        return raw_rows

    resolved_intervals: list[str] = []
    seen_intervals: set[str] = set()
    for raw in market_intervals:
        interval = str(raw).strip()
        if not interval or interval in seen_intervals:
            continue
        seen_intervals.add(interval)
        resolved_intervals.append(interval)
    base_interval = _resolve_backtest_base_interval(resolved_intervals)
    if base_interval not in resolved_intervals:
        resolved_intervals.insert(0, base_interval)

    candles_15m = _load_interval_cached_with_fallback(base_interval)
    market_candles: dict[str, list[_Kline15m]] = {}
    for interval in resolved_intervals:
        if interval == base_interval:
            continue
        market_candles[interval] = _load_interval_cached_with_fallback(interval)
    premium_rows_15m = _load_local_backtest_cached_premium_for_symbol(
        cache_root=cache_dir,
        symbol=symbol,
        years=years,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    funding_rows = _load_local_backtest_cached_funding_for_symbol(
        cache_root=cache_dir,
        symbol=symbol,
        years=years,
        start_ms=start_ms,
        end_ms=end_ms,
    )

    if len(candles_15m) == 0:
        return {
            "symbol": symbol,
            "skipped": True,
            "reason": "no_cached_15m_candles",
        }

    storage = RuntimeStorage(sqlite_path=sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    state_store.set(mode=cfg.mode, status="RUNNING")

    collector = _ReplayDecision()
    provider = _HistoricalSnapshotProvider(
        symbol=symbol,
        candles_15m=candles_15m,
        base_interval=base_interval,
        market_candles=market_candles,
        premium_rows_15m=premium_rows_15m,
        funding_rows=funding_rows,
        market_intervals=resolved_intervals,
    )

    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        tick=0,
        dry_run=True,
        rest_client=None,
        snapshot_provider=provider,
        overheat_fetcher=None,
        journal_logger=collector,
        max_leverage=fixed_leverage,
    )
    enabled_strategies = [
        str(entry.name).strip()
        for entry in cfg.behavior.strategies
        if bool(getattr(entry, "enabled", False))
    ]
    active_strategy_name = enabled_strategies[0] if enabled_strategies else "none"
    strategy_runtime_params = _local_backtest_strategy_runtime_params(
        active_strategy_name=active_strategy_name,
        enabled_alphas_override=enabled_alphas_override,
        alpha_squeeze_percentile_max=(
            None if alpha_squeeze_percentile_max is None else float(alpha_squeeze_percentile_max)
        ),
        alpha_expansion_buffer_bps=(
            None if alpha_expansion_buffer_bps is None else float(alpha_expansion_buffer_bps)
        ),
        alpha_expansion_range_atr_min=(
            None if alpha_expansion_range_atr_min is None else float(alpha_expansion_range_atr_min)
        ),
        alpha_expansion_body_ratio_min=(
            None if alpha_expansion_body_ratio_min is None else float(alpha_expansion_body_ratio_min)
        ),
        alpha_expansion_close_location_min=(
            None
            if alpha_expansion_close_location_min is None
            else float(alpha_expansion_close_location_min)
        ),
        alpha_expansion_width_expansion_min=(
            None
            if alpha_expansion_width_expansion_min is None
            else float(alpha_expansion_width_expansion_min)
        ),
        alpha_expansion_break_distance_atr_min=(
            None
            if alpha_expansion_break_distance_atr_min is None
            else float(alpha_expansion_break_distance_atr_min)
        ),
        alpha_expansion_breakout_efficiency_min=(
            None
            if alpha_expansion_breakout_efficiency_min is None
            else float(alpha_expansion_breakout_efficiency_min)
        ),
        alpha_expansion_breakout_stability_score_min=(
            None
            if alpha_expansion_breakout_stability_score_min is None
            else float(alpha_expansion_breakout_stability_score_min)
        ),
        alpha_expansion_breakout_stability_edge_score_min=(
            None
            if alpha_expansion_breakout_stability_edge_score_min is None
            else float(alpha_expansion_breakout_stability_edge_score_min)
        ),
        alpha_expansion_quality_score_min=(
            None
            if alpha_expansion_quality_score_min is None
            else float(alpha_expansion_quality_score_min)
        ),
        alpha_expansion_quality_score_v2_min=(
            None
            if alpha_expansion_quality_score_v2_min is None
            else float(alpha_expansion_quality_score_v2_min)
        ),
        alpha_min_volume_ratio=(
            None if alpha_min_volume_ratio is None else float(alpha_min_volume_ratio)
        ),
        alpha_take_profit_r=(
            None if alpha_take_profit_r is None else float(alpha_take_profit_r)
        ),
        alpha_time_stop_bars=(
            None if alpha_time_stop_bars is None else int(alpha_time_stop_bars)
        ),
        alpha_trend_adx_min_4h=(
            None if alpha_trend_adx_min_4h is None else float(alpha_trend_adx_min_4h)
        ),
        alpha_trend_adx_max_4h=(
            None if alpha_trend_adx_max_4h is None else float(alpha_trend_adx_max_4h)
        ),
        alpha_trend_adx_rising_lookback_4h=(
            None
            if alpha_trend_adx_rising_lookback_4h is None
            else int(alpha_trend_adx_rising_lookback_4h)
        ),
        alpha_trend_adx_rising_min_delta_4h=(
            None
            if alpha_trend_adx_rising_min_delta_4h is None
            else float(alpha_trend_adx_rising_min_delta_4h)
        ),
        alpha_expected_move_cost_mult=(
            None
            if alpha_expected_move_cost_mult is None
            else float(alpha_expected_move_cost_mult)
        ),
        drift_side_mode=None if drift_side_mode is None else str(drift_side_mode),
        drift_take_profit_r=(
            None if drift_take_profit_r is None else float(drift_take_profit_r)
        ),
        drift_time_stop_bars=(
            None if drift_time_stop_bars is None else int(drift_time_stop_bars)
        ),
        fb_failed_break_buffer_bps=float(fb_failed_break_buffer_bps),
        fb_wick_ratio_min=float(fb_wick_ratio_min),
        fb_take_profit_r=float(fb_take_profit_r),
        fb_time_stop_bars=int(fb_time_stop_bars),
        cbr_squeeze_percentile_max=float(cbr_squeeze_percentile_max),
        cbr_breakout_buffer_bps=float(cbr_breakout_buffer_bps),
        cbr_take_profit_r=float(cbr_take_profit_r),
        cbr_time_stop_bars=int(cbr_time_stop_bars),
        cbr_trend_adx_min_4h=float(cbr_trend_adx_min_4h),
        cbr_ema_gap_trend_min_frac_4h=float(cbr_ema_gap_trend_min_frac_4h),
        cbr_breakout_min_range_atr=float(cbr_breakout_min_range_atr),
        cbr_breakout_min_volume_ratio=float(cbr_breakout_min_volume_ratio),
        sfd_reclaim_sweep_buffer_bps=float(sfd_reclaim_sweep_buffer_bps),
        sfd_reclaim_wick_ratio_min=float(sfd_reclaim_wick_ratio_min),
        sfd_drive_breakout_range_atr_min=float(sfd_drive_breakout_range_atr_min),
        sfd_take_profit_r=float(sfd_take_profit_r),
        pfd_premium_z_min=float(pfd_premium_z_min),
        pfd_funding_24h_min=float(pfd_funding_24h_min),
        pfd_reclaim_buffer_atr=float(pfd_reclaim_buffer_atr),
        pfd_take_profit_r=float(pfd_take_profit_r),
    )
    strategy_runtime_params = _merge_local_backtest_profile_alpha_overrides(
        profile_name=profile,
        active_strategy_name=active_strategy_name,
        strategy_runtime_params=strategy_runtime_params,
        config_path=config_path,
    )
    if strategy_runtime_params:
        kernel.set_strategy_runtime_params(**strategy_runtime_params)
    bracket_planner = BracketPlanner(
        cfg=BracketConfig(
            take_profit_pct=cfg.behavior.tpsl.take_profit_pct,
            stop_loss_pct=cfg.behavior.tpsl.stop_loss_pct,
        )
    )

    rows: list[dict[str, Any]] = []
    state_counter: Counter[str] = Counter()
    for _tick in range(len(provider)):
        cycle = kernel.run_once()
        decision = collector.take()
        row, state = _build_local_backtest_cycle_input(
            cycle=cycle,
            decision=decision,
            bracket_planner=bracket_planner,
        )
        rows.append(row)
        state_counter[state] += 1

    per_symbol_report = _simulate_symbol_metrics(
        symbol=symbol,
        rows=rows,
        candles_15m=candles_15m,
        initial_capital=initial_capital,
        execution_model=execution_model,
        fixed_leverage=fixed_leverage,
        fixed_leverage_margin_use_pct=fixed_leverage_margin_use_pct,
        reverse_min_hold_bars=reverse_min_hold_bars,
        reverse_cooldown_bars=reverse_cooldown_bars,
        min_expected_edge_over_roundtrip_cost=min_expected_edge_over_roundtrip_cost,
        min_reward_risk_ratio=min_reward_risk_ratio,
        max_trades_per_day_per_symbol=max_trades_per_day_per_symbol,
        daily_loss_limit_pct=daily_loss_limit_pct,
        equity_floor_pct=equity_floor_pct,
        max_trade_margin_loss_fraction=max_trade_margin_loss_fraction,
        min_signal_score=min_signal_score,
        reverse_exit_min_profit_pct=reverse_exit_min_profit_pct,
        reverse_exit_min_signal_score=reverse_exit_min_signal_score,
        default_reverse_exit_min_r=default_reverse_exit_min_r,
        default_risk_per_trade_pct=default_risk_per_trade_pct,
        default_max_effective_leverage=default_max_effective_leverage,
        drawdown_scale_start_pct=drawdown_scale_start_pct,
        drawdown_scale_end_pct=drawdown_scale_end_pct,
        drawdown_margin_scale_min=drawdown_margin_scale_min,
        stoploss_streak_trigger=stoploss_streak_trigger,
        stoploss_cooldown_bars=stoploss_cooldown_bars,
        loss_cooldown_bars=loss_cooldown_bars,
        max_peak_drawdown_pct=max_peak_drawdown_pct,
    )
    interval_counts = {interval: len(rows) for interval, rows in market_candles.items()}
    interval_counts[base_interval] = len(candles_15m)
    per_symbol_report["candles_by_interval"] = interval_counts
    per_symbol_report["candles_10m"] = int(interval_counts.get("10m", 0))
    per_symbol_report["candles_30m"] = int(interval_counts.get("30m", 0))
    per_symbol_report["candles_1h"] = int(interval_counts.get("1h", 0))
    per_symbol_report["candles_4h"] = int(interval_counts.get("4h", 0))
    per_symbol_report["premium_15m"] = len(premium_rows_15m)
    per_symbol_report["funding_events"] = len(funding_rows)
    per_symbol_report["cycles"] = len(rows)
    return {
        "symbol": symbol,
        "skipped": False,
        "report": per_symbol_report,
        "state_distribution": dict(state_counter),
        "total_cycles": len(rows),
    }


def _load_local_backtest_cached_market_for_symbol(
    *,
    cache_root: Path,
    symbol: str,
    years: int,
    start_ms: int,
    end_ms: int,
    market_intervals: list[str],
) -> dict[str, list[_Kline15m]]:
    def _load_interval_cached_with_fallback(interval: str) -> list[_Kline15m]:
        cache_path = _cache_file_for_klines(
            cache_root=cache_root,
            symbol=symbol,
            interval=interval,
            years=years,
        )
        strict_rows = _load_cached_klines_for_range(
            path=cache_path,
            interval=interval,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        if strict_rows:
            return strict_rows
        raw_rows = _read_klines_csv_rows(cache_path)
        if not raw_rows:
            return []
        fallback_rows = [
            row
            for row in raw_rows
            if int(row.open_time_ms) >= int(start_ms) and int(row.open_time_ms) < int(end_ms)
        ]
        if fallback_rows:
            return fallback_rows
        return raw_rows

    resolved_intervals: list[str] = []
    seen_intervals: set[str] = set()
    for raw in market_intervals:
        interval = str(raw).strip()
        if not interval or interval in seen_intervals:
            continue
        seen_intervals.add(interval)
        resolved_intervals.append(interval)
    base_interval = _resolve_backtest_base_interval(resolved_intervals)
    if base_interval not in resolved_intervals:
        resolved_intervals.insert(0, base_interval)

    candles_by_interval: dict[str, list[_Kline15m]] = {}
    for interval in resolved_intervals:
        candles_by_interval[interval] = _load_interval_cached_with_fallback(interval)
    return candles_by_interval


def _load_local_backtest_cached_premium_for_symbol(
    *,
    cache_root: Path,
    symbol: str,
    years: int,
    start_ms: int,
    end_ms: int,
) -> list[_Kline15m]:
    cache_path = _cache_file_for_premium(
        cache_root=cache_root,
        symbol=symbol,
        interval="15m",
        years=years,
    )
    strict_rows = _load_cached_premium_for_range(
        path=cache_path,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    if strict_rows:
        return strict_rows
    raw_rows = _read_klines_csv_rows(cache_path)
    if not raw_rows:
        return []
    fallback_rows = [
        row
        for row in raw_rows
        if int(row.open_time_ms) >= int(start_ms) and int(row.open_time_ms) < int(end_ms)
    ]
    if fallback_rows:
        return fallback_rows
    return raw_rows


def _load_local_backtest_cached_funding_for_symbol(
    *,
    cache_root: Path,
    symbol: str,
    years: int,
    start_ms: int,
    end_ms: int,
) -> list[_FundingRateRow]:
    cache_path = _cache_file_for_funding(
        cache_root=cache_root,
        symbol=symbol,
        years=years,
    )
    strict_rows = _load_cached_funding_for_range(
        path=cache_path,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    if strict_rows:
        return strict_rows
    raw_rows = _read_funding_csv_rows(cache_path)
    if not raw_rows:
        return []
    fallback_rows = [
        row
        for row in raw_rows
        if int(row.funding_time_ms) >= int(start_ms) and int(row.funding_time_ms) < int(end_ms)
    ]
    if fallback_rows:
        return fallback_rows
    return raw_rows


def _run_local_backtest_portfolio_replay(
    *,
    cfg: EffectiveConfig,
    symbols: list[str],
    active_strategy_name: str,
    years: int,
    start_ms: int,
    end_ms: int,
    cache_root: Path,
    market_intervals: list[str],
    initial_capital: float,
    execution_model: _BacktestExecutionModel,
    fixed_leverage_margin_use_pct: float,
    reverse_cooldown_bars: int,
    max_trades_per_day_per_symbol: int,
    min_expected_edge_over_roundtrip_cost: float,
    min_reward_risk_ratio: float,
    daily_loss_limit_pct: float,
    equity_floor_pct: float,
    max_trade_margin_loss_fraction: float,
    min_signal_score: float,
    drawdown_scale_start_pct: float,
    drawdown_scale_end_pct: float,
    drawdown_margin_scale_min: float,
    strategy_runtime_params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    candles_by_symbol: dict[str, dict[str, list[_Kline15m]]] = {}
    interval_counts_by_symbol: dict[str, dict[str, int]] = {}
    base_interval = _resolve_backtest_base_interval(market_intervals)
    for symbol in symbols:
        market = _load_local_backtest_cached_market_for_symbol(
            cache_root=cache_root,
            symbol=symbol,
            years=years,
            start_ms=start_ms,
            end_ms=end_ms,
            market_intervals=market_intervals,
        )
        if len(market.get(base_interval, [])) == 0:
            continue
        candles_by_symbol[symbol] = market
        interval_counts_by_symbol[symbol] = {
            interval: len(rows) for interval, rows in market.items()
        }
    if not candles_by_symbol:
        raise RuntimeError("no portfolio backtest candles available")

    rows, state_counter = _build_local_backtest_portfolio_rows(
        cfg=cfg,
        candles_by_symbol=candles_by_symbol,
        premium_by_symbol=None,
        funding_by_symbol=None,
        market_intervals=market_intervals,
        strategy_runtime_params=strategy_runtime_params,
    )
    metrics = _simulate_portfolio_metrics(
        rows=rows,
        initial_capital=float(initial_capital),
        execution_model=execution_model,
        fixed_leverage_margin_use_pct=float(fixed_leverage_margin_use_pct),
        max_open_positions=max(int(getattr(cfg.behavior.engine, "max_open_positions", 1) or 1), 1),
        max_new_entries_per_tick=max(
            1,
            min(int(getattr(cfg.behavior.engine, "max_open_positions", 1) or 1), 2),
        ),
        reverse_cooldown_bars=int(reverse_cooldown_bars),
        max_trades_per_day_per_symbol=int(max_trades_per_day_per_symbol),
        min_expected_edge_over_roundtrip_cost=float(min_expected_edge_over_roundtrip_cost),
        min_reward_risk_ratio=float(min_reward_risk_ratio),
        daily_loss_limit_pct=float(daily_loss_limit_pct),
        equity_floor_pct=float(equity_floor_pct),
        max_trade_margin_loss_fraction=float(max_trade_margin_loss_fraction),
        min_signal_score=float(min_signal_score),
        drawdown_scale_start_pct=float(drawdown_scale_start_pct),
        drawdown_scale_end_pct=float(drawdown_scale_end_pct),
        drawdown_margin_scale_min=float(drawdown_margin_scale_min),
    )
    cleaned_count = _cleanup_local_backtest_artifacts(cache_root.parent)
    metrics["state_distribution"] = dict(state_counter)
    metrics["symbols"] = {
        symbol: interval_counts_by_symbol.get(symbol, {}) for symbol in sorted(candles_by_symbol.keys())
    }
    return metrics, cleaned_count


def _run_local_backtest(
    cfg: EffectiveConfig,
    *,
    config_path: str | None,
    symbols: list[str],
    years: int,
    initial_capital: float,
    fee_bps: float,
    slippage_bps: float,
    funding_bps_per_8h: float,
    margin_use_pct: float,
    replay_workers: int,
    reverse_min_hold_bars: int,
    reverse_cooldown_bars: int,
    min_expected_edge_multiple: float,
    min_reward_risk_ratio: float,
    max_trades_per_day: int,
    daily_loss_limit_pct: float,
    equity_floor_pct: float,
    max_trade_margin_loss_fraction: float,
    min_signal_score: float,
    enabled_alphas_override: str | None = None,
    reverse_exit_min_profit_pct: float,
    reverse_exit_min_signal_score: float,
    drawdown_scale_start_pct: float,
    drawdown_scale_end_pct: float,
    drawdown_margin_scale_min: float,
    stoploss_streak_trigger: int,
    stoploss_cooldown_bars: int,
    loss_cooldown_bars: int,
    alpha_squeeze_percentile_max: float,
    alpha_expansion_buffer_bps: float,
    alpha_expansion_range_atr_min: float,
    alpha_expansion_body_ratio_min: float,
    alpha_expansion_close_location_min: float,
    alpha_expansion_width_expansion_min: float,
    alpha_expansion_break_distance_atr_min: float,
    alpha_expansion_breakout_efficiency_min: float,
    alpha_expansion_breakout_stability_score_min: float,
    alpha_expansion_breakout_stability_edge_score_min: float,
    alpha_expansion_quality_score_min: float,
    alpha_expansion_quality_score_v2_min: float | None,
    alpha_min_volume_ratio: float,
    alpha_take_profit_r: float,
    alpha_time_stop_bars: int,
    alpha_trend_adx_min_4h: float,
    alpha_trend_adx_max_4h: float,
    alpha_trend_adx_rising_lookback_4h: int,
    alpha_trend_adx_rising_min_delta_4h: float,
    alpha_expected_move_cost_mult: float,
    drift_side_mode: str,
    drift_take_profit_r: float,
    drift_time_stop_bars: int,
    fb_failed_break_buffer_bps: float,
    fb_wick_ratio_min: float,
    fb_take_profit_r: float,
    fb_time_stop_bars: int,
    cbr_squeeze_percentile_max: float,
    cbr_breakout_buffer_bps: float,
    cbr_take_profit_r: float,
    cbr_time_stop_bars: int,
    cbr_trend_adx_min_4h: float,
    cbr_ema_gap_trend_min_frac_4h: float,
    cbr_breakout_min_range_atr: float,
    cbr_breakout_min_volume_ratio: float,
    sfd_reclaim_sweep_buffer_bps: float,
    sfd_reclaim_wick_ratio_min: float,
    sfd_drive_breakout_range_atr_min: float,
    sfd_take_profit_r: float,
    pfd_premium_z_min: float,
    pfd_funding_24h_min: float,
    pfd_reclaim_buffer_atr: float,
    pfd_take_profit_r: float,
    offline: bool,
    fetch_sleep_sec: float,
    backtest_start_ms: int | None = None,
    backtest_end_ms: int | None = None,
    report_dir: str,
    report_path: str | None,
) -> int:
    requested_initial_capital = float(initial_capital)
    initial_capital = _locked_local_backtest_initial_capital(requested_initial_capital)
    enabled_strategies = [
        str(entry.name).strip()
        for entry in cfg.behavior.strategies
        if bool(getattr(entry, "enabled", False))
    ]
    active_strategy_name = enabled_strategies[0] if enabled_strategies else "none"
    if active_strategy_name not in _VOL_TARGET_STRATEGIES:
        print(
            json.dumps(
                {"error": f"unsupported_strategy:{active_strategy_name}"},
                ensure_ascii=True,
            )
        )
        return 1
    use_ra_2026_mode = _is_vol_target_backtest_strategy(active_strategy_name)
    market_intervals = _resolve_market_intervals(cfg)
    base_interval = _resolve_backtest_base_interval(market_intervals)
    fixed_leverage = (
        max(float(cfg.behavior.risk.max_leverage), 1.0) if use_ra_2026_mode else 30.0
    )
    fixed_leverage_margin_use_pct = max(0.01, min(float(margin_use_pct) / 100.0, 1.0))
    reverse_min_hold_bars = max(int(reverse_min_hold_bars), 0)
    reverse_cooldown_bars = max(int(reverse_cooldown_bars), 0)
    min_expected_edge_over_roundtrip_cost = max(float(min_expected_edge_multiple), 0.0)
    min_reward_risk_ratio = max(float(min_reward_risk_ratio), 0.0)
    max_trades_per_day_per_symbol = max(int(max_trades_per_day), 0)
    daily_loss_limit_pct = max(float(daily_loss_limit_pct) / 100.0, 0.0)
    equity_floor_pct = max(float(equity_floor_pct) / 100.0, 0.0)
    max_trade_margin_loss_fraction = max(
        0.0,
        min(float(max_trade_margin_loss_fraction) / 100.0, 1.0),
    )
    min_signal_score = max(float(min_signal_score), 0.0)
    reverse_exit_min_profit_pct = max(float(reverse_exit_min_profit_pct) / 100.0, 0.0)
    reverse_exit_min_signal_score = max(float(reverse_exit_min_signal_score), 0.0)
    drawdown_scale_start_pct = max(float(drawdown_scale_start_pct) / 100.0, 0.0)
    drawdown_scale_end_pct = max(float(drawdown_scale_end_pct) / 100.0, 0.0)
    drawdown_margin_scale_min = max(
        0.05,
        min(float(drawdown_margin_scale_min) / 100.0, 1.0),
    )
    stoploss_streak_trigger = max(int(stoploss_streak_trigger), 0)
    stoploss_cooldown_bars = max(int(stoploss_cooldown_bars), 0)
    loss_cooldown_bars = max(int(loss_cooldown_bars), 0)
    if alpha_squeeze_percentile_max is not None:
        alpha_squeeze_percentile_max = min(max(float(alpha_squeeze_percentile_max), 0.05), 0.95)
    if alpha_expansion_buffer_bps is not None:
        alpha_expansion_buffer_bps = max(float(alpha_expansion_buffer_bps), 0.0)
    if alpha_expansion_range_atr_min is not None:
        alpha_expansion_range_atr_min = max(float(alpha_expansion_range_atr_min), 0.0)
    if alpha_expansion_body_ratio_min is not None:
        alpha_expansion_body_ratio_min = min(max(float(alpha_expansion_body_ratio_min), 0.0), 1.0)
    if alpha_expansion_close_location_min is not None:
        alpha_expansion_close_location_min = min(
            max(float(alpha_expansion_close_location_min), 0.0),
            1.0,
        )
    if alpha_expansion_width_expansion_min is not None:
        alpha_expansion_width_expansion_min = max(float(alpha_expansion_width_expansion_min), 0.0)
    if alpha_expansion_break_distance_atr_min is not None:
        alpha_expansion_break_distance_atr_min = max(
            float(alpha_expansion_break_distance_atr_min),
            0.0,
        )
    if alpha_expansion_breakout_efficiency_min is not None:
        alpha_expansion_breakout_efficiency_min = max(
            float(alpha_expansion_breakout_efficiency_min),
            0.0,
        )
    if alpha_expansion_breakout_stability_score_min is not None:
        alpha_expansion_breakout_stability_score_min = min(
            max(float(alpha_expansion_breakout_stability_score_min), 0.0),
            1.0,
        )
    if alpha_expansion_breakout_stability_edge_score_min is not None:
        alpha_expansion_breakout_stability_edge_score_min = min(
            max(float(alpha_expansion_breakout_stability_edge_score_min), 0.0),
            1.0,
        )
    if alpha_expansion_quality_score_min is not None:
        alpha_expansion_quality_score_min = min(
            max(float(alpha_expansion_quality_score_min), 0.0),
            1.0,
        )
    if alpha_expansion_quality_score_v2_min is not None:
        alpha_expansion_quality_score_v2_min = min(
            max(float(alpha_expansion_quality_score_v2_min), 0.0),
            1.0,
        )
    if alpha_min_volume_ratio is not None:
        alpha_min_volume_ratio = max(float(alpha_min_volume_ratio), 0.0)
    if alpha_take_profit_r is not None:
        alpha_take_profit_r = max(float(alpha_take_profit_r), 0.5)
    if alpha_time_stop_bars is not None:
        alpha_time_stop_bars = max(int(alpha_time_stop_bars), 1)
    if alpha_trend_adx_min_4h is not None:
        alpha_trend_adx_min_4h = max(float(alpha_trend_adx_min_4h), 0.0)
    if alpha_trend_adx_max_4h is not None:
        alpha_trend_adx_max_4h = max(float(alpha_trend_adx_max_4h), 0.0)
    if alpha_trend_adx_rising_lookback_4h is not None:
        alpha_trend_adx_rising_lookback_4h = max(int(alpha_trend_adx_rising_lookback_4h), 0)
    if alpha_trend_adx_rising_min_delta_4h is not None:
        alpha_trend_adx_rising_min_delta_4h = max(
            float(alpha_trend_adx_rising_min_delta_4h),
            0.0,
        )
    if alpha_expected_move_cost_mult is not None:
        alpha_expected_move_cost_mult = max(float(alpha_expected_move_cost_mult), 0.1)
    if drift_take_profit_r is not None:
        drift_take_profit_r = max(float(drift_take_profit_r), 0.5)
    if drift_time_stop_bars is not None:
        drift_time_stop_bars = max(int(drift_time_stop_bars), 1)
    fb_failed_break_buffer_bps = max(float(fb_failed_break_buffer_bps), 0.0)
    fb_wick_ratio_min = max(float(fb_wick_ratio_min), 0.1)
    fb_take_profit_r = max(float(fb_take_profit_r), 0.5)
    fb_time_stop_bars = max(int(fb_time_stop_bars), 1)
    cbr_squeeze_percentile_max = min(max(float(cbr_squeeze_percentile_max), 0.05), 0.95)
    cbr_breakout_buffer_bps = max(float(cbr_breakout_buffer_bps), 0.0)
    cbr_take_profit_r = max(float(cbr_take_profit_r), 0.5)
    cbr_time_stop_bars = max(int(cbr_time_stop_bars), 1)
    cbr_trend_adx_min_4h = max(float(cbr_trend_adx_min_4h), 0.0)
    cbr_ema_gap_trend_min_frac_4h = max(float(cbr_ema_gap_trend_min_frac_4h), 0.0)
    cbr_breakout_min_range_atr = max(float(cbr_breakout_min_range_atr), 0.0)
    cbr_breakout_min_volume_ratio = max(float(cbr_breakout_min_volume_ratio), 0.0)
    sfd_reclaim_sweep_buffer_bps = max(float(sfd_reclaim_sweep_buffer_bps), 0.0)
    sfd_reclaim_wick_ratio_min = max(float(sfd_reclaim_wick_ratio_min), 0.1)
    sfd_drive_breakout_range_atr_min = max(float(sfd_drive_breakout_range_atr_min), 0.0)
    sfd_take_profit_r = max(float(sfd_take_profit_r), 0.5)
    pfd_premium_z_min = max(float(pfd_premium_z_min), 0.5)
    pfd_funding_24h_min = max(float(pfd_funding_24h_min), 0.0)
    pfd_reclaim_buffer_atr = max(float(pfd_reclaim_buffer_atr), 0.0)
    pfd_take_profit_r = max(float(pfd_take_profit_r), 0.5)
    default_reverse_exit_min_r = 1.0 if use_ra_2026_mode else 0.0
    default_risk_per_trade_pct = 0.0
    default_max_effective_leverage = max(float(cfg.behavior.risk.max_leverage), 1.0)
    max_peak_drawdown_pct = 0.20 if active_strategy_name in _VOL_TARGET_STRATEGIES else None
    strategy_runtime_params = _local_backtest_strategy_runtime_params(
        active_strategy_name=active_strategy_name,
        enabled_alphas_override=enabled_alphas_override,
        alpha_squeeze_percentile_max=(
            None if alpha_squeeze_percentile_max is None else float(alpha_squeeze_percentile_max)
        ),
        alpha_expansion_buffer_bps=(
            None if alpha_expansion_buffer_bps is None else float(alpha_expansion_buffer_bps)
        ),
        alpha_expansion_range_atr_min=(
            None if alpha_expansion_range_atr_min is None else float(alpha_expansion_range_atr_min)
        ),
        alpha_expansion_body_ratio_min=(
            None if alpha_expansion_body_ratio_min is None else float(alpha_expansion_body_ratio_min)
        ),
        alpha_expansion_close_location_min=(
            None
            if alpha_expansion_close_location_min is None
            else float(alpha_expansion_close_location_min)
        ),
        alpha_expansion_width_expansion_min=(
            None
            if alpha_expansion_width_expansion_min is None
            else float(alpha_expansion_width_expansion_min)
        ),
        alpha_expansion_break_distance_atr_min=(
            None
            if alpha_expansion_break_distance_atr_min is None
            else float(alpha_expansion_break_distance_atr_min)
        ),
        alpha_expansion_breakout_efficiency_min=(
            None
            if alpha_expansion_breakout_efficiency_min is None
            else float(alpha_expansion_breakout_efficiency_min)
        ),
        alpha_expansion_breakout_stability_score_min=(
            None
            if alpha_expansion_breakout_stability_score_min is None
            else float(alpha_expansion_breakout_stability_score_min)
        ),
        alpha_expansion_breakout_stability_edge_score_min=(
            None
            if alpha_expansion_breakout_stability_edge_score_min is None
            else float(alpha_expansion_breakout_stability_edge_score_min)
        ),
        alpha_expansion_quality_score_min=(
            None
            if alpha_expansion_quality_score_min is None
            else float(alpha_expansion_quality_score_min)
        ),
        alpha_expansion_quality_score_v2_min=(
            None
            if alpha_expansion_quality_score_v2_min is None
            else float(alpha_expansion_quality_score_v2_min)
        ),
        alpha_min_volume_ratio=(
            None if alpha_min_volume_ratio is None else float(alpha_min_volume_ratio)
        ),
        alpha_take_profit_r=(
            None if alpha_take_profit_r is None else float(alpha_take_profit_r)
        ),
        alpha_time_stop_bars=(
            None if alpha_time_stop_bars is None else int(alpha_time_stop_bars)
        ),
        alpha_trend_adx_min_4h=(
            None if alpha_trend_adx_min_4h is None else float(alpha_trend_adx_min_4h)
        ),
        alpha_trend_adx_max_4h=(
            None if alpha_trend_adx_max_4h is None else float(alpha_trend_adx_max_4h)
        ),
        alpha_trend_adx_rising_lookback_4h=(
            None
            if alpha_trend_adx_rising_lookback_4h is None
            else int(alpha_trend_adx_rising_lookback_4h)
        ),
        alpha_trend_adx_rising_min_delta_4h=(
            None
            if alpha_trend_adx_rising_min_delta_4h is None
            else float(alpha_trend_adx_rising_min_delta_4h)
        ),
        alpha_expected_move_cost_mult=(
            None
            if alpha_expected_move_cost_mult is None
            else float(alpha_expected_move_cost_mult)
        ),
        drift_side_mode=None if drift_side_mode is None else str(drift_side_mode),
        drift_take_profit_r=(
            None if drift_take_profit_r is None else float(drift_take_profit_r)
        ),
        drift_time_stop_bars=(
            None if drift_time_stop_bars is None else int(drift_time_stop_bars)
        ),
        fb_failed_break_buffer_bps=float(fb_failed_break_buffer_bps),
        fb_wick_ratio_min=float(fb_wick_ratio_min),
        fb_take_profit_r=float(fb_take_profit_r),
        fb_time_stop_bars=int(fb_time_stop_bars),
        cbr_squeeze_percentile_max=float(cbr_squeeze_percentile_max),
        cbr_breakout_buffer_bps=float(cbr_breakout_buffer_bps),
        cbr_take_profit_r=float(cbr_take_profit_r),
        cbr_time_stop_bars=int(cbr_time_stop_bars),
        cbr_trend_adx_min_4h=float(cbr_trend_adx_min_4h),
        cbr_ema_gap_trend_min_frac_4h=float(cbr_ema_gap_trend_min_frac_4h),
        cbr_breakout_min_range_atr=float(cbr_breakout_min_range_atr),
        cbr_breakout_min_volume_ratio=float(cbr_breakout_min_volume_ratio),
        sfd_reclaim_sweep_buffer_bps=float(sfd_reclaim_sweep_buffer_bps),
        sfd_reclaim_wick_ratio_min=float(sfd_reclaim_wick_ratio_min),
        sfd_drive_breakout_range_atr_min=float(sfd_drive_breakout_range_atr_min),
        sfd_take_profit_r=float(sfd_take_profit_r),
        pfd_premium_z_min=float(pfd_premium_z_min),
        pfd_funding_24h_min=float(pfd_funding_24h_min),
        pfd_reclaim_buffer_atr=float(pfd_reclaim_buffer_atr),
        pfd_take_profit_r=float(pfd_take_profit_r),
    )
    strategy_runtime_params = _merge_local_backtest_profile_alpha_overrides(
        profile_name=cfg.profile,
        active_strategy_name=active_strategy_name,
        strategy_runtime_params=strategy_runtime_params,
        config_path=config_path,
    )
    def _runtime_override(key: str, fallback: Any = None) -> Any:
        return strategy_runtime_params.get(key, fallback)

    effective_alpha_expansion_quality_score_v2_min = float(
        _to_float(strategy_runtime_params.get("expansion_quality_score_v2_min")) or 0.0
    )

    if years <= 0:
        print(json.dumps({"error": "backtest years must be > 0"}, ensure_ascii=True))
        return 1
    if initial_capital <= 0:
        print(json.dumps({"error": "backtest initial capital must be > 0"}, ensure_ascii=True))
        return 1
    if not symbols:
        print(json.dumps({"error": "no backtest symbols"}, ensure_ascii=True))
        return 1

    if (backtest_start_ms is not None) != (backtest_end_ms is not None):
        print(
            json.dumps(
                {
                    "error": "backtest-start-utc and backtest-end-utc must be used together",
                },
                ensure_ascii=True,
            )
        )
        return 1

    now = datetime.now(timezone.utc)
    if backtest_start_ms is None and backtest_end_ms is None:
        start = now.replace(microsecond=0) - timedelta(days=int(years) * 365)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        backtest_window_label = "rolling"
    else:
        start_ms = int(backtest_start_ms)
        end_ms = int(backtest_end_ms)
        if start_ms >= end_ms:
            print(
                json.dumps(
                    {
                        "error": "backtest-start-utc must be earlier than backtest-end-utc",
                    },
                    ensure_ascii=True,
                )
            )
            return 1
        backtest_window_label = "fixed"

    rest: BinanceRESTClient | None = None
    if not offline:
        rest = BinanceRESTClient(
            env=cfg.env,
            api_key=None,
            api_secret=None,
            recv_window_ms=cfg.behavior.exchange.recv_window_ms,
            time_sync_enabled=False,
            rate_limit_per_sec=cfg.behavior.exchange.request_rate_limit_per_sec,
            backoff_policy=BackoffPolicy(
                base_seconds=cfg.behavior.exchange.backoff_base_seconds,
                cap_seconds=cfg.behavior.exchange.backoff_cap_seconds,
            ),
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    output_root = Path(report_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    cache_root = output_root / "_cache"
    cache_root.mkdir(parents=True, exist_ok=True)

    symbol_reports: list[dict[str, Any]] = []
    combined_state_counter: Counter[str] = Counter()
    total_cycles = 0
    downloaded_symbols: list[dict[str, Any]] = []
    total_symbols = len(symbols)
    per_symbol_initial_capital = float(initial_capital) / max(int(total_symbols), 1)
    execution_model = _BacktestExecutionModel(
        fee_bps=max(float(fee_bps), 0.0),
        slippage_bps=max(float(slippage_bps), 0.0),
        funding_bps_per_8h=max(float(funding_bps_per_8h), 0.0),
    )

    print(
        f"[LOCAL_BACKTEST] start symbols={','.join(symbols)} years={years} window={backtest_window_label} "
        f"start={_format_utc_iso(start_ms)} end={_format_utc_iso(end_ms)} initial_capital={initial_capital:.2f} "
        f"per_symbol={per_symbol_initial_capital:.2f} strategy={active_strategy_name} sizing_mode={'vol_target' if use_ra_2026_mode else 'fixed_leverage'} "
        f"leverage_cap={fixed_leverage:.1f}x intervals={','.join(market_intervals)} offline={offline}"
    )
    if abs(requested_initial_capital - initial_capital) > 1e-9:
        print(
            f"[LOCAL_BACKTEST] initial_capital_locked requested={requested_initial_capital:.2f} enforced={initial_capital:.2f}"
        )

    cleaned_count = 0
    try:
        with asyncio.Runner() as runner:
            try:
                for symbol_index, symbol in enumerate(symbols, start=1):
                    print(
                        f"[LOCAL_BACKTEST] [{symbol_index}/{total_symbols}] {symbol} download started"
                    )

                    last_download_pct = -1

                    def _on_download_progress(
                        percent: int,
                        *,
                        _symbol_index: int = symbol_index,
                        _symbol: str = symbol,
                        _total_symbols: int = total_symbols,
                    ) -> None:
                        nonlocal last_download_pct
                        if percent <= last_download_pct:
                            return
                        last_download_pct = percent
                        print(
                            f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} download {percent}%"
                        )

                    def _load_interval_with_cache(
                        *,
                        interval: str,
                        on_progress: Callable[[int], None] | None = None,
                        _symbol: str = symbol,
                        _symbol_index: int = symbol_index,
                        _total_symbols: int = total_symbols,
                    ) -> list[_Kline15m]:
                        def _fetch_range(
                            *, fetch_start_ms: int, fetch_end_ms: int
                        ) -> list[_Kline15m]:
                            if rest is None:
                                raise RuntimeError("offline cache-only backtest")
                            if interval == "15m":
                                return runner.run(
                                    _fetch_klines_15m(
                                        rest_client=rest,
                                        symbol=_symbol,
                                        start_ms=fetch_start_ms,
                                        end_ms=fetch_end_ms,
                                        on_progress=on_progress,
                                        sleep_sec=fetch_sleep_sec,
                                    )
                                )
                            return runner.run(
                                _fetch_klines_interval(
                                    rest_client=rest,
                                    symbol=_symbol,
                                    interval=interval,
                                    start_ms=fetch_start_ms,
                                    end_ms=fetch_end_ms,
                                    sleep_sec=fetch_sleep_sec,
                                )
                            )

                        cache_file = _cache_file_for_klines(
                            cache_root=cache_root,
                            symbol=_symbol,
                            interval=interval,
                            years=years,
                        )
                        cache_has_volume = _klines_csv_has_volume_column(cache_file)
                        cached_rows: list[_Kline15m] = []
                        if cache_has_volume:
                            cached_rows = _load_cached_klines_for_range(
                                path=cache_file,
                                interval=interval,
                                start_ms=start_ms,
                                end_ms=end_ms,
                            )
                        if cached_rows:
                            if on_progress is not None:
                                on_progress(100)
                            print(
                                f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache hit interval={interval} candles={len(cached_rows)}"
                            )
                            return cached_rows

                        raw_cached_rows = _read_klines_csv_rows(cache_file)
                        if raw_cached_rows and not cache_has_volume and not offline:
                            print(
                                f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache stale interval={interval} reason=missing_volume recaching"
                            )
                            raw_cached_rows = []
                        interval_ms = _interval_to_ms(interval)
                        if raw_cached_rows:
                            filtered_cached_rows = [
                                row
                                for row in raw_cached_rows
                                if int(row.open_time_ms) >= int(start_ms)
                                and int(row.open_time_ms) < int(end_ms)
                            ]
                            if offline:
                                if filtered_cached_rows:
                                    print(
                                        f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache_only interval={interval} candles={len(filtered_cached_rows)}"
                                    )
                                    return filtered_cached_rows
                                print(
                                    f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache_only_missing interval={interval} no_cached_file={cache_file}"
                                )
                                return []
                            if filtered_cached_rows:
                                expected = max(
                                    int((int(end_ms) - int(start_ms)) // max(interval_ms, 1)),
                                    1,
                                )
                                head_ok = (
                                    int(filtered_cached_rows[0].open_time_ms)
                                    <= int(start_ms) + interval_ms
                                )
                                density_ok = len(filtered_cached_rows) >= int(expected * 0.90)
                                tail_ok = int(filtered_cached_rows[-1].open_time_ms) >= int(
                                    end_ms
                                ) - (interval_ms * 2)

                                if head_ok and density_ok and not tail_ok:
                                    delta_start_ms = (
                                        int(filtered_cached_rows[-1].open_time_ms) + interval_ms
                                    )
                                    if delta_start_ms < int(end_ms):
                                        if offline:
                                            print(
                                                f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache stale interval={interval} offline=1 skip_extend"
                                            )
                                            return filtered_cached_rows
                                        print(
                                            f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache stale interval={interval} extending"
                                        )
                                        try:
                                            delta_rows = _fetch_range(
                                                fetch_start_ms=delta_start_ms,
                                                fetch_end_ms=int(end_ms),
                                            )
                                        except Exception as exc:  # noqa: BLE001
                                            print(
                                                f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache extend failed interval={interval} error={type(exc).__name__}:{exc} fallback=stale_cache"
                                            )
                                            return filtered_cached_rows
                                        if delta_rows:
                                            merged: dict[int, _Kline15m] = {
                                                int(row.open_time_ms): row
                                                for row in raw_cached_rows
                                            }
                                            for row in delta_rows:
                                                merged[int(row.open_time_ms)] = row
                                            merged_rows = [
                                                merged[key] for key in sorted(merged.keys())
                                            ]
                                            _write_klines_csv(
                                                path=cache_file,
                                                symbol=_symbol,
                                                rows=merged_rows,
                                            )
                                            recached_rows = _load_cached_klines_for_range(
                                                path=cache_file,
                                                interval=interval,
                                                start_ms=start_ms,
                                                end_ms=end_ms,
                                            )
                                            if recached_rows:
                                                print(
                                                    f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache extend hit interval={interval} candles={len(recached_rows)}"
                                                )
                                                return recached_rows

                        if offline:
                            print(
                                f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache_only miss interval={interval} no_cached_klines"
                            )
                            return []

                        print(
                            f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache miss interval={interval} downloading"
                        )
                        try:
                            fetched_rows = _fetch_range(
                                fetch_start_ms=int(start_ms),
                                fetch_end_ms=int(end_ms),
                            )
                        except Exception as exc:  # noqa: BLE001
                            print(
                                f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} fetch failed interval={interval} error={type(exc).__name__}:{exc}"
                            )
                            if raw_cached_rows:
                                print(
                                    f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} fallback interval={interval} raw_cached_rows={len(raw_cached_rows)}"
                                )
                                return raw_cached_rows
                            raise
                        if fetched_rows:
                            _write_klines_csv(path=cache_file, symbol=_symbol, rows=fetched_rows)
                        return fetched_rows

                    candles_15m = _load_interval_with_cache(
                        interval=base_interval,
                        on_progress=_on_download_progress,
                    )
                    if len(candles_15m) == 0:
                        print(f"[LOCAL_BACKTEST] [{symbol_index}/{total_symbols}] {symbol} no_data")
                        continue

                    print(
                        f"[LOCAL_BACKTEST] [{symbol_index}/{total_symbols}] {symbol} download complete candles={len(candles_15m)}"
                    )

                    interval_counts: dict[str, int] = {base_interval: len(candles_15m)}
                    aux_intervals = [interval for interval in market_intervals if interval != base_interval]
                    if aux_intervals:
                        print(
                            f"[LOCAL_BACKTEST] [{symbol_index}/{total_symbols}] {symbol} aux timeframe download started ({','.join(aux_intervals)})"
                        )
                    for interval in aux_intervals:
                        candles_aux = _load_interval_with_cache(interval=interval)
                        interval_counts[interval] = len(candles_aux)
                    if aux_intervals:
                        aux_summary = " ".join(
                            f"{interval}={interval_counts.get(interval, 0)}"
                            for interval in aux_intervals
                        )
                        print(
                            f"[LOCAL_BACKTEST] [{symbol_index}/{total_symbols}] {symbol} aux complete {aux_summary}"
                        )

                    downloaded_symbols.append(
                        {
                            "symbol": symbol,
                            "candles_by_interval": interval_counts,
                        }
                    )
            finally:
                if rest is not None:
                    runner.run(rest.aclose())

        if len(downloaded_symbols) == 0:
            print(json.dumps({"error": "no historical klines downloaded"}, ensure_ascii=True))
            return 1

        if active_strategy_name in _LEGACY_PORTFOLIO_BACKTEST_STRATEGIES:
            portfolio_metrics, cleaned_count = _run_local_backtest_portfolio_replay(
                cfg=cfg,
                symbols=symbols,
                active_strategy_name=active_strategy_name,
                years=years,
                start_ms=start_ms,
                end_ms=end_ms,
                cache_root=cache_root,
                market_intervals=market_intervals,
                initial_capital=float(initial_capital),
                execution_model=execution_model,
                fixed_leverage_margin_use_pct=float(fixed_leverage_margin_use_pct),
                reverse_cooldown_bars=int(reverse_cooldown_bars),
                max_trades_per_day_per_symbol=int(max_trades_per_day_per_symbol),
                min_expected_edge_over_roundtrip_cost=float(min_expected_edge_over_roundtrip_cost),
                min_reward_risk_ratio=float(min_reward_risk_ratio),
                daily_loss_limit_pct=float(daily_loss_limit_pct),
                equity_floor_pct=float(equity_floor_pct),
                max_trade_margin_loss_fraction=float(max_trade_margin_loss_fraction),
                min_signal_score=float(min_signal_score),
                drawdown_scale_start_pct=float(drawdown_scale_start_pct),
                drawdown_scale_end_pct=float(drawdown_scale_end_pct),
                drawdown_margin_scale_min=float(drawdown_margin_scale_min),
                strategy_runtime_params=strategy_runtime_params,
            )
            symbol_reports = []
            raw_symbol_counts = portfolio_metrics.get("symbols")
            if isinstance(raw_symbol_counts, dict):
                for symbol, interval_counts in sorted(raw_symbol_counts.items()):
                    if not isinstance(interval_counts, dict):
                        continue
                    normalized_counts: dict[str, int] = {}
                    for key, value in interval_counts.items():
                        try:
                            normalized_counts[str(key)] = int(value)
                        except (TypeError, ValueError):
                            continue
                    symbol_reports.append(
                        {
                            "symbol": symbol,
                            "candles_by_interval": normalized_counts,
                            "candles_10m": int(normalized_counts.get("10m", 0)),
                            "candles_30m": int(normalized_counts.get("30m", 0)),
                            "candles_1h": int(normalized_counts.get("1h", 0)),
                            "candles_4h": int(normalized_counts.get("4h", 0)),
                        }
                    )

            window_slices_6m = _build_half_year_window_summaries(
                symbol_reports=[{"trade_events": portfolio_metrics.get("trade_events", [])}],
                start_ms=int(start_ms),
                end_ms=int(end_ms),
                total_initial_capital=float(initial_capital),
            )
            state_distribution = portfolio_metrics.get("state_distribution")
            total_cycles = (
                sum(int(value) for value in state_distribution.values())
                if isinstance(state_distribution, dict)
                else 0
            )
            gross_profit = float(portfolio_metrics.get("gross_profit", 0.0))
            gross_trade_pnl = float(portfolio_metrics.get("gross_trade_pnl", 0.0))
            total_fees = float(portfolio_metrics.get("total_fees", 0.0))
            fee_to_gross_profit_pct = (
                round((total_fees / gross_profit) * 100.0, 6) if gross_profit > 0.0 else None
            )
            fee_to_trade_gross_pct = (
                round((total_fees / gross_trade_pnl) * 100.0, 6)
                if gross_trade_pnl > 0.0
                else None
            )
            research_gate = _portfolio_research_gate(
                years=int(years),
                total_net_profit=float(portfolio_metrics.get("net_profit", 0.0)),
                profit_factor=_to_float(portfolio_metrics.get("profit_factor")),
                max_drawdown_pct=float(portfolio_metrics.get("max_drawdown_pct", 0.0)),
                fee_to_trade_gross_pct=fee_to_trade_gross_pct,
            )
            report_payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "backtest": {
                    "years": years,
                    "symbols": symbols,
                    "start_ms": int(start_ms),
                    "end_ms": int(end_ms),
                    "env": cfg.env,
                    "profile": cfg.profile,
                    "mode": cfg.mode,
                    "strategy_name": active_strategy_name,
                    "window_mode": backtest_window_label,
                    "backtest_start_utc": _format_utc_iso(start_ms),
                    "backtest_end_utc": _format_utc_iso(end_ms),
                    "timeframe_base": base_interval,
                    "timeframe_context": [interval for interval in market_intervals if interval != base_interval],
                    "initial_capital_usdt": round(float(initial_capital), 6),
                    "initial_capital_locked": True,
                    "position_sizing_mode": "portfolio_risk_budget",
                    "fixed_leverage": round(float(fixed_leverage), 6),
                    "fixed_leverage_margin_use_pct": round(float(fixed_leverage_margin_use_pct), 6),
                    "reverse_min_hold_bars": int(reverse_min_hold_bars),
                    "reverse_cooldown_bars": int(reverse_cooldown_bars),
                    "min_expected_edge_over_roundtrip_cost": round(
                        float(min_expected_edge_over_roundtrip_cost),
                        6,
                    ),
                    "min_reward_risk_ratio": round(float(min_reward_risk_ratio), 6),
                    "max_trades_per_day_per_symbol": int(max_trades_per_day_per_symbol),
                    "daily_loss_limit_pct": round(float(daily_loss_limit_pct), 6),
                    "equity_floor_pct": round(float(equity_floor_pct), 6),
                    "max_trade_margin_loss_fraction": round(
                        float(max_trade_margin_loss_fraction),
                        6,
                    ),
                    "min_signal_score": round(float(min_signal_score), 6),
                    "reverse_exit_min_profit_pct": round(float(reverse_exit_min_profit_pct), 6),
                    "reverse_exit_min_signal_score": round(float(reverse_exit_min_signal_score), 6),
                    "pfd_premium_z_min": round(float(pfd_premium_z_min), 6),
                    "pfd_funding_24h_min": round(float(pfd_funding_24h_min), 6),
                    "pfd_reclaim_buffer_atr": round(float(pfd_reclaim_buffer_atr), 6),
                    "pfd_take_profit_r": round(float(pfd_take_profit_r), 6),
                    "drawdown_scale_start_pct": round(float(drawdown_scale_start_pct), 6),
                    "drawdown_scale_end_pct": round(float(drawdown_scale_end_pct), 6),
                    "drawdown_margin_scale_min": round(float(drawdown_margin_scale_min), 6),
                    "portfolio_engine": {
                        "max_open_positions": max(
                            int(getattr(cfg.behavior.engine, "max_open_positions", 1) or 1),
                            1,
                        ),
                        "max_new_entries_per_tick": max(
                            1,
                            min(int(getattr(cfg.behavior.engine, "max_open_positions", 1) or 1), 2),
                        ),
                    },
                    "execution_model": {
                        "fee_bps": round(float(execution_model.fee_bps), 6),
                        "slippage_bps": round(float(execution_model.slippage_bps), 6),
                        "funding_bps_per_8h": round(float(execution_model.funding_bps_per_8h), 6),
                    },
                },
                "summary": {
                    "symbols_ran": len(symbol_reports),
                    "total_cycles": int(total_cycles),
                    "state_distribution": state_distribution if isinstance(state_distribution, dict) else {},
                    "entry_block_distribution": portfolio_metrics.get("entry_block_distribution", {}),
                    "bucket_block_distribution": portfolio_metrics.get("bucket_block_distribution", {}),
                    "portfolio_open_slots_usage": portfolio_metrics.get("portfolio_open_slots_usage", {}),
                    "capital_utilization": portfolio_metrics.get("capital_utilization", {}),
                    "simultaneous_position_histogram": portfolio_metrics.get(
                        "simultaneous_position_histogram",
                        {},
                    ),
                    "total_initial_capital": round(float(initial_capital), 6),
                    "total_final_equity": round(
                        float(portfolio_metrics.get("final_equity", initial_capital)),
                        6,
                    ),
                    "total_net_profit": round(float(portfolio_metrics.get("net_profit", 0.0)), 6),
                    "total_return_pct": round(float(portfolio_metrics.get("total_return_pct", 0.0)), 6),
                    "total_trades": int(portfolio_metrics.get("total_trades", 0)),
                    "wins": int(portfolio_metrics.get("wins", 0)),
                    "losses": int(portfolio_metrics.get("losses", 0)),
                    "win_rate_pct": round(float(portfolio_metrics.get("win_rate_pct", 0.0)), 6),
                    "gross_profit": round(float(portfolio_metrics.get("gross_profit", 0.0)), 6),
                    "gross_loss": round(float(portfolio_metrics.get("gross_loss", 0.0)), 6),
                    "gross_trade_pnl": round(float(portfolio_metrics.get("gross_trade_pnl", 0.0)), 6),
                    "total_fees": round(float(portfolio_metrics.get("total_fees", 0.0)), 6),
                    "fee_to_gross_profit_pct": fee_to_gross_profit_pct,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                    "total_funding_pnl": round(float(portfolio_metrics.get("total_funding_pnl", 0.0)), 6),
                    "profit_factor": portfolio_metrics.get("profit_factor"),
                    "max_drawdown_pct": round(float(portfolio_metrics.get("max_drawdown_pct", 0.0)), 6),
                    "alpha_stats": portfolio_metrics.get("alpha_stats", {}),
                    "window_slices_6m": window_slices_6m,
                    "cleaned_artifacts": int(cleaned_count),
                    "research_gate": research_gate,
                },
                "symbols": symbol_reports,
                "portfolio": {
                    "trade_events": portfolio_metrics.get("trade_events", []),
                    "alpha_block_distribution": portfolio_metrics.get("alpha_block_distribution", {}),
                },
            }

            if report_path is not None:
                target = Path(report_path)
                target.parent.mkdir(parents=True, exist_ok=True)
            else:
                target = output_root / f"local_backtest_{stamp}.json"
            target.write_text(json.dumps(report_payload, indent=2, ensure_ascii=True), encoding="utf-8")
            markdown_target = _write_local_backtest_markdown(
                report_payload=report_payload,
                target_json=target,
            )

            print("LOCAL BACKTEST SUMMARY")
            print(
                " | ".join(
                    [
                        f"strategy={active_strategy_name}",
                        "sizing=portfolio",
                        f"net={float(portfolio_metrics.get('net_profit', 0.0)):.2f} USDT",
                        f"return={float(portfolio_metrics.get('total_return_pct', 0.0)):.2f}%",
                        f"win_rate={float(portfolio_metrics.get('win_rate_pct', 0.0)):.2f}%",
                        f"pf={float(portfolio_metrics.get('profit_factor') or 0.0):.3f}",
                        f"max_dd={float(portfolio_metrics.get('max_drawdown_pct', 0.0)):.2f}%",
                        f"slots={max(int(getattr(cfg.behavior.engine, 'max_open_positions', 1) or 1), 1)}",
                    ]
                )
            )
            print(f"REPORT_JSON={target}")
            print(f"REPORT_MD={markdown_target}")
            return 0

        requested_workers = int(replay_workers)
        if requested_workers > 0:
            replay_worker_count = max(1, min(int(len(downloaded_symbols)), requested_workers))
        else:
            cpu_workers = max(int(os.cpu_count() or 1), 1)
            replay_worker_count = max(1, min(int(len(downloaded_symbols)), cpu_workers))
        print(
            f"[LOCAL_BACKTEST] replay parallel started symbols={len(downloaded_symbols)} workers={replay_worker_count}"
        )
        def _worker_kwargs(item: dict[str, Any]) -> dict[str, Any]:
            symbol = str(item["symbol"])
            sqlite_path = output_root / f"backtest_{symbol.lower()}_{stamp}.sqlite3"
            return {
                "symbol": symbol,
                "profile": cfg.profile,
                "mode": cfg.mode,
                "env": cfg.env,
                "years": years,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "config_path": config_path,
                "cache_root": str(cache_root),
                "sqlite_path": str(sqlite_path),
                "initial_capital": per_symbol_initial_capital,
                "execution_model": execution_model,
                "fixed_leverage": fixed_leverage,
                "fixed_leverage_margin_use_pct": fixed_leverage_margin_use_pct,
                "reverse_min_hold_bars": reverse_min_hold_bars,
                "reverse_cooldown_bars": reverse_cooldown_bars,
                "min_expected_edge_over_roundtrip_cost": min_expected_edge_over_roundtrip_cost,
                "min_reward_risk_ratio": min_reward_risk_ratio,
                "max_trades_per_day_per_symbol": max_trades_per_day_per_symbol,
                "daily_loss_limit_pct": daily_loss_limit_pct,
                "equity_floor_pct": equity_floor_pct,
                "max_trade_margin_loss_fraction": max_trade_margin_loss_fraction,
                "min_signal_score": min_signal_score,
                "enabled_alphas_override": enabled_alphas_override,
                "reverse_exit_min_profit_pct": reverse_exit_min_profit_pct,
                "reverse_exit_min_signal_score": reverse_exit_min_signal_score,
                "default_reverse_exit_min_r": default_reverse_exit_min_r,
                "default_risk_per_trade_pct": default_risk_per_trade_pct,
                "default_max_effective_leverage": default_max_effective_leverage,
                "drawdown_scale_start_pct": drawdown_scale_start_pct,
                "drawdown_scale_end_pct": drawdown_scale_end_pct,
                "drawdown_margin_scale_min": drawdown_margin_scale_min,
                "stoploss_streak_trigger": stoploss_streak_trigger,
                "stoploss_cooldown_bars": stoploss_cooldown_bars,
                "loss_cooldown_bars": loss_cooldown_bars,
                "alpha_squeeze_percentile_max": alpha_squeeze_percentile_max,
                "alpha_expansion_buffer_bps": alpha_expansion_buffer_bps,
                "alpha_expansion_range_atr_min": alpha_expansion_range_atr_min,
                "alpha_expansion_body_ratio_min": alpha_expansion_body_ratio_min,
                "alpha_expansion_close_location_min": alpha_expansion_close_location_min,
                "alpha_expansion_width_expansion_min": alpha_expansion_width_expansion_min,
                "alpha_expansion_break_distance_atr_min": alpha_expansion_break_distance_atr_min,
                "alpha_expansion_breakout_efficiency_min": alpha_expansion_breakout_efficiency_min,
                "alpha_expansion_breakout_stability_score_min": alpha_expansion_breakout_stability_score_min,
                "alpha_expansion_breakout_stability_edge_score_min": alpha_expansion_breakout_stability_edge_score_min,
                "alpha_expansion_quality_score_min": alpha_expansion_quality_score_min,
                "alpha_expansion_quality_score_v2_min": effective_alpha_expansion_quality_score_v2_min,
                "alpha_min_volume_ratio": alpha_min_volume_ratio,
                "alpha_take_profit_r": alpha_take_profit_r,
                "alpha_time_stop_bars": alpha_time_stop_bars,
                "alpha_trend_adx_min_4h": alpha_trend_adx_min_4h,
                "alpha_trend_adx_max_4h": alpha_trend_adx_max_4h,
                "alpha_trend_adx_rising_lookback_4h": alpha_trend_adx_rising_lookback_4h,
                "alpha_trend_adx_rising_min_delta_4h": alpha_trend_adx_rising_min_delta_4h,
                "alpha_expected_move_cost_mult": alpha_expected_move_cost_mult,
                "drift_side_mode": drift_side_mode,
                "drift_take_profit_r": drift_take_profit_r,
                "drift_time_stop_bars": drift_time_stop_bars,
                "fb_failed_break_buffer_bps": fb_failed_break_buffer_bps,
                "fb_wick_ratio_min": fb_wick_ratio_min,
                "fb_take_profit_r": fb_take_profit_r,
                "fb_time_stop_bars": fb_time_stop_bars,
                "cbr_squeeze_percentile_max": cbr_squeeze_percentile_max,
                "cbr_breakout_buffer_bps": cbr_breakout_buffer_bps,
                "cbr_take_profit_r": cbr_take_profit_r,
                "cbr_time_stop_bars": cbr_time_stop_bars,
                "cbr_trend_adx_min_4h": cbr_trend_adx_min_4h,
                "cbr_ema_gap_trend_min_frac_4h": cbr_ema_gap_trend_min_frac_4h,
                "cbr_breakout_min_range_atr": cbr_breakout_min_range_atr,
                "cbr_breakout_min_volume_ratio": cbr_breakout_min_volume_ratio,
                "sfd_reclaim_sweep_buffer_bps": sfd_reclaim_sweep_buffer_bps,
                "sfd_reclaim_wick_ratio_min": sfd_reclaim_wick_ratio_min,
                "sfd_drive_breakout_range_atr_min": sfd_drive_breakout_range_atr_min,
                "sfd_take_profit_r": sfd_take_profit_r,
                "pfd_premium_z_min": pfd_premium_z_min,
                "pfd_funding_24h_min": pfd_funding_24h_min,
                "pfd_reclaim_buffer_atr": pfd_reclaim_buffer_atr,
                "pfd_take_profit_r": pfd_take_profit_r,
                "market_intervals": list(market_intervals),
                "max_peak_drawdown_pct": max_peak_drawdown_pct,
            }

        def _consume_result(*, item: dict[str, Any], result: dict[str, Any], done_count: int) -> None:
            nonlocal total_cycles
            symbol = str(item.get("symbol", "UNKNOWN"))
            if bool(result.get("skipped")):
                print(
                    f"[LOCAL_BACKTEST] replay skipped symbol={symbol} reason={result.get('reason', 'unknown')}"
                )
                return

            report_raw = result.get("report")
            if not isinstance(report_raw, dict):
                print(f"[LOCAL_BACKTEST] replay invalid report symbol={symbol}")
                return

            per_symbol_report = dict(report_raw)
            interval_counts = item.get("candles_by_interval")
            if isinstance(interval_counts, dict):
                normalized_counts: dict[str, int] = {}
                for key, value in interval_counts.items():
                    try:
                        normalized_counts[str(key)] = int(value)
                    except (TypeError, ValueError):
                        continue
                per_symbol_report["candles_by_interval"] = normalized_counts
                per_symbol_report["candles_10m"] = int(normalized_counts.get("10m", 0))
                per_symbol_report["candles_30m"] = int(normalized_counts.get("30m", 0))
                per_symbol_report["candles_1h"] = int(normalized_counts.get("1h", 0))
                per_symbol_report["candles_4h"] = int(normalized_counts.get("4h", 0))

            symbol_cycles = 0
            try:
                symbol_cycles = int(result.get("total_cycles", 0))
            except (TypeError, ValueError):
                symbol_cycles = 0
            per_symbol_report["cycles"] = symbol_cycles
            total_cycles += symbol_cycles

            state_distribution = result.get("state_distribution")
            if isinstance(state_distribution, dict):
                for key, value in state_distribution.items():
                    try:
                        combined_state_counter[str(key)] += int(value)
                    except (TypeError, ValueError):
                        continue

            symbol_reports.append(per_symbol_report)
            print(
                f"[LOCAL_BACKTEST] [{done_count}/{len(downloaded_symbols)}] {symbol} done net={float(per_symbol_report['net_profit']):.2f} return={float(per_symbol_report['total_return_pct']):.2f}% win={float(per_symbol_report['win_rate_pct']):.2f}% max_dd={float(per_symbol_report['max_drawdown_pct']):.2f}%"
            )

        def _run_replay_sequential(items: list[dict[str, Any]]) -> None:
            print("[LOCAL_BACKTEST] replay mode=sequential")
            for idx, item in enumerate(items, start=1):
                symbol = str(item.get("symbol", "UNKNOWN"))
                kwargs = _worker_kwargs(item)
                try:
                    result = get_local_backtest_symbol_replay_worker()(**kwargs)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[LOCAL_BACKTEST] replay failed symbol={symbol} error={type(exc).__name__}:{exc}"
                    )
                    continue
                _consume_result(item=item, result=result, done_count=idx)

        if replay_worker_count <= 1:
            _run_replay_sequential(downloaded_symbols)
        else:
            try:
                with ProcessPoolExecutor(max_workers=replay_worker_count) as pool:
                    future_to_symbol: dict[Any, dict[str, Any]] = {}
                    for item in downloaded_symbols:
                        symbol = str(item["symbol"])
                        future = pool.submit(
                            get_local_backtest_symbol_replay_worker(),
                            **_worker_kwargs(item),
                        )
                        future_to_symbol[future] = item
                        print(f"[LOCAL_BACKTEST] replay queued symbol={symbol}")

                    done_count = 0
                    pending = set(future_to_symbol.keys())
                    replay_started_at = time.monotonic()
                    heartbeat_interval_sec = 20.0

                    while pending:
                        done_now, pending = wait(
                            pending,
                            timeout=heartbeat_interval_sec,
                            return_when=FIRST_COMPLETED,
                        )

                        if not done_now:
                            elapsed_sec = int(time.monotonic() - replay_started_at)
                            pending_symbols = [
                                str(future_to_symbol[f].get("symbol", "UNKNOWN"))
                                for f in pending
                                if f in future_to_symbol
                            ]
                            pending_symbols.sort()
                            pending_label = ",".join(pending_symbols[:6])
                            if len(pending_symbols) > 6:
                                pending_label = pending_label + ",..."
                            print(
                                f"[LOCAL_BACKTEST] replay running done={done_count}/{len(downloaded_symbols)} pending={len(pending)} elapsed={elapsed_sec}s symbols={pending_label}"
                            )
                            continue

                        for future in done_now:
                            done_count += 1
                            item = future_to_symbol[future]
                            symbol = str(item.get("symbol", "UNKNOWN"))
                            try:
                                result = future.result()
                            except Exception as exc:
                                print(
                                    f"[LOCAL_BACKTEST] replay failed symbol={symbol} error={type(exc).__name__}:{exc}"
                                )
                                continue
                            _consume_result(item=item, result=result, done_count=done_count)
            except PermissionError as exc:
                print(
                    f"[LOCAL_BACKTEST] replay parallel unavailable error={type(exc).__name__}:{exc} fallback=sequential"
                )
                _run_replay_sequential(downloaded_symbols)

        if len(symbol_reports) == 0:
            print(json.dumps({"error": "no local backtest results"}, ensure_ascii=True))
            return 1

        total_initial = sum(float(item.get("initial_capital", 0.0)) for item in symbol_reports)
        total_final = sum(float(item["final_equity"]) for item in symbol_reports)
        total_net = sum(float(item["net_profit"]) for item in symbol_reports)
        total_gross_profit = sum(float(item["gross_profit"]) for item in symbol_reports)
        total_gross_loss = sum(float(item["gross_loss"]) for item in symbol_reports)
        total_trade_gross_pnl = sum(
            float(item.get("gross_trade_pnl", 0.0)) for item in symbol_reports
        )
        total_fees = sum(float(item.get("total_fees", 0.0)) for item in symbol_reports)
        total_funding_pnl = sum(
            float(item.get("total_funding_pnl", 0.0)) for item in symbol_reports
        )
        entry_block_counter = Counter()
        for item in symbol_reports:
            blocks = item.get("entry_block_distribution")
            if isinstance(blocks, dict):
                for key, value in blocks.items():
                    try:
                        entry_block_counter[str(key)] += int(value)
                    except (TypeError, ValueError):
                        continue
        alpha_block_distribution: dict[str, Counter[str]] = {}
        combined_trade_events: list[dict[str, Any]] = []
        for item in symbol_reports:
            alpha_blocks = item.get("alpha_block_distribution")
            if isinstance(alpha_blocks, dict):
                for alpha_id, source in alpha_blocks.items():
                    if not isinstance(source, dict):
                        continue
                    counter = alpha_block_distribution.setdefault(str(alpha_id), Counter())
                    for reason, value in source.items():
                        try:
                            counter[str(reason)] += int(value)
                        except (TypeError, ValueError):
                            continue
            trade_events = item.get("trade_events")
            if isinstance(trade_events, list):
                for trade in trade_events:
                    if isinstance(trade, dict):
                        combined_trade_events.append(dict(trade))
        alpha_stats = _summarize_alpha_stats(
            trade_events=combined_trade_events,
            alpha_block_distribution=alpha_block_distribution,
            initial_capital=float(total_initial),
        )
        total_trades = sum(int(item["total_trades"]) for item in symbol_reports)
        total_wins = sum(int(item["wins"]) for item in symbol_reports)
        total_losses = sum(int(item["losses"]) for item in symbol_reports)
        win_rate_pct = (
            (float(total_wins) / float(total_trades) * 100.0) if total_trades > 0 else 0.0
        )
        profit_factor = (total_gross_profit / total_gross_loss) if total_gross_loss > 0 else None
        max_drawdown_pct = max(float(item["max_drawdown_pct"]) for item in symbol_reports)
        total_return_pct = (
            ((total_final - total_initial) / total_initial * 100.0) if total_initial > 0 else 0.0
        )
        fee_to_gross_profit_pct = (
            (total_fees / total_gross_profit * 100.0) if total_gross_profit > 0 else None
        )
        fee_to_trade_gross_pct = (
            (total_fees / total_trade_gross_pnl * 100.0) if total_trade_gross_pnl > 0 else None
        )
        window_slices_6m = _build_half_year_window_summaries(
            symbol_reports=symbol_reports,
            start_ms=int(start_ms),
            end_ms=int(end_ms),
            total_initial_capital=float(total_initial),
        )

        cleaned_count = _cleanup_local_backtest_artifacts(output_root)
        if cleaned_count > 0:
            print(f"[LOCAL_BACKTEST] cleanup removed artifacts={cleaned_count}")

        cycle_counter = Counter(combined_state_counter)
        report_payload: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "backtest": {
                "years": years,
                "symbols": symbols,
                "start_ms": int(start_ms),
                "end_ms": int(end_ms),
                "env": cfg.env,
                "profile": cfg.profile,
                "mode": cfg.mode,
                "strategy_name": active_strategy_name,
                "window_mode": backtest_window_label,
                "backtest_start_utc": _format_utc_iso(start_ms),
                "backtest_end_utc": _format_utc_iso(end_ms),
                "timeframe_base": base_interval,
                "timeframe_context": [interval for interval in market_intervals if interval != base_interval],
                "initial_capital_usdt": round(float(initial_capital), 6),
                "initial_capital_locked": True,
                "position_sizing_mode": (
                    "volatility_targeted" if use_ra_2026_mode else "fixed_leverage"
                ),
                "fixed_leverage": round(float(fixed_leverage), 6),
                "fixed_leverage_margin_use_pct": round(float(fixed_leverage_margin_use_pct), 6),
                "reverse_min_hold_bars": int(reverse_min_hold_bars),
                "reverse_cooldown_bars": int(reverse_cooldown_bars),
                "min_expected_edge_over_roundtrip_cost": round(
                    float(min_expected_edge_over_roundtrip_cost),
                    6,
                ),
                "min_reward_risk_ratio": round(float(min_reward_risk_ratio), 6),
                "max_trades_per_day_per_symbol": int(max_trades_per_day_per_symbol),
                "daily_loss_limit_pct": round(float(daily_loss_limit_pct), 6),
                "equity_floor_pct": round(float(equity_floor_pct), 6),
                "max_trade_margin_loss_fraction": round(
                    float(max_trade_margin_loss_fraction),
                    6,
                ),
                "min_signal_score": round(float(min_signal_score), 6),
                "reverse_exit_min_profit_pct": round(float(reverse_exit_min_profit_pct), 6),
                "reverse_exit_min_signal_score": round(float(reverse_exit_min_signal_score), 6),
                "alpha_squeeze_percentile_max": round(
                    float(_runtime_override("squeeze_percentile_threshold", alpha_squeeze_percentile_max) or 0.0),
                    6,
                ),
                "alpha_expansion_buffer_bps": round(
                    float(_runtime_override("expansion_buffer_bps", alpha_expansion_buffer_bps) or 0.0),
                    6,
                ),
                "alpha_expansion_range_atr_min": round(
                    float(_runtime_override("expansion_range_atr_min", alpha_expansion_range_atr_min) or 0.0),
                    6,
                ),
                "alpha_expansion_body_ratio_min": round(
                    float(_runtime_override("expansion_body_ratio_min", alpha_expansion_body_ratio_min) or 0.0),
                    6,
                ),
                "alpha_expansion_close_location_min": round(
                    float(_runtime_override("expansion_close_location_min", alpha_expansion_close_location_min) or 0.0),
                    6,
                ),
                "alpha_expansion_width_expansion_min": round(
                    float(_runtime_override("expansion_width_expansion_min", alpha_expansion_width_expansion_min) or 0.0),
                    6,
                ),
                "alpha_expansion_break_distance_atr_min": round(
                    float(_runtime_override("expansion_break_distance_atr_min", alpha_expansion_break_distance_atr_min) or 0.0),
                    6,
                ),
                "alpha_expansion_breakout_efficiency_min": round(
                    float(_runtime_override("expansion_breakout_efficiency_min", alpha_expansion_breakout_efficiency_min) or 0.0),
                    6,
                ),
                "alpha_expansion_breakout_stability_score_min": round(
                    float(_runtime_override("expansion_breakout_stability_score_min", alpha_expansion_breakout_stability_score_min) or 0.0),
                    6,
                ),
                "alpha_expansion_breakout_stability_edge_score_min": round(
                    float(_runtime_override("expansion_breakout_stability_edge_score_min", alpha_expansion_breakout_stability_edge_score_min) or 0.0),
                    6,
                ),
                "alpha_expansion_quality_score_min": round(
                    float(_runtime_override("expansion_quality_score_min", alpha_expansion_quality_score_min) or 0.0),
                    6,
                ),
                "alpha_expansion_quality_score_v2_min": round(
                    float(effective_alpha_expansion_quality_score_v2_min),
                    6,
                ),
                "alpha_min_volume_ratio": round(
                    float(_runtime_override("min_volume_ratio_15m", alpha_min_volume_ratio) or 0.0),
                    6,
                ),
                "alpha_take_profit_r": round(
                    float(_runtime_override("take_profit_r", alpha_take_profit_r) or 0.0),
                    6,
                ),
                "alpha_time_stop_bars": int(_runtime_override("time_stop_bars", alpha_time_stop_bars) or 0),
                "alpha_trend_adx_min_4h": round(
                    float(_runtime_override("trend_adx_min_4h", alpha_trend_adx_min_4h) or 0.0),
                    6,
                ),
                "alpha_trend_adx_max_4h": round(
                    float(_runtime_override("trend_adx_max_4h", alpha_trend_adx_max_4h) or 0.0),
                    6,
                ),
                "alpha_trend_adx_rising_lookback_4h": int(
                    _runtime_override("trend_adx_rising_lookback_4h", alpha_trend_adx_rising_lookback_4h)
                    or 0
                ),
                "alpha_trend_adx_rising_min_delta_4h": round(
                    float(_runtime_override("trend_adx_rising_min_delta_4h", alpha_trend_adx_rising_min_delta_4h) or 0.0),
                    6,
                ),
                "alpha_expected_move_cost_mult": round(
                    float(_runtime_override("expected_move_cost_mult", alpha_expected_move_cost_mult) or 0.0),
                    6,
                ),
                "fb_failed_break_buffer_bps": round(float(fb_failed_break_buffer_bps), 6),
                "fb_wick_ratio_min": round(float(fb_wick_ratio_min), 6),
                "fb_take_profit_r": round(float(fb_take_profit_r), 6),
                "fb_time_stop_bars": int(fb_time_stop_bars),
                "cbr_squeeze_percentile_max": round(float(cbr_squeeze_percentile_max), 6),
                "cbr_breakout_buffer_bps": round(float(cbr_breakout_buffer_bps), 6),
                "cbr_take_profit_r": round(float(cbr_take_profit_r), 6),
                "cbr_time_stop_bars": int(cbr_time_stop_bars),
                "cbr_trend_adx_min_4h": round(float(cbr_trend_adx_min_4h), 6),
                "cbr_ema_gap_trend_min_frac_4h": round(
                    float(cbr_ema_gap_trend_min_frac_4h),
                    6,
                ),
                "cbr_breakout_min_range_atr": round(float(cbr_breakout_min_range_atr), 6),
                "cbr_breakout_min_volume_ratio": round(float(cbr_breakout_min_volume_ratio), 6),
                "sfd_reclaim_sweep_buffer_bps": round(float(sfd_reclaim_sweep_buffer_bps), 6),
                "sfd_reclaim_wick_ratio_min": round(float(sfd_reclaim_wick_ratio_min), 6),
                "sfd_drive_breakout_range_atr_min": round(
                    float(sfd_drive_breakout_range_atr_min),
                    6,
                ),
                "sfd_take_profit_r": round(float(sfd_take_profit_r), 6),
                "pfd_premium_z_min": round(float(pfd_premium_z_min), 6),
                "pfd_funding_24h_min": round(float(pfd_funding_24h_min), 6),
                "pfd_reclaim_buffer_atr": round(float(pfd_reclaim_buffer_atr), 6),
                "pfd_take_profit_r": round(float(pfd_take_profit_r), 6),
                "default_reverse_exit_min_r": round(float(default_reverse_exit_min_r), 6),
                "default_risk_per_trade_pct": round(float(default_risk_per_trade_pct), 6),
                "default_max_effective_leverage": round(
                    float(default_max_effective_leverage),
                    6,
                ),
                "drawdown_scale_start_pct": round(float(drawdown_scale_start_pct), 6),
                "drawdown_scale_end_pct": round(float(drawdown_scale_end_pct), 6),
                "drawdown_margin_scale_min": round(float(drawdown_margin_scale_min), 6),
                "stoploss_streak_trigger": int(stoploss_streak_trigger),
                "stoploss_cooldown_bars": int(stoploss_cooldown_bars),
                "loss_cooldown_bars": int(loss_cooldown_bars),
                "max_peak_drawdown_pct": (
                    None
                    if max_peak_drawdown_pct is None
                    else round(float(max_peak_drawdown_pct), 6)
                ),
                "execution_model": {
                    "fee_bps": round(float(execution_model.fee_bps), 6),
                    "slippage_bps": round(float(execution_model.slippage_bps), 6),
                    "funding_bps_per_8h": round(float(execution_model.funding_bps_per_8h), 6),
                },
            },
            "summary": {
                "symbols_ran": len(symbol_reports),
                "total_cycles": int(total_cycles),
                "state_distribution": dict(cycle_counter),
                "entry_block_distribution": dict(entry_block_counter),
                "total_initial_capital": round(total_initial, 6),
                "total_final_equity": round(total_final, 6),
                "total_net_profit": round(total_net, 6),
                "total_return_pct": round(total_return_pct, 6),
                "total_trades": total_trades,
                "wins": total_wins,
                "losses": total_losses,
                "win_rate_pct": round(win_rate_pct, 6),
                "gross_profit": round(total_gross_profit, 6),
                "gross_loss": round(total_gross_loss, 6),
                "gross_trade_pnl": round(total_trade_gross_pnl, 6),
                "total_fees": round(total_fees, 6),
                "fee_to_gross_profit_pct": (
                    None if fee_to_gross_profit_pct is None else round(fee_to_gross_profit_pct, 6)
                ),
                "fee_to_trade_gross_pct": (
                    None if fee_to_trade_gross_pct is None else round(fee_to_trade_gross_pct, 6)
                ),
                "total_funding_pnl": round(total_funding_pnl, 6),
                "profit_factor": None if profit_factor is None else round(profit_factor, 6),
                "max_drawdown_pct": round(max_drawdown_pct, 6),
                "alpha_stats": alpha_stats,
                "window_slices_6m": window_slices_6m,
                "cleaned_artifacts": cleaned_count,
            },
            "symbols": symbol_reports,
        }
        if report_path is not None:
            target = Path(report_path)
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            target = output_root / f"local_backtest_{stamp}.json"
        target.write_text(json.dumps(report_payload, indent=2, ensure_ascii=True), encoding="utf-8")
        markdown_target = _write_local_backtest_markdown(
            report_payload=report_payload, target_json=target
        )

        print("LOCAL BACKTEST SUMMARY")
        print(
            " | ".join(
                [
                    f"strategy={active_strategy_name}",
                    f"sizing={'vol_target' if use_ra_2026_mode else 'fixed'}",
                    f"net={total_net:.2f} USDT",
                    f"return={total_return_pct:.2f}%",
                    f"win_rate={win_rate_pct:.2f}%",
                    f"pf={(profit_factor if profit_factor is not None else 0.0):.3f}",
                    f"lev={fixed_leverage:.0f}x",
                    f"margin_use={fixed_leverage_margin_use_pct * 100:.0f}%",
                    f"daily_stop={daily_loss_limit_pct * 100:.1f}%",
                    f"equity_floor={equity_floor_pct * 100:.1f}%",
                    f"score_min={min_signal_score:.2f}",
                    f"rr_min={min_reward_risk_ratio:.2f}",
                    f"rev_profit_min={reverse_exit_min_profit_pct * 100:.2f}%",
                    f"rev_score_min={reverse_exit_min_signal_score:.2f}",
                    f"dd_scale={drawdown_scale_start_pct * 100:.0f}->{drawdown_scale_end_pct * 100:.0f}%/{drawdown_margin_scale_min * 100:.0f}%",
                    f"sl_streak={stoploss_streak_trigger}",
                    f"sl_cd={stoploss_cooldown_bars}",
                    f"loss_cd={loss_cooldown_bars}",
                    f"fees={total_fees:.2f}",
                    f"funding={total_funding_pnl:.2f}",
                    f"max_dd={max_drawdown_pct:.2f}%",
                    f"trades={total_trades}",
                    f"cleaned={cleaned_count}",
                ]
            )
        )
        top_entry_block_items: list[tuple[str, int]] = []
        for reason, count in entry_block_counter.items():
            try:
                top_entry_block_items.append((str(reason), int(count)))
            except (TypeError, ValueError):
                continue
        top_entry_block_items.sort(key=lambda item: (-item[1], item[0]))
        if top_entry_block_items:
            top_entry_preview = ", ".join(
                f"{reason}:{count}" for reason, count in top_entry_block_items[:8]
            )
            print(f"ENTRY_BLOCK_TOP={top_entry_preview}")

        suggest_signal = float(min_signal_score)
        suggest_rr = float(min_reward_risk_ratio)
        suggest_cd = int(reverse_cooldown_bars)
        suggest_edge = float(min_expected_edge_over_roundtrip_cost)
        suggest_trades = max(1, int(max_trades_per_day_per_symbol))
        suggest_hold = max(0, int(reverse_min_hold_bars) - 2)
        suggest_reason = ""

        if top_entry_block_items:
            suggest_reason = top_entry_block_items[0][0]
            top_count = int(top_entry_block_items[0][1])
            if top_count > 0:
                if suggest_reason == "score_quality_block":
                    suggest_signal = max(0.10, suggest_signal - 0.15)
                elif suggest_reason == "edge_cost_block":
                    suggest_edge = max(1.0, suggest_edge - 0.60)
                elif suggest_reason in {
                    "reward_risk_block",
                    "reward_risk_missing_block",
                }:
                    suggest_rr = max(0.40, suggest_rr - 0.80)
                elif suggest_reason == "reverse_cooldown_block":
                    suggest_cd = max(6, int(suggest_cd * 0.6))
                elif suggest_reason == "reverse_hold_block":
                    suggest_hold = max(0, suggest_hold - 2)
                elif suggest_reason in {
                    "stoploss_cooldown_block",
                    "loss_streak_cooldown_block",
                }:
                    suggest_cd = max(6, int(suggest_cd * 0.7))
                    suggest_trades = max(1, suggest_trades - 1)

        weak_sample = int(total_trades) < 12 or (
            profit_factor is not None and float(profit_factor) < 1.0
        )
        if weak_sample:
            symbol_csv = ",".join(symbols)
            suggest_signal = max(0.10, suggest_signal - 0.05)
            suggest_rr = max(0.40, suggest_rr - 0.10)
            suggest_cd = max(6, int(suggest_cd * 0.9))
            suggest_trades = max(1, suggest_trades)
            suggest_signal = min(suggest_signal, float(min_signal_score))
            suggest_env = [
                f"BACKTEST_OFFLINE={1 if offline else 0}",
                f"BACKTEST_SYMBOLS={symbol_csv}",
                f"MIN_SIGNAL_SCORE={suggest_signal:.2f}",
                f"MIN_REWARD_RISK_RATIO={suggest_rr:.2f}",
                f"MIN_EXPECTED_EDGE_MULTIPLE={suggest_edge:.2f}",
                f"REVERSE_COOLDOWN_BARS={suggest_cd}",
                f"REVERSE_MIN_HOLD_BARS={suggest_hold}",
                f"MAX_TRADES_PER_DAY={suggest_trades}",
            ]
            if backtest_window_label == "fixed":
                suggest_env.extend(
                    [
                        f"BACKTEST_START_UTC={_format_utc_iso(start_ms)}",
                        f"BACKTEST_END_UTC={_format_utc_iso(end_ms)}",
                    ]
                )
            print(f"SUGGEST={' '.join(suggest_env)}")
            suggest_cmd = f"{' '.join(suggest_env)} bash local_backtest/run_local_backtest.sh {symbol_csv} {years} {initial_capital:g}"
            print(
                f"SUGGEST_CMD={suggest_cmd}",
            )
            if suggest_reason:
                print(f"SUGGEST_REASON=top_entry_block={suggest_reason}")

        print(f"REPORT_JSON={target}")
        print(f"REPORT_MD={markdown_target}")

        print(
            json.dumps(
                {
                    "backtest": {
                        "status": "completed",
                        "report": str(target),
                        "report_markdown": str(markdown_target),
                        "symbols": symbols,
                        "years": years,
                        "cleaned_artifacts": cleaned_count,
                    }
                },
                ensure_ascii=True,
            )
        )
        return 0
    finally:
        leftovers = _cleanup_local_backtest_artifacts(output_root)
        if leftovers > 0:
            print(f"[LOCAL_BACKTEST] cleanup removed leftovers={leftovers}")
