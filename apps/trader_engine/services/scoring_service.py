from __future__ import annotations

from dataclasses import asdict, dataclass, field
import logging
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence

from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.indicators import AtrMult, atr_mult, clamp, ema, roc, rsi
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
    score_by_timeframe: Dict[str, float] = field(default_factory=dict)
    active_timeframes: List[str] = field(default_factory=list)

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
            "score_by_timeframe": dict(self.score_by_timeframe),
            "active_timeframes": list(self.active_timeframes),
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
    score_by_timeframe: Dict[str, float] = field(default_factory=dict)
    active_timeframes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "confidence": float(self.confidence),
            "strength": float(self.strength),
            "second_strength": float(self.second_strength),
            "regime_4h": self.regime_4h,
            "vol_shock": bool(self.vol_shock),
            "score_by_timeframe": dict(self.score_by_timeframe),
            "active_timeframes": list(self.active_timeframes),
        }


@dataclass(frozen=True)
class ScoringResult:
    scores: Dict[str, SymbolScore]
    rejection_reasons: Dict[str, int]
    scan_stats: Dict[str, Any]


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
        min_bars_factor: float = 1.0,
    ) -> ScoringResult:
        logger = logging.getLogger(__name__)
        out: Dict[str, SymbolScore] = {}
        reasons: Dict[str, int] = {
            "symbols_seen": 0,
            "scored": 0,
            "skipped_no_usable_timeframes": 0,
            "skipped_scoring_exception": 0,
        }
        interval_weights = self.get_timeframe_weights(cfg=cfg)
        for sym, by_itv in candles_by_symbol_interval.items():
            reasons["symbols_seen"] += 1
            try:
                scored, per_symbol_reasons = self.score_symbol(
                    cfg=cfg,
                    symbol=str(sym).upper(),
                    candles_by_interval=by_itv,
                    interval_weights=interval_weights,
                    min_bars_factor=min_bars_factor,
                )
                for k, v in per_symbol_reasons.items():
                    reasons[k] = int(reasons.get(k, 0)) + int(v)
                if scored is None:
                    reasons["skipped_no_usable_timeframes"] += 1
                    continue
                out[str(sym).upper()] = scored
                reasons["scored"] += 1
            except Exception as e:
                # Fail closed: skip on parse issues.
                reasons["skipped_scoring_exception"] += 1
                exc_name = type(e).__name__
                exc_key = f"scoring_exception_{exc_name}"
                reasons[exc_key] = int(reasons.get(exc_key, 0)) + 1
                logger.warning(
                    "scoring_error",
                    extra={
                        "symbol": str(sym).upper(),
                        "error": str(e),
                        "error_type": exc_name,
                        "timeframes": sorted(list(by_itv.keys())),
                        "min_bars_factor": float(min_bars_factor),
                    },
                    exc_info=True,
                )
                continue
        return ScoringResult(
            scores=out,
            rejection_reasons=reasons,
            scan_stats={
                "requested_symbols": reasons["symbols_seen"],
                "scored_symbols": reasons["scored"],
                "scoring_timeframes": sorted(interval_weights.keys()),
                "min_bars_factor": float(min_bars_factor),
                "score_scan_weights": dict(interval_weights),
            },
        )

    def score_symbol(
        self,
        *,
        cfg: RiskConfig,
        symbol: str,
        candles_by_interval: Mapping[str, Sequence[Candle]],
        interval_weights: Optional[Mapping[str, float]] = None,
        min_bars_factor: float = 1.0,
    ) -> tuple[Optional[SymbolScore], Dict[str, int]]:
        sym = symbol.strip().upper()
        weights = dict(interval_weights or self.get_timeframe_weights(cfg=cfg))
        if not weights:
            weights = self.get_timeframe_weights(cfg=cfg)

        reason_counts: Dict[str, int] = {}
        wsum = sum(max(w, 0.0) for w in weights.values()) or 1.0
        for k in list(weights.keys()):
            weights[k] = max(weights[k], 0.0) / wsum

        tf_ind: Dict[str, TimeframeIndicators] = {}
        combined = 0.0
        score_by_timeframe: Dict[str, float] = {}
        vol_shock = False
        regime_4h: Regime = "CHOPPY"

        base_len = max(
            self._ema_slow,
            self._rsi_period + 1,
            self._roc_period + 1,
            self._atr_period + 1,
        )
        required_len = max(10, int(base_len * float(min_bars_factor)))

        for itv in ("10m", "15m", "30m", "1h", "4h"):
            if itv not in weights:
                reason_counts[f"tf_not_configured_{itv}"] = reason_counts.get(f"tf_not_configured_{itv}", 0) + 1
                continue

            candles = list(candles_by_interval.get(itv) or [])
            if not candles:
                reason_counts[f"tf_no_candles_{itv}"] = reason_counts.get(f"tf_no_candles_{itv}", 0) + 1
                continue
            closes = [float(c.close) for c in candles if float(c.close) > 0]
            if len(closes) < required_len:
                reason_counts[f"tf_insufficient_bars_{itv}"] = reason_counts.get(f"tf_insufficient_bars_{itv}", 0) + 1
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

            if itv in {"4h", "1h"}:
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
                regime_4h=regime_4h if itv in {"4h", "1h"} else None,
            )

            # Track actual weighted components for downstream transparency.
            score_by_timeframe[itv] = float(comp)

        if not tf_ind:
            reason_counts["no_usable_timeframes"] = reason_counts.get("no_usable_timeframes", 0) + 1
            return None, reason_counts

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
            score_by_timeframe=score_by_timeframe,
            active_timeframes=[k for k in ("10m", "15m", "30m", "1h", "4h") if k in tf_ind],
        )

    def pick_candidate(
        self,
        *,
        scores: Mapping[str, SymbolScore],
        score_conf_threshold: float = 0.0,
        score_gap_threshold: float = 0.0,
    ) -> tuple[Optional[Candidate], Dict[str, int]]:
        reasons: Dict[str, int] = {
            "scored_symbols": 0,
            "short_filtered": 0,
            "selected": 0,
            "all_short_filtered": 0,
            "empty": 0,
            "confidence_below_threshold": 0,
            "gap_below_threshold": 0,
        }
        if not scores:
            reasons["empty"] = 1
            reasons["scored_symbols"] = 0
            return None, reasons

        ranked = sorted(scores.values(), key=lambda s: float(s.strength), reverse=True)
        reasons["scored_symbols"] = len(ranked)

        # Apply short restriction at candidate time: only allow SHORT if 4h BEAR.
        filtered: List[SymbolScore] = []
        for s in ranked:
            if s.direction == "SHORT" and s.regime_4h != "BEAR":
                reasons["short_filtered"] += 1
                continue
            filtered.append(s)

        if not filtered:
            reasons["all_short_filtered"] = 1
            return None, reasons

        best = filtered[0]
        second_strength = float(filtered[1].strength) if len(filtered) > 1 else 0.0
        gap = float(best.strength) - float(second_strength)
        denom = float(best.strength) if float(best.strength) > 1e-9 else 1.0
        confidence = clamp(gap / denom, 0.0, 1.0)

        if float(confidence) < float(score_conf_threshold):
            reasons["confidence_below_threshold"] = reasons.get("confidence_below_threshold", 0) + 1
            return None, reasons
        if float(gap) < float(score_gap_threshold):
            reasons["gap_below_threshold"] = reasons.get("gap_below_threshold", 0) + 1
            return None, reasons

        reasons["selected"] = 1

        return Candidate(
            symbol=best.symbol,
            direction=best.direction,
            confidence=float(confidence),
            strength=float(best.strength),
            second_strength=float(second_strength),
            regime_4h=best.regime_4h,
            vol_shock=bool(best.vol_shock),
            score_by_timeframe=dict(best.score_by_timeframe),
            active_timeframes=list(best.active_timeframes),
        ), reasons

    @staticmethod
    def get_timeframe_weights(cfg: RiskConfig) -> Dict[str, float]:
        # Default composition is stable and explicit:
        # 10m + 30m + 1h + 4h. 15m can be enabled via flag.
        weights: Dict[str, float] = {
            "10m": float(cfg.tf_weight_10m),
            "15m": float(cfg.tf_weight_15m),
            "30m": float(cfg.tf_weight_30m),
            "1h": float(cfg.tf_weight_1h),
            "4h": float(cfg.tf_weight_4h),
        }
        if not bool(getattr(cfg, "score_tf_15m_enabled", False)):
            weights.pop("15m", None)

        cleaned: Dict[str, float] = {}
        for itv, wt in list(weights.items()):
            if float(wt) > 0:
                cleaned[itv] = float(wt)

        if not cleaned:
            return {"10m": 0.25, "30m": 0.25, "1h": 0.25, "4h": 0.25}

        return cleaned
