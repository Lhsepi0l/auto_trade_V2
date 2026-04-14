from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from statistics import mean
from typing import Any, Literal

from v2.kernel.contracts import Candidate, CandidateSelector, KernelContext
from v2.strategies.alpha_shared import (
    _clamp_score,
    _percentile,
    _to_bars,
    _to_float,
    bollinger_bandwidth,
    ema,
)
from v2.strategies.base import DesiredPosition, StrategyPlugin

Side = Literal["LONG", "SHORT", "NONE"]


def _stddev(values: list[float]) -> float:
    if not values:
        return 0.0
    avg = mean(values)
    variance = sum((float(item) - float(avg)) ** 2 for item in values) / float(len(values))
    return variance**0.5


def _bollinger_bands(closes: list[float], period: int, std_mult: float) -> tuple[float, float, float] | None:
    if period <= 1 or len(closes) < period:
        return None
    window = [float(item) for item in closes[-period:]]
    mid = mean(window)
    dev = _stddev(window)
    upper = float(mid) + (float(std_mult) * float(dev))
    lower = float(mid) - (float(std_mult) * float(dev))
    return float(lower), float(mid), float(upper)


def _cci(highs: list[float], lows: list[float], closes: list[float], period: int) -> float | None:
    if period <= 1 or min(len(highs), len(lows), len(closes)) < period:
        return None
    tps = [
        (float(highs[-period + idx]) + float(lows[-period + idx]) + float(closes[-period + idx])) / 3.0
        for idx in range(period)
    ]
    tp_mean = mean(tps)
    mean_dev = mean(abs(float(tp) - float(tp_mean)) for tp in tps)
    if mean_dev <= 1e-12:
        return 0.0
    return (float(tps[-1]) - float(tp_mean)) / (0.015 * float(mean_dev))


@dataclass(frozen=True)
class EBCParams:
    supported_symbols: tuple[str, ...] = ("BTCUSDT",)
    side_mode: str = "BOTH"
    ema_period_12h: int = 50
    ema_period_2h: int = 34
    ema_period_30m: int = 20
    ema_period_5m: int = 20
    bb_period_30m: int = 20
    bb_std_30m: float = 2.0
    cci_period_2h: int = 20
    cci_period_5m: int = 20
    cci_pullback_floor: float = -50.0
    cci_trigger_level: float = 0.0
    bias_max_distance_frac_2h: float = 0.012
    bias_cci_extreme_cap: float = 180.0
    squeeze_width_frac_max: float = 0.035
    stop_loss_pct: float = 0.006
    take_profit_r: float = 1.8
    time_stop_bars: int = 24
    min_entry_score: float = 0.0
    risk_per_trade_pct: float = 0.01
    max_effective_leverage: float = 5.0
    stop_exit_cooldown_bars: int = 18
    profit_exit_cooldown_bars: int = 0
    regime_min_slope_12h: float = 0.0002
    bias_min_slope_2h: float = 0.0004
    squeeze_lookback_30m: int = 48
    squeeze_percentile_threshold_30m: float = 0.35
    reexpand_ratio_min_30m: float = 1.10
    recent_pullback_lookback_30m: int = 8
    trigger_cross_lookback_5m: int = 4
    trigger_max_chase_frac_5m: float = 0.0025
    trigger_cci_reset_level: float = 0.0
    band_location_min_long: float = 0.55
    band_location_max_long: float = 0.92
    band_location_min_short: float = 0.08
    band_location_max_short: float = 0.45
    max_extension_risk: float = 0.58
    progress_check_bars: int = 8
    progress_min_mfe_r: float = 0.35
    progress_extend_trigger_r: float = 0.95
    progress_extend_bars: int = 8
    selective_extension_proof_bars: int = 8
    selective_extension_min_mfe_r: float = 0.95
    selective_extension_min_regime_strength: float = 0.60
    selective_extension_min_bias_strength: float = 0.55
    selective_extension_min_quality_score_v2: float = 0.72
    selective_extension_time_stop_bars: int = 32
    selective_extension_take_profit_r: float = 2.2
    selective_extension_move_stop_to_be_at_r: float = 1.0
    stalled_trend_timeout_bars: int = 10
    stalled_volume_ratio_floor: float = 0.90
    volume_sma_period_5m: int = 20
    rotation_regime_weight: float = 0.20
    rotation_bias_weight: float = 0.15
    rotation_trigger_weight: float = 0.10
    rotation_relative_weight: float = 0.20
    rotation_extension_penalty_weight: float = 0.12

    @classmethod
    def from_params(cls, raw: dict[str, Any] | None) -> "EBCParams":
        source = raw or {}

        def _i(name: str, default: int) -> int:
            try:
                return max(int(source.get(name, default)), 1)
            except (TypeError, ValueError):
                return int(default)

        def _f(name: str, default: float) -> float:
            try:
                return float(source.get(name, default))
            except (TypeError, ValueError):
                return float(default)

        supported = source.get("supported_symbols", cls.supported_symbols)
        if isinstance(supported, str):
            symbols = tuple(token.strip().upper() for token in supported.split(",") if token.strip())
        elif isinstance(supported, (list, tuple)):
            symbols = tuple(str(token).strip().upper() for token in supported if str(token).strip())
        else:
            symbols = cls.supported_symbols
        return cls(
            supported_symbols=symbols or cls.supported_symbols,
            side_mode=(
                str(source.get("side_mode", cls.side_mode)).strip().upper()
                if str(source.get("side_mode", cls.side_mode)).strip().upper() in {"BOTH", "LONG", "SHORT"}
                else str(cls.side_mode)
            ),
            ema_period_12h=_i("ema_period_12h", cls.ema_period_12h),
            ema_period_2h=_i("ema_period_2h", cls.ema_period_2h),
            ema_period_30m=_i("ema_period_30m", cls.ema_period_30m),
            ema_period_5m=_i("ema_period_5m", cls.ema_period_5m),
            bb_period_30m=_i("bb_period_30m", cls.bb_period_30m),
            bb_std_30m=max(_f("bb_std_30m", cls.bb_std_30m), 0.1),
            cci_period_2h=_i("cci_period_2h", cls.cci_period_2h),
            cci_period_5m=_i("cci_period_5m", cls.cci_period_5m),
            cci_pullback_floor=_f("cci_pullback_floor", cls.cci_pullback_floor),
            cci_trigger_level=_f("cci_trigger_level", cls.cci_trigger_level),
            bias_max_distance_frac_2h=max(
                _f("bias_max_distance_frac_2h", cls.bias_max_distance_frac_2h),
                0.0,
            ),
            bias_cci_extreme_cap=max(
                _f("bias_cci_extreme_cap", cls.bias_cci_extreme_cap),
                0.0,
            ),
            squeeze_width_frac_max=max(_f("squeeze_width_frac_max", cls.squeeze_width_frac_max), 0.0),
            stop_loss_pct=max(_f("stop_loss_pct", cls.stop_loss_pct), 0.0001),
            take_profit_r=max(_f("take_profit_r", cls.take_profit_r), 0.5),
            time_stop_bars=_i("time_stop_bars", cls.time_stop_bars),
            min_entry_score=min(max(_f("min_entry_score", cls.min_entry_score), 0.0), 1.0),
            risk_per_trade_pct=min(max(_f("risk_per_trade_pct", cls.risk_per_trade_pct), 0.0), 1.0),
            max_effective_leverage=max(_f("max_effective_leverage", cls.max_effective_leverage), 1.0),
            stop_exit_cooldown_bars=_i(
                "stop_exit_cooldown_bars",
                cls.stop_exit_cooldown_bars,
            ),
            profit_exit_cooldown_bars=max(
                _i("profit_exit_cooldown_bars", cls.profit_exit_cooldown_bars),
                0,
            ),
            regime_min_slope_12h=max(_f("regime_min_slope_12h", cls.regime_min_slope_12h), 0.0),
            bias_min_slope_2h=max(_f("bias_min_slope_2h", cls.bias_min_slope_2h), 0.0),
            squeeze_lookback_30m=_i("squeeze_lookback_30m", cls.squeeze_lookback_30m),
            squeeze_percentile_threshold_30m=min(
                max(
                    _f(
                        "squeeze_percentile_threshold_30m",
                        cls.squeeze_percentile_threshold_30m,
                    ),
                    0.05,
                ),
                0.95,
            ),
            reexpand_ratio_min_30m=max(_f("reexpand_ratio_min_30m", cls.reexpand_ratio_min_30m), 1.0),
            recent_pullback_lookback_30m=_i(
                "recent_pullback_lookback_30m",
                cls.recent_pullback_lookback_30m,
            ),
            trigger_cross_lookback_5m=_i("trigger_cross_lookback_5m", cls.trigger_cross_lookback_5m),
            trigger_max_chase_frac_5m=max(
                _f("trigger_max_chase_frac_5m", cls.trigger_max_chase_frac_5m),
                0.0,
            ),
            trigger_cci_reset_level=_f("trigger_cci_reset_level", cls.trigger_cci_reset_level),
            band_location_min_long=min(
                max(_f("band_location_min_long", cls.band_location_min_long), 0.0),
                1.0,
            ),
            band_location_max_long=min(
                max(_f("band_location_max_long", cls.band_location_max_long), 0.0),
                1.0,
            ),
            band_location_min_short=min(
                max(_f("band_location_min_short", cls.band_location_min_short), 0.0),
                1.0,
            ),
            band_location_max_short=min(
                max(_f("band_location_max_short", cls.band_location_max_short), 0.0),
                1.0,
            ),
            max_extension_risk=min(max(_f("max_extension_risk", cls.max_extension_risk), 0.0), 1.0),
            progress_check_bars=_i("progress_check_bars", cls.progress_check_bars),
            progress_min_mfe_r=max(_f("progress_min_mfe_r", cls.progress_min_mfe_r), 0.0),
            progress_extend_trigger_r=max(
                _f("progress_extend_trigger_r", cls.progress_extend_trigger_r),
                0.0,
            ),
            progress_extend_bars=_i("progress_extend_bars", cls.progress_extend_bars),
            selective_extension_proof_bars=_i(
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
            selective_extension_time_stop_bars=_i(
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
            stalled_trend_timeout_bars=_i(
                "stalled_trend_timeout_bars",
                cls.stalled_trend_timeout_bars,
            ),
            stalled_volume_ratio_floor=max(
                _f("stalled_volume_ratio_floor", cls.stalled_volume_ratio_floor),
                0.0,
            ),
            volume_sma_period_5m=_i("volume_sma_period_5m", cls.volume_sma_period_5m),
            rotation_regime_weight=max(
                _f("rotation_regime_weight", cls.rotation_regime_weight),
                0.0,
            ),
            rotation_bias_weight=max(
                _f("rotation_bias_weight", cls.rotation_bias_weight),
                0.0,
            ),
            rotation_trigger_weight=max(
                _f("rotation_trigger_weight", cls.rotation_trigger_weight),
                0.0,
            ),
            rotation_relative_weight=max(
                _f("rotation_relative_weight", cls.rotation_relative_weight),
                0.0,
            ),
            rotation_extension_penalty_weight=max(
                _f(
                    "rotation_extension_penalty_weight",
                    cls.rotation_extension_penalty_weight,
                ),
                0.0,
            ),
        )


def _frame(raw: Any) -> tuple[list[float], list[float], list[float]] | None:
    bars = _to_bars(raw)
    if not bars:
        return None
    closes = [float(bar.close) for bar in bars]
    highs = [float(bar.high) for bar in bars]
    lows = [float(bar.low) for bar in bars]
    return closes, highs, lows


def _ema_slope_frac(values: list[float], period: int) -> float | None:
    if len(values) < period + 1:
        return None
    current = ema(values, period)
    previous = ema(values[:-1], period)
    if current is None or previous is None or abs(float(previous)) <= 1e-9:
        return None
    return (float(current) - float(previous)) / abs(float(previous))


def _recent_touch(
    *,
    highs: list[float],
    lows: list[float],
    closes: list[float],
    ema_period: int,
    side: Side,
    lookback: int,
) -> bool:
    if side == "NONE":
        return False
    limit = min(max(int(lookback), 1), max(len(closes) - ema_period, 0))
    for back in range(1, limit + 1):
        end = len(closes) - back
        ema_hist = ema(closes[:end], ema_period)
        if ema_hist is None:
            continue
        idx = end - 1
        if side == "LONG" and float(lows[idx]) <= float(ema_hist):
            return True
        if side == "SHORT" and float(highs[idx]) >= float(ema_hist):
            return True
    return False


def _recent_cci_reset(
    *,
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int,
    side: Side,
    lookback: int,
    reset_level: float,
) -> bool:
    if side == "NONE":
        return False
    limit = min(max(int(lookback), 1), max(len(closes) - period, 0))
    for back in range(1, limit + 1):
        end = len(closes) - back
        cci_hist = _cci(highs[:end], lows[:end], closes[:end], period)
        if cci_hist is None:
            continue
        if side == "LONG" and float(cci_hist) <= float(reset_level):
            return True
        if side == "SHORT" and float(cci_hist) >= -float(reset_level):
            return True
    return False


def _recent_width_context(
    *,
    closes: list[float],
    period: int,
    std_mult: float,
    lookback: int,
    percentile_q: float,
) -> dict[str, float] | None:
    widths: list[float] = []
    min_end = max(int(period), len(closes) - max(int(lookback), 3))
    for end in range(min_end, len(closes) + 1):
        width = bollinger_bandwidth(closes[:end], period, std_mult)
        if width is None:
            continue
        widths.append(float(width))
    if len(widths) < max(int(lookback), 3):
        return None
    recent = widths[-max(int(lookback), 3) :]
    history = recent[:-1]
    if len(history) < 2:
        return None
    recent_min = min(history)
    current_width = recent[-1]
    previous_width = recent[-2]
    threshold = _percentile(history, percentile_q)
    reexpand_ratio = current_width / max(recent_min, 1e-9)
    return {
        "recent_min": float(recent_min),
        "current_width": float(current_width),
        "previous_width": float(previous_width),
        "threshold": float(threshold),
        "reexpand_ratio": float(reexpand_ratio),
    }


def _band_location(close: float, lower: float, upper: float) -> float:
    width = float(upper) - float(lower)
    if width <= 1e-9:
        return 0.5
    return _clamp_score((float(close) - float(lower)) / width)


def _sma(values: list[float], period: int) -> float | None:
    if period <= 1 or len(values) < period:
        return None
    window = [float(v) for v in values[-period:]]
    return sum(window) / float(len(window))


class EBCV1Continuation(StrategyPlugin):
    name = "ebc_v1_continuation"

    def __init__(self, *, params: dict[str, Any] | None = None, logger: Any | None = None) -> None:
        self._cfg = EBCParams.from_params(params)
        self._logger = logger

    def set_runtime_params(self, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
        merged = self._cfg.__dict__.copy()
        merged.update(kwargs)
        self._cfg = EBCParams.from_params(merged)

    def decide(self, market_snapshot: dict[str, Any]) -> dict[str, Any]:
        symbol = str(market_snapshot.get("symbol") or "").strip().upper()
        if symbol not in self._cfg.supported_symbols:
            return DesiredPosition(symbol=symbol or "BTCUSDT", reason="unsupported_symbol").to_dict()
        market = market_snapshot.get("market")
        if not isinstance(market, dict):
            return DesiredPosition(symbol=symbol, reason="missing_market").to_dict()

        f12h = _frame(market.get("12h"))
        f2h = _frame(market.get("2h"))
        f30m = _frame(market.get("30m"))
        f5m = _frame(market.get("5m"))
        if not all((f12h, f2h, f30m, f5m)):
            return DesiredPosition(symbol=symbol, reason="insufficient_multitimeframe_data").to_dict()

        closes_12h, highs_12h, lows_12h = f12h
        closes_2h, highs_2h, lows_2h = f2h
        closes_30m, highs_30m, lows_30m = f30m
        closes_5m, highs_5m, lows_5m = f5m
        bars_5m = _to_bars(market.get("5m"))
        volumes_5m = [float(bar.volume or 0.0) for bar in bars_5m]

        ema_12h = ema(closes_12h, self._cfg.ema_period_12h)
        ema_2h = ema(closes_2h, self._cfg.ema_period_2h)
        ema_30m = ema(closes_30m, self._cfg.ema_period_30m)
        ema_5m = ema(closes_5m, self._cfg.ema_period_5m)
        slope_12h = _ema_slope_frac(closes_12h, self._cfg.ema_period_12h)
        slope_2h = _ema_slope_frac(closes_2h, self._cfg.ema_period_2h)
        cci_2h = _cci(highs_2h, lows_2h, closes_2h, self._cfg.cci_period_2h)
        cci_5m = _cci(highs_5m, lows_5m, closes_5m, self._cfg.cci_period_5m)
        bands_30m = _bollinger_bands(closes_30m, self._cfg.bb_period_30m, self._cfg.bb_std_30m)
        width_ctx = _recent_width_context(
            closes=closes_30m,
            period=self._cfg.bb_period_30m,
            std_mult=self._cfg.bb_std_30m,
            lookback=self._cfg.squeeze_lookback_30m,
            percentile_q=self._cfg.squeeze_percentile_threshold_30m,
        )

        if None in {ema_12h, ema_2h, ema_30m, ema_5m, cci_2h, cci_5m, slope_12h, slope_2h} or bands_30m is None:
            return DesiredPosition(symbol=symbol, reason="indicator_unavailable").to_dict()
        if width_ctx is None:
            return DesiredPosition(symbol=symbol, reason="indicator_unavailable").to_dict()

        lower_30m, mid_30m, upper_30m = bands_30m
        width_frac_30m = float(width_ctx["current_width"])
        band_location = _band_location(float(closes_30m[-1]), float(lower_30m), float(upper_30m))
        regime_side: Side
        if float(closes_12h[-1]) >= float(ema_12h) and float(slope_12h) >= float(self._cfg.regime_min_slope_12h):
            regime_side = "LONG"
        elif float(closes_12h[-1]) <= float(ema_12h) and float(slope_12h) <= -float(self._cfg.regime_min_slope_12h):
            regime_side = "SHORT"
        else:
            regime_side = "NONE"
        regime_strength = _clamp_score(
            (
                min(abs(float(slope_12h)) / max(float(self._cfg.regime_min_slope_12h), 1e-9), 1.0) * 0.5
                + min(abs(float(closes_12h[-1]) - float(ema_12h)) / max(abs(float(ema_12h)) * 0.01, 1e-9), 1.0)
                * 0.5
            )
        )
        bias_side: Side
        bias_distance_frac_2h = abs(float(closes_2h[-1]) - float(ema_2h)) / max(abs(float(ema_2h)), 1e-9)
        if (
            float(closes_2h[-1]) >= float(ema_2h)
            and float(cci_2h) >= float(self._cfg.cci_pullback_floor)
            and float(slope_2h) >= float(self._cfg.bias_min_slope_2h)
            and float(bias_distance_frac_2h) <= float(self._cfg.bias_max_distance_frac_2h)
            and abs(float(cci_2h)) <= float(self._cfg.bias_cci_extreme_cap)
        ):
            bias_side = "LONG"
        elif (
            float(closes_2h[-1]) <= float(ema_2h)
            and float(cci_2h) <= -float(self._cfg.cci_pullback_floor)
            and float(slope_2h) <= -float(self._cfg.bias_min_slope_2h)
            and float(bias_distance_frac_2h) <= float(self._cfg.bias_max_distance_frac_2h)
            and abs(float(cci_2h)) <= float(self._cfg.bias_cci_extreme_cap)
        ):
            bias_side = "SHORT"
        else:
            bias_side = "NONE"

        if regime_side == "NONE" or bias_side == "NONE" or regime_side != bias_side:
            return DesiredPosition(symbol=symbol, reason="bias_missing").to_dict()
        if str(self._cfg.side_mode) != "BOTH" and str(bias_side) != str(self._cfg.side_mode):
            return DesiredPosition(
                symbol=symbol,
                reason="side_not_allowed",
                regime="BULL" if bias_side == "LONG" else "BEAR",
                allowed_side=str(self._cfg.side_mode),
                blocks=["side_mode"],
            ).to_dict()

        recent_pullback = _recent_touch(
            highs=highs_30m,
            lows=lows_30m,
            closes=closes_30m,
            ema_period=self._cfg.ema_period_30m,
            side=bias_side,
            lookback=self._cfg.recent_pullback_lookback_30m,
        )
        squeeze_ready = float(width_ctx["recent_min"]) <= float(width_ctx["threshold"])
        reexpand_ready = (
            float(width_ctx["current_width"]) <= float(self._cfg.squeeze_width_frac_max)
            and float(width_ctx["reexpand_ratio"]) >= float(self._cfg.reexpand_ratio_min_30m)
            and float(width_ctx["current_width"]) >= float(width_ctx["previous_width"])
        )
        if not squeeze_ready or not reexpand_ready:
            return DesiredPosition(symbol=symbol, reason="setup_missing").to_dict()

        if bias_side == "LONG":
            setup_ok = (
                recent_pullback
                and float(closes_30m[-1]) >= float(ema_30m)
                and float(band_location) >= float(self._cfg.band_location_min_long)
                and float(band_location) <= float(self._cfg.band_location_max_long)
            )
        else:
            setup_ok = (
                recent_pullback
                and float(closes_30m[-1]) <= float(ema_30m)
                and float(band_location) >= float(self._cfg.band_location_min_short)
                and float(band_location) <= float(self._cfg.band_location_max_short)
            )

        if not setup_ok:
            return DesiredPosition(symbol=symbol, reason="setup_missing").to_dict()

        chase_frac_5m = abs(float(closes_5m[-1]) - float(ema_5m)) / max(abs(float(ema_5m)), 1e-9)
        reclaim_5m = _recent_touch(
            highs=highs_5m,
            lows=lows_5m,
            closes=closes_5m,
            ema_period=self._cfg.ema_period_5m,
            side=bias_side,
            lookback=self._cfg.trigger_cross_lookback_5m,
        )
        cci_reset = _recent_cci_reset(
            highs=highs_5m,
            lows=lows_5m,
            closes=closes_5m,
            period=self._cfg.cci_period_5m,
            side=bias_side,
            lookback=self._cfg.trigger_cross_lookback_5m,
            reset_level=self._cfg.trigger_cci_reset_level,
        )
        if bias_side == "LONG":
            trigger_ok = (
                reclaim_5m
                and cci_reset
                and float(closes_5m[-1]) >= float(ema_5m)
                and float(cci_5m) >= float(self._cfg.cci_trigger_level)
                and float(closes_5m[-1]) > float(closes_5m[-2])
                and float(chase_frac_5m) <= float(self._cfg.trigger_max_chase_frac_5m)
            )
        else:
            trigger_ok = (
                reclaim_5m
                and cci_reset
                and float(closes_5m[-1]) <= float(ema_5m)
                and float(cci_5m) <= -float(self._cfg.cci_trigger_level)
                and float(closes_5m[-1]) < float(closes_5m[-2])
                and float(chase_frac_5m) <= float(self._cfg.trigger_max_chase_frac_5m)
            )

        if not trigger_ok:
            return DesiredPosition(symbol=symbol, reason="trigger_missing").to_dict()

        entry_price = float(closes_5m[-1])
        stop_price = (
            float(entry_price) * (1.0 - float(self._cfg.stop_loss_pct))
            if bias_side == "LONG"
            else float(entry_price) * (1.0 + float(self._cfg.stop_loss_pct))
        )
        stop_distance_frac = abs(float(entry_price) - float(stop_price)) / max(abs(float(entry_price)), 1e-9)
        bias_strength = _clamp_score(
            (
                min(abs(float(slope_2h)) / max(float(self._cfg.bias_min_slope_2h), 1e-9), 1.0) * 0.35
            )
            + (min(abs(float(closes_2h[-1]) - float(ema_2h)) / max(abs(float(ema_2h)) * 0.005, 1e-9), 1.0) * 0.35)
            + (min(abs(float(cci_2h)) / 200.0, 1.0) * 0.30)
        )
        squeeze_quality = _clamp_score(
            1.0 - (float(width_ctx["recent_min"]) / max(float(width_ctx["threshold"]), 1e-9))
        )
        reexpand_quality = _clamp_score(
            (float(width_ctx["reexpand_ratio"]) - float(self._cfg.reexpand_ratio_min_30m))
            / max(float(self._cfg.reexpand_ratio_min_30m), 1e-9)
        )
        trigger_strength = _clamp_score(
            (min(abs(float(cci_5m)) / 200.0, 1.0) * 0.6)
            + (_clamp_score(1.0 - (float(chase_frac_5m) / max(float(self._cfg.trigger_max_chase_frac_5m), 1e-9))) * 0.4)
        )
        volume_sma_5m = _sma(volumes_5m, self._cfg.volume_sma_period_5m)
        volume_ratio_5m = (
            float(volumes_5m[-1]) / max(float(volume_sma_5m), 1e-9)
            if volume_sma_5m is not None and volume_sma_5m > 0.0
            else 1.0
        )
        structure_quality = _clamp_score(
            (float(squeeze_quality) * 0.4)
            + (float(reexpand_quality) * 0.35)
            + (float(band_location) * 0.25 if bias_side == "LONG" else (1.0 - float(band_location)) * 0.25)
        )
        raw_score = float(
            _clamp_score(
                (float(regime_strength) * 0.25)
                + (float(bias_strength) * 0.25)
                + (float(structure_quality) * 0.30)
                + (float(trigger_strength) * 0.20)
            )
        )
        extension_risk = _clamp_score(
            (
                (
                    max(float(band_location) - 0.78, 0.0)
                    if bias_side == "LONG"
                    else max(0.22 - float(band_location), 0.0)
                )
                / 0.22
            )
            * 0.45
            + min(abs(float(cci_5m)) / 250.0, 1.0) * 0.35
            + min(float(chase_frac_5m) / max(float(self._cfg.trigger_max_chase_frac_5m), 1e-9), 1.0) * 0.20
        )
        score = float(raw_score)
        portfolio_score = float(
            raw_score
            + (float(regime_strength) * float(self._cfg.rotation_regime_weight))
            + (float(bias_strength) * float(self._cfg.rotation_bias_weight))
            + (float(trigger_strength) * float(self._cfg.rotation_trigger_weight))
        )
        take_profit = (
            float(entry_price) + (float(entry_price) - float(stop_price)) * float(self._cfg.take_profit_r)
            if bias_side == "LONG"
            else float(entry_price) - (float(stop_price) - float(entry_price)) * float(self._cfg.take_profit_r)
        )
        indicators = {
            "ema_12h": float(ema_12h),
            "ema_2h": float(ema_2h),
            "ema_30m": float(ema_30m),
            "ema_5m": float(ema_5m),
            "cci_2h": float(cci_2h),
            "cci_5m": float(cci_5m),
            "bb_width_frac_30m": float(width_frac_30m),
            "bb_recent_min_width_frac_30m": float(width_ctx["recent_min"]),
            "bb_squeeze_threshold_30m": float(width_ctx["threshold"]),
            "bb_reexpand_ratio_30m": float(width_ctx["reexpand_ratio"]),
            "band_location_30m": float(band_location),
            "bias_distance_frac_2h": float(bias_distance_frac_2h),
            "regime_strength": float(regime_strength),
            "bias_strength": float(bias_strength),
            "structure_quality": float(structure_quality),
            "trigger_strength": float(trigger_strength),
            "extension_risk": float(extension_risk),
            "chase_frac_5m": float(chase_frac_5m),
            "volume_ratio_15m": float(volume_ratio_5m),
            "close_30m": float(closes_30m[-1]),
            "ema20_30m": float(ema_30m),
            "slope_12h": float(slope_12h),
            "slope_2h": float(slope_2h),
        }
        regime = "BULL" if bias_side == "LONG" else "BEAR"
        allowed_side = "LONG" if bias_side == "LONG" else "SHORT"
        if float(score) < float(self._cfg.min_entry_score):
            blocked_payload = DesiredPosition(
                symbol=symbol,
                score=float(score),
                reason="quality_score_below_min",
                regime=regime,
                allowed_side=allowed_side,
                indicators=indicators,
                blocks=["min_entry_score"],
            ).to_dict()
            blocked_payload["raw_score"] = float(raw_score)
            blocked_payload["portfolio_score"] = float(portfolio_score)
            return blocked_payload
        payload = dict(
            DesiredPosition(
                symbol=symbol,
                intent="LONG" if bias_side == "LONG" else "SHORT",
                side="BUY" if bias_side == "LONG" else "SELL",
                score=float(raw_score),
                reason="entry_signal",
                entry_price=float(entry_price),
                stop_hint=float(stop_price),
                management_hint="ebc_continuation",
                regime=regime,
                allowed_side=allowed_side,
                signals={"ebc_v1_continuation": True},
                blocks=[],
                indicators=indicators,
            ).to_dict()
        )
        payload["entry_family"] = "ebc_continuation"
        payload["alpha_id"] = "alpha_ebc"
        payload["raw_score"] = float(raw_score)
        payload["portfolio_score"] = float(portfolio_score)
        payload["regime_strength"] = float(regime_strength)
        payload["risk_per_trade_pct"] = float(self._cfg.risk_per_trade_pct)
        payload["max_effective_leverage"] = float(self._cfg.max_effective_leverage)
        payload["stop_price_hint"] = float(stop_price)
        payload["stop_distance_frac"] = float(stop_distance_frac)
        payload["sl_tp"] = {
            "take_profit": float(take_profit),
            "stop_loss": float(stop_price),
        }
        payload["execution"] = {
            "reward_risk_reference_r": float(self._cfg.take_profit_r),
            "time_stop_bars": int(self._cfg.time_stop_bars),
            "progress_check_bars": int(self._cfg.progress_check_bars),
            "progress_min_mfe_r": float(self._cfg.progress_min_mfe_r),
            "progress_extend_trigger_r": float(self._cfg.progress_extend_trigger_r),
            "progress_extend_bars": int(self._cfg.progress_extend_bars),
            "entry_quality_score_v2": float(score),
            "entry_regime_strength": float(regime_strength),
            "entry_bias_strength": float(bias_strength),
            "quality_exit_applied": True,
            "selective_extension_proof_bars": int(self._cfg.selective_extension_proof_bars),
            "selective_extension_min_mfe_r": float(self._cfg.selective_extension_min_mfe_r),
            "selective_extension_min_regime_strength": float(
                self._cfg.selective_extension_min_regime_strength
            ),
            "selective_extension_min_bias_strength": float(
                self._cfg.selective_extension_min_bias_strength
            ),
            "selective_extension_min_quality_score_v2": float(
                self._cfg.selective_extension_min_quality_score_v2
            ),
            "selective_extension_time_stop_bars": int(
                self._cfg.selective_extension_time_stop_bars
            ),
            "selective_extension_take_profit_r": float(
                self._cfg.selective_extension_take_profit_r
            ),
            "selective_extension_move_stop_to_be_at_r": float(
                self._cfg.selective_extension_move_stop_to_be_at_r
            ),
            "stalled_trend_timeout_bars": int(self._cfg.stalled_trend_timeout_bars),
            "stalled_volume_ratio_floor": float(self._cfg.stalled_volume_ratio_floor),
            "rotation_score": float(portfolio_score),
            "stop_exit_cooldown_bars": int(self._cfg.stop_exit_cooldown_bars),
            "profit_exit_cooldown_bars": int(self._cfg.profit_exit_cooldown_bars),
        }
        return payload


class EBCV1ContinuationCandidateSelector(CandidateSelector):
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
        self._symbols = [str(sym).strip().upper() for sym in symbols if str(sym).strip()] or ["BTCUSDT"]
        self._snapshot_provider = snapshot_provider
        self._overheat_fetcher = overheat_fetcher
        self._journal_logger = journal_logger
        self._last_no_candidate_reason: str | None = None
        self._last_no_candidate_context: dict[str, Any] | None = None
        self._sync_strategy_supported_symbols()

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

    def get_last_no_candidate_reason(self) -> str | None:
        return self._last_no_candidate_reason

    def get_last_no_candidate_context(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._last_no_candidate_context)

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

    @staticmethod
    def _candidate_from_decision(symbol: str, decision: dict[str, Any]) -> Candidate | None:
        intent = str(decision.get("intent") or "NONE").upper()
        side = str(decision.get("side") or "NONE").upper()
        if intent not in {"LONG", "SHORT"} or side not in {"BUY", "SELL"}:
            return None
        score = _to_float(decision.get("score")) or 0.0
        entry_price = _to_float(decision.get("entry_price")) or 0.0
        if score <= 0.0 or entry_price <= 0.0:
            return None
        indicators = decision.get("indicators")
        regime_strength = _to_float(decision.get("regime_strength"))
        if regime_strength is None and isinstance(indicators, dict):
            regime_strength = _to_float(indicators.get("regime_strength"))
        return Candidate(
            symbol=symbol,
            side="BUY" if side == "BUY" else "SELL",
            score=float(score),
            raw_score=_to_float(decision.get("raw_score")),
            portfolio_score=_to_float(decision.get("portfolio_score")),
            alpha_id=str(decision.get("alpha_id") or "").strip() or None,
            entry_family=str(decision.get("entry_family") or "").strip() or None,
            reason=str(decision.get("reason") or "entry_signal"),
            source="ebc_v1_continuation",
            entry_price=float(entry_price),
            stop_price_hint=_to_float(decision.get("stop_price_hint")),
            stop_distance_frac=_to_float(decision.get("stop_distance_frac")),
            regime_hint=str(decision.get("regime") or "").strip().upper() or None,
            regime_strength=regime_strength,
            risk_per_trade_pct=_to_float(decision.get("risk_per_trade_pct")),
            max_effective_leverage=_to_float(decision.get("max_effective_leverage")),
        )

    def rank(self, *, context: KernelContext) -> list[Candidate]:
        _ = context
        self._last_no_candidate_reason = None
        self._last_no_candidate_context = None
        base_snapshot: dict[str, Any] = {"symbol": self._symbols[0]}
        if self._snapshot_provider is not None:
            provided = self._snapshot_provider()
            if isinstance(provided, dict):
                base_snapshot.update(provided)
        symbols_market = base_snapshot.get("symbols")
        ranked_pairs: list[tuple[Candidate, dict[str, Any]]] = []
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
            if self._journal_logger is not None and isinstance(decision, dict):
                self._journal_logger(decision)
            if not isinstance(decision, dict):
                skipped[symbol] = "invalid_decision"
                continue
            candidate = self._candidate_from_decision(symbol, decision)
            if candidate is not None:
                ranked_pairs.append((candidate, decision))
                continue
            skipped[symbol] = str(decision.get("reason") or "no_candidate")
            skipped_context[symbol] = {
                "reason": str(decision.get("reason") or "no_candidate"),
                "blocks": copy.deepcopy(decision.get("blocks") or []),
            }
        if ranked_pairs:
            raw_scores = [float(candidate.score) for candidate, _ in ranked_pairs]
            min_score = min(raw_scores)
            max_score = max(raw_scores)
            spread = max(max_score - min_score, 1e-9)
            adjusted: list[Candidate] = []
            cfg = getattr(self._strategy, "_cfg", None)
            relative_weight = float(getattr(cfg, "rotation_relative_weight", 0.0) or 0.0)
            extension_penalty_weight = float(
                getattr(cfg, "rotation_extension_penalty_weight", 0.0) or 0.0
            )
            for candidate, decision in ranked_pairs:
                indicators = decision.get("indicators") if isinstance(decision, dict) else {}
                if not isinstance(indicators, dict):
                    indicators = {}
                relative_advantage = (
                    (float(candidate.score) - float(min_score)) / float(spread)
                    if len(ranked_pairs) > 1
                    else 1.0
                )
                extension_risk = _to_float(indicators.get("extension_risk")) or 0.0
                adjusted_score = float(
                    (candidate.portfolio_score if candidate.portfolio_score is not None else candidate.score)
                    + (relative_advantage * relative_weight)
                    - (float(extension_risk) * extension_penalty_weight)
                )
                adjusted.append(replace(candidate, portfolio_score=adjusted_score))
            ranked = adjusted
        else:
            ranked = []

        ranked.sort(
            key=lambda item: (
                float(item.portfolio_score if item.portfolio_score is not None else item.score),
                float(item.raw_score if item.raw_score is not None else item.score),
                float(item.regime_strength or 0.0),
            ),
            reverse=True,
        )
        if ranked:
            return ranked
        if skipped:
            reasons = sorted({reason for reason in skipped.values() if reason})
            self._last_no_candidate_reason = reasons[0] if len(reasons) == 1 else "no_candidate_multi"
            self._last_no_candidate_context = {"symbols": skipped_context}
        return []

    def select(self, *, context: KernelContext) -> Candidate | None:
        ranked = self.rank(context=context)
        return ranked[0] if ranked else None
