from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bars(raw: Any) -> list[_Bar]:
    rows: list[_Bar] = []
    if not isinstance(raw, list):
        return rows
    if raw and isinstance(raw[0], _Bar):
        return raw
    for item in raw:
        if isinstance(item, dict):
            o = _to_float(item.get("open"))
            h = _to_float(item.get("high"))
            low = _to_float(item.get("low"))
            c = _to_float(item.get("close"))
            vol = _to_float(item.get("volume"))
        elif isinstance(item, (list, tuple)) and len(item) >= 5:
            o = _to_float(item[1])
            h = _to_float(item[2])
            low = _to_float(item[3])
            c = _to_float(item[4])
            vol = _to_float(item[5]) if len(item) >= 6 else None
        else:
            continue
        if o is None or h is None or low is None or c is None:
            continue
        rows.append(_Bar(open=o, high=h, low=low, close=c, volume=vol))
    return rows


def ema(values: list[float], period: int) -> float | None:
    if period <= 1:
        return float(values[-1]) if values else None
    if len(values) < period:
        return None
    alpha = 2.0 / (float(period) + 1.0)
    value = float(values[0])
    for item in values[1:]:
        value = (alpha * float(item)) + ((1.0 - alpha) * value)
    return value


def atr(bars: list[_Bar], period: int) -> float | None:
    if period <= 1 or len(bars) < period + 1:
        return None
    tr_values: list[float] = []
    prev_close = bars[0].close
    for bar in bars[1:]:
        tr = max(
            float(bar.high) - float(bar.low),
            abs(float(bar.high) - prev_close),
            abs(float(bar.low) - prev_close),
        )
        tr_values.append(tr)
        prev_close = bar.close
    if len(tr_values) < period:
        return None
    return sum(tr_values[-period:]) / float(period)


def adx(bars: list[_Bar], period: int) -> float | None:
    if period <= 1 or len(bars) < period + 1:
        return None

    tr_values: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    prev = bars[0]

    for bar in bars[1:]:
        up_move = float(bar.high) - float(prev.high)
        down_move = float(prev.low) - float(bar.low)
        plus_dm.append(up_move if up_move > down_move and up_move > 0.0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0.0 else 0.0)
        tr = max(
            float(bar.high) - float(bar.low),
            abs(float(bar.high) - float(prev.close)),
            abs(float(bar.low) - float(prev.close)),
        )
        tr_values.append(tr)
        prev = bar

    if len(tr_values) < period:
        return None

    atr_seed = sum(tr_values[:period]) / float(period)
    plus_seed = sum(plus_dm[:period]) / float(period)
    minus_seed = sum(minus_dm[:period]) / float(period)
    if atr_seed <= 0.0:
        return 0.0

    def _dx(atr_value: float, plus_value: float, minus_value: float) -> float:
        if atr_value <= 0.0:
            return 0.0
        plus_di = 100.0 * (plus_value / atr_value)
        minus_di = 100.0 * (minus_value / atr_value)
        denom = plus_di + minus_di
        if denom <= 0.0:
            return 0.0
        return abs(plus_di - minus_di) / denom * 100.0

    atr_smooth = atr_seed
    plus_smooth = plus_seed
    minus_smooth = minus_seed

    dx_values: list[float] = [_dx(atr_smooth, plus_smooth, minus_smooth)]
    for idx in range(period, len(tr_values)):
        atr_smooth = ((atr_smooth * (period - 1)) + tr_values[idx]) / float(period)
        plus_smooth = ((plus_smooth * (period - 1)) + plus_dm[idx]) / float(period)
        minus_smooth = ((minus_smooth * (period - 1)) + minus_dm[idx]) / float(period)
        dx_values.append(_dx(atr_smooth, plus_smooth, minus_smooth))

    if not dx_values:
        return None
    return sum(dx_values[-period:]) / float(min(period, len(dx_values)))


def rsi(closes: list[float], period: int) -> float | None:
    if period <= 1 or len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for idx in range(-period, 0):
        delta = float(closes[idx]) - float(closes[idx - 1])
        if delta >= 0.0:
            gains += delta
        else:
            losses += -delta
    avg_gain = gains / float(period)
    avg_loss = losses / float(period)
    if avg_loss <= 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def bollinger_bandwidth(closes: list[float], period: int, std_mult: float) -> float | None:
    if period <= 1 or len(closes) < period:
        return None
    window = [float(v) for v in closes[-period:]]
    mean = sum(window) / float(period)
    if mean == 0.0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in window) / float(period)
    deviation = math.sqrt(max(variance, 0.0))
    upper = mean + (float(std_mult) * deviation)
    lower = mean - (float(std_mult) * deviation)
    return (upper - lower) / abs(mean)


def donchian(bars: list[_Bar], period: int = 20) -> tuple[float, float] | None:
    if period <= 1 or len(bars) < period:
        return None
    window = bars[-period:]
    upper = max(float(row.high) for row in window)
    lower = min(float(row.low) for row in window)
    return upper, lower


def _sma(values: list[float], period: int) -> float | None:
    if period <= 1 or len(values) < period:
        return None
    window = [float(v) for v in values[-period:]]
    return sum(window) / float(len(window))


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(float(v) for v in values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = (len(sorted_values) - 1) * min(max(float(q), 0.0), 1.0)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_values[lo]
    ratio = idx - lo
    return (sorted_values[lo] * (1.0 - ratio)) + (sorted_values[hi] * ratio)


def _clamp_score(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(float(value), high))


def expected_move_gate(
    *,
    atr_15m: float,
    close_15m: float,
    taker_fee: float,
    slippage_bps: float,
    spread_estimate_bps: float,
    spread_limit_bps: float,
    min_expected_move_floor: float,
    expected_move_cost_mult: float,
) -> tuple[bool, float, float]:
    if close_15m <= 0.0:
        return False, 0.0, 0.0
    move = abs(float(atr_15m)) / float(close_15m)
    roundtrip_cost = (2.0 * float(taker_fee)) + (2.0 * (float(slippage_bps) / 10000.0))
    required = max(float(min_expected_move_floor), float(expected_move_cost_mult) * roundtrip_cost)
    if float(spread_estimate_bps) > float(spread_limit_bps):
        return False, move, required
    return move >= required, move, required


def _spread_bps_from_bar(bar: _Bar, *, fallback_bps: float) -> float:
    _ = bar
    return max(float(fallback_bps), 0.0)
