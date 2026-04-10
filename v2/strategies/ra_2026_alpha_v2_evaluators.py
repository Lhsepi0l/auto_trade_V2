from __future__ import annotations

from v2.strategies.alpha_shared import _clamp_score, _percentile, bollinger_bandwidth, donchian
from v2.strategies.ra_2026_alpha_v2_alpha import AllowedSideName
from v2.strategies.ra_2026_alpha_v2_helpers import (
    FIFTEEN_MIN_MS,
    _AlphaEvaluation,
    _breakout_stability_edge_score,
    _breakout_stability_score,
    _common_cost_reason,
    _common_stop,
    _drift_bias_side,
    _drift_cost_near_pass_allowed,
    _drift_setup_qualifies,
    _DriftSetupState,
    _expansion_cost_near_pass_allowed,
    _expansion_quality_score,
    _expansion_quality_score_v2,
    _SharedContext,
    _to_int,
)
from v2.strategies.ra_2026_alpha_v2_params import RA2026AlphaV2Params


def evaluate_alpha_breakout(*, ctx: _SharedContext, cfg: RA2026AlphaV2Params) -> _AlphaEvaluation:
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


def evaluate_alpha_pullback(*, ctx: _SharedContext, cfg: RA2026AlphaV2Params) -> _AlphaEvaluation:
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


def evaluate_alpha_expansion(*, ctx: _SharedContext, cfg: RA2026AlphaV2Params) -> _AlphaEvaluation:
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


def evaluate_alpha_drift(
    *,
    ctx: _SharedContext,
    cfg: RA2026AlphaV2Params,
    drift_setups: dict[str, _DriftSetupState],
    open_time_ms: int | None,
) -> _AlphaEvaluation:
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
    setup = drift_setups.get(ctx.symbol)
    if setup is not None and current_open_time is not None:
        bars_since_setup = max(int((current_open_time - int(setup.open_time_ms)) // FIFTEEN_MIN_MS), 0)
        if float(ctx.current_bar.low) < float(setup.setup_low) or bars_since_setup > int(cfg.drift_setup_expiry_bars):
            drift_setups.pop(ctx.symbol, None)
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
                drift_setups.pop(ctx.symbol, None)
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
        drift_setups[ctx.symbol] = _DriftSetupState(
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
