from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Literal


Side = Literal["LONG", "SHORT", "NONE"]


def ema(values: list[float], period: int) -> float | None:
    if period <= 1:
        return float(values[-1]) if values else None
    if len(values) < period:
        return None
    alpha = 2.0 / (float(period) + 1.0)
    current = float(values[0])
    for item in values[1:]:
        current = (alpha * float(item)) + ((1.0 - alpha) * current)
    return current


def stddev(values: list[float]) -> float:
    if not values:
        return 0.0
    avg = mean(values)
    variance = sum((float(item) - float(avg)) ** 2 for item in values) / float(len(values))
    return variance**0.5


def bollinger_bands(closes: list[float], period: int, std_mult: float) -> tuple[float, float, float] | None:
    if period <= 1 or len(closes) < period:
        return None
    window = [float(item) for item in closes[-period:]]
    mid = mean(window)
    dev = stddev(window)
    upper = float(mid) + (float(std_mult) * float(dev))
    lower = float(mid) - (float(std_mult) * float(dev))
    return float(lower), float(mid), float(upper)


def cci(highs: list[float], lows: list[float], closes: list[float], period: int) -> float | None:
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
class EBCFrame:
    closes: list[float]
    highs: list[float]
    lows: list[float]


@dataclass(frozen=True)
class EBCParams:
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
    squeeze_width_frac_max: float = 0.035


def evaluate_12h_regime(frame: EBCFrame, *, params: EBCParams) -> Side:
    ema_now = ema(frame.closes, params.ema_period_12h)
    if ema_now is None:
        return "NONE"
    return "LONG" if float(frame.closes[-1]) >= float(ema_now) else "SHORT"


def evaluate_2h_bias(frame: EBCFrame, *, params: EBCParams) -> Side:
    ema_now = ema(frame.closes, params.ema_period_2h)
    cci_now = cci(frame.highs, frame.lows, frame.closes, params.cci_period_2h)
    if ema_now is None or cci_now is None:
        return "NONE"
    close_now = float(frame.closes[-1])
    if close_now >= float(ema_now) and float(cci_now) >= float(params.cci_pullback_floor):
        return "LONG"
    if close_now <= float(ema_now) and float(cci_now) <= -float(params.cci_pullback_floor):
        return "SHORT"
    return "NONE"


def evaluate_30m_setup(frame: EBCFrame, *, side: Side, params: EBCParams) -> bool:
    bands = bollinger_bands(frame.closes, params.bb_period_30m, params.bb_std_30m)
    ema_now = ema(frame.closes, params.ema_period_30m)
    if bands is None or ema_now is None or side == "NONE":
        return False
    lower, mid, upper = bands
    width_frac = (float(upper) - float(lower)) / max(abs(float(mid)), 1e-9)
    close_now = float(frame.closes[-1])
    if float(width_frac) > float(params.squeeze_width_frac_max):
        return False
    if side == "LONG":
        return close_now >= float(ema_now)
    return close_now <= float(ema_now)


def evaluate_5m_trigger(frame: EBCFrame, *, side: Side, params: EBCParams) -> bool:
    ema_now = ema(frame.closes, params.ema_period_5m)
    cci_now = cci(frame.highs, frame.lows, frame.closes, params.cci_period_5m)
    if ema_now is None or cci_now is None or side == "NONE":
        return False
    close_now = float(frame.closes[-1])
    if side == "LONG":
        return close_now >= float(ema_now) and float(cci_now) >= float(params.cci_trigger_level)
    return close_now <= float(ema_now) and float(cci_now) <= -float(params.cci_trigger_level)


def evaluate_ebc_continuation(
    *,
    frame_12h: EBCFrame,
    frame_2h: EBCFrame,
    frame_30m: EBCFrame,
    frame_5m: EBCFrame,
    params: EBCParams | None = None,
) -> Side:
    cfg = params or EBCParams()
    regime_side = evaluate_12h_regime(frame_12h, params=cfg)
    bias_side = evaluate_2h_bias(frame_2h, params=cfg)
    if regime_side == "NONE" or bias_side == "NONE" or regime_side != bias_side:
        return "NONE"
    if not evaluate_30m_setup(frame_30m, side=bias_side, params=cfg):
        return "NONE"
    if not evaluate_5m_trigger(frame_5m, side=bias_side, params=cfg):
        return "NONE"
    return bias_side


__all__ = [
    "EBCFrame",
    "EBCParams",
    "ema",
    "bollinger_bands",
    "cci",
    "evaluate_12h_regime",
    "evaluate_2h_bias",
    "evaluate_30m_setup",
    "evaluate_5m_trigger",
    "evaluate_ebc_continuation",
]
