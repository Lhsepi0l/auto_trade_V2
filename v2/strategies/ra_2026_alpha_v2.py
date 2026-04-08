from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from v2.kernel.contracts import Candidate, CandidateSelector, KernelContext
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
from v2.strategies.base import StrategyPlugin
from v2.strategies.ra_2026_alpha_v2_alpha import (
    BLOCK_REASON_PRIORITY,
    AllowedSideName,
    AlphaId,
    RegimeName,
)
from v2.strategies.ra_2026_alpha_v2_params import RA2026AlphaV2Params
from v2.strategies.ra_2026_alpha_v2_payload import (
    _build_entry_payload,
    _decision_none,
)

EXPANSION_COST_NEAR_PASS_MIN_EDGE_RATIO = 0.95
EXPANSION_COST_NEAR_PASS_MIN_QV2 = 0.70
FIFTEEN_MIN_MS = 15 * 60 * 1000


@dataclass(frozen=True)
class _AlphaEvaluation:
    alpha_id: AlphaId
    reason: str
    score: float = 0.0
    payload: dict[str, Any] | None = None
    diagnostics: dict[str, Any] | None = None


@dataclass(frozen=True)
class _DriftSetupState:
    open_time_ms: int
    setup_close: float
    setup_low: float
    range_atr: float
    body_ratio: float
    favored_close_long: float
    width_expansion_frac: float
    edge_ratio: float


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



def _aggregate_block_reason(reasons: list[str]) -> str:
    if not reasons:
        return "trigger_missing"
    ordered = sorted(
        reasons,
        key=lambda item: (BLOCK_REASON_PRIORITY.get(str(item), 99), str(item)),
    )
    return str(ordered[0])


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _drift_bias_side(*, ctx: _SharedContext, cfg: RA2026AlphaV2Params) -> tuple[AllowedSideName, dict[str, float]]:
    closes = [float(bar.close) for bar in ctx.candles_1h]
    ema_now = ema(closes, cfg.ema_bias_period_1h)
    ema_prev = ema(closes[:-1], cfg.ema_bias_period_1h) if len(closes) > cfg.ema_bias_period_1h else None
    rsi_now = rsi(closes, cfg.rsi_period_1h)
    close_now = float(closes[-1] if closes else 0.0)
    metrics = {
        "drift_close_1h": float(close_now),
        "drift_ema_1h": float(ema_now or 0.0),
        "drift_ema_prev_1h": float(ema_prev or 0.0),
        "drift_rsi_1h": float(rsi_now or 0.0),
        "drift_bias_rsi_long_min": float(cfg.drift_bias_rsi_long_min),
        "drift_bias_rsi_short_max": float(cfg.drift_bias_rsi_short_max),
    }
    if ema_now is None or ema_prev is None or rsi_now is None or not closes:
        return "NONE", metrics
    long_ok = (
        float(close_now) >= (float(ema_now) * 0.998)
        and float(rsi_now) >= float(cfg.drift_bias_rsi_long_min)
    )
    short_ok = (
        float(close_now) <= float(ema_now)
        and float(ema_now) <= float(ema_prev)
        and float(rsi_now) <= float(cfg.drift_bias_rsi_short_max)
    )
    if long_ok:
        return "LONG", metrics
    if short_ok:
        return "SHORT", metrics
    return "NONE", metrics


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


def _common_cost_subreason(
    *,
    stop_distance_frac: float,
    ctx: _SharedContext,
    cfg: RA2026AlphaV2Params,
) -> str | None:
    if float(ctx.spread_estimate_bps) > float(cfg.max_spread_bps):
        return "spread_cap"
    if float(stop_distance_frac) < float(cfg.min_stop_distance_frac):
        return "stop_distance_too_small"
    if float(ctx.expected_move_frac) <= 0.0 or float(ctx.required_move_frac) <= 0.0:
        return "edge_unavailable"
    if float(ctx.expected_move_frac) < float(ctx.required_move_frac):
        return "edge_shortfall"
    return None


def _expansion_cost_near_pass_allowed(
    *,
    stop_distance_frac: float,
    ctx: _SharedContext,
    cfg: RA2026AlphaV2Params,
    quality_score_v2: float,
) -> bool:
    if (
        _common_cost_subreason(
            stop_distance_frac=float(stop_distance_frac),
            ctx=ctx,
            cfg=cfg,
        )
        != "edge_shortfall"
    ):
        return False
    edge_ratio = float(ctx.expected_move_frac) / max(float(ctx.required_move_frac), 1e-9)
    min_quality = max(float(cfg.expansion_quality_score_v2_min), EXPANSION_COST_NEAR_PASS_MIN_QV2)
    return (
        float(edge_ratio) >= float(EXPANSION_COST_NEAR_PASS_MIN_EDGE_RATIO)
        and float(quality_score_v2) >= float(min_quality)
    )


def _drift_cost_near_pass_allowed(
    *,
    stop_distance_frac: float,
    ctx: _SharedContext,
    cfg: RA2026AlphaV2Params,
    setup: _DriftSetupState,
) -> bool:
    return (
        _common_cost_subreason(
            stop_distance_frac=float(stop_distance_frac),
            ctx=ctx,
            cfg=cfg,
        )
        == "edge_shortfall"
        and float(setup.edge_ratio) >= float(cfg.drift_long_edge_ratio_min)
    )


def _drift_setup_qualifies(
    *,
    ctx: _SharedContext,
    cfg: RA2026AlphaV2Params,
) -> tuple[bool, dict[str, float]]:
    closes = [float(bar.close) for bar in ctx.candles_15m]
    widths: list[float] = []
    for idx in range(int(cfg.bb_period_15m), len(closes) + 1):
        value = bollinger_bandwidth(closes[:idx], cfg.bb_period_15m, cfg.bb_std_15m)
        if value is not None:
            widths.append(float(value))
    if len(widths) < 2:
        return False, {}
    true_range = max(float(ctx.current_bar.high) - float(ctx.current_bar.low), 1e-9)
    range_atr = float(true_range) / max(float(ctx.atr_15m), 1e-9)
    body_ratio = abs(float(ctx.current_bar.close) - float(ctx.current_bar.open)) / float(true_range)
    favored_close_long = (float(ctx.current_bar.close) - float(ctx.current_bar.low)) / float(true_range)
    width_expansion_frac = max(
        (float(widths[-1]) - float(widths[-2])) / max(float(widths[-2]), 1e-9),
        0.0,
    )
    edge_ratio = float(ctx.expected_move_frac) / max(float(ctx.required_move_frac), 1e-9)
    metrics = {
        "range_atr": float(range_atr),
        "body_ratio": float(body_ratio),
        "favored_close_long": float(favored_close_long),
        "width_expansion_frac": float(width_expansion_frac),
        "edge_ratio": float(edge_ratio),
    }
    ok = (
        float(range_atr) >= float(cfg.drift_range_atr_min)
        and float(body_ratio) >= float(cfg.drift_body_ratio_min)
        and float(favored_close_long) < float(cfg.drift_close_location_max)
        and float(width_expansion_frac) >= float(cfg.drift_long_width_expansion_min)
        and float(edge_ratio) >= float(cfg.drift_long_edge_ratio_min)
    )
    return bool(ok), metrics




class RA2026AlphaV2(StrategyPlugin):
    name = "ra_2026_alpha_v2"

    def __init__(self, *, params: dict[str, Any] | None = None, logger: Any | None = None) -> None:
        self._cfg = RA2026AlphaV2Params.from_params(params)
        self._logger = logger
        self._drift_setups: dict[str, _DriftSetupState] = {}

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
        if cost_reason is not None and not _expansion_cost_near_pass_allowed(
            stop_distance_frac=stop_distance_frac,
            ctx=ctx,
            cfg=cfg,
            quality_score_v2=float(quality_score_v2),
        ):
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

    def _alpha_drift(
        self,
        *,
        ctx: _SharedContext,
        open_time_ms: int | None,
    ) -> _AlphaEvaluation:
        cfg = self._cfg
        drift_bias_side, drift_bias_metrics = _drift_bias_side(ctx=ctx, cfg=cfg)
        if str(cfg.drift_side_mode) == "SHORT":
            return _AlphaEvaluation(
                alpha_id="alpha_drift",
                reason="bias_missing",
                diagnostics={**drift_bias_metrics, "drift_side_mode": str(cfg.drift_side_mode)},
            )
        if drift_bias_side != "LONG":
            return _AlphaEvaluation(
                alpha_id="alpha_drift",
                reason="bias_missing",
                diagnostics=drift_bias_metrics,
            )
        if str(cfg.drift_side_mode) != "BOTH" and str(drift_bias_side) != str(cfg.drift_side_mode):
            return _AlphaEvaluation(
                alpha_id="alpha_drift",
                reason="bias_missing",
                diagnostics={
                    **drift_bias_metrics,
                    "drift_side_mode": str(cfg.drift_side_mode),
                    "drift_bias_side": str(drift_bias_side),
                },
            )
        if ctx.vol_ratio_15m < float(cfg.min_volume_ratio_15m):
            return _AlphaEvaluation(
                alpha_id="alpha_drift",
                reason="volume_missing",
                diagnostics={
                    "vol_ratio_15m": float(ctx.vol_ratio_15m),
                    "min_volume_ratio_15m": float(cfg.min_volume_ratio_15m),
                },
            )
        setup_ok, setup_metrics = _drift_setup_qualifies(ctx=ctx, cfg=cfg)
        current_open_time = _to_int(open_time_ms)
        setup = self._drift_setups.get(ctx.symbol)
        if setup is not None and current_open_time is not None:
            bars_since_setup = max(int((current_open_time - int(setup.open_time_ms)) // FIFTEEN_MIN_MS), 0)
            if float(ctx.current_bar.low) < float(setup.setup_low) or bars_since_setup > int(cfg.drift_setup_expiry_bars):
                self._drift_setups.pop(ctx.symbol, None)
                setup = None
            elif bars_since_setup >= 1 and float(ctx.current_bar.close) > max(float(setup.setup_close), float(ctx.ema_15m)):
                entry_price = float(ctx.current_bar.close)
                stop_price = min(float(setup.setup_low), float(ctx.current_bar.low), float(entry_price))
                stop_distance_frac = max(float(entry_price) - float(stop_price), 0.0) / max(float(entry_price), 1e-9)
                if float(stop_distance_frac) <= 0.0:
                    return _AlphaEvaluation(alpha_id="alpha_drift", reason="cost_missing")
                cost_reason = _common_cost_reason(
                    stop_distance_frac=float(stop_distance_frac),
                    ctx=ctx,
                    cfg=cfg,
                )
                if cost_reason is None or _drift_cost_near_pass_allowed(
                    stop_distance_frac=float(stop_distance_frac),
                    ctx=ctx,
                    cfg=cfg,
                    setup=setup,
                ):
                    self._drift_setups.pop(ctx.symbol, None)
                    score = _clamp_score(
                        0.50
                        + (float(ctx.bias_strength) * 0.16)
                        + min(max((float(setup.range_atr) - float(cfg.drift_range_atr_min)) / 2.0, 0.0), 1.0) * 0.14
                        + min(max((float(setup.body_ratio) - float(cfg.drift_body_ratio_min)) / 0.4, 0.0), 1.0) * 0.12
                        + min(max((float(cfg.drift_close_location_max) - float(setup.favored_close_long)) / max(float(cfg.drift_close_location_max), 1e-9), 0.0), 1.0) * 0.10
                        + min(max(float(setup.width_expansion_frac) / 0.5, 0.0), 1.0) * 0.08
                        + min(max((float(setup.edge_ratio) - float(cfg.drift_long_edge_ratio_min)) / 1.5, 0.0), 1.0) * 0.08
                    )
                    return _AlphaEvaluation(
                        alpha_id="alpha_drift",
                        reason="entry_signal",
                        score=float(score),
                        payload={
                            "side": "LONG",
                            "entry_price": float(entry_price),
                            "stop_price": float(stop_price),
                            "stop_distance_frac": float(stop_distance_frac),
                            "setup_open_time_ms": int(setup.open_time_ms),
                            "take_profit_r_override": float(cfg.drift_take_profit_r),
                            "time_stop_bars_override": int(cfg.drift_time_stop_bars),
                        },
                        diagnostics={
                            **setup_metrics,
                            "setup_open_time_ms": int(setup.open_time_ms),
                            "bars_since_setup": int(bars_since_setup),
                        },
                    )

        if not setup_ok:
            return _AlphaEvaluation(
                alpha_id="alpha_drift",
                reason="trigger_missing",
                diagnostics={
                    **drift_bias_metrics,
                    **setup_metrics,
                    "drift_bias_side": drift_bias_side,
                },
            )
        if current_open_time is not None:
            self._drift_setups[ctx.symbol] = _DriftSetupState(
                open_time_ms=int(current_open_time),
                setup_close=float(ctx.current_bar.close),
                setup_low=float(ctx.current_bar.low),
                range_atr=float(setup_metrics["range_atr"]),
                body_ratio=float(setup_metrics["body_ratio"]),
                favored_close_long=float(setup_metrics["favored_close_long"]),
                width_expansion_frac=float(setup_metrics["width_expansion_frac"]),
                edge_ratio=float(setup_metrics["edge_ratio"]),
            )
        return _AlphaEvaluation(
            alpha_id="alpha_drift",
            reason="trigger_missing",
            diagnostics={
                **drift_bias_metrics,
                **setup_metrics,
                "drift_setup_queued": 1.0,
                "drift_setup_open_time_ms": float(current_open_time or 0),
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
            elif alpha_id == "alpha_drift":
                evaluations.append(
                    self._alpha_drift(
                        ctx=ctx,
                        open_time_ms=_to_int(market_snapshot.get("open_time")),
                    )
                )

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
