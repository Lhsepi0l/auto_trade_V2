from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal

from v2.clean_room.contracts import Candidate, CandidateSelector, KernelContext
from v2.strategies.alpha_shared import (
    _Bar,
    _clamp_score,
    _percentile,
    _sma,
    _spread_bps_from_bar,
    _to_bars,
    _to_float,
    adx,
    atr,
    bollinger_bandwidth,
    donchian,
    ema,
    expected_move_gate,
    rsi,
)
from v2.strategies.base import DesiredPosition, StrategyPlugin

AlphaId = Literal["alpha_breakout", "alpha_pullback", "alpha_expansion"]
RegimeName = Literal["TREND_UP", "TREND_DOWN", "UNKNOWN"]
AllowedSideName = Literal["LONG", "SHORT", "NONE"]

SUPPORTED_SYMBOLS = ("BTCUSDT",)
DEFAULT_ALPHA_IDS: tuple[AlphaId, ...] = (
    "alpha_breakout",
    "alpha_pullback",
    "alpha_expansion",
)
ALPHA_ENTRY_FAMILY: dict[AlphaId, str] = {
    "alpha_breakout": "breakout",
    "alpha_pullback": "pullback",
    "alpha_expansion": "expansion",
}
BLOCK_REASON_PRIORITY = {
    "regime_missing": 0,
    "regime_adx_window_missing": 0,
    "regime_adx_rising_missing": 0,
    "bias_missing": 1,
    "trigger_missing": 2,
    "short_overextension_risk": 2,
    "quality_score_missing": 2,
    "quality_score_v2_missing": 2,
    "breakout_efficiency_missing": 2,
    "breakout_stability_missing": 2,
    "breakout_stability_edge_missing": 2,
    "volume_missing": 3,
    "cost_missing": 4,
}


@dataclass(frozen=True)
class _AlphaEvaluation:
    alpha_id: AlphaId
    reason: str
    score: float = 0.0
    payload: dict[str, Any] | None = None
    diagnostics: dict[str, Any] | None = None


@dataclass(frozen=True)
class _SharedContext:
    symbol: str
    candles_4h: list[_Bar]
    candles_1h: list[_Bar]
    candles_15m: list[_Bar]
    regime: RegimeName
    regime_side: AllowedSideName
    regime_block_reason: str
    regime_strength: float
    bias_side: AllowedSideName
    bias_strength: float
    atr_15m: float
    ema_15m: float
    vol_ratio_15m: float
    current_bar: _Bar
    prev_bar: _Bar
    spread_estimate_bps: float
    expected_move_frac: float
    required_move_frac: float
    indicators: dict[str, float]


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
            allowed = set(DEFAULT_ALPHA_IDS)
            deduped: list[AlphaId] = []
            for token in tokens:
                if token in allowed and token not in deduped:
                    deduped.append(token)  # type: ignore[arg-type]
            return tuple(deduped) if deduped else tuple(default)

        return cls(
            supported_symbols=_symbols("supported_symbols", cls.supported_symbols),
            enabled_alphas=_alphas("enabled_alphas", cls.enabled_alphas),
            ema_fast_4h=_i("ema_fast_4h", cls.ema_fast_4h),
            ema_slow_4h=_i("ema_slow_4h", cls.ema_slow_4h),
            adx_period_4h=_i("adx_period_4h", cls.adx_period_4h),
            trend_adx_min_4h=max(_f("trend_adx_min_4h", cls.trend_adx_min_4h), 0.0),
            trend_adx_max_4h=max(_f("trend_adx_max_4h", cls.trend_adx_max_4h), 0.0),
            trend_adx_rising_lookback_4h=max(
                _i("trend_adx_rising_lookback_4h", cls.trend_adx_rising_lookback_4h),
                0,
            ),
            trend_adx_rising_min_delta_4h=max(
                _f("trend_adx_rising_min_delta_4h", cls.trend_adx_rising_min_delta_4h),
                0.0,
            ),
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
            expansion_body_ratio_min=min(
                max(_f("expansion_body_ratio_min", cls.expansion_body_ratio_min), 0.0),
                1.0,
            ),
            expansion_close_location_min=min(
                max(_f("expansion_close_location_min", cls.expansion_close_location_min), 0.0),
                1.0,
            ),
            expansion_width_expansion_min=max(
                _f("expansion_width_expansion_min", cls.expansion_width_expansion_min),
                0.0,
            ),
            expansion_break_distance_atr_min=max(
                _f("expansion_break_distance_atr_min", cls.expansion_break_distance_atr_min),
                0.0,
            ),
            expansion_short_break_distance_atr_max=max(
                _f(
                    "expansion_short_break_distance_atr_max",
                    cls.expansion_short_break_distance_atr_max,
                ),
                0.0,
            ),
            expansion_breakout_efficiency_min=max(
                _f(
                    "expansion_breakout_efficiency_min",
                    cls.expansion_breakout_efficiency_min,
                ),
                0.0,
            ),
            expansion_breakout_stability_score_min=min(
                max(
                    _f(
                        "expansion_breakout_stability_score_min",
                        cls.expansion_breakout_stability_score_min,
                    ),
                    0.0,
                ),
                1.0,
            ),
            expansion_breakout_stability_edge_score_min=min(
                max(
                    _f(
                        "expansion_breakout_stability_edge_score_min",
                        cls.expansion_breakout_stability_edge_score_min,
                    ),
                    0.0,
                ),
                1.0,
            ),
            expansion_quality_score_min=min(
                max(_f("expansion_quality_score_min", cls.expansion_quality_score_min), 0.0),
                1.0,
            ),
            expansion_quality_score_v2_min=min(
                max(_f("expansion_quality_score_v2_min", cls.expansion_quality_score_v2_min), 0.0),
                1.0,
            ),
            min_volume_ratio_15m=max(_f("min_volume_ratio_15m", cls.min_volume_ratio_15m), 0.0),
            pullback_touch_atr_mult=max(_f("pullback_touch_atr_mult", cls.pullback_touch_atr_mult), 0.0),
            expansion_range_atr_min=max(_f("expansion_range_atr_min", cls.expansion_range_atr_min), 0.1),
            squeeze_percentile_threshold=min(
                max(_f("squeeze_percentile_threshold", cls.squeeze_percentile_threshold), 0.05),
                0.95,
            ),
            squeeze_lookback_15m=_i("squeeze_lookback_15m", cls.squeeze_lookback_15m),
            taker_fee=max(_f("taker_fee", cls.taker_fee), 0.0),
            slippage_bps=max(_f("slippage_bps", cls.slippage_bps), 0.0),
            max_spread_bps=max(_f("max_spread_bps", cls.max_spread_bps), 0.0),
            backtest_spread_bps_fallback=max(
                _f("backtest_spread_bps_fallback", cls.backtest_spread_bps_fallback),
                0.0,
            ),
            min_expected_move_floor=max(
                _f("min_expected_move_floor", cls.min_expected_move_floor),
                0.0,
            ),
            expected_move_cost_mult=max(
                _f("expected_move_cost_mult", cls.expected_move_cost_mult),
                0.0,
            ),
            min_stop_distance_frac=max(
                _f("min_stop_distance_frac", cls.min_stop_distance_frac),
                0.0,
            ),
            risk_per_trade_pct=max(_f("risk_per_trade_pct", cls.risk_per_trade_pct), 0.0),
            max_effective_leverage=max(
                _f("max_effective_leverage", cls.max_effective_leverage),
                0.0,
            ),
            stop_atr_mult=max(_f("stop_atr_mult", cls.stop_atr_mult), 0.1),
            take_profit_r=max(_f("take_profit_r", cls.take_profit_r), 0.5),
            time_stop_bars=_i("time_stop_bars", cls.time_stop_bars),
            stop_exit_cooldown_bars=max(
                _i("stop_exit_cooldown_bars", cls.stop_exit_cooldown_bars),
                0,
            ),
            profit_exit_cooldown_bars=max(
                _i("profit_exit_cooldown_bars", cls.profit_exit_cooldown_bars),
                0,
            ),
            progress_check_bars=_i0("progress_check_bars", cls.progress_check_bars),
            progress_min_mfe_r=max(_f("progress_min_mfe_r", cls.progress_min_mfe_r), 0.0),
            progress_extend_trigger_r=max(
                _f("progress_extend_trigger_r", cls.progress_extend_trigger_r),
                0.0,
            ),
            progress_extend_bars=_i0("progress_extend_bars", cls.progress_extend_bars),
            quality_exit_score_threshold=min(
                max(_f("quality_exit_score_threshold", cls.quality_exit_score_threshold), 0.0),
                1.0,
            ),
            quality_exit_take_profit_r=max(
                _f("quality_exit_take_profit_r", cls.quality_exit_take_profit_r),
                0.0,
            ),
            quality_exit_time_stop_bars=_i0(
                "quality_exit_time_stop_bars",
                cls.quality_exit_time_stop_bars,
            ),
            selective_extension_proof_bars=_i0(
                "selective_extension_proof_bars",
                cls.selective_extension_proof_bars,
            ),
            selective_extension_min_mfe_r=max(
                _f("selective_extension_min_mfe_r", cls.selective_extension_min_mfe_r),
                0.0,
            ),
            selective_extension_min_regime_strength=min(
                max(
                    _f(
                        "selective_extension_min_regime_strength",
                        cls.selective_extension_min_regime_strength,
                    ),
                    0.0,
                ),
                1.0,
            ),
            selective_extension_min_bias_strength=min(
                max(
                    _f(
                        "selective_extension_min_bias_strength",
                        cls.selective_extension_min_bias_strength,
                    ),
                    0.0,
                ),
                1.0,
            ),
            selective_extension_min_quality_score_v2=min(
                max(
                    _f(
                        "selective_extension_min_quality_score_v2",
                        cls.selective_extension_min_quality_score_v2,
                    ),
                    0.0,
                ),
                1.0,
            ),
            selective_extension_time_stop_bars=_i0(
                "selective_extension_time_stop_bars",
                cls.selective_extension_time_stop_bars,
            ),
            selective_extension_take_profit_r=max(
                _f("selective_extension_take_profit_r", cls.selective_extension_take_profit_r),
                0.0,
            ),
            selective_extension_move_stop_to_be_at_r=max(
                _f(
                    "selective_extension_move_stop_to_be_at_r",
                    cls.selective_extension_move_stop_to_be_at_r,
                ),
                0.0,
            ),
        )


def _decision_none(
    *,
    symbol: str,
    reason: str,
    regime: RegimeName,
    allowed_side: AllowedSideName,
    indicators: dict[str, float],
    alpha_diagnostics: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = dict(
        DesiredPosition(
            symbol=symbol,
            intent="NONE",
            side="NONE",
            score=0.0,
            reason=reason,
            allowed_side=allowed_side,
            signals={},
            blocks=[reason],
            indicators=indicators,
        ).to_dict()
    )
    payload["regime"] = regime
    payload["allowed_side"] = allowed_side
    if alpha_diagnostics:
        payload["alpha_diagnostics"] = copy.deepcopy(alpha_diagnostics)
        payload["alpha_blocks"] = {
            str(alpha_id): str(meta.get("reason") or "").strip()
            for alpha_id, meta in alpha_diagnostics.items()
            if str(meta.get("state") or "").strip() != "candidate"
            and str(meta.get("reason") or "").strip()
        }
        payload["alpha_reject_metrics"] = {
            str(alpha_id): copy.deepcopy(meta.get("metrics") or {})
            for alpha_id, meta in alpha_diagnostics.items()
            if str(meta.get("state") or "").strip() != "candidate"
            and isinstance(meta.get("metrics"), dict)
            and meta.get("metrics")
        }
    return payload


def _aggregate_block_reason(reasons: list[str]) -> str:
    if not reasons:
        return "trigger_missing"
    ordered = sorted(
        reasons,
        key=lambda item: (BLOCK_REASON_PRIORITY.get(str(item), 99), str(item)),
    )
    return str(ordered[0])


def _deficit_penalty(value: float, target: float) -> float:
    if float(target) <= 0.0:
        return 0.0
    return max(float(target) - float(value), 0.0) / max(float(target), 1e-9)


def _ceiling_penalty(value: float, ceiling: float) -> float:
    if float(ceiling) <= 0.0:
        return 0.0
    return max(float(value) - float(ceiling), 0.0) / max(float(ceiling), 1e-9)


def _expansion_quality_score(
    *,
    body_ratio: float,
    favored_close: float,
    width_expansion_frac: float,
    expected_move_frac: float,
    required_move_frac: float,
    cfg: RA2026AlphaV2Params,
) -> float:
    edge_ratio = float(expected_move_frac) / max(float(required_move_frac), 1e-9)
    deficits = (
        _deficit_penalty(float(body_ratio), 0.33),
        _deficit_penalty(float(favored_close), 0.55),
        _deficit_penalty(
            float(width_expansion_frac),
            max(float(cfg.expansion_width_expansion_min), 0.08),
        ),
        _deficit_penalty(float(edge_ratio), 1.20),
    )
    return _clamp_score(1.0 - (sum(deficits) / len(deficits)))


def _breakout_structure_penalties(
    *,
    overhang_frac: float,
    rejection_wick_frac: float,
    width_expansion_frac: float,
    expected_move_frac: float,
    required_move_frac: float,
    cfg: RA2026AlphaV2Params,
) -> tuple[float, float, float, float]:
    edge_ratio = float(expected_move_frac) / max(float(required_move_frac), 1e-9)
    overhang_penalty = _deficit_penalty(float(overhang_frac), 0.10)
    rejection_penalty = _ceiling_penalty(float(rejection_wick_frac), 0.28)
    edge_penalty = _deficit_penalty(float(edge_ratio), 1.35)
    width_penalty = _deficit_penalty(
        float(width_expansion_frac),
        max(float(cfg.expansion_width_expansion_min), 0.08),
    )
    return (
        float(overhang_penalty),
        float(rejection_penalty),
        float(edge_penalty),
        float(width_penalty),
    )


def _breakout_stability_score(
    *,
    overhang_frac: float,
    rejection_wick_frac: float,
    width_expansion_frac: float,
    expected_move_frac: float,
    required_move_frac: float,
    cfg: RA2026AlphaV2Params,
) -> float:
    overhang_penalty, rejection_penalty, _, _ = _breakout_structure_penalties(
        overhang_frac=float(overhang_frac),
        rejection_wick_frac=float(rejection_wick_frac),
        width_expansion_frac=float(width_expansion_frac),
        expected_move_frac=float(expected_move_frac),
        required_move_frac=float(required_move_frac),
        cfg=cfg,
    )
    return _clamp_score(1.0 - ((float(overhang_penalty) + float(rejection_penalty)) / 2.0))


def _breakout_stability_edge_score(
    *,
    overhang_frac: float,
    rejection_wick_frac: float,
    width_expansion_frac: float,
    expected_move_frac: float,
    required_move_frac: float,
    cfg: RA2026AlphaV2Params,
) -> float:
    overhang_penalty, rejection_penalty, edge_penalty, _ = _breakout_structure_penalties(
        overhang_frac=float(overhang_frac),
        rejection_wick_frac=float(rejection_wick_frac),
        width_expansion_frac=float(width_expansion_frac),
        expected_move_frac=float(expected_move_frac),
        required_move_frac=float(required_move_frac),
        cfg=cfg,
    )
    structure_penalty = (float(overhang_penalty) + float(rejection_penalty)) / 2.0
    interaction_penalty = min(float(structure_penalty) * (1.0 + float(edge_penalty)), 1.0)
    return _clamp_score(1.0 - float(interaction_penalty))


def _expansion_quality_score_v2(
    *,
    overhang_frac: float,
    rejection_wick_frac: float,
    width_expansion_frac: float,
    expected_move_frac: float,
    required_move_frac: float,
    cfg: RA2026AlphaV2Params,
) -> float:
    overhang_penalty, rejection_penalty, edge_penalty, width_penalty = (
        _breakout_structure_penalties(
            overhang_frac=float(overhang_frac),
            rejection_wick_frac=float(rejection_wick_frac),
            width_expansion_frac=float(width_expansion_frac),
            expected_move_frac=float(expected_move_frac),
            required_move_frac=float(required_move_frac),
            cfg=cfg,
        )
    )
    structure_penalty = (float(overhang_penalty) + float(rejection_penalty)) / 2.0
    width_edge_penalty = min(float(width_penalty) * (1.0 + float(edge_penalty)), 1.0)
    return _clamp_score(1.0 - max(float(structure_penalty), float(width_edge_penalty)))


def _bias_from_1h(
    *,
    candles_1h: list[_Bar],
    cfg: RA2026AlphaV2Params,
) -> tuple[AllowedSideName, float, dict[str, float]]:
    closes = [float(bar.close) for bar in candles_1h]
    ema_now = ema(closes, cfg.ema_bias_period_1h)
    ema_prev = ema(closes[:-1], cfg.ema_bias_period_1h) if len(closes) > cfg.ema_bias_period_1h else None
    rsi_now = rsi(closes, cfg.rsi_period_1h)
    indicators = {
        "ema_bias_1h": float(ema_now or 0.0),
        "ema_bias_prev_1h": float(ema_prev or 0.0),
        "rsi_1h": float(rsi_now or 0.0),
        "close_1h": float(closes[-1] if closes else 0.0),
    }
    if ema_now is None or ema_prev is None or rsi_now is None or not closes:
        return "NONE", 0.0, indicators

    close_now = float(closes[-1])
    distance_frac = abs(close_now - float(ema_now)) / max(close_now, 1e-9)
    slope_up = float(ema_now) >= float(ema_prev)
    slope_down = float(ema_now) <= float(ema_prev)
    if (
        close_now >= float(ema_now)
        and slope_up
        and float(rsi_now) >= cfg.bias_rsi_long_min_1h
        and distance_frac >= 0.0004
    ):
        strength = _clamp_score(
            ((close_now - float(ema_now)) / max(close_now, 1e-9)) * 120.0
            + max(float(rsi_now) - 50.0, 0.0) / 20.0
        )
        return "LONG", strength, indicators
    if (
        close_now <= float(ema_now)
        and slope_down
        and float(rsi_now) <= cfg.bias_rsi_short_max_1h
        and distance_frac >= 0.0004
    ):
        strength = _clamp_score(
            ((float(ema_now) - close_now) / max(close_now, 1e-9)) * 120.0
            + max(50.0 - float(rsi_now), 0.0) / 20.0
        )
        return "SHORT", strength, indicators
    return "NONE", 0.0, indicators


def _regime_from_4h(
    *,
    candles_4h: list[_Bar],
    cfg: RA2026AlphaV2Params,
) -> tuple[RegimeName, AllowedSideName, str, float, dict[str, float]]:
    closes = [float(bar.close) for bar in candles_4h]
    ema_fast = ema(closes, cfg.ema_fast_4h)
    ema_slow = ema(closes, cfg.ema_slow_4h)
    adx_now = adx(candles_4h, cfg.adx_period_4h)
    adx_prev = None
    lookback = max(int(cfg.trend_adx_rising_lookback_4h), 0)
    if lookback > 0 and len(candles_4h) > lookback + int(cfg.adx_period_4h) + 2:
        adx_prev = adx(candles_4h[:-lookback], cfg.adx_period_4h)
    indicators = {
        "ema_fast_4h": float(ema_fast or 0.0),
        "ema_slow_4h": float(ema_slow or 0.0),
        "adx_4h": float(adx_now or 0.0),
        "adx_prev_4h": float(adx_prev or 0.0),
        "adx_delta_4h": float((adx_now or 0.0) - (adx_prev or 0.0)),
        "close_4h": float(closes[-1] if closes else 0.0),
    }
    if ema_fast is None or ema_slow is None or adx_now is None or not closes:
        return "UNKNOWN", "NONE", "regime_missing", 0.0, indicators

    close_now = float(closes[-1])
    ema_gap = abs(float(ema_fast) - float(ema_slow)) / max(close_now, 1e-9)
    adx_score = max(float(adx_now) - float(cfg.trend_adx_min_4h), 0.0) / 15.0
    strength = _clamp_score((ema_gap * 150.0) + adx_score)
    if float(cfg.trend_adx_max_4h) > 0.0 and float(adx_now) > float(cfg.trend_adx_max_4h):
        return "UNKNOWN", "NONE", "regime_adx_window_missing", 0.0, indicators
    if lookback > 0:
        if adx_prev is None:
            return "UNKNOWN", "NONE", "regime_adx_rising_missing", 0.0, indicators
        if float(adx_now) < float(adx_prev) + float(cfg.trend_adx_rising_min_delta_4h):
            return "UNKNOWN", "NONE", "regime_adx_rising_missing", 0.0, indicators
    if (
        float(adx_now) >= float(cfg.trend_adx_min_4h)
        and float(ema_fast) > float(ema_slow)
        and close_now >= float(ema_fast)
    ):
        return "TREND_UP", "LONG", "", strength, indicators
    if (
        float(adx_now) >= float(cfg.trend_adx_min_4h)
        and float(ema_fast) < float(ema_slow)
        and close_now <= float(ema_fast)
    ):
        return "TREND_DOWN", "SHORT", "", strength, indicators
    return "UNKNOWN", "NONE", "regime_missing", 0.0, indicators


def _build_shared_context(
    *,
    symbol: str,
    market: dict[str, Any],
    cfg: RA2026AlphaV2Params,
) -> tuple[_SharedContext | None, str]:
    candles_4h = _to_bars(market.get("4h"))
    candles_1h = _to_bars(market.get("1h"))
    candles_15m = _to_bars(market.get("15m"))
    min_4h = max(cfg.ema_slow_4h + 5, cfg.adx_period_4h + 5)
    min_1h = max(cfg.ema_bias_period_1h + 5, cfg.rsi_period_1h + 5)
    min_15m = max(
        cfg.atr_period_15m + 5,
        cfg.ema_pullback_period_15m + 5,
        cfg.donchian_period_15m + 2,
        cfg.bb_period_15m + 5,
        cfg.volume_sma_period_15m + 2,
        cfg.swing_lookback_15m + 2,
        cfg.squeeze_lookback_15m + cfg.bb_period_15m + 2,
    )

    if len(candles_4h) < min_4h:
        return None, "insufficient_4h_data"
    if len(candles_1h) < min_1h:
        return None, "insufficient_1h_data"
    if len(candles_15m) < min_15m:
        return None, "insufficient_15m_data"

    regime, regime_side, regime_block_reason, regime_strength, regime_indicators = _regime_from_4h(
        candles_4h=candles_4h,
        cfg=cfg,
    )
    bias_side, bias_strength, bias_indicators = _bias_from_1h(candles_1h=candles_1h, cfg=cfg)
    atr_15m = atr(candles_15m, cfg.atr_period_15m)
    ema_15m = ema([float(bar.close) for bar in candles_15m], cfg.ema_pullback_period_15m)
    if atr_15m is None or ema_15m is None:
        return None, "insufficient_15m_data"

    current_bar = candles_15m[-1]
    prev_bar = candles_15m[-2]
    volumes = [float(bar.volume or 0.0) for bar in candles_15m]
    volume_sma = _sma(volumes[:-1], cfg.volume_sma_period_15m)
    if volume_sma is None or volume_sma <= 0.0:
        return None, "insufficient_15m_data"
    vol_ratio = float(current_bar.volume or 0.0) / float(volume_sma)
    spread_estimate_bps = _spread_bps_from_bar(
        current_bar,
        fallback_bps=cfg.backtest_spread_bps_fallback,
    )
    expected_ok, expected_move_frac, required_move_frac = expected_move_gate(
        atr_15m=float(atr_15m),
        close_15m=float(current_bar.close),
        taker_fee=float(cfg.taker_fee),
        slippage_bps=float(cfg.slippage_bps),
        spread_estimate_bps=float(spread_estimate_bps),
        spread_limit_bps=float(cfg.max_spread_bps),
        min_expected_move_floor=float(cfg.min_expected_move_floor),
        expected_move_cost_mult=float(cfg.expected_move_cost_mult),
    )
    range_expansion_frac = max(
        (float(current_bar.high) - float(current_bar.low)) / max(float(current_bar.close), 1e-9),
        0.0,
    )
    effective_expected_move_frac = max(float(expected_move_frac), float(range_expansion_frac) * 0.6)
    indicators = {
        **regime_indicators,
        **bias_indicators,
        "atr14_15m": float(atr_15m),
        "ema_pullback_15m": float(ema_15m),
        "volume_ratio_15m": float(vol_ratio),
        "spread_estimate_bps": float(spread_estimate_bps),
        "expected_move_frac": float(effective_expected_move_frac),
        "required_move_frac": float(required_move_frac),
        "expected_move_gate": 1.0 if expected_ok else 0.0,
        "range_expansion_frac": float(range_expansion_frac),
    }
    return (
        _SharedContext(
            symbol=symbol,
            candles_4h=candles_4h,
            candles_1h=candles_1h,
            candles_15m=candles_15m,
            regime=regime,
            regime_side=regime_side,
            regime_block_reason=regime_block_reason,
            regime_strength=regime_strength,
            bias_side=bias_side,
            bias_strength=bias_strength,
            atr_15m=float(atr_15m),
            ema_15m=float(ema_15m),
            vol_ratio_15m=float(vol_ratio),
            current_bar=current_bar,
            prev_bar=prev_bar,
            spread_estimate_bps=float(spread_estimate_bps),
            expected_move_frac=float(effective_expected_move_frac),
            required_move_frac=float(required_move_frac),
            indicators=indicators,
        ),
        "",
    )


def _common_stop(
    *,
    side: AllowedSideName,
    entry_price: float,
    ctx: _SharedContext,
    cfg: RA2026AlphaV2Params,
) -> tuple[float | None, float]:
    recent = ctx.candles_15m[-(cfg.swing_lookback_15m + 1) : -1]
    if not recent:
        return None, 0.0
    if side == "LONG":
        swing_stop = min(float(bar.low) for bar in recent)
        atr_stop = float(entry_price) - (float(ctx.atr_15m) * float(cfg.stop_atr_mult))
        stop_price = min(swing_stop, atr_stop)
        stop_distance = max(float(entry_price) - float(stop_price), 0.0)
    else:
        swing_stop = max(float(bar.high) for bar in recent)
        atr_stop = float(entry_price) + (float(ctx.atr_15m) * float(cfg.stop_atr_mult))
        stop_price = max(swing_stop, atr_stop)
        stop_distance = max(float(stop_price) - float(entry_price), 0.0)
    if stop_price <= 0.0 or stop_distance <= 0.0:
        return None, 0.0
    return float(stop_price), float(stop_distance) / max(float(entry_price), 1e-9)


def _common_cost_reason(
    *,
    stop_distance_frac: float,
    ctx: _SharedContext,
    cfg: RA2026AlphaV2Params,
) -> str | None:
    if float(ctx.spread_estimate_bps) > float(cfg.max_spread_bps):
        return "cost_missing"
    if float(stop_distance_frac) < float(cfg.min_stop_distance_frac):
        return "cost_missing"
    if float(ctx.expected_move_frac) <= 0.0 or float(ctx.required_move_frac) <= 0.0:
        return "cost_missing"
    if float(ctx.expected_move_frac) < float(ctx.required_move_frac):
        return "cost_missing"
    return None


def _build_entry_payload(
    *,
    alpha_id: AlphaId,
    side: AllowedSideName,
    entry_price: float,
    stop_price: float,
    stop_distance_frac: float,
    score: float,
    ctx: _SharedContext,
    cfg: RA2026AlphaV2Params,
    alpha_diagnostics: dict[str, dict[str, Any]],
    entry_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = entry_meta or {}
    quality_score_v2 = max(float(_to_float(meta.get("quality_score_v2")) or 0.0), 0.0)
    effective_take_profit_r = float(cfg.take_profit_r)
    effective_time_stop_bars = int(cfg.time_stop_bars)
    quality_exit_applied = False
    if (
        float(cfg.quality_exit_score_threshold) > 0.0
        and float(quality_score_v2) >= float(cfg.quality_exit_score_threshold)
    ):
        if float(cfg.quality_exit_take_profit_r) > 0.0:
            effective_take_profit_r = max(float(cfg.quality_exit_take_profit_r), 0.5)
        if int(cfg.quality_exit_time_stop_bars) > 0:
            effective_time_stop_bars = int(cfg.quality_exit_time_stop_bars)
        quality_exit_applied = True
    take_profit = (
        float(entry_price)
        + (float(stop_distance_frac) * float(entry_price) * float(effective_take_profit_r))
        if side == "LONG"
        else float(entry_price)
        - (float(stop_distance_frac) * float(entry_price) * float(effective_take_profit_r))
    )
    payload = dict(
        DesiredPosition(
            symbol=ctx.symbol,
            intent="LONG" if side == "LONG" else "SHORT",
            side="BUY" if side == "LONG" else "SELL",
            score=float(score),
            reason="entry_signal",
            entry_price=float(entry_price),
            stop_hint=float(stop_price),
            management_hint="single_exit_2r",
            regime=ctx.regime,
            allowed_side=ctx.regime_side if alpha_id != "alpha_expansion" else ctx.bias_side,
            signals={str(alpha_id): True},
            blocks=[],
            indicators=dict(ctx.indicators),
        ).to_dict()
    )
    payload["alpha_id"] = alpha_id
    payload["entry_family"] = ALPHA_ENTRY_FAMILY[alpha_id]
    payload["stop_price_hint"] = float(stop_price)
    payload["stop_distance_frac"] = float(stop_distance_frac)
    payload["risk_per_trade_pct"] = float(cfg.risk_per_trade_pct)
    if float(cfg.max_effective_leverage) > 0.0:
        payload["max_effective_leverage"] = float(cfg.max_effective_leverage)
    payload["expected_move_frac"] = float(ctx.expected_move_frac)
    payload["required_move_frac"] = float(ctx.required_move_frac)
    payload["regime_strength"] = float(ctx.regime_strength)
    payload["bias_strength"] = float(ctx.bias_strength)
    payload["entry_quality_score_v2"] = float(quality_score_v2)
    payload["alpha_diagnostics"] = copy.deepcopy(alpha_diagnostics)
    payload["alpha_blocks"] = {
        str(alpha_id_key): str(meta.get("reason") or "").strip()
        for alpha_id_key, meta in alpha_diagnostics.items()
        if str(meta.get("state") or "").strip() != "candidate"
        and str(meta.get("reason") or "").strip()
    }
    payload["alpha_reject_metrics"] = {
        str(alpha_id_key): copy.deepcopy(meta.get("metrics") or {})
        for alpha_id_key, meta in alpha_diagnostics.items()
        if str(alpha_id_key) != str(alpha_id)
        and str(meta.get("state") or "").strip() != "candidate"
        and isinstance(meta.get("metrics"), dict)
        and meta.get("metrics")
    }
    payload["sl_tp"] = {
        "take_profit": float(take_profit),
        "stop_loss": float(stop_price),
    }
    payload["execution"] = {
        "time_stop_bars": int(effective_time_stop_bars),
        "stop_exit_cooldown_bars": int(cfg.stop_exit_cooldown_bars),
        "profit_exit_cooldown_bars": int(cfg.profit_exit_cooldown_bars),
        "progress_check_bars": int(cfg.progress_check_bars),
        "progress_min_mfe_r": float(cfg.progress_min_mfe_r),
        "progress_extend_trigger_r": float(cfg.progress_extend_trigger_r),
        "progress_extend_bars": int(cfg.progress_extend_bars),
        "loss_streak_trigger": 0,
        "loss_streak_cooldown_bars": 0,
        "reward_risk_reference_r": float(effective_take_profit_r),
        "allow_reverse_exit": False,
        "reduce_only_exit": True,
        "entry_quality_score_v2": float(quality_score_v2),
        "quality_exit_applied": bool(quality_exit_applied),
        "entry_regime_strength": float(ctx.regime_strength),
        "entry_bias_strength": float(ctx.bias_strength),
        "selective_extension_proof_bars": int(cfg.selective_extension_proof_bars),
        "selective_extension_min_mfe_r": float(cfg.selective_extension_min_mfe_r),
        "selective_extension_min_regime_strength": float(
            cfg.selective_extension_min_regime_strength
        ),
        "selective_extension_min_bias_strength": float(cfg.selective_extension_min_bias_strength),
        "selective_extension_min_quality_score_v2": float(
            cfg.selective_extension_min_quality_score_v2
        ),
        "selective_extension_time_stop_bars": int(cfg.selective_extension_time_stop_bars),
        "selective_extension_take_profit_r": float(cfg.selective_extension_take_profit_r),
        "selective_extension_move_stop_to_be_at_r": float(
            cfg.selective_extension_move_stop_to_be_at_r
        ),
    }
    return payload


class RA2026AlphaV2(StrategyPlugin):
    name = "ra_2026_alpha_v2"

    def __init__(self, *, params: dict[str, Any] | None = None, logger: Any | None = None) -> None:
        self._cfg = RA2026AlphaV2Params.from_params(params)
        self._logger = logger

    def set_runtime_params(self, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
        merged = self._cfg.__dict__.copy()
        merged.update(kwargs)
        self._cfg = RA2026AlphaV2Params.from_params(merged)

    def _alpha_breakout(self, *, ctx: _SharedContext) -> _AlphaEvaluation:
        cfg = self._cfg
        if ctx.regime_side == "NONE" or ctx.regime == "UNKNOWN":
            return _AlphaEvaluation(
                alpha_id="alpha_breakout",
                reason=str(ctx.regime_block_reason or "regime_missing"),
            )
        if ctx.bias_side != ctx.regime_side:
            return _AlphaEvaluation(alpha_id="alpha_breakout", reason="bias_missing")
        if ctx.vol_ratio_15m < float(cfg.min_volume_ratio_15m):
            return _AlphaEvaluation(
                alpha_id="alpha_breakout",
                reason="volume_missing",
                diagnostics={
                    "vol_ratio_15m": float(ctx.vol_ratio_15m),
                    "min_volume_ratio_15m": float(cfg.min_volume_ratio_15m),
                },
            )

        previous_channel = donchian(ctx.candles_15m[:-1], cfg.donchian_period_15m)
        if previous_channel is None:
            return _AlphaEvaluation(alpha_id="alpha_breakout", reason="trigger_missing")
        upper, lower = previous_channel
        buffer = float(cfg.breakout_buffer_bps) / 10000.0
        long_breakout_level = float(upper) * (1.0 + buffer)
        short_breakout_level = float(lower) * (1.0 - buffer)
        long_trigger = (
            ctx.regime_side == "LONG"
            and float(ctx.current_bar.close) > float(long_breakout_level)
        )
        short_trigger = (
            ctx.regime_side == "SHORT"
            and float(ctx.current_bar.close) < float(short_breakout_level)
        )
        if not long_trigger and not short_trigger:
            return _AlphaEvaluation(
                alpha_id="alpha_breakout",
                reason="trigger_missing",
                diagnostics={
                    "close": float(ctx.current_bar.close),
                    "breakout_level_long": float(long_breakout_level),
                    "breakout_level_short": float(short_breakout_level),
                    "regime_side": ctx.regime_side,
                },
            )

        side: AllowedSideName = "LONG" if long_trigger else "SHORT"
        entry_price = float(ctx.current_bar.close)
        stop_price, stop_distance_frac = _common_stop(
            side=side,
            entry_price=entry_price,
            ctx=ctx,
            cfg=cfg,
        )
        if stop_price is None:
            return _AlphaEvaluation(alpha_id="alpha_breakout", reason="cost_missing")
        cost_reason = _common_cost_reason(
            stop_distance_frac=stop_distance_frac,
            ctx=ctx,
            cfg=cfg,
        )
        if cost_reason is not None:
            return _AlphaEvaluation(alpha_id="alpha_breakout", reason=cost_reason)

        breakout_distance = (
            (float(ctx.current_bar.close) - float(upper)) / max(entry_price, 1e-9)
            if side == "LONG"
            else (float(lower) - float(ctx.current_bar.close)) / max(entry_price, 1e-9)
        )
        score = _clamp_score(
            0.52
            + (float(ctx.regime_strength) * 0.16)
            + (float(ctx.bias_strength) * 0.14)
            + min(max(float(ctx.vol_ratio_15m) - 1.0, 0.0), 1.0) * 0.10
            + min(max(float(breakout_distance) * 200.0, 0.0), 1.0) * 0.08
        )
        return _AlphaEvaluation(
            alpha_id="alpha_breakout",
            reason="entry_signal",
            score=float(score),
            payload={
                "side": side,
                "entry_price": entry_price,
                "stop_price": float(stop_price),
                "stop_distance_frac": float(stop_distance_frac),
            },
        )

    def _alpha_pullback(self, *, ctx: _SharedContext) -> _AlphaEvaluation:
        cfg = self._cfg
        if ctx.regime_side == "NONE" or ctx.regime == "UNKNOWN":
            return _AlphaEvaluation(
                alpha_id="alpha_pullback",
                reason=str(ctx.regime_block_reason or "regime_missing"),
            )
        if ctx.bias_side != ctx.regime_side:
            return _AlphaEvaluation(alpha_id="alpha_pullback", reason="bias_missing")
        if ctx.vol_ratio_15m < float(cfg.min_volume_ratio_15m):
            return _AlphaEvaluation(
                alpha_id="alpha_pullback",
                reason="volume_missing",
                diagnostics={
                    "vol_ratio_15m": float(ctx.vol_ratio_15m),
                    "min_volume_ratio_15m": float(cfg.min_volume_ratio_15m),
                },
            )

        touch_band = float(ctx.atr_15m) * float(cfg.pullback_touch_atr_mult)
        recent = ctx.candles_15m[-6:-1]
        if len(recent) < 5:
            return _AlphaEvaluation(alpha_id="alpha_pullback", reason="trigger_missing")

        long_touch = any(float(bar.low) <= float(ctx.ema_15m) + touch_band for bar in recent)
        short_touch = any(float(bar.high) >= float(ctx.ema_15m) - touch_band for bar in recent)
        long_trigger = (
            ctx.regime_side == "LONG"
            and long_touch
            and float(ctx.current_bar.close) > float(ctx.ema_15m)
            and float(ctx.current_bar.high) > float(ctx.prev_bar.high)
        )
        short_trigger = (
            ctx.regime_side == "SHORT"
            and short_touch
            and float(ctx.current_bar.close) < float(ctx.ema_15m)
            and float(ctx.current_bar.low) < float(ctx.prev_bar.low)
        )
        if not long_trigger and not short_trigger:
            return _AlphaEvaluation(
                alpha_id="alpha_pullback",
                reason="trigger_missing",
                diagnostics={
                    "touch_band": float(touch_band),
                    "ema_15m": float(ctx.ema_15m),
                    "recent_touch_long": bool(long_touch),
                    "recent_touch_short": bool(short_touch),
                    "close": float(ctx.current_bar.close),
                    "prev_high": float(ctx.prev_bar.high),
                    "prev_low": float(ctx.prev_bar.low),
                },
            )

        side: AllowedSideName = "LONG" if long_trigger else "SHORT"
        entry_price = float(ctx.current_bar.close)
        stop_price, stop_distance_frac = _common_stop(
            side=side,
            entry_price=entry_price,
            ctx=ctx,
            cfg=cfg,
        )
        if stop_price is None:
            return _AlphaEvaluation(alpha_id="alpha_pullback", reason="cost_missing")
        cost_reason = _common_cost_reason(
            stop_distance_frac=stop_distance_frac,
            ctx=ctx,
            cfg=cfg,
        )
        if cost_reason is not None:
            return _AlphaEvaluation(alpha_id="alpha_pullback", reason=cost_reason)

        rejection_strength = (
            (float(ctx.current_bar.close) - float(ctx.ema_15m)) / max(entry_price, 1e-9)
            if side == "LONG"
            else (float(ctx.ema_15m) - float(ctx.current_bar.close)) / max(entry_price, 1e-9)
        )
        score = _clamp_score(
            0.50
            + (float(ctx.regime_strength) * 0.14)
            + (float(ctx.bias_strength) * 0.16)
            + min(max(float(ctx.vol_ratio_15m) - 1.0, 0.0), 1.0) * 0.08
            + min(max(float(rejection_strength) * 160.0, 0.0), 1.0) * 0.10
        )
        return _AlphaEvaluation(
            alpha_id="alpha_pullback",
            reason="entry_signal",
            score=float(score),
            payload={
                "side": side,
                "entry_price": entry_price,
                "stop_price": float(stop_price),
                "stop_distance_frac": float(stop_distance_frac),
            },
        )

    def _alpha_expansion(self, *, ctx: _SharedContext) -> _AlphaEvaluation:
        cfg = self._cfg
        if ctx.bias_side == "NONE":
            return _AlphaEvaluation(alpha_id="alpha_expansion", reason="bias_missing")
        if ctx.vol_ratio_15m < float(cfg.min_volume_ratio_15m):
            return _AlphaEvaluation(
                alpha_id="alpha_expansion",
                reason="volume_missing",
                diagnostics={
                    "vol_ratio_15m": float(ctx.vol_ratio_15m),
                    "min_volume_ratio_15m": float(cfg.min_volume_ratio_15m),
                },
            )

        closes = [float(bar.close) for bar in ctx.candles_15m]
        widths: list[float] = []
        for idx in range(int(cfg.bb_period_15m), len(closes) + 1):
            value = bollinger_bandwidth(closes[:idx], cfg.bb_period_15m, cfg.bb_std_15m)
            if value is not None:
                widths.append(float(value))
        if len(widths) < max(int(cfg.squeeze_lookback_15m), 4):
            return _AlphaEvaluation(alpha_id="alpha_expansion", reason="trigger_missing")
        current_width = widths[-1]
        previous_width = widths[-2]
        squeeze_threshold = _percentile(
            widths[-int(cfg.squeeze_lookback_15m) :],
            cfg.squeeze_percentile_threshold,
        )
        previous_channel = donchian(ctx.candles_15m[:-1], cfg.donchian_period_15m)
        if previous_channel is None:
            return _AlphaEvaluation(alpha_id="alpha_expansion", reason="trigger_missing")
        upper, lower = previous_channel
        buffer = float(cfg.expansion_buffer_bps) / 10000.0
        long_breakout_level = float(upper) * (1.0 + buffer)
        short_breakout_level = float(lower) * (1.0 - buffer)
        true_range = float(ctx.current_bar.high) - float(ctx.current_bar.low)
        range_atr = true_range / max(float(ctx.atr_15m), 1e-9)
        body_ratio = abs(float(ctx.current_bar.close) - float(ctx.current_bar.open)) / max(
            true_range,
            1e-9,
        )
        favored_close_long = (
            (float(ctx.current_bar.close) - float(ctx.current_bar.low)) / max(true_range, 1e-9)
        )
        favored_close_short = (
            (float(ctx.current_bar.high) - float(ctx.current_bar.close)) / max(true_range, 1e-9)
        )
        width_expansion_frac = max(
            (float(current_width) - float(previous_width)) / max(float(previous_width), 1e-9),
            0.0,
        )
        breakout_distance_atr_long = max(
            (float(ctx.current_bar.close) - float(upper)) / max(float(ctx.atr_15m), 1e-9),
            0.0,
        )
        breakout_distance_atr_short = max(
            (float(lower) - float(ctx.current_bar.close)) / max(float(ctx.atr_15m), 1e-9),
            0.0,
        )
        breakout_efficiency_long = max(
            (float(ctx.current_bar.close) - float(upper)) / max(true_range, 1e-9),
            0.0,
        )
        breakout_efficiency_short = max(
            (float(lower) - float(ctx.current_bar.close)) / max(true_range, 1e-9),
            0.0,
        )
        overhang_frac_long = max(
            (float(ctx.current_bar.close) - float(long_breakout_level)) / max(true_range, 1e-9),
            0.0,
        )
        overhang_frac_short = max(
            (float(short_breakout_level) - float(ctx.current_bar.close)) / max(true_range, 1e-9),
            0.0,
        )
        rejection_wick_frac_long = max(
            (float(ctx.current_bar.high) - float(ctx.current_bar.close)) / max(true_range, 1e-9),
            0.0,
        )
        rejection_wick_frac_short = max(
            (float(ctx.current_bar.close) - float(ctx.current_bar.low)) / max(true_range, 1e-9),
            0.0,
        )
        squeeze_ready = float(previous_width) <= float(squeeze_threshold)
        width_ready = float(width_expansion_frac) >= float(cfg.expansion_width_expansion_min)
        body_ready = float(body_ratio) >= float(cfg.expansion_body_ratio_min)
        close_ready = float(favored_close_long) >= float(cfg.expansion_close_location_min)
        short_close_ready = float(favored_close_short) >= float(cfg.expansion_close_location_min)
        breakout_distance_ready_long = float(breakout_distance_atr_long) >= float(
            cfg.expansion_break_distance_atr_min
        )
        breakout_distance_ready_short = float(breakout_distance_atr_short) >= float(
            cfg.expansion_break_distance_atr_min
        )
        long_trigger = (
            ctx.bias_side == "LONG"
            and float(range_atr) >= float(cfg.expansion_range_atr_min)
            and float(ctx.current_bar.close) > float(long_breakout_level)
        )
        short_trigger = (
            ctx.bias_side == "SHORT"
            and float(range_atr) >= float(cfg.expansion_range_atr_min)
            and float(ctx.current_bar.close) < float(short_breakout_level)
        )
        if not long_trigger and not short_trigger:
            return _AlphaEvaluation(
                alpha_id="alpha_expansion",
                reason="trigger_missing",
                diagnostics={
                    "bias_side": ctx.bias_side,
                    "range_atr": float(range_atr),
                    "expansion_range_atr_min": float(cfg.expansion_range_atr_min),
                    "close": float(ctx.current_bar.close),
                    "breakout_level_long": float(long_breakout_level),
                    "breakout_level_short": float(short_breakout_level),
                    "squeeze_ready": bool(squeeze_ready),
                    "width_ready": bool(width_ready),
                    "body_ready": bool(body_ready),
                    "close_ready": bool(close_ready),
                    "short_close_ready": bool(short_close_ready),
                    "breakout_distance_ready_long": bool(breakout_distance_ready_long),
                    "breakout_distance_ready_short": bool(breakout_distance_ready_short),
                    "width_expansion_frac": float(width_expansion_frac),
                    "body_ratio": float(body_ratio),
                    "favored_close_long": float(favored_close_long),
                    "favored_close_short": float(favored_close_short),
                    "breakout_distance_atr_long": float(breakout_distance_atr_long),
                    "breakout_distance_atr_short": float(breakout_distance_atr_short),
                },
            )

        side: AllowedSideName = "LONG" if long_trigger else "SHORT"
        favored_close = float(favored_close_long if side == "LONG" else favored_close_short)
        breakout_efficiency = float(
            breakout_efficiency_long if side == "LONG" else breakout_efficiency_short
        )
        breakout_distance_atr = float(
            breakout_distance_atr_long if side == "LONG" else breakout_distance_atr_short
        )
        overhang_frac = float(overhang_frac_long if side == "LONG" else overhang_frac_short)
        rejection_wick_frac = float(
            rejection_wick_frac_long if side == "LONG" else rejection_wick_frac_short
        )
        breakout_stability_score = _breakout_stability_score(
            overhang_frac=float(overhang_frac),
            rejection_wick_frac=float(rejection_wick_frac),
            width_expansion_frac=float(width_expansion_frac),
            expected_move_frac=float(ctx.expected_move_frac),
            required_move_frac=float(ctx.required_move_frac),
            cfg=cfg,
        )
        breakout_stability_edge_score = _breakout_stability_edge_score(
            overhang_frac=float(overhang_frac),
            rejection_wick_frac=float(rejection_wick_frac),
            width_expansion_frac=float(width_expansion_frac),
            expected_move_frac=float(ctx.expected_move_frac),
            required_move_frac=float(ctx.required_move_frac),
            cfg=cfg,
        )
        quality_score = _expansion_quality_score(
            body_ratio=float(body_ratio),
            favored_close=float(favored_close),
            width_expansion_frac=float(width_expansion_frac),
            expected_move_frac=float(ctx.expected_move_frac),
            required_move_frac=float(ctx.required_move_frac),
            cfg=cfg,
        )
        quality_score_v2 = _expansion_quality_score_v2(
            overhang_frac=float(overhang_frac),
            rejection_wick_frac=float(rejection_wick_frac),
            width_expansion_frac=float(width_expansion_frac),
            expected_move_frac=float(ctx.expected_move_frac),
            required_move_frac=float(ctx.required_move_frac),
            cfg=cfg,
        )
        if float(breakout_efficiency) < float(cfg.expansion_breakout_efficiency_min):
            return _AlphaEvaluation(
                alpha_id="alpha_expansion",
                reason="breakout_efficiency_missing",
            )
        if float(breakout_stability_score) < float(cfg.expansion_breakout_stability_score_min):
            return _AlphaEvaluation(
                alpha_id="alpha_expansion",
                reason="breakout_stability_missing",
            )
        if (
            float(breakout_stability_edge_score)
            < float(cfg.expansion_breakout_stability_edge_score_min)
        ):
            return _AlphaEvaluation(
                alpha_id="alpha_expansion",
                reason="breakout_stability_edge_missing",
            )
        if (
            side == "SHORT"
            and float(cfg.expansion_short_break_distance_atr_max) > 0.0
            and float(breakout_distance_atr) > float(cfg.expansion_short_break_distance_atr_max)
        ):
            return _AlphaEvaluation(
                alpha_id="alpha_expansion",
                reason="short_overextension_risk",
                diagnostics={
                    "breakout_distance_atr": float(breakout_distance_atr),
                    "expansion_short_break_distance_atr_max": float(
                        cfg.expansion_short_break_distance_atr_max
                    ),
                    "range_atr": float(range_atr),
                    "favored_close": float(favored_close),
                },
            )
        if float(quality_score) < float(cfg.expansion_quality_score_min):
            return _AlphaEvaluation(alpha_id="alpha_expansion", reason="quality_score_missing")
        if float(quality_score_v2) < float(cfg.expansion_quality_score_v2_min):
            return _AlphaEvaluation(alpha_id="alpha_expansion", reason="quality_score_v2_missing")
        entry_price = float(ctx.current_bar.close)
        stop_price, stop_distance_frac = _common_stop(
            side=side,
            entry_price=entry_price,
            ctx=ctx,
            cfg=cfg,
        )
        if stop_price is None:
            return _AlphaEvaluation(alpha_id="alpha_expansion", reason="cost_missing")
        cost_reason = _common_cost_reason(
            stop_distance_frac=stop_distance_frac,
            ctx=ctx,
            cfg=cfg,
        )
        if cost_reason is not None:
            return _AlphaEvaluation(alpha_id="alpha_expansion", reason=cost_reason)

        score = _clamp_score(
            0.49
            + (float(ctx.bias_strength) * 0.16)
            + min(max(float(ctx.vol_ratio_15m) - float(cfg.min_volume_ratio_15m), 0.0), 1.0) * 0.10
            + min(max(float(range_atr) - 1.0, 0.0), 1.0) * 0.11
            + min(max(float(width_expansion_frac), 0.0), 1.0) * 0.08
            + min(max(float(breakout_distance_atr), 0.0), 1.0) * 0.08
            + min(max(float(body_ratio), 0.0), 1.0) * 0.06
            + min(max(float(favored_close), 0.0), 1.0) * 0.06
            + (0.04 if squeeze_ready else 0.0)
        )
        return _AlphaEvaluation(
            alpha_id="alpha_expansion",
            reason="entry_signal",
            score=float(score),
            payload={
                "side": side,
                "entry_price": entry_price,
                "stop_price": float(stop_price),
                "stop_distance_frac": float(stop_distance_frac),
                "quality_score_v2": float(quality_score_v2),
                "breakout_stability_edge_score": float(breakout_stability_edge_score),
            },
            diagnostics={
                "vol_ratio_15m": float(ctx.vol_ratio_15m),
                "min_volume_ratio_15m": float(cfg.min_volume_ratio_15m),
                "range_atr": float(range_atr),
                "expansion_range_atr_min": float(cfg.expansion_range_atr_min),
                "body_ratio": float(body_ratio),
                "expansion_body_ratio_min": float(cfg.expansion_body_ratio_min),
                "favored_close": float(favored_close),
                "expansion_close_location_min": float(cfg.expansion_close_location_min),
                "width_expansion_frac": float(width_expansion_frac),
                "expansion_width_expansion_min": float(cfg.expansion_width_expansion_min),
                "breakout_distance_atr": float(breakout_distance_atr),
                "expansion_break_distance_atr_min": float(cfg.expansion_break_distance_atr_min),
                "squeeze_ready": bool(squeeze_ready),
            },
        )

    def decide(self, market_snapshot: dict[str, Any]) -> dict[str, Any]:
        symbol = str(market_snapshot.get("symbol") or "").strip().upper()
        if symbol not in self._cfg.supported_symbols:
            return _decision_none(
                symbol=symbol or "BTCUSDT",
                reason="unsupported_symbol",
                regime="UNKNOWN",
                allowed_side="NONE",
                indicators={},
            )

        market = market_snapshot.get("market")
        if not isinstance(market, dict):
            return _decision_none(
                symbol=symbol,
                reason="missing_market",
                regime="UNKNOWN",
                allowed_side="NONE",
                indicators={},
            )

        ctx, shared_reason = _build_shared_context(symbol=symbol, market=market, cfg=self._cfg)
        if ctx is None:
            return _decision_none(
                symbol=symbol,
                reason=shared_reason,
                regime="UNKNOWN",
                allowed_side="NONE",
                indicators={},
            )

        evaluations: list[_AlphaEvaluation] = []
        for alpha_id in self._cfg.enabled_alphas:
            if alpha_id == "alpha_breakout":
                evaluations.append(self._alpha_breakout(ctx=ctx))
            elif alpha_id == "alpha_pullback":
                evaluations.append(self._alpha_pullback(ctx=ctx))
            elif alpha_id == "alpha_expansion":
                evaluations.append(self._alpha_expansion(ctx=ctx))

        alpha_diagnostics = {
            item.alpha_id: {
                "state": "candidate" if item.payload is not None else "blocked",
                "reason": item.reason,
                "score": float(item.score),
                "metrics": copy.deepcopy(item.diagnostics or {}),
            }
            for item in evaluations
        }
        candidates = [item for item in evaluations if item.payload is not None]
        if candidates:
            best = max(candidates, key=lambda item: float(item.score))
            payload = _build_entry_payload(
                alpha_id=best.alpha_id,
                side=str(best.payload.get("side") or "LONG"),  # type: ignore[arg-type]
                entry_price=float(best.payload["entry_price"]),
                stop_price=float(best.payload["stop_price"]),
                stop_distance_frac=float(best.payload["stop_distance_frac"]),
                score=float(best.score),
                ctx=ctx,
                cfg=self._cfg,
                alpha_diagnostics=alpha_diagnostics,
                entry_meta=best.payload,
            )
            return payload

        final_reason = _aggregate_block_reason([item.reason for item in evaluations])
        allowed_side = ctx.regime_side if ctx.regime_side != "NONE" else ctx.bias_side
        return _decision_none(
            symbol=symbol,
            reason=final_reason,
            regime=ctx.regime,
            allowed_side=allowed_side,
            indicators=dict(ctx.indicators),
            alpha_diagnostics=alpha_diagnostics,
        )


class RA2026AlphaV2CandidateSelector(CandidateSelector):
    def __init__(
        self,
        *,
        strategy: StrategyPlugin,
        symbols: list[str],
        snapshot_provider: Any | None = None,
        overheat_fetcher: Any | None = None,
        journal_logger: Any | None = None,
    ) -> None:
        self._strategy = strategy
        normalized = [str(sym).strip().upper() for sym in symbols if str(sym).strip()]
        self._symbols = normalized or ["BTCUSDT"]
        self._snapshot_provider = snapshot_provider
        self._overheat_fetcher = overheat_fetcher
        self._journal_logger = journal_logger
        self._last_no_candidate_reason: str | None = None
        self._last_no_candidate_context: dict[str, Any] | None = None
        self._sync_strategy_supported_symbols()

    def get_last_no_candidate_reason(self) -> str | None:
        return self._last_no_candidate_reason

    def get_last_no_candidate_context(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._last_no_candidate_context)

    def _sync_strategy_supported_symbols(self) -> None:
        updater = getattr(self._strategy, "set_runtime_params", None)
        if callable(updater):
            updater(supported_symbols=list(self._symbols))

    def set_symbols(self, symbols: list[str]) -> None:
        normalized = [str(sym).strip().upper() for sym in symbols if str(sym).strip()]
        if normalized:
            self._symbols = normalized
            self._sync_strategy_supported_symbols()

    def set_strategy_runtime_params(self, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
        updater = getattr(self._strategy, "set_runtime_params", None)
        if callable(updater):
            updater(**kwargs)

    def inspect_symbol_decision(
        self,
        *,
        symbol: str,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        symbol_u = str(symbol).strip().upper()
        if not symbol_u:
            return None
        base_snapshot: dict[str, Any] = {"symbol": symbol_u}
        provided = snapshot if isinstance(snapshot, dict) else None
        if provided is None and self._snapshot_provider is not None:
            candidate_snapshot = self._snapshot_provider()
            if isinstance(candidate_snapshot, dict):
                provided = candidate_snapshot
        if isinstance(provided, dict):
            base_snapshot.update(provided)

        symbols_market = base_snapshot.get("symbols")
        if isinstance(symbols_market, dict):
            market = symbols_market.get(symbol_u)
            if isinstance(market, dict):
                base_snapshot["market"] = market
        base_snapshot["symbol"] = symbol_u

        decision = self._strategy.decide(base_snapshot)
        return copy.deepcopy(decision) if isinstance(decision, dict) else None

    def select(self, *, context: KernelContext) -> Candidate | None:
        _ = context
        self._last_no_candidate_reason = None
        self._last_no_candidate_context = None
        base_snapshot: dict[str, Any] = {"symbol": self._symbols[0]}
        if self._snapshot_provider is not None:
            provided = self._snapshot_provider()
            if isinstance(provided, dict):
                base_snapshot.update(provided)

        symbols_market = base_snapshot.get("symbols")
        candidates: list[Candidate] = []
        skipped: dict[str, str] = {}
        skipped_context: dict[str, dict[str, Any]] = {}

        for symbol in self._symbols:
            symbol_snapshot = dict(base_snapshot)
            if isinstance(symbols_market, dict):
                market = symbols_market.get(symbol)
                if isinstance(market, dict):
                    symbol_snapshot["market"] = market
            _ = self._overheat_fetcher
            symbol_snapshot["symbol"] = symbol

            decision = self._strategy.decide(symbol_snapshot)
            if self._journal_logger is not None:
                self._journal_logger(decision)

            intent = str(decision.get("intent") or "NONE")
            side = str(decision.get("side") or "NONE")
            if intent not in {"LONG", "SHORT"} or side not in {"BUY", "SELL"}:
                skipped[symbol] = str(decision.get("reason") or "no_entry")
                skipped_context[symbol] = {
                    "reason": str(decision.get("reason") or "no_entry"),
                    "alpha_blocks": copy.deepcopy(decision.get("alpha_blocks") or {}),
                    "alpha_reject_metrics": copy.deepcopy(
                        decision.get("alpha_reject_metrics")
                        or {
                            alpha_id: (meta.get("metrics") or {})
                            for alpha_id, meta in (decision.get("alpha_diagnostics") or {}).items()
                            if isinstance(meta, dict)
                            and str(meta.get("state") or "").strip() != "candidate"
                            and isinstance(meta.get("metrics"), dict)
                            and meta.get("metrics")
                        }
                    ),
                }
                continue

            score = _to_float(decision.get("score")) or 0.0
            if score <= 0.0:
                skipped[symbol] = "non_positive_score"
                continue

            indicators = decision.get("indicators")
            atr_hint = None
            spread_pct = None
            if isinstance(indicators, dict):
                atr_hint = _to_float(indicators.get("atr14_15m"))
                spread_bps = _to_float(indicators.get("spread_estimate_bps"))
                if spread_bps is not None:
                    spread_pct = float(spread_bps) / 100.0

            candidates.append(
                Candidate(
                    symbol=symbol,
                    side="BUY" if side == "BUY" else "SELL",
                    score=float(score),
                    alpha_id=str(decision.get("alpha_id") or "").strip() or None,
                    entry_family=str(decision.get("entry_family") or "").strip() or None,
                    reason=str(decision.get("reason") or "entry_signal"),
                    source=str(getattr(self._strategy, "name", "ra_2026_alpha_v2")),
                    entry_price=_to_float(decision.get("entry_price")),
                    stop_price_hint=_to_float(decision.get("stop_price_hint")),
                    stop_distance_frac=_to_float(decision.get("stop_distance_frac")),
                    volatility_hint=atr_hint,
                    regime_hint=str(decision.get("regime") or "").upper() or None,
                    regime_strength=_to_float(decision.get("regime_strength")),
                    risk_per_trade_pct=_to_float(decision.get("risk_per_trade_pct")),
                    max_effective_leverage=_to_float(decision.get("max_effective_leverage")),
                    expected_move_frac=_to_float(decision.get("expected_move_frac")),
                    required_move_frac=_to_float(decision.get("required_move_frac")),
                    spread_pct=spread_pct,
                    take_profit_hint=_to_float((decision.get("sl_tp") or {}).get("take_profit")),
                    execution_hints=(
                        copy.deepcopy(decision.get("execution"))
                        if isinstance(decision.get("execution"), dict)
                        else None
                    ),
                )
            )

        if candidates:
            return max(candidates, key=lambda item: float(item.score))

        if skipped:
            ordered = sorted(skipped.items())
            reasons = sorted({reason for _, reason in ordered})
            self._last_no_candidate_context = copy.deepcopy(skipped_context)
            if len(reasons) == 1:
                self._last_no_candidate_reason = reasons[0]
            else:
                snippet = ";".join(f"{sym}:{reason}" for sym, reason in ordered[:3])
                self._last_no_candidate_reason = f"no_candidate_multi:{snippet}"
        else:
            self._last_no_candidate_reason = "no_candidate"
        return None
