# FINAL-1 Output

Changed/added files for FINAL-1 (Strategy Core: Rotation Quant).

## apps/trader_engine/services/market_data_service.py

```python
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from apps.trader_engine.exchange.binance_usdm import BinanceUSDMClient
from shared.utils.retry import retry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int


def _as_float(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def _as_int(x: Any) -> int:
    try:
        return int(x)
    except Exception:
        return 0


def _parse_klines(rows: List[List[Any]]) -> List[Candle]:
    out: List[Candle] = []
    for r in rows:
        # Binance kline row layout:
        # [0] open_time, [1] open, [2] high, [3] low, [4] close, [5] volume,
        # [6] close_time, ...
        if len(r) < 7:
            continue
        out.append(
            Candle(
                open_time_ms=_as_int(r[0]),
                open=_as_float(r[1]),
                high=_as_float(r[2]),
                low=_as_float(r[3]),
                close=_as_float(r[4]),
                volume=_as_float(r[5]),
                close_time_ms=_as_int(r[6]),
            )
        )
    return out


class MarketDataService:
    """Minimal in-memory kline cache for scheduler/decision service.

    NOTE: Binance client uses synchronous requests; this service stays sync too.
    The scheduler should call it via asyncio.to_thread().
    """

    def __init__(
        self,
        *,
        client: BinanceUSDMClient,
        cache_ttl_sec: float = 20.0,
        retry_attempts: int = 3,
        retry_backoff_sec: float = 0.25,
    ) -> None:
        self._client = client
        self._cache_ttl_sec = float(cache_ttl_sec)
        self._retry_attempts = int(retry_attempts)
        self._retry_backoff_sec = float(retry_backoff_sec)
        # key: (symbol, interval, limit) -> (fetched_at_ms, candles)
        self._cache: Dict[Tuple[str, str, int], Tuple[int, List[Candle]]] = {}

    def get_klines(self, *, symbol: str, interval: str, limit: int = 200) -> List[Candle]:
        sym = symbol.strip().upper()
        itv = str(interval).strip()
        lim = int(limit)
        key = (sym, itv, lim)
        now_ms = int(time.time() * 1000)
        cached = self._cache.get(key)
        if cached:
            fetched_at_ms, candles = cached
            if (now_ms - fetched_at_ms) <= int(self._cache_ttl_sec * 1000):
                return list(candles)

        def _fetch():
            return self._client.get_klines(symbol=sym, interval=itv, limit=lim)

        rows = retry(_fetch, attempts=self._retry_attempts, base_delay_sec=self._retry_backoff_sec)
        candles = _parse_klines(rows)
        if not candles:
            logger.warning("klines_empty", extra={"symbol": sym, "interval": itv, "limit": lim})
        self._cache[key] = (now_ms, candles)
        return list(candles)

    def get_last_close(self, *, symbol: str, interval: str, limit: int = 2) -> Optional[float]:
        candles = self.get_klines(symbol=symbol, interval=interval, limit=limit)
        if not candles:
            return None
        return float(candles[-1].close)

```

## apps/trader_engine/services/indicators.py

```python
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


```

## apps/trader_engine/services/scoring_service.py

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence

from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.indicators import AtrMult, atr_mult, clamp, ema, mean, roc, rsi
from apps.trader_engine.services.market_data_service import Candle


Regime = Literal["BULL", "BEAR", "CHOPPY"]
Direction = Literal["LONG", "SHORT", "HOLD"]


def _regime_4h(*, ema_fast: Optional[float], ema_slow: Optional[float], rsi_v: Optional[float]) -> Regime:
    if ema_fast is None or ema_slow is None or ema_slow <= 0:
        return "CHOPPY"
    if rsi_v is None:
        rsi_v = 50.0
    # Very simple regime classifier: trend + momentum confirmation.
    if ema_fast > ema_slow and float(rsi_v) >= 55.0:
        return "BULL"
    if ema_fast < ema_slow and float(rsi_v) <= 45.0:
        return "BEAR"
    return "CHOPPY"


@dataclass(frozen=True)
class TimeframeIndicators:
    interval: str
    ema_fast: float
    ema_slow: float
    rsi: float
    roc: float
    atr_pct: float
    atr_pct_mean: float
    atr_mult: float
    regime_4h: Optional[Regime] = None


@dataclass(frozen=True)
class SymbolScore:
    symbol: str
    composite: float  # [-1, 1]
    long_score: float  # [0, 1]
    short_score: float  # [0, 1]
    regime_4h: Regime
    vol_shock: bool
    strength: float  # max(long, short)
    direction: Direction  # LONG/SHORT based on composite sign (HOLD only when neutral)
    timeframes: Dict[str, TimeframeIndicators]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "composite": float(self.composite),
            "long_score": float(self.long_score),
            "short_score": float(self.short_score),
            "regime_4h": self.regime_4h,
            "vol_shock": bool(self.vol_shock),
            "strength": float(self.strength),
            "direction": self.direction,
            "timeframes": {k: asdict(v) for k, v in self.timeframes.items()},
        }


@dataclass(frozen=True)
class Candidate:
    symbol: str
    direction: Direction
    confidence: float  # 0..1
    strength: float  # 0..1
    second_strength: float  # 0..1
    regime_4h: Regime
    vol_shock: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "confidence": float(self.confidence),
            "strength": float(self.strength),
            "second_strength": float(self.second_strength),
            "regime_4h": self.regime_4h,
            "vol_shock": bool(self.vol_shock),
        }


class ScoringService:
    """Rotation quant scoring (multi-timeframe).

    Produces per-symbol long/short scores and a top candidate with confidence (top vs 2nd).
    """

    def __init__(
        self,
        *,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        roc_period: int = 12,
        atr_period: int = 14,
    ) -> None:
        self._ema_fast = int(ema_fast)
        self._ema_slow = int(ema_slow)
        self._rsi_period = int(rsi_period)
        self._roc_period = int(roc_period)
        self._atr_period = int(atr_period)

    def score_universe(
        self,
        *,
        cfg: RiskConfig,
        candles_by_symbol_interval: Mapping[str, Mapping[str, Sequence[Candle]]],
    ) -> Dict[str, SymbolScore]:
        out: Dict[str, SymbolScore] = {}
        for sym, by_itv in candles_by_symbol_interval.items():
            try:
                out[str(sym).upper()] = self.score_symbol(cfg=cfg, symbol=str(sym).upper(), candles_by_interval=by_itv)
            except Exception:
                # Fail closed: skip on parse issues
                continue
        return out

    def score_symbol(
        self,
        *,
        cfg: RiskConfig,
        symbol: str,
        candles_by_interval: Mapping[str, Sequence[Candle]],
    ) -> SymbolScore:
        sym = symbol.strip().upper()
        weights = {
            "4h": float(cfg.tf_weight_4h),
            "1h": float(cfg.tf_weight_1h),
            "30m": float(cfg.tf_weight_30m),
        }
        wsum = sum(max(w, 0.0) for w in weights.values()) or 1.0
        for k in list(weights.keys()):
            weights[k] = max(weights[k], 0.0) / wsum

        tf_ind: Dict[str, TimeframeIndicators] = {}
        combined = 0.0
        vol_shock = False
        regime_4h: Regime = "CHOPPY"

        for itv in ("4h", "1h", "30m"):
            candles = list(candles_by_interval.get(itv) or [])
            closes = [float(c.close) for c in candles if float(c.close) > 0]
            if len(closes) < max(self._ema_slow, self._rsi_period + 1, self._roc_period + 1, self._atr_period + 1):
                continue

            # Use extra history for EMA stability.
            hist = closes[-(self._ema_slow * 3) :]
            e_fast = ema(hist, self._ema_fast)
            e_slow = ema(hist, self._ema_slow)
            rsi_v = rsi(closes[-(self._rsi_period + 1) :], self._rsi_period)
            roc_v = roc(closes, self._roc_period)
            am: Optional[AtrMult] = atr_mult(candles, period=self._atr_period, mean_window=int(cfg.atr_mult_mean_window))

            e_fast_f = float(e_fast) if e_fast is not None else 0.0
            e_slow_f = float(e_slow) if e_slow is not None else 0.0
            rsi_f = float(rsi_v) if rsi_v is not None else 50.0
            roc_f = float(roc_v) if roc_v is not None else 0.0
            atr_pct_f = float(am.atr_pct) if am is not None else 0.0
            atr_mean_f = float(am.atr_pct_mean) if am is not None else 0.0
            atr_mult_f = float(am.mult) if am is not None else 0.0

            if itv == "4h":
                regime_4h = _regime_4h(ema_fast=e_fast, ema_slow=e_slow, rsi_v=rsi_v)

            # Trend score: EMA spread normalized to [-1, 1].
            if e_slow is None or e_slow <= 0 or e_fast is None:
                trend_score = 0.0
            else:
                rel = (float(e_fast) - float(e_slow)) / float(e_slow)
                trend_score = clamp(rel * 50.0, -1.0, 1.0)

            # Momentum score: RSI + ROC.
            rsi_norm = clamp((rsi_f - 50.0) / 50.0, -1.0, 1.0)
            # ROC ~ few % per window; scale into [-1, 1].
            roc_norm = clamp(roc_f * 10.0, -1.0, 1.0)
            momentum_score = clamp(0.65 * rsi_norm + 0.35 * roc_norm, -1.0, 1.0)

            # Vol shock: ATR% / mean(ATR%) >= threshold => shock.
            if atr_mult_f >= float(cfg.vol_shock_atr_mult_threshold):
                vol_shock = True

            comp = clamp(0.6 * trend_score + 0.4 * momentum_score, -1.0, 1.0)
            combined += float(weights[itv]) * float(comp)

            tf_ind[itv] = TimeframeIndicators(
                interval=itv,
                ema_fast=e_fast_f,
                ema_slow=e_slow_f,
                rsi=rsi_f,
                roc=roc_f,
                atr_pct=atr_pct_f,
                atr_pct_mean=atr_mean_f,
                atr_mult=atr_mult_f,
                regime_4h=regime_4h if itv == "4h" else None,
            )

        combined = clamp(float(combined), -1.0, 1.0)
        long_score = max(combined, 0.0)
        short_score = max(-combined, 0.0)
        strength = max(long_score, short_score)
        if strength <= 0:
            direction: Direction = "HOLD"
        else:
            direction = "LONG" if long_score >= short_score else "SHORT"

        return SymbolScore(
            symbol=sym,
            composite=float(combined),
            long_score=float(long_score),
            short_score=float(short_score),
            regime_4h=regime_4h,
            vol_shock=bool(vol_shock),
            strength=float(strength),
            direction=direction,
            timeframes=tf_ind,
        )

    def pick_candidate(
        self,
        *,
        scores: Mapping[str, SymbolScore],
    ) -> Optional[Candidate]:
        ranked = sorted(scores.values(), key=lambda s: float(s.strength), reverse=True)
        if not ranked:
            return None

        # Apply short restriction at candidate time: only allow SHORT if 4h BEAR.
        filtered: List[SymbolScore] = []
        for s in ranked:
            if s.direction == "SHORT" and s.regime_4h != "BEAR":
                continue
            filtered.append(s)

        if not filtered:
            return None

        best = filtered[0]
        second_strength = float(filtered[1].strength) if len(filtered) > 1 else 0.0
        gap = float(best.strength) - float(second_strength)
        denom = float(best.strength) if float(best.strength) > 1e-9 else 1.0
        confidence = clamp(gap / denom, 0.0, 1.0)

        return Candidate(
            symbol=best.symbol,
            direction=best.direction,
            confidence=float(confidence),
            strength=float(best.strength),
            second_strength=float(second_strength),
            regime_4h=best.regime_4h,
            vol_shock=bool(best.vol_shock),
        )


```

## apps/trader_engine/services/strategy_service.py

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.scoring_service import Candidate, SymbolScore


DecisionKind = Literal["HOLD", "ENTER", "REBALANCE", "CLOSE"]


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass(frozen=True)
class StrategyDecision:
    kind: DecisionKind
    reason: str
    enter_symbol: Optional[str] = None
    enter_direction: Optional[str] = None  # LONG|SHORT
    close_symbol: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "enter_symbol": self.enter_symbol,
            "enter_direction": self.enter_direction,
            "close_symbol": self.close_symbol,
        }


@dataclass(frozen=True)
class PositionState:
    symbol: Optional[str]
    position_amt: float
    unrealized_pnl: float
    last_entry_symbol: Optional[str]
    last_entry_at: Optional[datetime]


class StrategyService:
    """Rotation strategy decision layer.

    This service decides "what to do next" (HOLD/ENTER/REBALANCE/CLOSE).
    Order sizing and actual execution remain in other services.
    """

    def decide_next_action(
        self,
        *,
        cfg: RiskConfig,
        now: Optional[datetime],
        candidate: Optional[Candidate],
        scores: Dict[str, SymbolScore],
        position: PositionState,
    ) -> StrategyDecision:
        ts = now or _utcnow()
        pos_sym = (position.symbol or "").upper() if position.symbol else None

        # No position: try to enter on candidate.
        if not pos_sym:
            if not candidate:
                return StrategyDecision(kind="HOLD", reason="no_candidate")
            if candidate.vol_shock:
                return StrategyDecision(kind="HOLD", reason="vol_shock_no_entry")
            if float(candidate.confidence) < float(cfg.score_conf_threshold):
                return StrategyDecision(kind="HOLD", reason="confidence_below_threshold")
            if candidate.direction == "SHORT" and candidate.regime_4h != "BEAR":
                return StrategyDecision(kind="HOLD", reason="short_not_allowed_regime")
            return StrategyDecision(
                kind="ENTER",
                reason="enter_candidate",
                enter_symbol=candidate.symbol,
                enter_direction=candidate.direction,
            )

        # Position exists.
        sym_score = scores.get(pos_sym)
        if sym_score and sym_score.vol_shock:
            return StrategyDecision(kind="CLOSE", reason="vol_shock_close", close_symbol=pos_sym)

        # Profit hold rule.
        if float(position.unrealized_pnl or 0.0) > 0.0:
            return StrategyDecision(kind="HOLD", reason="profit_hold")

        # If no candidate, just hold.
        if not candidate:
            return StrategyDecision(kind="HOLD", reason="no_candidate")

        # Candidate might be same symbol; avoid churn.
        if candidate.symbol.upper() == pos_sym:
            return StrategyDecision(kind="HOLD", reason="same_symbol")

        # Min-hold guard before rebalancing (unless shock, handled above).
        min_hold = int(cfg.min_hold_minutes)
        if min_hold > 0 and position.last_entry_at and position.last_entry_symbol:
            if position.last_entry_symbol.upper() == pos_sym:
                held_min = (ts - position.last_entry_at).total_seconds() / 60.0
                if held_min < float(min_hold):
                    return StrategyDecision(kind="HOLD", reason=f"min_hold_active:{int(held_min)}/{min_hold}")

        # Confidence threshold.
        if float(candidate.confidence) < float(cfg.score_conf_threshold):
            return StrategyDecision(kind="HOLD", reason="confidence_below_threshold")

        # Gap threshold: avoid weak rotations.
        cur_strength = 0.0
        if sym_score:
            # Use position direction to choose relevant score.
            if float(position.position_amt or 0.0) >= 0.0:
                cur_strength = float(sym_score.long_score)
            else:
                cur_strength = float(sym_score.short_score)
        gap = float(candidate.strength) - float(cur_strength)
        if gap < float(cfg.score_gap_threshold):
            return StrategyDecision(kind="HOLD", reason="gap_below_threshold")

        # Short restriction.
        if candidate.direction == "SHORT" and candidate.regime_4h != "BEAR":
            return StrategyDecision(kind="HOLD", reason="short_not_allowed_regime")

        return StrategyDecision(
            kind="REBALANCE",
            reason="rebalance_to_better_candidate",
            close_symbol=pos_sym,
            enter_symbol=candidate.symbol,
            enter_direction=candidate.direction,
        )


```

## apps/trader_engine/scheduler.py

```python
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from apps.trader_engine.domain.enums import Direction, EngineState, ExecHint
from apps.trader_engine.services.ai_service import AiService, AiSignal
from apps.trader_engine.services.binance_service import BinanceService
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.execution_service import ExecutionRejected, ExecutionService
from apps.trader_engine.services.market_data_service import MarketDataService
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.services.scoring_service import Candidate, ScoringService, SymbolScore
from apps.trader_engine.services.sizing_service import SizingService
from apps.trader_engine.services.strategy_service import PositionState, StrategyDecision, StrategyService

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class SchedulerSnapshot:
    tick_started_at: str
    tick_finished_at: Optional[str]
    engine_state: str
    enabled_symbols: List[str]

    # Backward-compatible fields
    candidate: Optional[Dict[str, Any]]
    scores: Dict[str, Any]
    ai_signal: Optional[Dict[str, Any]]

    # FINAL-1 fields
    last_scores: Dict[str, Any]
    last_candidate: Optional[Dict[str, Any]]
    last_decision_reason: Optional[str]

    last_action: Optional[str] = None
    last_error: Optional[str] = None


class TraderScheduler:
    """30m tick strategy loop.

    - Always computes scores/snapshots.
    - Only executes when engine state is RUNNING.
    """

    def __init__(
        self,
        *,
        engine: EngineService,
        risk: RiskConfigService,
        pnl: PnLService,
        binance: BinanceService,
        market_data: MarketDataService,
        scoring: ScoringService,
        strategy: StrategyService,
        ai: AiService,
        sizing: SizingService,
        execution: ExecutionService,
        tick_sec: float = 1800.0,
        reverse_threshold: float = 0.55,
    ) -> None:
        self._engine = engine
        self._risk = risk
        self._pnl = pnl
        self._binance = binance
        self._market_data = market_data
        self._scoring = scoring
        self._strategy = strategy
        self._ai = ai
        self._sizing = sizing
        self._execution = execution

        self._tick_sec = float(tick_sec)
        self._reverse_threshold = float(reverse_threshold)

        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self.snapshot: Optional[SchedulerSnapshot] = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="trader_scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            started = _utcnow().isoformat()
            st = self._engine.get_state().state

            enabled = list(self._binance.enabled_symbols)
            snap = SchedulerSnapshot(
                tick_started_at=started,
                tick_finished_at=None,
                engine_state=st.value,
                enabled_symbols=enabled,
                candidate=None,
                scores={},
                ai_signal=None,
                last_scores={},
                last_candidate=None,
                last_decision_reason=None,
                last_action=None,
                last_error=None,
            )
            self.snapshot = snap

            try:
                await self._tick(snap)
            except Exception as e:  # noqa: BLE001
                logger.exception("scheduler_tick_failed", extra={"err": type(e).__name__})
                snap.last_error = f"{type(e).__name__}: {e}"
            finally:
                snap.tick_finished_at = _utcnow().isoformat()
                self.snapshot = snap

            await self._sleep_until_next()

    async def _sleep_until_next(self) -> None:
        total = max(self._tick_sec, 1.0)
        end = time.time() + total
        while time.time() < end:
            if self._stop.is_set():
                return
            await asyncio.sleep(min(1.0, end - time.time()))

    async def _tick(self, snap: SchedulerSnapshot) -> None:
        st = self._engine.get_state().state
        cfg = self._risk.get_config()
        enabled = list(self._binance.enabled_symbols)

        # Fetch Binance status in a thread to avoid blocking the event loop.
        b: Mapping[str, Any] = await asyncio.to_thread(self._binance.get_status)

        # Compute equity and open position summary.
        bal = (b.get("usdt_balance") or {}) if isinstance(b, dict) else {}
        positions = (b.get("positions") or {}) if isinstance(b, dict) else {}
        wallet = float(bal.get("wallet") or 0.0)
        available = float(bal.get("available") or 0.0)

        open_pos_symbol = None
        open_pos_amt = 0.0
        open_pos_upnl = 0.0
        upnl_total = 0.0
        if isinstance(positions, dict):
            for sym, row in positions.items():
                if not isinstance(row, dict):
                    continue
                upnl_total += float(row.get("unrealized_pnl") or 0.0)
                amt = float(row.get("position_amt") or 0.0)
                if abs(amt) > 0:
                    open_pos_symbol = str(sym).upper()
                    open_pos_amt = amt
                    open_pos_upnl = float(row.get("unrealized_pnl") or 0.0)
        equity = wallet + upnl_total

        # Update equity peak tracking (also helps /status).
        await asyncio.to_thread(self._pnl.update_equity_peak, equity_usdt=equity)

        # Collect market data (multi TF) for universe.
        candles_by_symbol_interval: Dict[str, Dict[str, Any]] = {}
        for sym in enabled:
            by_itv: Dict[str, Any] = {}
            for itv in ("30m", "1h", "4h"):
                cs = await asyncio.to_thread(self._market_data.get_klines, symbol=sym, interval=itv, limit=260)
                by_itv[itv] = cs
            candles_by_symbol_interval[sym] = by_itv

        scores: Dict[str, SymbolScore] = await asyncio.to_thread(
            self._scoring.score_universe,
            cfg=cfg,
            candles_by_symbol_interval=candles_by_symbol_interval,
        )
        candidate: Optional[Candidate] = self._scoring.pick_candidate(scores=scores)

        # Snapshot: expose "last_scores / last_candidate" and keep older fields populated.
        snap.last_scores = {k: v.as_dict() for k, v in scores.items()}
        snap.last_candidate = candidate.as_dict() if candidate else None
        snap.scores = dict(snap.last_scores)
        if candidate:
            ss = scores.get(candidate.symbol)
            vol_tag = "VOL_SHOCK" if (ss and ss.vol_shock) else "NORMAL"
            comp = float(ss.composite) if ss else 0.0
            snap.candidate = {
                "symbol": candidate.symbol,
                "direction": candidate.direction,
                "strength": float(candidate.strength),
                "composite": comp,
                "vol_tag": vol_tag,
                "confidence": float(candidate.confidence),
                "regime_4h": candidate.regime_4h,
            }
        else:
            snap.candidate = None

        # AI signal is advisory only; keep it for status/exec_hint default.
        st_pnl = await asyncio.to_thread(self._pnl.get_or_bootstrap)
        ai_ctx = {
            "candidate": snap.candidate,
            "scores": snap.scores,
            "engine_state": st.value,
            "position": {
                "symbol": open_pos_symbol,
                "amt": open_pos_amt,
                "upnl": open_pos_upnl,
            },
            "pnl": {
                "cooldown_until": getattr(st_pnl, "cooldown_until", None),
                "lose_streak": getattr(st_pnl, "lose_streak", 0),
            },
            "spreads": b.get("spreads") if isinstance(b, dict) else {},
        }
        ai_sig: AiSignal = self._ai.get_signal(ai_ctx)
        snap.ai_signal = ai_sig.as_dict()

        # Strategy decision.
        pos_state = PositionState(
            symbol=open_pos_symbol,
            position_amt=open_pos_amt,
            unrealized_pnl=open_pos_upnl,
            last_entry_symbol=getattr(st_pnl, "last_entry_symbol", None),
            last_entry_at=getattr(st_pnl, "last_entry_at", None),
        )
        dec: StrategyDecision = self._strategy.decide_next_action(
            cfg=cfg,
            now=_utcnow(),
            candidate=candidate,
            scores=scores,
            position=pos_state,
        )
        snap.last_decision_reason = dec.reason

        logger.info(
            "strategy_tick",
            extra={
                "engine_state": st.value,
                "enabled_symbols": enabled,
                "candidate": snap.last_candidate,
                "decision": dec.as_dict(),
                "open_pos_symbol": open_pos_symbol,
            },
        )

        # Execution gating: RUNNING only.
        if st != EngineState.RUNNING:
            return

        # Apply decision.
        if dec.kind == "HOLD":
            snap.last_action = "hold"
            return

        if dec.kind == "CLOSE":
            sym = str(dec.close_symbol or "").upper()
            if not sym:
                snap.last_error = "close_symbol_missing"
                return
            try:
                out = await asyncio.to_thread(self._execution.close_position, sym)
                snap.last_action = f"close:{sym}"
                snap.last_error = None
                logger.info("strategy_close", extra={"symbol": sym, "detail": out})
            except ExecutionRejected as e:
                snap.last_action = f"close:{sym}"
                snap.last_error = str(e)
            return

        # ENTER/REBALANCE both require sizing a target notional.
        target_symbol = str(dec.enter_symbol or "").upper()
        if not target_symbol:
            snap.last_error = "enter_symbol_missing"
            return
        dir_s = str(dec.enter_direction or "").upper()
        direction = Direction.LONG if dir_s == "LONG" else Direction.SHORT

        # REBALANCE: close first, then enter.
        if dec.kind == "REBALANCE":
            close_sym = str(dec.close_symbol or "").upper()
            if close_sym:
                try:
                    out = await asyncio.to_thread(self._execution.close_position, close_sym)
                    logger.info("strategy_rebalance_close", extra={"symbol": close_sym, "detail": out})
                except ExecutionRejected as e:
                    snap.last_action = f"rebalance_close:{close_sym}"
                    snap.last_error = str(e)
                    return

        # Compute sizing: use 30m ATR% as stop distance proxy (fallback 1%).
        ss = scores.get(target_symbol)
        atr_pct = 1.0
        if ss and "30m" in ss.timeframes:
            atr_pct = float(ss.timeframes["30m"].atr_pct or 1.0)
        stop_distance_pct = max(float(atr_pct), 0.5)

        # Price reference: prefer book mid from spreads, else last close.
        bt = (b.get("spreads") or {}).get(target_symbol) if isinstance(b, dict) else None
        bid = float((bt or {}).get("bid") or 0.0) if isinstance(bt, dict) else 0.0
        ask = float((bt or {}).get("ask") or 0.0) if isinstance(bt, dict) else 0.0
        price = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0.0
        if price <= 0.0:
            last = await asyncio.to_thread(self._market_data.get_last_close, symbol=target_symbol, interval="30m", limit=2)
            price = float(last or 0.0)
        if price <= 0.0:
            snap.last_error = "price_unavailable"
            return

        size = await asyncio.to_thread(
            self._sizing.compute,
            symbol=target_symbol,
            risk=cfg,
            equity_usdt=equity,
            available_usdt=available,
            price=price,
            stop_distance_pct=stop_distance_pct,
            existing_exposure_notional_usdt=0.0,
        )
        if size.target_notional_usdt <= 0 or size.target_qty <= 0:
            snap.last_error = f"sizing_blocked:{size.capped_by or 'unknown'}"
            return

        # Exec hint: take AI's suggestion (advisory) but default to LIMIT.
        hint_raw = str(ai_sig.exec_hint or "LIMIT").upper()
        exec_hint = ExecHint.LIMIT
        if hint_raw in ("MARKET", "LIMIT", "SPLIT"):
            exec_hint = ExecHint(hint_raw)

        intent = {
            "symbol": target_symbol,
            "direction": direction,
            "exec_hint": exec_hint,
            "notional_usdt": float(size.target_notional_usdt),
        }

        try:
            out = await asyncio.to_thread(self._execution.enter_position, intent)
            snap.last_action = f"{dec.kind.lower()}_enter:{target_symbol}:{direction.value}"
            snap.last_error = None
            logger.info("strategy_enter", extra={"symbol": target_symbol, "direction": direction.value, "detail": out})
        except ExecutionRejected as e:
            snap.last_action = f"{dec.kind.lower()}_enter:{target_symbol}:{direction.value}"
            snap.last_error = str(e)
        except Exception as e:  # noqa: BLE001
            snap.last_action = f"{dec.kind.lower()}_enter:{target_symbol}:{direction.value}"
            snap.last_error = f"{type(e).__name__}: {e}"


```

## apps/trader_engine/main.py

```python
from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from apps.trader_engine.api.routes import router
from apps.trader_engine.config import load_settings
from apps.trader_engine.exchange.binance_usdm import BinanceCredentials, BinanceUSDMClient
from apps.trader_engine.exchange.time_sync import TimeSync
from apps.trader_engine.logging_setup import LoggingConfig, setup_logging
from apps.trader_engine.services.binance_service import BinanceService
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.execution_service import ExecutionService
from apps.trader_engine.services.market_data_service import MarketDataService
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.ai_service import AiService
from apps.trader_engine.services.risk_service import RiskService
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.services.scoring_service import ScoringService
from apps.trader_engine.services.sizing_service import SizingService
from apps.trader_engine.services.strategy_service import StrategyService
from apps.trader_engine.storage.db import close, connect, migrate
from apps.trader_engine.storage.repositories import EngineStateRepo, PnLStateRepo, RiskConfigRepo, StatusSnapshotRepo
from apps.trader_engine.scheduler import TraderScheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    setup_logging(LoggingConfig(level=settings.log_level, log_dir=settings.log_dir, json=settings.log_json))

    db = connect(settings.db_path)
    migrate(db)

    engine_state_repo = EngineStateRepo(db)
    risk_config_repo = RiskConfigRepo(db)
    _status_snapshot_repo = StatusSnapshotRepo(db)  # reserved for later wiring
    pnl_state_repo = PnLStateRepo(db)

    engine_service = EngineService(engine_state_repo=engine_state_repo)
    risk_config_service = RiskConfigService(risk_config_repo=risk_config_repo)
    pnl_service = PnLService(repo=pnl_state_repo)
    # Ensure defaults exist at boot.
    _ = engine_service.get_state()
    cfg = risk_config_service.get_config()
    _ = pnl_service.get_or_bootstrap()

    # Binance USDT-M Futures (議고쉶 ?꾩슜)
    binance_client = BinanceUSDMClient(
        BinanceCredentials(api_key=settings.binance_api_key, api_secret=settings.binance_api_secret),
        base_url=settings.binance_base_url,
        time_sync=TimeSync(),
        timeout_sec=settings.request_timeout_sec,
        retry_count=settings.retry_count,
        retry_backoff=settings.retry_backoff,
        recv_window_ms=settings.binance_recv_window_ms,
    )
    binance_service = BinanceService(
        client=binance_client,
        allowed_symbols=cfg.universe_symbols,
        spread_wide_pct=cfg.spread_max_pct,
    )
    binance_service.startup()

    policy = RiskService(
        risk=risk_config_service,
        engine=engine_service,
        pnl=pnl_service,
        stop_on_daily_loss=bool(settings.risk_stop_on_daily_loss),
    )

    execution_service = ExecutionService(
        client=binance_client,
        engine=engine_service,
        risk=risk_config_service,
        pnl=pnl_service,
        policy=policy,
        allowed_symbols=binance_service.enabled_symbols,
        split_parts=settings.exec_split_parts,
        dry_run=bool(settings.trading_dry_run),
        dry_run_strict=bool(settings.dry_run_strict),
    )

    market_data_service = MarketDataService(
        client=binance_client,
        cache_ttl_sec=20.0,
        retry_attempts=settings.retry_count,
        retry_backoff_sec=settings.retry_backoff,
    )
    scoring_service = ScoringService()
    strategy_service = StrategyService()
    ai_service = AiService(
        mode=settings.ai_mode,
        conf_threshold=settings.ai_conf_threshold,
        manual_risk_tag=settings.manual_risk_tag,
    )
    sizing_service = SizingService(client=binance_client)

    scheduler = TraderScheduler(
        engine=engine_service,
        risk=risk_config_service,
        pnl=pnl_service,
        binance=binance_service,
        market_data=market_data_service,
        scoring=scoring_service,
        strategy=strategy_service,
        ai=ai_service,
        sizing=sizing_service,
        execution=execution_service,
        tick_sec=float(settings.scheduler_tick_sec),
        reverse_threshold=float(settings.reverse_threshold),
    )

    app.state.settings = settings
    app.state.db = db
    app.state.engine_service = engine_service
    app.state.risk_config_service = risk_config_service
    app.state.pnl_service = pnl_service
    app.state.risk_service = policy
    app.state.binance_service = binance_service
    app.state.execution_service = execution_service
    app.state.market_data_service = market_data_service
    app.state.scoring_service = scoring_service
    app.state.strategy_service = strategy_service
    app.state.ai_service = ai_service
    app.state.sizing_service = sizing_service
    app.state.scheduler = scheduler
    app.state.scheduler_snapshot = None

    logger.info("api_boot", extra={"db_path": settings.db_path})
    try:
        if bool(settings.scheduler_enabled):
            scheduler.start()
            logger.info(
                "scheduler_started",
                extra={"tick_sec": settings.scheduler_tick_sec, "score_threshold": settings.score_threshold},
            )
        yield
    finally:
        try:
            try:
                await scheduler.stop()
            except Exception:
                pass
            binance_service.close()
        except Exception:
            pass
        close(db)


def create_app() -> FastAPI:
    app = FastAPI(title="auto-trader control api", version="0.2.0", lifespan=lifespan)
    app.include_router(router)
    return app


# FastAPI entrypoint for uvicorn:
#   uvicorn apps.trader_engine.main:app --reload
app = create_app()


def main() -> int:
    parser = argparse.ArgumentParser(prog="auto-trader")
    parser.add_argument("--api", action="store_true", help="run control API via uvicorn")
    args = parser.parse_args()

    if not args.api:
        # Simple non-server boot: initializes DB + defaults then exits.
        settings = load_settings()
        setup_logging(LoggingConfig(level=settings.log_level, log_dir=settings.log_dir, json=settings.log_json))
        db = connect(settings.db_path)
        migrate(db)
        try:
            EngineService(engine_state_repo=EngineStateRepo(db))
            RiskConfigService(risk_config_repo=RiskConfigRepo(db)).get_config()
            logger.info("boot_ok", extra={"db_path": settings.db_path})
            return 0
        finally:
            close(db)

    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "apps.trader_engine.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

```

## apps/trader_engine/domain/enums.py

```python
from __future__ import annotations

from enum import Enum


class EngineState(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    COOLDOWN = "COOLDOWN"
    PANIC = "PANIC"


class RiskConfigKey(str, Enum):
    per_trade_risk_pct = "per_trade_risk_pct"
    max_exposure_pct = "max_exposure_pct"
    max_notional_pct = "max_notional_pct"
    max_leverage = "max_leverage"
    # Loss limits are stored as ratios (e.g. -0.02 for -2%)
    daily_loss_limit_pct = "daily_loss_limit_pct"
    dd_limit_pct = "dd_limit_pct"
    lose_streak_n = "lose_streak_n"
    cooldown_hours = "cooldown_hours"
    min_hold_minutes = "min_hold_minutes"
    score_conf_threshold = "score_conf_threshold"
    score_gap_threshold = "score_gap_threshold"
    exec_limit_timeout_sec = "exec_limit_timeout_sec"
    exec_limit_retries = "exec_limit_retries"
    notify_interval_sec = "notify_interval_sec"
    spread_max_pct = "spread_max_pct"
    allow_market_when_wide_spread = "allow_market_when_wide_spread"
    universe_symbols = "universe_symbols"
    enable_watchdog = "enable_watchdog"
    watchdog_interval_sec = "watchdog_interval_sec"
    shock_1m_pct = "shock_1m_pct"
    shock_from_entry_pct = "shock_from_entry_pct"
    tf_weight_4h = "tf_weight_4h"
    tf_weight_1h = "tf_weight_1h"
    tf_weight_30m = "tf_weight_30m"
    vol_shock_atr_mult_threshold = "vol_shock_atr_mult_threshold"
    atr_mult_mean_window = "atr_mult_mean_window"


class RiskPresetName(str, Enum):
    conservative = "conservative"
    normal = "normal"
    aggressive = "aggressive"


# Placeholders for A-stage expansion.
class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ExecHint(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SPLIT = "SPLIT"

```

## apps/trader_engine/domain/models.py

```python
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from apps.trader_engine.domain.enums import EngineState


class RiskConfig(BaseModel):
    # Percent units: 0..100
    per_trade_risk_pct: float = Field(ge=0, le=100)
    max_exposure_pct: float = Field(ge=0, le=100)
    max_notional_pct: float = Field(ge=0, le=100)

    # Hardcap is enforced again in RiskService, but keep it here too.
    max_leverage: float = Field(ge=1, le=50)

    # Loss limits are negative ratios (allowed range: -1..0)
    # Example: -0.02 == -2%
    daily_loss_limit_pct: float = Field(ge=-1, le=0, default=-0.02)
    dd_limit_pct: float = Field(ge=-1, le=0, default=-0.15)

    lose_streak_n: int = Field(ge=1, le=10)
    cooldown_hours: float = Field(ge=1, le=72)

    # Strategy/execution controls (stored in the same singleton config row).
    min_hold_minutes: int = Field(ge=0, le=24 * 60, default=240)
    score_conf_threshold: float = Field(ge=0, le=1, default=0.65)
    score_gap_threshold: float = Field(ge=0, le=1, default=0.20)

    exec_limit_timeout_sec: float = Field(gt=0, le=60, default=5.0)
    exec_limit_retries: int = Field(ge=0, le=10, default=2)
    notify_interval_sec: int = Field(ge=10, le=3600)

    spread_max_pct: float = Field(ge=0, le=0.1, default=0.0015)
    allow_market_when_wide_spread: bool = Field(default=False)

    universe_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "XAUTUSDT"])

    enable_watchdog: bool = Field(default=True)
    watchdog_interval_sec: int = Field(ge=1, le=300, default=10)

    shock_1m_pct: float = Field(ge=0, le=0.5, default=0.010)
    shock_from_entry_pct: float = Field(ge=0, le=0.5, default=0.012)

    # Scoring config (rotation strategy)
    tf_weight_4h: float = Field(ge=0, le=1, default=0.5)
    tf_weight_1h: float = Field(ge=0, le=1, default=0.3)
    tf_weight_30m: float = Field(ge=0, le=1, default=0.2)

    vol_shock_atr_mult_threshold: float = Field(ge=1, le=10, default=2.5)
    atr_mult_mean_window: int = Field(ge=10, le=500, default=50)

    @field_validator("universe_symbols", mode="before")
    @classmethod
    def _parse_universe_symbols(cls, v):  # type: ignore[no-untyped-def]
        # Accept list[str] or CSV-like strings from DB/env.
        if v is None:
            return ["BTCUSDT", "ETHUSDT", "XAUTUSDT"]
        if isinstance(v, str):
            parts = [p.strip().upper() for p in v.split(",") if p.strip()]
            return parts
        if isinstance(v, (list, tuple)):
            return [str(x).strip().upper() for x in v if str(x).strip()]
        return v


class EngineStateRow(BaseModel):
    state: EngineState
    updated_at: datetime


class PnLState(BaseModel):
    # Stored as a singleton row (id=1). "day" is YYYY-MM-DD in UTC.
    day: str
    daily_realized_pnl: float = 0.0
    equity_peak: float = 0.0
    lose_streak: int = 0
    cooldown_until: datetime | None = None
    last_entry_symbol: str | None = None
    last_entry_at: datetime | None = None
    last_block_reason: str | None = None
    updated_at: datetime

```

## apps/trader_engine/services/risk_config_service.py

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict

from apps.trader_engine.domain.enums import RiskConfigKey, RiskPresetName
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.storage.repositories import RiskConfigRepo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskConfigValidationError(Exception):
    message: str


_PRESETS: Dict[RiskPresetName, RiskConfig] = {
    RiskPresetName.conservative: RiskConfig(
        per_trade_risk_pct=0.5,
        max_exposure_pct=10,
        max_notional_pct=20,
        max_leverage=3,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.05,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=300,
    ),
    RiskPresetName.normal: RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=20,
        max_notional_pct=50,
        max_leverage=5,
        # Default policy baseline (percent units):
        # - daily_loss_limit: -2% (block new entries; optional STOP is handled by RiskService setting)
        # - dd_limit: -15% (PANIC)
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=120,
    ),
    RiskPresetName.aggressive: RiskConfig(
        per_trade_risk_pct=2,
        max_exposure_pct=40,
        max_notional_pct=80,
        max_leverage=10,
        daily_loss_limit_pct=-0.10,
        dd_limit_pct=-0.20,
        lose_streak_n=2,
        cooldown_hours=1,
        notify_interval_sec=60,
    ),
}


class RiskConfigService:
    def __init__(self, *, risk_config_repo: RiskConfigRepo) -> None:
        self._risk_config_repo = risk_config_repo

    def get_config(self) -> RiskConfig:
        cfg = self._risk_config_repo.get()
        if cfg is None:
            cfg = _PRESETS[RiskPresetName.normal]
            self._risk_config_repo.upsert(cfg)
            logger.info("risk_config_bootstrapped", extra={"preset": RiskPresetName.normal.value})
        else:
            # Forward-fill any newly added config fields/columns with model defaults.
            try:
                self._risk_config_repo.upsert(cfg)
            except Exception:
                pass
        return cfg

    def apply_preset(self, name: RiskPresetName) -> RiskConfig:
        cfg = _PRESETS[name]
        self._risk_config_repo.upsert(cfg)
        logger.info("risk_config_preset_applied", extra={"preset": name.value})
        return cfg

    def set_value(self, key: RiskConfigKey, value: str) -> RiskConfig:
        cfg = self.get_config()
        updated = cfg.model_copy()

        try:
            parsed: Any = self._parse_value(key, value)
        except ValueError as e:
            raise RiskConfigValidationError(str(e)) from e

        # Set and validate via pydantic (domain model constraints).
        payload = updated.model_dump()
        payload[key.value] = parsed
        try:
            validated = RiskConfig(**payload)
        except Exception as e:  # pydantic ValidationError
            raise RiskConfigValidationError(str(e)) from e

        self._risk_config_repo.upsert(validated)
        logger.info("risk_config_value_set", extra={"key": key.value})
        return validated

    @staticmethod
    def _parse_value(key: RiskConfigKey, value: str) -> Any:
        value = value.strip()
        if key in {
            RiskConfigKey.lose_streak_n,
            RiskConfigKey.notify_interval_sec,
            RiskConfigKey.min_hold_minutes,
            RiskConfigKey.exec_limit_retries,
            RiskConfigKey.watchdog_interval_sec,
            RiskConfigKey.atr_mult_mean_window,
        }:
            try:
                return int(value)
            except Exception as e:
                raise ValueError(f"invalid_int_for_{key.value}") from e

        if key in {RiskConfigKey.allow_market_when_wide_spread, RiskConfigKey.enable_watchdog}:
            v = value.lower()
            if v in ("1", "true", "t", "yes", "y", "on"):
                return True
            if v in ("0", "false", "f", "no", "n", "off"):
                return False
            raise ValueError(f"invalid_bool_for_{key.value}")

        if key == RiskConfigKey.universe_symbols:
            # CSV: BTCUSDT,ETHUSDT,XAUTUSDT
            parts = [p.strip().upper() for p in value.split(",") if p.strip()]
            if not parts:
                raise ValueError("universe_symbols_empty")
            return parts

        # Everything else: float
        try:
            return float(value)
        except Exception as e:
            raise ValueError(f"invalid_float_for_{key.value}") from e

```

## apps/trader_engine/storage/db.py

```python
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Iterable, Optional


SCHEMA_MIGRATIONS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS risk_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        per_trade_risk_pct REAL NOT NULL,
        max_exposure_pct REAL NOT NULL,
        max_notional_pct REAL NOT NULL,
        max_leverage REAL NOT NULL,
        -- Legacy percent-unit fields kept for backward compatibility (e.g. -2 for -2%)
        daily_loss_limit REAL NOT NULL,
        dd_limit REAL NOT NULL,
        -- Preferred ratio-unit fields (e.g. -0.02 for -2%)
        daily_loss_limit_pct REAL,
        dd_limit_pct REAL,
        lose_streak_n INTEGER NOT NULL,
        cooldown_hours REAL NOT NULL,
        notify_interval_sec INTEGER NOT NULL,
        min_hold_minutes INTEGER,
        score_conf_threshold REAL,
        score_gap_threshold REAL,
        exec_limit_timeout_sec REAL,
        exec_limit_retries INTEGER,
        spread_max_pct REAL,
        allow_market_when_wide_spread INTEGER,
        universe_symbols TEXT,
        enable_watchdog INTEGER,
        watchdog_interval_sec INTEGER,
        shock_1m_pct REAL,
        shock_from_entry_pct REAL,
        tf_weight_4h REAL,
        tf_weight_1h REAL,
        tf_weight_30m REAL,
        vol_shock_atr_mult_threshold REAL,
        atr_mult_mean_window INTEGER,
        updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS engine_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        state TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS status_snapshot (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS pnl_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        day TEXT NOT NULL,
        daily_realized_pnl REAL NOT NULL,
        equity_peak REAL NOT NULL,
        lose_streak INTEGER NOT NULL,
        cooldown_until TEXT,
        last_entry_symbol TEXT,
        last_entry_at TEXT,
        last_block_reason TEXT,
        updated_at TEXT NOT NULL
    )
    """.strip(),
]


@dataclass
class Database:
    conn: sqlite3.Connection
    lock: threading.RLock

    def execute(self, sql: str, params: Iterable[object] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def executescript(self, sql: str) -> None:
        with self.lock:
            self.conn.executescript(sql)
            self.conn.commit()


def connect(db_path: str) -> Database:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        check_same_thread=False,
        isolation_level=None,  # autocommit mode; we still guard with a lock
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return Database(conn=conn, lock=threading.RLock())


def get_schema_version(db: Database) -> int:
    try:
        row = db.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        if not row or row["v"] is None:
            return 0
        return int(row["v"])
    except sqlite3.OperationalError:
        return 0


def migrate(db: Database) -> None:
    # Simple linear migrations: apply SCHEMA_MIGRATIONS entries in order.
    # Each entry index is its version number.
    db.executescript(SCHEMA_MIGRATIONS[0])
    current = get_schema_version(db)

    for version in range(current + 1, len(SCHEMA_MIGRATIONS)):
        db.executescript(SCHEMA_MIGRATIONS[version])
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
            (version,),
        )

    # Best-effort forward-compatible schema tweaks for existing DBs.
    _ensure_columns(db)
    _backfill_derived_columns(db)


def _table_columns(db: Database, table: str) -> set[str]:
    try:
        rows = db.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r["name"]) for r in rows if r and r["name"]}
    except Exception:
        return set()


def _ensure_columns(db: Database) -> None:
    # SQLite lacks "ADD COLUMN IF NOT EXISTS", so we check first.
    risk_cols = _table_columns(db, "risk_config")
    if risk_cols:
        adds: list[tuple[str, str]] = [
            ("daily_loss_limit_pct", "REAL"),
            ("dd_limit_pct", "REAL"),
            ("min_hold_minutes", "INTEGER"),
            ("score_conf_threshold", "REAL"),
            ("score_gap_threshold", "REAL"),
            ("exec_limit_timeout_sec", "REAL"),
            ("exec_limit_retries", "INTEGER"),
            ("spread_max_pct", "REAL"),
            ("allow_market_when_wide_spread", "INTEGER"),
            ("universe_symbols", "TEXT"),
            ("enable_watchdog", "INTEGER"),
            ("watchdog_interval_sec", "INTEGER"),
            ("shock_1m_pct", "REAL"),
            ("shock_from_entry_pct", "REAL"),
            ("tf_weight_4h", "REAL"),
            ("tf_weight_1h", "REAL"),
            ("tf_weight_30m", "REAL"),
            ("vol_shock_atr_mult_threshold", "REAL"),
            ("atr_mult_mean_window", "INTEGER"),
        ]
        for name, typ in adds:
            if name in risk_cols:
                continue
            try:
                db.execute(f"ALTER TABLE risk_config ADD COLUMN {name} {typ}")
            except Exception:
                pass

    pnl_cols = _table_columns(db, "pnl_state")
    if pnl_cols:
        adds2: list[tuple[str, str]] = [
            ("last_entry_symbol", "TEXT"),
            ("last_entry_at", "TEXT"),
        ]
        for name, typ in adds2:
            if name in pnl_cols:
                continue
            try:
                db.execute(f"ALTER TABLE pnl_state ADD COLUMN {name} {typ}")
            except Exception:
                pass


def _backfill_derived_columns(db: Database) -> None:
    # Backfill ratio-unit loss limits from legacy percent-unit columns when present.
    cols = _table_columns(db, "risk_config")
    if not cols:
        return
    if "daily_loss_limit_pct" in cols and "daily_loss_limit" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET daily_loss_limit_pct = (daily_loss_limit / 100.0)
                WHERE id=1 AND (daily_loss_limit_pct IS NULL)
                """.strip()
            )
        except Exception:
            pass
    if "dd_limit_pct" in cols and "dd_limit" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET dd_limit_pct = (dd_limit / 100.0)
                WHERE id=1 AND (dd_limit_pct IS NULL)
                """.strip()
            )
        except Exception:
            pass


def close(db: Database) -> None:
    with db.lock:
        db.conn.close()

```

## apps/trader_engine/storage/repositories.py

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import EngineStateRow, PnLState, RiskConfig
from apps.trader_engine.storage.db import Database


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    # ISO 8601 parser for the limited scope of this project.
    return datetime.fromisoformat(value)


class RiskConfigRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self) -> Optional[RiskConfig]:
        row = self._db.conn.execute("SELECT * FROM risk_config WHERE id=1").fetchone()
        if not row:
            return None
        keys = set(row.keys())
        payload: Dict[str, Any] = {}
        for k in RiskConfig.model_fields.keys():
            if k in keys:
                # If an existing DB row has newly-added columns, they'll be NULL.
                # Do not pass None into pydantic; let model defaults apply instead.
                v = row[k]
                if v is None:
                    continue
                payload[k] = v

        # Backward-compat: derive ratio-unit loss limits from legacy percent columns.
        if "daily_loss_limit_pct" not in payload:
            if "daily_loss_limit_pct" in keys and row["daily_loss_limit_pct"] is not None:
                payload["daily_loss_limit_pct"] = float(row["daily_loss_limit_pct"])
            elif "daily_loss_limit" in keys and row["daily_loss_limit"] is not None:
                payload["daily_loss_limit_pct"] = float(row["daily_loss_limit"]) / 100.0

        if "dd_limit_pct" not in payload:
            if "dd_limit_pct" in keys and row["dd_limit_pct"] is not None:
                payload["dd_limit_pct"] = float(row["dd_limit_pct"])
            elif "dd_limit" in keys and row["dd_limit"] is not None:
                payload["dd_limit_pct"] = float(row["dd_limit"]) / 100.0

        return RiskConfig(**payload)

    def upsert(self, cfg: RiskConfig) -> None:
        # Keep legacy percent-unit columns in sync for older DBs/tools.
        daily_loss_limit_legacy = float(cfg.daily_loss_limit_pct) * 100.0
        dd_limit_legacy = float(cfg.dd_limit_pct) * 100.0
        universe_csv = ",".join([s.strip().upper() for s in (cfg.universe_symbols or []) if s.strip()])

        self._db.execute(
            """
            INSERT INTO risk_config(
                id,
                per_trade_risk_pct,
                max_exposure_pct,
                max_notional_pct,
                max_leverage,
                daily_loss_limit,
                dd_limit,
                daily_loss_limit_pct,
                dd_limit_pct,
                lose_streak_n,
                cooldown_hours,
                notify_interval_sec,
                min_hold_minutes,
                score_conf_threshold,
                score_gap_threshold,
                exec_limit_timeout_sec,
                exec_limit_retries,
                spread_max_pct,
                allow_market_when_wide_spread,
                universe_symbols,
                enable_watchdog,
                watchdog_interval_sec,
                shock_1m_pct,
                shock_from_entry_pct,
                tf_weight_4h,
                tf_weight_1h,
                tf_weight_30m,
                vol_shock_atr_mult_threshold,
                atr_mult_mean_window,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                per_trade_risk_pct=excluded.per_trade_risk_pct,
                max_exposure_pct=excluded.max_exposure_pct,
                max_notional_pct=excluded.max_notional_pct,
                max_leverage=excluded.max_leverage,
                daily_loss_limit=excluded.daily_loss_limit,
                dd_limit=excluded.dd_limit,
                daily_loss_limit_pct=excluded.daily_loss_limit_pct,
                dd_limit_pct=excluded.dd_limit_pct,
                lose_streak_n=excluded.lose_streak_n,
                cooldown_hours=excluded.cooldown_hours,
                notify_interval_sec=excluded.notify_interval_sec,
                min_hold_minutes=excluded.min_hold_minutes,
                score_conf_threshold=excluded.score_conf_threshold,
                score_gap_threshold=excluded.score_gap_threshold,
                exec_limit_timeout_sec=excluded.exec_limit_timeout_sec,
                exec_limit_retries=excluded.exec_limit_retries,
                spread_max_pct=excluded.spread_max_pct,
                allow_market_when_wide_spread=excluded.allow_market_when_wide_spread,
                universe_symbols=excluded.universe_symbols,
                enable_watchdog=excluded.enable_watchdog,
                watchdog_interval_sec=excluded.watchdog_interval_sec,
                shock_1m_pct=excluded.shock_1m_pct,
                shock_from_entry_pct=excluded.shock_from_entry_pct,
                tf_weight_4h=excluded.tf_weight_4h,
                tf_weight_1h=excluded.tf_weight_1h,
                tf_weight_30m=excluded.tf_weight_30m,
                vol_shock_atr_mult_threshold=excluded.vol_shock_atr_mult_threshold,
                atr_mult_mean_window=excluded.atr_mult_mean_window,
                updated_at=excluded.updated_at
            """.strip(),
            (
                cfg.per_trade_risk_pct,
                cfg.max_exposure_pct,
                cfg.max_notional_pct,
                cfg.max_leverage,
                daily_loss_limit_legacy,
                dd_limit_legacy,
                float(cfg.daily_loss_limit_pct),
                float(cfg.dd_limit_pct),
                cfg.lose_streak_n,
                cfg.cooldown_hours,
                cfg.notify_interval_sec,
                int(cfg.min_hold_minutes),
                float(cfg.score_conf_threshold),
                float(cfg.score_gap_threshold),
                float(cfg.exec_limit_timeout_sec),
                int(cfg.exec_limit_retries),
                float(cfg.spread_max_pct),
                int(bool(cfg.allow_market_when_wide_spread)),
                universe_csv,
                int(bool(cfg.enable_watchdog)),
                int(cfg.watchdog_interval_sec),
                float(cfg.shock_1m_pct),
                float(cfg.shock_from_entry_pct),
                float(cfg.tf_weight_4h),
                float(cfg.tf_weight_1h),
                float(cfg.tf_weight_30m),
                float(cfg.vol_shock_atr_mult_threshold),
                int(cfg.atr_mult_mean_window),
                _utcnow_iso(),
            ),
        )


class EngineStateRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self) -> EngineStateRow:
        row = self._db.conn.execute("SELECT * FROM engine_state WHERE id=1").fetchone()
        if not row:
            # Default bootstrap state (persisted).
            state = EngineStateRow(state=EngineState.STOPPED, updated_at=datetime.now(tz=timezone.utc))
            self.upsert(state)
            return state
        return EngineStateRow(state=EngineState(row["state"]), updated_at=_parse_dt(row["updated_at"]))

    def upsert(self, state: EngineStateRow) -> None:
        self._db.execute(
            """
            INSERT INTO engine_state(id, state, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state=excluded.state,
                updated_at=excluded.updated_at
            """.strip(),
            (state.state.value, state.updated_at.isoformat()),
        )


class StatusSnapshotRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get_json(self) -> Optional[Dict[str, Any]]:
        row = self._db.conn.execute("SELECT json FROM status_snapshot WHERE id=1").fetchone()
        if not row:
            return None
        return json.loads(row["json"])

    def upsert_json(self, payload: Dict[str, Any]) -> None:
        self._db.execute(
            """
            INSERT INTO status_snapshot(id, json, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                json=excluded.json,
                updated_at=excluded.updated_at
            """.strip(),
            (json.dumps(payload, ensure_ascii=True, default=str), _utcnow_iso()),
        )


class PnLStateRepo:
    """Persist minimal PnL/risk-related state required for policy guards.

    Stored as a singleton row (id=1).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self) -> Optional[PnLState]:
        row = self._db.conn.execute("SELECT * FROM pnl_state WHERE id=1").fetchone()
        if not row:
            return None
        cooldown_until = row["cooldown_until"]
        keys = set(row.keys())
        last_entry_at = row["last_entry_at"] if "last_entry_at" in keys else None
        last_entry_symbol = row["last_entry_symbol"] if "last_entry_symbol" in keys else None
        return PnLState(
            day=str(row["day"]),
            daily_realized_pnl=float(row["daily_realized_pnl"] or 0.0),
            equity_peak=float(row["equity_peak"] or 0.0),
            lose_streak=int(row["lose_streak"] or 0),
            cooldown_until=_parse_dt(cooldown_until) if cooldown_until else None,
            last_entry_symbol=str(last_entry_symbol) if last_entry_symbol is not None else None,
            last_entry_at=_parse_dt(last_entry_at) if last_entry_at else None,
            last_block_reason=str(row["last_block_reason"]) if row["last_block_reason"] is not None else None,
            updated_at=_parse_dt(row["updated_at"]),
        )

    def upsert(self, st: PnLState) -> None:
        self._db.execute(
            """
            INSERT INTO pnl_state(
                id,
                day,
                daily_realized_pnl,
                equity_peak,
                lose_streak,
                cooldown_until,
                last_entry_symbol,
                last_entry_at,
                last_block_reason,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                day=excluded.day,
                daily_realized_pnl=excluded.daily_realized_pnl,
                equity_peak=excluded.equity_peak,
                lose_streak=excluded.lose_streak,
                cooldown_until=excluded.cooldown_until,
                last_entry_symbol=excluded.last_entry_symbol,
                last_entry_at=excluded.last_entry_at,
                last_block_reason=excluded.last_block_reason,
                updated_at=excluded.updated_at
            """.strip(),
            (
                st.day,
                float(st.daily_realized_pnl),
                float(st.equity_peak),
                int(st.lose_streak),
                st.cooldown_until.isoformat() if st.cooldown_until else None,
                st.last_entry_symbol,
                st.last_entry_at.isoformat() if st.last_entry_at else None,
                st.last_block_reason,
                st.updated_at.isoformat(),
            ),
        )

```

## apps/trader_engine/api/schemas.py

```python
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from pydantic import Field

from apps.trader_engine.domain.enums import Direction, EngineState, ExecHint, RiskConfigKey, RiskPresetName


class RiskConfigSchema(BaseModel):
    per_trade_risk_pct: float
    max_exposure_pct: float
    max_notional_pct: float
    max_leverage: float
    daily_loss_limit_pct: float
    dd_limit_pct: float
    lose_streak_n: int
    cooldown_hours: float
    min_hold_minutes: int
    score_conf_threshold: float
    score_gap_threshold: float
    exec_limit_timeout_sec: float
    exec_limit_retries: int
    notify_interval_sec: int
    spread_max_pct: float
    allow_market_when_wide_spread: bool
    universe_symbols: List[str]
    enable_watchdog: bool
    watchdog_interval_sec: int
    shock_1m_pct: float
    shock_from_entry_pct: float
    tf_weight_4h: float
    tf_weight_1h: float
    tf_weight_30m: float
    vol_shock_atr_mult_threshold: float
    atr_mult_mean_window: int


class EngineStateSchema(BaseModel):
    state: EngineState
    updated_at: datetime


class DisabledSymbolSchema(BaseModel):
    symbol: str
    reason: str


class BinanceStatusSchema(BaseModel):
    startup_ok: bool
    startup_error: Optional[str] = None

    enabled_symbols: List[str]
    disabled_symbols: List[DisabledSymbolSchema]

    server_time_ms: int
    time_offset_ms: int
    time_measured_at_ms: int

    private_ok: bool
    private_error: Optional[str] = None

    usdt_balance: Optional[Dict[str, float]] = None
    positions: Optional[Dict[str, Dict[str, float]]] = None
    open_orders: Optional[Dict[str, List[Dict[str, Any]]]] = None
    spreads: Dict[str, Any]


class PnLStatusSchema(BaseModel):
    day: str
    daily_realized_pnl: float
    equity_peak: float
    daily_pnl_pct: float
    drawdown_pct: float
    lose_streak: int
    cooldown_until: Optional[datetime] = None
    last_block_reason: Optional[str] = None


class CandidateSchema(BaseModel):
    symbol: str
    direction: str
    strength: float
    composite: float
    vol_tag: str
    confidence: Optional[float] = None
    regime_4h: Optional[str] = None


class AiSignalSchema(BaseModel):
    target_asset: str
    direction: str
    confidence: float
    exec_hint: str
    risk_tag: str
    notes: Optional[str] = None


class SchedulerSnapshotSchema(BaseModel):
    tick_started_at: str
    tick_finished_at: Optional[str] = None
    engine_state: str
    enabled_symbols: List[str]
    candidate: Optional[CandidateSchema] = None
    ai_signal: Optional[AiSignalSchema] = None
    scores: Dict[str, Any] = Field(default_factory=dict)
    last_scores: Dict[str, Any] = Field(default_factory=dict)
    last_candidate: Optional[Dict[str, Any]] = None
    last_decision_reason: Optional[str] = None
    last_action: Optional[str] = None
    last_error: Optional[str] = None


class StatusResponse(BaseModel):
    dry_run: bool = False
    dry_run_strict: bool = False
    config_summary: Dict[str, Any] = Field(default_factory=dict)
    last_error: Optional[str] = None
    engine_state: EngineStateSchema
    risk_config: RiskConfigSchema
    binance: Optional[BinanceStatusSchema] = None
    pnl: Optional[PnLStatusSchema] = None
    scheduler: Optional[SchedulerSnapshotSchema] = None


class SetValueRequest(BaseModel):
    key: RiskConfigKey
    value: str


class PresetRequest(BaseModel):
    name: RiskPresetName


class TradeEnterRequest(BaseModel):
    symbol: str
    direction: Direction
    exec_hint: ExecHint
    notional_usdt: Optional[float] = None
    qty: Optional[float] = None
    leverage: Optional[float] = None


class TradeCloseRequest(BaseModel):
    symbol: str


class TradeResult(BaseModel):
    symbol: str
    hint: Optional[str] = None
    orders: List[Dict[str, Any]] = Field(default_factory=list)
    detail: Optional[Dict[str, Any]] = None

```

## apps/trader_engine/api/routes.py

```python
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status

from apps.trader_engine.api.schemas import (
    EngineStateSchema,
    PnLStatusSchema,
    PresetRequest,
    RiskConfigSchema,
    SetValueRequest,
    StatusResponse,
    SchedulerSnapshotSchema,
    TradeCloseRequest,
    TradeEnterRequest,
    TradeResult,
)
from apps.trader_engine.services.binance_service import BinanceService
from apps.trader_engine.services.engine_service import EngineConflict, EngineService
from apps.trader_engine.services.execution_service import (
    ExecutionRejected,
    ExecutionService,
    ExecutionValidationError,
)
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.risk_config_service import RiskConfigService, RiskConfigValidationError

logger = logging.getLogger(__name__)

router = APIRouter()


def _engine_service(request: Request) -> EngineService:
    return request.app.state.engine_service  # type: ignore[attr-defined]


def _risk_service(request: Request) -> RiskConfigService:
    return request.app.state.risk_config_service  # type: ignore[attr-defined]


def _binance_service(request: Request) -> BinanceService:
    return request.app.state.binance_service  # type: ignore[attr-defined]


def _execution_service(request: Request) -> ExecutionService:
    return request.app.state.execution_service  # type: ignore[attr-defined]


def _pnl_service(request: Request) -> PnLService:
    return request.app.state.pnl_service  # type: ignore[attr-defined]


@router.get("/", include_in_schema=False)
def root() -> Dict[str, Any]:
    return {"ok": True, "hint": "see /docs, /health, /status"}


@router.get("/health")
def health() -> dict:
    return {"ok": True}


@router.get("/status", response_model=StatusResponse)
def get_status(
    request: Request,
    engine: EngineService = Depends(_engine_service),
    risk: RiskConfigService = Depends(_risk_service),
    binance: BinanceService = Depends(_binance_service),
    pnl: PnLService = Depends(_pnl_service),
) -> StatusResponse:
    state = engine.get_state()
    cfg = risk.get_config()
    b = binance.get_status()
    settings = getattr(request.app.state, "settings", None)

    pnl_payload = None
    try:
        st = pnl.get_or_bootstrap()
        bal = (b.get("usdt_balance") or {}) if isinstance(b, dict) else {}
        pos = (b.get("positions") or {}) if isinstance(b, dict) else {}
        wallet = float(bal.get("wallet") or 0.0)
        upnl = 0.0
        if isinstance(pos, dict):
            for row in pos.values():
                if isinstance(row, dict):
                    upnl += float(row.get("unrealized_pnl") or 0.0)
        equity = wallet + upnl

        st2 = pnl.update_equity_peak(equity_usdt=equity)
        m = pnl.compute_metrics(st=st2, equity_usdt=equity)
        pnl_payload = PnLStatusSchema(
            day=st2.day,
            daily_realized_pnl=float(st2.daily_realized_pnl),
            equity_peak=float(st2.equity_peak),
            daily_pnl_pct=float(m.daily_pnl_pct),
            drawdown_pct=float(m.drawdown_pct),
            lose_streak=int(st2.lose_streak),
            cooldown_until=st2.cooldown_until,
            last_block_reason=st2.last_block_reason,
        )
    except Exception:
        logger.exception("pnl_status_failed")

    sched = (
        SchedulerSnapshotSchema(**request.app.state.scheduler.snapshot.__dict__)  # type: ignore[attr-defined]
        if getattr(request.app.state, "scheduler", None) and getattr(request.app.state.scheduler, "snapshot", None)
        else None
    )

    last_error = None
    if sched and isinstance(sched, SchedulerSnapshotSchema) and sched.last_error:
        last_error = sched.last_error
    elif isinstance(b, dict) and (b.get("private_error") or b.get("startup_error")):
        last_error = str(b.get("private_error") or b.get("startup_error"))

    summary = {
        "universe_symbols": cfg.universe_symbols,
        "max_leverage": cfg.max_leverage,
        "daily_loss_limit_pct": cfg.daily_loss_limit_pct,
        "dd_limit_pct": cfg.dd_limit_pct,
        "lose_streak_n": cfg.lose_streak_n,
        "cooldown_hours": cfg.cooldown_hours,
        "min_hold_minutes": cfg.min_hold_minutes,
        "score_conf_threshold": cfg.score_conf_threshold,
        "score_gap_threshold": cfg.score_gap_threshold,
        "exec_limit_timeout_sec": cfg.exec_limit_timeout_sec,
        "exec_limit_retries": cfg.exec_limit_retries,
        "spread_max_pct": cfg.spread_max_pct,
        "allow_market_when_wide_spread": cfg.allow_market_when_wide_spread,
        "enable_watchdog": cfg.enable_watchdog,
        "watchdog_interval_sec": cfg.watchdog_interval_sec,
        "shock_1m_pct": cfg.shock_1m_pct,
        "shock_from_entry_pct": cfg.shock_from_entry_pct,
        "tf_weight_4h": cfg.tf_weight_4h,
        "tf_weight_1h": cfg.tf_weight_1h,
        "tf_weight_30m": cfg.tf_weight_30m,
        "vol_shock_atr_mult_threshold": cfg.vol_shock_atr_mult_threshold,
        "atr_mult_mean_window": cfg.atr_mult_mean_window,
    }

    return StatusResponse(
        dry_run=bool(getattr(settings, "trading_dry_run", False)) if settings else False,
        dry_run_strict=bool(getattr(settings, "dry_run_strict", False)) if settings else False,
        config_summary=summary,
        last_error=last_error,
        engine_state=EngineStateSchema(state=state.state, updated_at=state.updated_at),
        risk_config=RiskConfigSchema(**cfg.model_dump()),
        binance=b,
        pnl=pnl_payload,
        scheduler=sched,
    )


@router.post("/start", response_model=EngineStateSchema)
def start(engine: EngineService = Depends(_engine_service)) -> EngineStateSchema:
    try:
        row = engine.start()
        return EngineStateSchema(state=row.state, updated_at=row.updated_at)
    except EngineConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/stop", response_model=EngineStateSchema)
def stop(engine: EngineService = Depends(_engine_service)) -> EngineStateSchema:
    try:
        row = engine.stop()
        return EngineStateSchema(state=row.state, updated_at=row.updated_at)
    except EngineConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/panic", response_model=EngineStateSchema)
def panic(
    engine: EngineService = Depends(_engine_service),
    exe: ExecutionService = Depends(_execution_service),
) -> EngineStateSchema:
    # PANIC should lock state + attempt best-effort cancel/close.
    _ = exe.panic()
    row = engine.get_state()
    return EngineStateSchema(state=row.state, updated_at=row.updated_at)


@router.get("/risk", response_model=RiskConfigSchema)
def get_risk(risk: RiskConfigService = Depends(_risk_service)) -> RiskConfigSchema:
    cfg = risk.get_config()
    return RiskConfigSchema(**cfg.model_dump())


@router.post("/set", response_model=RiskConfigSchema)
def set_value(
    req: SetValueRequest,
    risk: RiskConfigService = Depends(_risk_service),
) -> RiskConfigSchema:
    try:
        cfg = risk.set_value(req.key, req.value)
        return RiskConfigSchema(**cfg.model_dump())
    except RiskConfigValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.message) from e


@router.post("/preset", response_model=RiskConfigSchema)
def preset(
    req: PresetRequest,
    risk: RiskConfigService = Depends(_risk_service),
) -> RiskConfigSchema:
    cfg = risk.apply_preset(req.name)
    return RiskConfigSchema(**cfg.model_dump())


@router.post("/trade/enter", response_model=TradeResult)
def trade_enter(
    req: TradeEnterRequest,
    exe: ExecutionService = Depends(_execution_service),
) -> TradeResult:
    try:
        out = exe.enter_position(req.model_dump())
        return TradeResult(symbol=out.get("symbol", req.symbol), hint=out.get("hint"), orders=out.get("orders", []))
    except ExecutionValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.message) from e
    except ExecutionRejected as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/trade/close", response_model=TradeResult)
def trade_close(
    req: TradeCloseRequest,
    exe: ExecutionService = Depends(_execution_service),
) -> TradeResult:
    try:
        out = exe.close_position(req.symbol)
        return TradeResult(symbol=out.get("symbol", req.symbol), detail=out)
    except ExecutionValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.message) from e
    except ExecutionRejected as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/trade/close_all", response_model=TradeResult)
def trade_close_all(
    exe: ExecutionService = Depends(_execution_service),
) -> TradeResult:
    try:
        out = exe.close_all_positions()
        return TradeResult(symbol=str(out.get("symbol", "")), detail=out)
    except ExecutionRejected as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e

```

## apps/discord_bot/commands.py

```python
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from apps.discord_bot.client import APIError, TraderAPIClient

logger = logging.getLogger(__name__)

RISK_KEYS: List[str] = [
    "per_trade_risk_pct",
    "max_exposure_pct",
    "max_notional_pct",
    "max_leverage",
    "daily_loss_limit_pct",
    "dd_limit_pct",
    "lose_streak_n",
    "cooldown_hours",
    "min_hold_minutes",
    "score_conf_threshold",
    "score_gap_threshold",
    "exec_limit_timeout_sec",
    "exec_limit_retries",
    "notify_interval_sec",
    "spread_max_pct",
    "allow_market_when_wide_spread",
    "universe_symbols",
    "enable_watchdog",
    "watchdog_interval_sec",
    "shock_1m_pct",
    "shock_from_entry_pct",
    "tf_weight_4h",
    "tf_weight_1h",
    "tf_weight_30m",
    "vol_shock_atr_mult_threshold",
    "atr_mult_mean_window",
]

PRESETS: List[str] = ["conservative", "normal", "aggressive"]


async def _safe_defer(interaction: discord.Interaction) -> bool:
    """Acknowledge the interaction quickly.

    Discord requires an initial response within a short deadline (~3s). If our
    event loop is busy or the user retries quickly, the interaction token can
    expire and defer() raises NotFound (10062).
    """
    try:
        await interaction.response.defer(thinking=True)
        return True
    except discord.InteractionResponded:
        return True
    except discord.NotFound:
        # Can't respond via interaction token anymore. Best-effort: post to channel.
        try:
            cmd = getattr(getattr(interaction, "command", None), "name", None)
            created = getattr(interaction, "created_at", None)
            logger.warning("discord_unknown_interaction", extra={"command": cmd, "created_at": str(created)})
        except Exception:
            pass
        try:
            ch = interaction.channel
            if ch is not None and hasattr(ch, "send"):
                await ch.send("Interaction expired (Discord timeout). Try the command again.")
        except Exception:
            pass
        return False


def _truncate(s: str, *, limit: int = 1800) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _fmt_money(x: Any) -> str:
    try:
        return f"{float(x):.4f}"
    except Exception:
        return str(x)


def _fmt_status_payload(payload: Dict[str, Any]) -> str:
    engine = payload.get("engine_state") or {}
    risk = payload.get("risk_config") or {}
    summary = payload.get("config_summary") or {}
    binance = payload.get("binance") or {}
    pnl = payload.get("pnl") or {}
    sched = payload.get("scheduler") or {}
    dry_run = bool(payload.get("dry_run", False))
    dry_run_strict = bool(payload.get("dry_run_strict", False))
    last_error = payload.get("last_error", None)

    state = str(engine.get("state", "UNKNOWN"))
    panic = state.upper() == "PANIC"
    state_line = f"Engine: {state}"
    if panic:
        state_line = f":warning: {state_line} (PANIC)"

    enabled = binance.get("enabled_symbols") or []
    disabled = binance.get("disabled_symbols") or []

    bal = binance.get("usdt_balance") or {}
    wallet = _fmt_money(bal.get("wallet", "n/a"))
    available = _fmt_money(bal.get("available", "n/a"))

    positions = binance.get("positions") or {}
    pos_lines: List[str] = []
    if isinstance(positions, dict):
        for sym in sorted(positions.keys()):
            row = positions.get(sym) or {}
            amt = row.get("position_amt", 0)
            pnl = row.get("unrealized_pnl", 0)
            lev = row.get("leverage", 0)
            entry = row.get("entry_price", 0)
            pos_lines.append(
                f"- {sym}: amt={amt} entry={entry} pnl={pnl} lev={lev}"
            )

    open_orders = binance.get("open_orders") or {}
    oo_total = 0
    if isinstance(open_orders, dict):
        for v in open_orders.values():
            if isinstance(v, list):
                oo_total += len(v)

    spread_wide: List[str] = []
    spreads = binance.get("spreads") or {}
    if isinstance(spreads, dict):
        for sym, row in spreads.items():
            if isinstance(row, dict) and row.get("is_wide"):
                spread_wide.append(f"- {sym}: spread_pct={row.get('spread_pct')}")

    lines: List[str] = []
    lines.append(state_line)
    lines.append(f"DRY_RUN: {dry_run} (strict={dry_run_strict})")
    lines.append(f"Enabled symbols: {', '.join(enabled) if enabled else '(none)'}")
    if disabled:
        # Show only first few.
        d0 = []
        for d in disabled[:5]:
            if isinstance(d, dict):
                d0.append(f"{d.get('symbol')}({d.get('reason')})")
        lines.append(f"Disabled symbols: {', '.join(d0)}")
    lines.append(f"USDT balance: wallet={wallet}, available={available}")
    lines.append(f"Open orders: {oo_total}")
    if pos_lines:
        lines.append("Positions:")
        lines.extend(pos_lines[:10])
    if spread_wide:
        lines.append("Wide spreads:")
        lines.extend(spread_wide[:5])

    # Policy guard / PnL snapshot (if available).
    if isinstance(pnl, dict) and pnl:
        dd = pnl.get("drawdown_pct", "n/a")
        dp = pnl.get("daily_pnl_pct", "n/a")
        ls = pnl.get("lose_streak", "n/a")
        cd = pnl.get("cooldown_until", None)
        lbr = pnl.get("last_block_reason", None)
        lines.append(f"PnL: daily_pct={dp} dd_pct={dd} lose_streak={ls}")
        if cd:
            lines.append(f"Cooldown until: {cd}")
        if lbr:
            lines.append(f"Last block: {lbr}")

    # Scheduler snapshot (if enabled)
    if isinstance(sched, dict) and sched:
        cand = sched.get("candidate") or {}
        ai = sched.get("ai_signal") or {}
        if isinstance(cand, dict) and cand.get("symbol"):
            lines.append(
                f"Candidate: {cand.get('symbol')} {cand.get('direction')} "
                f"strength={cand.get('strength')} vol={cand.get('vol_tag')}"
            )
        if isinstance(ai, dict) and ai.get("target_asset"):
            lines.append(
                f"AI: {ai.get('target_asset')} {ai.get('direction')} "
                f"conf={ai.get('confidence')} hint={ai.get('exec_hint')} tag={ai.get('risk_tag')}"
            )
        la = sched.get("last_action")
        le = sched.get("last_error")
        if la:
            lines.append(f"Scheduler last_action: {la}")
        if le:
            lines.append(f"Scheduler last_error: {le}")
    if last_error:
        lines.append(f"Last error: {last_error}")

    # Risk is often useful, but keep it short for /status.
    if isinstance(summary, dict) and summary:
        lines.append(
            "Config: "
            f"symbols={','.join(summary.get('universe_symbols') or [])} "
            f"max_lev={summary.get('max_leverage')} "
            f"dl={summary.get('daily_loss_limit_pct')} "
            f"dd={summary.get('dd_limit_pct')} "
            f"spread={summary.get('spread_max_pct')}"
        )
    elif isinstance(risk, dict):
        lines.append(
            f"Risk: per_trade={risk.get('per_trade_risk_pct')}% "
            f"max_lev={risk.get('max_leverage')} "
            f"notify={risk.get('notify_interval_sec')}s"
        )

    return _truncate("\n".join(lines))


def _fmt_json(payload: Any) -> str:
    import json

    try:
        s = json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True)
    except Exception:
        s = str(payload)
    return _truncate(s, limit=1900)


class RemoteControl(commands.Cog):
    def __init__(self, bot: commands.Bot, api: TraderAPIClient) -> None:
        self.bot = bot
        self.api = api

    @app_commands.command(name="status", description="Show trader_engine status (summary)")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.get_status()
            assert isinstance(payload, dict)
            msg = _fmt_status_payload(payload)
            await interaction.followup.send(f"```text\n{msg}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="risk", description="Get current risk config")
    async def risk(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.get_risk()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="start", description="POST /start")
    async def start(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.start()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="stop", description="POST /stop")
    async def stop(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.stop()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="panic", description="POST /panic")
    async def panic(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.panic()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="close", description="Close a position for a symbol (reduceOnly)")
    @app_commands.describe(symbol="Symbol, e.g. BTCUSDT")
    async def close(self, interaction: discord.Interaction, symbol: str) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.close_position(symbol.strip().upper())
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="closeall", description="Close any open position (single-asset rule)")
    async def closeall(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.close_all()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="set", description="POST /set (risk config)")
    @app_commands.describe(key="Risk config key", value="New value (string)")
    @app_commands.choices(
        key=[app_commands.Choice(name=k, value=k) for k in RISK_KEYS],
    )
    async def set_value(
        self,
        interaction: discord.Interaction,
        key: app_commands.Choice[str],
        value: str,
    ) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.set_value(key.value, value)
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="preset", description="POST /preset (risk config)")
    @app_commands.choices(name=[app_commands.Choice(name=p, value=p) for p in PRESETS])
    async def preset(
        self,
        interaction: discord.Interaction,
        name: app_commands.Choice[str],
    ) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.preset(name.value)
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API error: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"Error: {type(e).__name__}: {e}", ephemeral=True)


async def setup_commands(bot: commands.Bot, api: TraderAPIClient) -> None:
    await bot.add_cog(RemoteControl(bot, api))

```

