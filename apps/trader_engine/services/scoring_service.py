from __future__ import annotations

from dataclasses import asdict, dataclass, field
import logging
from collections.abc import Sequence as _Sequence
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


def _safe_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    if isinstance(v, bool):
        return 1 if v else 0
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _safe_nonneg_float(v: Any, *, default: float = 0.0) -> float:
    try:
        fv = float(v)
    except Exception:
        return default
    if fv != fv or fv == float("inf") or fv == float("-inf"):
        return default
    if fv < 0:
        return default
    return fv


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
            reasons["symbols_seen"] = _safe_int(reasons.get("symbols_seen", 0)) + 1
            if not isinstance(by_itv, Mapping):
                reasons["skipped_scoring_exception"] = _safe_int(reasons.get("skipped_scoring_exception", 0)) + 1
                reasons["scoring_exception_TypeError"] = _safe_int(reasons.get("scoring_exception_TypeError", 0)) + 1
                reasons["invalid_symbol_payload"] = _safe_int(reasons.get("invalid_symbol_payload", 0)) + 1
                logger.warning(
                    "scoring_symbol_payload_invalid",
                    extra={
                        "symbol": str(sym).upper(),
                        "timeframes": list(getattr(by_itv, "keys", lambda: [])()),
                    },
                )
                continue
            try:
                scored, per_symbol_reasons = self.score_symbol(
                    cfg=cfg,
                    symbol=str(sym).upper(),
                    candles_by_interval=by_itv,
                    interval_weights=interval_weights,
                    min_bars_factor=_safe_float(min_bars_factor, default=0.42),
                )
                for k, v in per_symbol_reasons.items():
                    key = str(k)
                    reasons[key] = _safe_int(reasons.get(key, 0)) + _safe_int(v)
                if scored is None:
                    reasons["skipped_no_usable_timeframes"] = _safe_int(
                        reasons.get("skipped_no_usable_timeframes", 0)
                    ) + 1
                    continue
                out[str(sym).upper()] = scored
                reasons["scored"] = _safe_int(reasons.get("scored", 0)) + 1
            except Exception as e:
                # Fail closed: skip on parse issues.
                reasons["skipped_scoring_exception"] = _safe_int(reasons.get("skipped_scoring_exception", 0)) + 1
                exc_name = type(e).__name__
                exc_key = f"scoring_exception_{exc_name}"
                reasons[exc_key] = _safe_int(reasons.get(exc_key, 0)) + 1
                if exc_name == "TypeError":
                    reasons["scoring_exception_TypeError"] = _safe_int(
                        reasons.get("scoring_exception_TypeError", 0)
                    ) + 1
                payload_meta = {
                    "symbol": str(sym).upper(),
                    "symbol_payload_type": type(by_itv).__name__,
                    "error": str(e),
                    "error_type": exc_name,
                    "timeframes": sorted(list(by_itv.keys())),
                }
                payload_meta["weight_keys"] = (
                    sorted(list(interval_weights.keys()))
                    if isinstance(interval_weights, Mapping)
                    else []
                )
                payload_meta["weight_values"] = {
                    str(k): _safe_float(v, default=0.0)
                    for k, v in (interval_weights.items() if isinstance(interval_weights, Mapping) else [])
                }
                payload_meta.update(
                    {
                        "cfg_atr_mult_mean_window": type(
                            getattr(cfg, "atr_mult_mean_window", None)
                        ).__name__,
                        "cfg_vol_shock_atr_mult_threshold": type(
                            getattr(cfg, "vol_shock_atr_mult_threshold", None)
                        ).__name__,
                        "score_symbol_kwargs": {
                            "symbol": str(sym).upper(),
                            "interval_count": _safe_int(len(by_itv)),
                            "min_bars_factor": _safe_float(min_bars_factor, default=0.42),
                            "interval_weights": interval_weights,
                        },
                        "min_bars_factor": _safe_float(min_bars_factor, default=0.42),
                    }
                )
                logger.warning("scoring_error", extra=payload_meta, exc_info=True)
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
        raw_weights = dict(interval_weights or self.get_timeframe_weights(cfg=cfg))
        weights: Dict[str, float] = {}
        for tf_name, raw_wt in raw_weights.items():
            wt = _safe_nonneg_float(raw_wt, default=0.0)
            if wt > 0:
                weights[str(tf_name)] = wt
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
        atr_mult_mean_window = max(1, _safe_int(getattr(cfg, "atr_mult_mean_window", 50), default=50))
        vol_shock_threshold = _safe_float(
            getattr(cfg, "vol_shock_atr_mult_threshold", 2.5),
            default=2.5,
        )
        if vol_shock_threshold <= 0:
            vol_shock_threshold = 2.5

        def _extract_float(v: Any) -> Optional[float]:
            try:
                if v is None:
                    return None
                fv = float(v)
            except Exception:
                return None
            if fv <= 0:
                return None
            return fv

        def _safe_row_float(v: Any, *, field: str) -> Optional[float]:
            val = _extract_float(v)
            if val is None or val < 0:
                return None
            if not (val == val):
                return None
            return val

        def _to_candle(v: Any) -> Optional[Candle]:
            if isinstance(v, Candle):
                return v
            if isinstance(v, _Sequence) and not isinstance(v, (str, bytes)):
                if len(v) < 7:
                    return None
                try:
                    return Candle(
                        open_time_ms=int(v[0]),
                        open=float(v[1]),
                        high=float(v[2]),
                        low=float(v[3]),
                        close=float(v[4]),
                        volume=float(v[5]),
                        close_time_ms=int(v[6]),
                    )
                except Exception:
                    return None
            if isinstance(v, Mapping):
                try:
                    return Candle(
                        open_time_ms=int(v.get("open_time", v.get("openTime", v.get("open_time_ms", 0)))),
                        open=float(v["open"]),
                        high=float(v["high"]),
                        low=float(v["low"]),
                        close=float(v["close"]),
                        volume=float(v["volume"]),
                        close_time_ms=int(
                            v.get("close_time", v.get("closeTime", v.get("close_time_ms", 0)))
                        ),
                    )
                except Exception:
                    return None
            return None

        base_len = max(
            self._ema_slow,
            self._rsi_period + 1,
            self._roc_period + 1,
            self._atr_period + 1,
        )
        try:
            mbf = float(min_bars_factor)
        except Exception:
            mbf = 0.42
        required_len = max(10, int(base_len * mbf))

        for itv in ("10m", "15m", "30m", "1h", "4h"):
            if itv not in weights:
                reason_counts[f"tf_not_configured_{itv}"] = reason_counts.get(f"tf_not_configured_{itv}", 0) + 1
                continue

            raw_candles = candles_by_interval.get(itv) if isinstance(candles_by_interval, Mapping) else None
            if raw_candles is None:
                reason_counts[f"tf_no_candles_{itv}"] = reason_counts.get(f"tf_no_candles_{itv}", 0) + 1
                continue
            if isinstance(raw_candles, Mapping):
                reason_counts[f"tf_scoring_exception_{itv}_TypeError"] = reason_counts.get(
                    f"tf_scoring_exception_{itv}_TypeError", 0
                ) + 1
                reason_counts["scoring_exception_TypeError"] = reason_counts.get(
                    "scoring_exception_TypeError", 0
                ) + 1
                logger.warning(
                    "scoring_timeframe_payload_invalid",
                    extra={
                        "symbol": sym,
                        "timeframe": itv,
                        "payload_type": type(raw_candles).__name__,
                    },
                )
                continue
            candles = list(raw_candles)
            if not candles:
                reason_counts[f"tf_no_candles_{itv}"] = reason_counts.get(f"tf_no_candles_{itv}", 0) + 1
                continue
            normalized: List[Candle] = []
            bad_rows = 0
            try:
                for row in candles:
                    parsed = _to_candle(row)
                    if parsed is None:
                        bad_rows += 1
                        continue
                    close = _safe_row_float(parsed.close, field="close")
                    if close is None:
                        bad_rows += 1
                        continue
                    if (
                        _safe_row_float(parsed.open, field="open") is None
                        or _safe_row_float(parsed.high, field="high") is None
                        or _safe_row_float(parsed.low, field="low") is None
                        or _safe_row_float(parsed.volume, field="volume") is None
                    ):
                        bad_rows += 1
                        continue
                    normalized.append(parsed)
            except Exception as e:
                exc_name = type(e).__name__
                tf_exc_key = f"tf_scoring_exception_{itv}_{exc_name}"
                reason_counts[tf_exc_key] = reason_counts.get(tf_exc_key, 0) + 1
                if exc_name == "TypeError":
                    reason_counts["scoring_exception_TypeError"] = reason_counts.get("scoring_exception_TypeError", 0) + 1
                logger.warning(
                    "scoring_timeframe_error",
                    extra={
                        "symbol": sym,
                        "timeframe": itv,
                        "error": str(e),
                        "error_type": exc_name,
                        "raw_payload_type": type(candles).__name__,
                    },
                    exc_info=True,
                )
                continue

            if bad_rows > 0:
                reason_counts[f"tf_bad_rows_{itv}"] = reason_counts.get(f"tf_bad_rows_{itv}", 0) + bad_rows

            closes = [float(c.close) for c in normalized]
            if len(closes) < required_len:
                reason_counts[f"tf_insufficient_bars_{itv}"] = reason_counts.get(f"tf_insufficient_bars_{itv}", 0) + 1
                continue
            # Keep indicator calculations stable with valid history only.
            candles = normalized

            try:
                # Use extra history for EMA stability.
                hist_len = max(1, int(self._ema_slow) * 3)
                hist = closes[-hist_len:]
                e_fast = ema(hist, int(self._ema_fast))
                e_slow = ema(hist, int(self._ema_slow))
                rsi_v = rsi(closes[-(self._rsi_period + 1):], self._rsi_period)
                roc_v = roc(closes, self._roc_period)
                am: Optional[AtrMult] = atr_mult(
                    candles,
                    period=self._atr_period,
                    mean_window=int(atr_mult_mean_window),
                )

                e_fast_f = float(e_fast) if e_fast is not None else 0.0
                e_slow_f = float(e_slow) if e_slow is not None else 0.0
                rsi_f = float(rsi_v) if rsi_v is not None else 50.0
                roc_f = float(roc_v) if roc_v is not None else 0.0
                atr_pct_f = float(am.atr_pct) if am is not None else 0.0
                atr_mean_f = float(am.atr_pct_mean) if am is not None else 0.0
                atr_mult_f = float(am.mult) if am is not None else 0.0

                if itv in {"4h", "1h"}:
                    regime_4h = _regime_4h(ema_fast=e_fast_f, ema_slow=e_slow_f, rsi_v=rsi_f)

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
                if atr_mult_f >= vol_shock_threshold:
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
            except Exception as e:
                exc_name = type(e).__name__
                tf_exc_key = f"tf_scoring_exception_{itv}_{exc_name}"
                reason_counts[tf_exc_key] = int(reason_counts.get(tf_exc_key, 0)) + 1
                reason_counts["scoring_exception_TypeError"] = int(
                    reason_counts.get("scoring_exception_TypeError", 0)
                ) + (1 if exc_name == "TypeError" else 0)
                logger.warning(
                    "scoring_timeframe_error",
                    extra={
                        "symbol": sym,
                        "timeframe": itv,
                        "error": str(e),
                        "error_type": exc_name,
                        "bar_count": len(normalized),
                    },
                    exc_info=True,
                )
                continue

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
        def _parse_bool(v: Any, default: bool = False) -> bool:
            if isinstance(v, bool):
                return v
            if v is None:
                return default
            text = str(v).strip().lower()
            if text in {"1", "true", "on", "yes", "y"}:
                return True
            if text in {"0", "false", "off", "no", "n", ""}:
                return False
            return default

        raw_weights = {
            "10m": _safe_nonneg_float(getattr(cfg, "tf_weight_10m", 0.25), default=0.25),
            "15m": _safe_nonneg_float(getattr(cfg, "tf_weight_15m", 0.0), default=0.0),
            "30m": _safe_nonneg_float(getattr(cfg, "tf_weight_30m", 0.25), default=0.25),
            "1h": _safe_nonneg_float(getattr(cfg, "tf_weight_1h", 0.25), default=0.25),
            "4h": _safe_nonneg_float(getattr(cfg, "tf_weight_4h", 0.25), default=0.25),
        }
        if not _parse_bool(getattr(cfg, "score_tf_15m_enabled", False), default=False):
            raw_weights.pop("15m", None)
        cleaned: Dict[str, float] = {}
        for itv, wt in list(raw_weights.items()):
            if wt > 0:
                cleaned[itv] = float(wt)

        if not cleaned:
            return {"10m": 0.25, "30m": 0.25, "1h": 0.25, "4h": 0.25}

        return cleaned
