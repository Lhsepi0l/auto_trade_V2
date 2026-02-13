from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from apps.trader_engine.services.market_data_service import Candle


VolTag = Literal["NORMAL", "VOL_SHOCK"]


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _ema(values: Sequence[float], period: int) -> Optional[float]:
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


def _rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    n = int(period)
    if n <= 1 or len(values) < (n + 1):
        return None
    gains = 0.0
    losses = 0.0
    # Simple RSI (not Wilder's smoothing) is sufficient for MVP scoring.
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


def _atr_pct(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    n = int(period)
    if n <= 1 or len(candles) < (n + 1):
        return None
    trs: List[float] = []
    prev_close = float(candles[0].close)
    for c in candles[1 : n + 1]:
        hi = float(c.high)
        lo = float(c.low)
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(float(tr))
        prev_close = float(c.close)
    atr = sum(trs) / float(n) if trs else 0.0
    last_close = float(candles[n].close)
    if last_close <= 0:
        return None
    return (atr / last_close) * 100.0


@dataclass(frozen=True)
class TimeframeScore:
    interval: str
    trend_score: float
    momentum_score: float
    atr_pct: float
    vol_tag: VolTag
    composite: float


@dataclass(frozen=True)
class SymbolScores:
    symbol: str
    long_score: float
    short_score: float
    vol_tag: VolTag
    composite: float
    timeframes: Dict[str, TimeframeScore]


class DecisionService:
    """Lightweight, practical scoring model for MVP scheduling decisions.

    Scoring conventions:
    - composite score in [-1, 1]
      +1 means strongly long-biased, -1 strongly short-biased
    - long_score = max(composite, 0), short_score = max(-composite, 0)
    """

    def __init__(
        self,
        *,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        atr_period: int = 14,
        vol_shock_threshold_pct: float = 2.0,
        weights: Optional[Mapping[str, float]] = None,
    ) -> None:
        self._ema_fast = int(ema_fast)
        self._ema_slow = int(ema_slow)
        self._rsi_period = int(rsi_period)
        self._atr_period = int(atr_period)
        self._vol_shock_threshold_pct = float(vol_shock_threshold_pct)
        self._weights = dict(weights or {"30m": 0.5, "1h": 0.3, "4h": 0.2})

    def score_symbol(
        self,
        *,
        symbol: str,
        candles_by_interval: Mapping[str, Sequence[Candle]],
    ) -> SymbolScores:
        sym = symbol.strip().upper()
        tfs: Dict[str, TimeframeScore] = {}

        # Normalize weights to sum to 1 (only for present intervals).
        present: List[Tuple[str, float]] = []
        for itv, w in self._weights.items():
            if itv in candles_by_interval:
                present.append((itv, float(w)))
        wsum = sum(w for _, w in present) or 1.0

        combined = 0.0
        worst_vol: VolTag = "NORMAL"
        for itv, w in present:
            candles = list(candles_by_interval.get(itv) or [])
            closes = [float(c.close) for c in candles if float(c.close) > 0]
            if len(closes) < max(self._ema_slow, self._rsi_period + 1, self._atr_period + 1):
                # Not enough data; treat as neutral.
                tf = TimeframeScore(
                    interval=itv,
                    trend_score=0.0,
                    momentum_score=0.0,
                    atr_pct=0.0,
                    vol_tag="NORMAL",
                    composite=0.0,
                )
                tfs[itv] = tf
                continue

            ema_fast = _ema(closes[-(self._ema_slow * 3) :], self._ema_fast)  # extra history for stability
            ema_slow = _ema(closes[-(self._ema_slow * 3) :], self._ema_slow)
            if ema_fast is None or ema_slow is None or ema_slow <= 0:
                trend_score = 0.0
            else:
                # Trend is relative EMA spread; normalized to [-1, 1].
                rel = (ema_fast - ema_slow) / ema_slow
                trend_score = _clamp(rel * 50.0, -1.0, 1.0)

            rsi = _rsi(closes[-(self._rsi_period + 1) :], self._rsi_period)
            if rsi is None:
                momentum_score = 0.0
            else:
                momentum_score = _clamp((float(rsi) - 50.0) / 50.0, -1.0, 1.0)

            atr_pct = _atr_pct(candles[-(self._atr_period + 1) :], self._atr_period) or 0.0
            vol_tag: VolTag = "VOL_SHOCK" if atr_pct >= self._vol_shock_threshold_pct else "NORMAL"
            if vol_tag == "VOL_SHOCK":
                worst_vol = "VOL_SHOCK"

            # Composite per timeframe: trend dominates, momentum adds confirmation.
            comp = _clamp(0.65 * trend_score + 0.35 * momentum_score, -1.0, 1.0)
            tfs[itv] = TimeframeScore(
                interval=itv,
                trend_score=float(trend_score),
                momentum_score=float(momentum_score),
                atr_pct=float(atr_pct),
                vol_tag=vol_tag,
                composite=float(comp),
            )
            combined += (w / wsum) * float(comp)

        combined = _clamp(float(combined), -1.0, 1.0)
        long_score = max(combined, 0.0)
        short_score = max(-combined, 0.0)
        return SymbolScores(
            symbol=sym,
            long_score=float(long_score),
            short_score=float(short_score),
            vol_tag=worst_vol,
            composite=float(combined),
            timeframes=tfs,
        )

    def pick_candidate(
        self,
        *,
        scores: Sequence[SymbolScores],
        score_threshold: float,
    ) -> Optional[Mapping[str, object]]:
        th = float(score_threshold)
        best = None
        best_strength = 0.0
        for s in scores:
            if s.vol_tag == "VOL_SHOCK":
                continue
            strength = max(float(s.long_score), float(s.short_score))
            if strength < th:
                continue
            if strength > best_strength:
                direction = "LONG" if s.long_score >= s.short_score else "SHORT"
                best = {
                    "symbol": s.symbol,
                    "direction": direction,
                    "strength": float(strength),
                    "composite": float(s.composite),
                    "vol_tag": s.vol_tag,
                }
                best_strength = float(strength)
        return best

