from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from apps.trader_engine.services.market_data_service import Candle


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def ema(values: Sequence[float], period: int) -> Optional[float]:
    n = int(period)
    if n <= 1:
        return float(values[-1]) if values else None
    if len(values) < n:
        return None
    alpha = 2.0 / (n + 1.0)
    e = float(values[0])
    for v in values[1:]:
        e = alpha * float(v) + (1.0 - alpha) * e
    return float(e)


def rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    n = int(period)
    if n <= 1 or len(values) < (n + 1):
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, n + 1):
        d = float(values[i]) - float(values[i - 1])
        if d >= 0:
            gains += d
        else:
            losses += -d
    avg_gain = gains / n
    avg_loss = losses / n
    if avg_loss <= 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def roc(values: Sequence[float], period: int = 12) -> Optional[float]:
    n = int(period)
    if n <= 0 or len(values) <= n:
        return None
    prev = float(values[-(n + 1)])
    cur = float(values[-1])
    if prev <= 0:
        return None
    return (cur / prev) - 1.0


def _true_range(cur: Candle, prev_close: float) -> float:
    hi = float(cur.high)
    lo = float(cur.low)
    return max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))


def atr(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    n = int(period)
    if n <= 1 or len(candles) < (n + 1):
        return None
    prev_close = float(candles[-(n + 1)].close)
    trs: List[float] = []
    for c in candles[-n:]:
        tr = _true_range(c, prev_close)
        trs.append(float(tr))
        prev_close = float(c.close)
    return sum(trs) / float(n) if trs else None


def atr_pct(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    a = atr(candles, period=period)
    if a is None:
        return None
    last_close = float(candles[-1].close) if candles else 0.0
    if last_close <= 0:
        return None
    return (float(a) / last_close) * 100.0


def atr_pct_series(candles: Sequence[Candle], *, period: int = 14, window: int = 50) -> List[float]:
    """Compute a trailing series of ATR% values (latest last).

    This is intentionally simple (O(window*period)) for small windows.
    """
    w = int(window)
    n = int(period)
    if w <= 0 or n <= 1:
        return []
    if len(candles) < (n + 1):
        return []
    out: List[float] = []
    # For each endpoint, compute ATR% on the slice ending there.
    max_end = len(candles)
    # Ensure we can compute at least 1 value.
    min_end = (n + 1)
    ends: List[int] = list(range(min_end, max_end + 1))
    # Keep only last w endpoints.
    ends = ends[-w:]
    for end in ends:
        sl = candles[:end]
        v = atr_pct(sl, period=n)
        if v is None:
            continue
        out.append(float(v))
    return out


def mean(xs: Iterable[float]) -> Optional[float]:
    total = 0.0
    n = 0
    for x in xs:
        total += float(x)
        n += 1
    if n <= 0:
        return None
    return total / float(n)


@dataclass(frozen=True)
class AtrMult:
    atr_pct: float
    atr_pct_mean: float
    mult: float


def atr_mult(candles: Sequence[Candle], *, period: int = 14, mean_window: int = 50) -> Optional[AtrMult]:
    latest = atr_pct(candles, period=period)
    if latest is None:
        return None
    series = atr_pct_series(candles, period=period, window=mean_window)
    m = mean(series) if series else None
    if m is None or m <= 0:
        return AtrMult(atr_pct=float(latest), atr_pct_mean=0.0, mult=0.0)
    return AtrMult(atr_pct=float(latest), atr_pct_mean=float(m), mult=float(latest) / float(m))

