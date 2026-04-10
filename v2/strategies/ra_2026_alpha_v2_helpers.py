from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from v2.strategies.alpha_shared import (
    _Bar,
    _clamp_score,
    _sma,
    _spread_bps_from_bar,
    _to_bars,
    adx,
    atr,
    bollinger_bandwidth,
    ema,
    expected_move_gate,
    rsi,
)
from v2.strategies.ra_2026_alpha_v2_alpha import (
    BLOCK_REASON_PRIORITY,
    AllowedSideName,
    AlphaId,
    RegimeName,
)
from v2.strategies.ra_2026_alpha_v2_params import RA2026AlphaV2Params

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
