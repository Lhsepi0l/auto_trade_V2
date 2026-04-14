from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from v2.management import normalize_management_policy
from v2.strategies.ra_2026_alpha_v2_alpha import (
    ALL_ALPHA_IDS,
    DEFAULT_ALPHA_IDS,
    SUPPORTED_SYMBOLS,
    AlphaId,
)


@dataclass(frozen=True)
class RA2026AlphaV2Params:
    supported_symbols: tuple[str, ...] = SUPPORTED_SYMBOLS
    enabled_alphas: tuple[AlphaId, ...] = DEFAULT_ALPHA_IDS
    ema_fast_4h: int = 50
    ema_slow_4h: int = 200
    adx_period_4h: int = 14
    trend_adx_min_4h: float = 16.0
    trend_adx_max_4h: float = 0.0
    trend_adx_rising_lookback_4h: int = 0
    trend_adx_rising_min_delta_4h: float = 0.0
    ema_bias_period_1h: int = 34
    rsi_period_1h: int = 14
    bias_rsi_long_min_1h: float = 50.0
    bias_rsi_short_max_1h: float = 50.0
    atr_period_15m: int = 14
    ema_pullback_period_15m: int = 20
    donchian_period_15m: int = 20
    bb_period_15m: int = 20
    bb_std_15m: float = 2.0
    volume_sma_period_15m: int = 20
    swing_lookback_15m: int = 8
    breakout_buffer_bps: float = 4.0
    expansion_buffer_bps: float = 3.0
    expansion_body_ratio_min: float = 0.0
    expansion_close_location_min: float = 0.0
    expansion_width_expansion_min: float = 0.0
    expansion_break_distance_atr_min: float = 0.0
    expansion_long_break_distance_atr_max: float = 1.1
    expansion_retest_band_atr_mult: float = 0.35
    expansion_strong_immediate_body_ratio_min: float = 0.55
    expansion_strong_immediate_close_location_min: float = 0.72
    expansion_strong_immediate_quality_score_v2_min: float = 0.72
    expansion_strong_immediate_breakout_distance_atr_max: float = 0.85
    expansion_strong_immediate_require_regime_alignment: bool = False
    expansion_strong_immediate_side_mode: str = "BOTH"
    expansion_short_confirm_break_distance_atr_min: float = 0.0
    expansion_short_confirm_close_location_min: float = 0.0
    expansion_long_confirm_close_location_max: float = 1.0
    expansion_short_break_distance_atr_max: float = 0.0
    expansion_breakout_efficiency_min: float = 0.0
    expansion_breakout_stability_score_min: float = 0.0
    expansion_breakout_stability_edge_score_min: float = 0.0
    expansion_quality_score_min: float = 0.0
    expansion_quality_score_v2_min: float = 0.0
    min_volume_ratio_15m: float = 0.9
    pullback_touch_atr_mult: float = 0.35
    expansion_range_atr_min: float = 1.1
    squeeze_percentile_threshold: float = 0.35
    squeeze_lookback_15m: int = 48
    drift_range_atr_min: float = 1.2
    drift_body_ratio_min: float = 0.5
    drift_close_location_max: float = 0.6
    drift_long_width_expansion_min: float = 0.10
    drift_long_edge_ratio_min: float = 1.10
    drift_short_width_expansion_max: float = 0.05
    drift_bias_rsi_long_min: float = 40.0
    drift_bias_rsi_short_max: float = 55.0
    drift_side_mode: str = "BOTH"
    drift_setup_expiry_bars: int = 8
    drift_take_profit_r: float = 1.8
    drift_time_stop_bars: int = 16
    taker_fee: float = 0.0006
    slippage_bps: float = 2.0
    max_spread_bps: float = 8.0
    backtest_spread_bps_fallback: float = 1.5
    min_expected_move_floor: float = 0.0006
    expected_move_cost_mult: float = 2.0
    min_stop_distance_frac: float = 0.002
    risk_per_trade_pct: float = 0.012
    max_effective_leverage: float = 0.0
    stop_atr_mult: float = 1.5
    take_profit_r: float = 2.0
    management_policy: str = "tp1_runner"
    tp_partial_ratio: float = 0.25
    tp_partial_target_frac: float = 0.60
    tp_partial_min_r: float = 1.0
    tp_partial_max_r: float = 1.2
    move_stop_to_be_at_r: float = 1.0
    time_stop_bars: int = 24
    stop_exit_cooldown_bars: int = 12
    profit_exit_cooldown_bars: int = 0
    progress_check_bars: int = 0
    progress_min_mfe_r: float = 0.0
    progress_extend_trigger_r: float = 0.0
    progress_extend_bars: int = 0
    quality_exit_score_threshold: float = 0.0
    quality_exit_take_profit_r: float = 0.0
    quality_exit_time_stop_bars: int = 0
    selective_extension_proof_bars: int = 0
    selective_extension_min_mfe_r: float = 0.0
    selective_extension_min_regime_strength: float = 0.0
    selective_extension_min_bias_strength: float = 0.0
    selective_extension_min_quality_score_v2: float = 0.0
    selective_extension_time_stop_bars: int = 0
    selective_extension_take_profit_r: float = 0.0
    selective_extension_move_stop_to_be_at_r: float = 0.0

    @classmethod
    def from_params(cls, raw: dict[str, Any] | None) -> "RA2026AlphaV2Params":
        source = raw or {}

        def _i(name: str, default: int) -> int:
            value = source.get(name, default)
            try:
                return max(int(value), 1)
            except (TypeError, ValueError):
                return int(default)

        def _i0(name: str, default: int) -> int:
            value = source.get(name, default)
            try:
                return max(int(value), 0)
            except (TypeError, ValueError):
                return int(default)

        def _f(name: str, default: float) -> float:
            value = source.get(name, default)
            try:
                return float(value)
            except (TypeError, ValueError):
                return float(default)

        def _b(name: str, default: bool) -> bool:
            value = source.get(name, default)
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"1", "true", "yes", "y", "on"}:
                return True
            if text in {"0", "false", "no", "n", "off"}:
                return False
            return bool(default)

        def _symbols(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw_symbols = source.get(name, default)
            if isinstance(raw_symbols, str):
                tokens = [token.strip().upper() for token in raw_symbols.split(",") if token.strip()]
            elif isinstance(raw_symbols, (list, tuple)):
                tokens = [str(token).strip().upper() for token in raw_symbols if str(token).strip()]
            else:
                tokens = []
            if not tokens:
                return tuple(default)
            deduped: list[str] = []
            for token in tokens:
                if token not in deduped:
                    deduped.append(token)
            return tuple(deduped)

        def _alphas(name: str, default: tuple[AlphaId, ...]) -> tuple[AlphaId, ...]:
            raw_alphas = source.get(name, default)
            if isinstance(raw_alphas, str):
                tokens = [token.strip().lower() for token in raw_alphas.split(",") if token.strip()]
            elif isinstance(raw_alphas, (list, tuple)):
                tokens = [str(token).strip().lower() for token in raw_alphas if str(token).strip()]
            else:
                tokens = []
            allowed = set(ALL_ALPHA_IDS)
            deduped: list[AlphaId] = []
            for token in tokens:
                if token in allowed and token not in deduped:
                    deduped.append(token)  # type: ignore[arg-type]
            return tuple(deduped) if deduped else tuple(default)

        def _management_policy(name: str, default: str) -> str:
            value = normalize_management_policy(source.get(name, default))
            return value or str(default)

        return cls(
            supported_symbols=_symbols("supported_symbols", cls.supported_symbols),
            enabled_alphas=_alphas("enabled_alphas", cls.enabled_alphas),
            ema_fast_4h=_i("ema_fast_4h", cls.ema_fast_4h),
            ema_slow_4h=_i("ema_slow_4h", cls.ema_slow_4h),
            adx_period_4h=_i("adx_period_4h", cls.adx_period_4h),
            trend_adx_min_4h=max(_f("trend_adx_min_4h", cls.trend_adx_min_4h), 0.0),
            trend_adx_max_4h=max(_f("trend_adx_max_4h", cls.trend_adx_max_4h), 0.0),
            trend_adx_rising_lookback_4h=max(_i("trend_adx_rising_lookback_4h", cls.trend_adx_rising_lookback_4h), 0),
            trend_adx_rising_min_delta_4h=max(_f("trend_adx_rising_min_delta_4h", cls.trend_adx_rising_min_delta_4h), 0.0),
            ema_bias_period_1h=_i("ema_bias_period_1h", cls.ema_bias_period_1h),
            rsi_period_1h=_i("rsi_period_1h", cls.rsi_period_1h),
            bias_rsi_long_min_1h=max(_f("bias_rsi_long_min_1h", cls.bias_rsi_long_min_1h), 0.0),
            bias_rsi_short_max_1h=max(_f("bias_rsi_short_max_1h", cls.bias_rsi_short_max_1h), 0.0),
            atr_period_15m=_i("atr_period_15m", cls.atr_period_15m),
            ema_pullback_period_15m=_i("ema_pullback_period_15m", cls.ema_pullback_period_15m),
            donchian_period_15m=_i("donchian_period_15m", cls.donchian_period_15m),
            bb_period_15m=_i("bb_period_15m", cls.bb_period_15m),
            bb_std_15m=max(_f("bb_std_15m", cls.bb_std_15m), 0.1),
            volume_sma_period_15m=_i("volume_sma_period_15m", cls.volume_sma_period_15m),
            swing_lookback_15m=_i("swing_lookback_15m", cls.swing_lookback_15m),
            breakout_buffer_bps=max(_f("breakout_buffer_bps", cls.breakout_buffer_bps), 0.0),
            expansion_buffer_bps=max(_f("expansion_buffer_bps", cls.expansion_buffer_bps), 0.0),
            expansion_body_ratio_min=min(max(_f("expansion_body_ratio_min", cls.expansion_body_ratio_min), 0.0), 1.0),
            expansion_close_location_min=min(max(_f("expansion_close_location_min", cls.expansion_close_location_min), 0.0), 1.0),
            expansion_width_expansion_min=max(_f("expansion_width_expansion_min", cls.expansion_width_expansion_min), 0.0),
            expansion_break_distance_atr_min=max(_f("expansion_break_distance_atr_min", cls.expansion_break_distance_atr_min), 0.0),
            expansion_long_break_distance_atr_max=max(_f("expansion_long_break_distance_atr_max", cls.expansion_long_break_distance_atr_max), 0.0),
            expansion_retest_band_atr_mult=max(_f("expansion_retest_band_atr_mult", cls.expansion_retest_band_atr_mult), 0.0),
            expansion_strong_immediate_body_ratio_min=min(max(_f("expansion_strong_immediate_body_ratio_min", cls.expansion_strong_immediate_body_ratio_min), 0.0), 1.0),
            expansion_strong_immediate_close_location_min=min(max(_f("expansion_strong_immediate_close_location_min", cls.expansion_strong_immediate_close_location_min), 0.0), 1.0),
            expansion_strong_immediate_quality_score_v2_min=min(max(_f("expansion_strong_immediate_quality_score_v2_min", cls.expansion_strong_immediate_quality_score_v2_min), 0.0), 1.0),
            expansion_strong_immediate_breakout_distance_atr_max=max(_f("expansion_strong_immediate_breakout_distance_atr_max", cls.expansion_strong_immediate_breakout_distance_atr_max), 0.0),
            expansion_strong_immediate_require_regime_alignment=_b("expansion_strong_immediate_require_regime_alignment", cls.expansion_strong_immediate_require_regime_alignment),
            expansion_strong_immediate_side_mode=(
                str(source.get("expansion_strong_immediate_side_mode", cls.expansion_strong_immediate_side_mode)).strip().upper()
                if str(source.get("expansion_strong_immediate_side_mode", cls.expansion_strong_immediate_side_mode)).strip().upper() in {"BOTH", "LONG", "SHORT"}
                else str(cls.expansion_strong_immediate_side_mode)
            ),
            expansion_short_confirm_break_distance_atr_min=max(_f("expansion_short_confirm_break_distance_atr_min", cls.expansion_short_confirm_break_distance_atr_min), 0.0),
            expansion_short_confirm_close_location_min=min(max(_f("expansion_short_confirm_close_location_min", cls.expansion_short_confirm_close_location_min), 0.0), 1.0),
            expansion_long_confirm_close_location_max=min(max(_f("expansion_long_confirm_close_location_max", cls.expansion_long_confirm_close_location_max), 0.0), 1.0),
            expansion_short_break_distance_atr_max=max(_f("expansion_short_break_distance_atr_max", cls.expansion_short_break_distance_atr_max), 0.0),
            expansion_breakout_efficiency_min=max(_f("expansion_breakout_efficiency_min", cls.expansion_breakout_efficiency_min), 0.0),
            expansion_breakout_stability_score_min=min(max(_f("expansion_breakout_stability_score_min", cls.expansion_breakout_stability_score_min), 0.0), 1.0),
            expansion_breakout_stability_edge_score_min=min(max(_f("expansion_breakout_stability_edge_score_min", cls.expansion_breakout_stability_edge_score_min), 0.0), 1.0),
            expansion_quality_score_min=min(max(_f("expansion_quality_score_min", cls.expansion_quality_score_min), 0.0), 1.0),
            expansion_quality_score_v2_min=min(max(_f("expansion_quality_score_v2_min", cls.expansion_quality_score_v2_min), 0.0), 1.0),
            min_volume_ratio_15m=max(_f("min_volume_ratio_15m", cls.min_volume_ratio_15m), 0.0),
            pullback_touch_atr_mult=max(_f("pullback_touch_atr_mult", cls.pullback_touch_atr_mult), 0.0),
            expansion_range_atr_min=max(_f("expansion_range_atr_min", cls.expansion_range_atr_min), 0.1),
            squeeze_percentile_threshold=min(max(_f("squeeze_percentile_threshold", cls.squeeze_percentile_threshold), 0.05), 0.95),
            squeeze_lookback_15m=_i("squeeze_lookback_15m", cls.squeeze_lookback_15m),
            drift_range_atr_min=max(_f("drift_range_atr_min", cls.drift_range_atr_min), 0.0),
            drift_body_ratio_min=min(max(_f("drift_body_ratio_min", cls.drift_body_ratio_min), 0.0), 1.0),
            drift_close_location_max=min(max(_f("drift_close_location_max", cls.drift_close_location_max), 0.0), 1.0),
            drift_long_width_expansion_min=max(_f("drift_long_width_expansion_min", cls.drift_long_width_expansion_min), 0.0),
            drift_long_edge_ratio_min=max(_f("drift_long_edge_ratio_min", cls.drift_long_edge_ratio_min), 0.0),
            drift_short_width_expansion_max=max(_f("drift_short_width_expansion_max", cls.drift_short_width_expansion_max), 0.0),
            drift_bias_rsi_long_min=max(_f("drift_bias_rsi_long_min", cls.drift_bias_rsi_long_min), 0.0),
            drift_bias_rsi_short_max=max(_f("drift_bias_rsi_short_max", cls.drift_bias_rsi_short_max), 0.0),
            drift_side_mode=(
                str(source.get("drift_side_mode", cls.drift_side_mode)).strip().upper()
                if str(source.get("drift_side_mode", cls.drift_side_mode)).strip().upper() in {"BOTH", "LONG", "SHORT"}
                else str(cls.drift_side_mode)
            ),
            drift_setup_expiry_bars=_i("drift_setup_expiry_bars", cls.drift_setup_expiry_bars),
            drift_take_profit_r=max(_f("drift_take_profit_r", cls.drift_take_profit_r), 0.5),
            drift_time_stop_bars=_i("drift_time_stop_bars", cls.drift_time_stop_bars),
            taker_fee=max(_f("taker_fee", cls.taker_fee), 0.0),
            slippage_bps=max(_f("slippage_bps", cls.slippage_bps), 0.0),
            max_spread_bps=max(_f("max_spread_bps", cls.max_spread_bps), 0.0),
            backtest_spread_bps_fallback=max(_f("backtest_spread_bps_fallback", cls.backtest_spread_bps_fallback), 0.0),
            min_expected_move_floor=max(_f("min_expected_move_floor", cls.min_expected_move_floor), 0.0),
            expected_move_cost_mult=max(_f("expected_move_cost_mult", cls.expected_move_cost_mult), 0.0),
            min_stop_distance_frac=max(_f("min_stop_distance_frac", cls.min_stop_distance_frac), 0.0),
            risk_per_trade_pct=max(_f("risk_per_trade_pct", cls.risk_per_trade_pct), 0.0),
            max_effective_leverage=max(_f("max_effective_leverage", cls.max_effective_leverage), 0.0),
            stop_atr_mult=max(_f("stop_atr_mult", cls.stop_atr_mult), 0.1),
            take_profit_r=max(_f("take_profit_r", cls.take_profit_r), 0.5),
            management_policy=_management_policy("management_policy", cls.management_policy),
            tp_partial_ratio=min(max(_f("tp_partial_ratio", cls.tp_partial_ratio), 0.0), 1.0),
            tp_partial_target_frac=max(_f("tp_partial_target_frac", cls.tp_partial_target_frac), 0.0),
            tp_partial_min_r=max(_f("tp_partial_min_r", cls.tp_partial_min_r), 0.0),
            tp_partial_max_r=max(_f("tp_partial_max_r", cls.tp_partial_max_r), 0.0),
            move_stop_to_be_at_r=max(_f("move_stop_to_be_at_r", cls.move_stop_to_be_at_r), 0.0),
            time_stop_bars=_i("time_stop_bars", cls.time_stop_bars),
            stop_exit_cooldown_bars=max(_i("stop_exit_cooldown_bars", cls.stop_exit_cooldown_bars), 0),
            profit_exit_cooldown_bars=max(_i("profit_exit_cooldown_bars", cls.profit_exit_cooldown_bars), 0),
            progress_check_bars=_i0("progress_check_bars", cls.progress_check_bars),
            progress_min_mfe_r=max(_f("progress_min_mfe_r", cls.progress_min_mfe_r), 0.0),
            progress_extend_trigger_r=max(_f("progress_extend_trigger_r", cls.progress_extend_trigger_r), 0.0),
            progress_extend_bars=_i0("progress_extend_bars", cls.progress_extend_bars),
            quality_exit_score_threshold=min(max(_f("quality_exit_score_threshold", cls.quality_exit_score_threshold), 0.0), 1.0),
            quality_exit_take_profit_r=max(_f("quality_exit_take_profit_r", cls.quality_exit_take_profit_r), 0.0),
            quality_exit_time_stop_bars=_i0("quality_exit_time_stop_bars", cls.quality_exit_time_stop_bars),
            selective_extension_proof_bars=_i0("selective_extension_proof_bars", cls.selective_extension_proof_bars),
            selective_extension_min_mfe_r=max(_f("selective_extension_min_mfe_r", cls.selective_extension_min_mfe_r), 0.0),
            selective_extension_min_regime_strength=min(max(_f("selective_extension_min_regime_strength", cls.selective_extension_min_regime_strength), 0.0), 1.0),
            selective_extension_min_bias_strength=min(max(_f("selective_extension_min_bias_strength", cls.selective_extension_min_bias_strength), 0.0), 1.0),
            selective_extension_min_quality_score_v2=min(max(_f("selective_extension_min_quality_score_v2", cls.selective_extension_min_quality_score_v2), 0.0), 1.0),
            selective_extension_time_stop_bars=_i0("selective_extension_time_stop_bars", cls.selective_extension_time_stop_bars),
            selective_extension_take_profit_r=max(_f("selective_extension_take_profit_r", cls.selective_extension_take_profit_r), 0.0),
            selective_extension_move_stop_to_be_at_r=max(_f("selective_extension_move_stop_to_be_at_r", cls.selective_extension_move_stop_to_be_at_r), 0.0),
        )
