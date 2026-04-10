from __future__ import annotations

from typing import Any

from v2.kernel import Candidate
from v2.kernel.portfolio import portfolio_bucket_for_symbol


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _candidate_to_payload(candidate: Candidate) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "side": candidate.side,
        "score": float(candidate.score),
        "raw_score": candidate.raw_score,
        "portfolio_score": candidate.portfolio_score,
        "portfolio_bucket": candidate.portfolio_bucket,
        "volume_quality": candidate.volume_quality,
        "edge_efficiency": candidate.edge_efficiency,
        "spread_penalty": candidate.spread_penalty,
        "correlation_penalty": candidate.correlation_penalty,
        "alpha_id": candidate.alpha_id,
        "entry_family": candidate.entry_family,
        "reason": candidate.reason,
        "entry_price": candidate.entry_price,
        "stop_price_hint": candidate.stop_price_hint,
        "stop_distance_frac": candidate.stop_distance_frac,
        "regime_hint": candidate.regime_hint,
        "regime_strength": candidate.regime_strength,
        "risk_per_trade_pct": candidate.risk_per_trade_pct,
        "max_effective_leverage": candidate.max_effective_leverage,
        "expected_move_frac": candidate.expected_move_frac,
        "required_move_frac": candidate.required_move_frac,
        "spread_pct": candidate.spread_pct,
    }


def _candidate_from_payload(payload: dict[str, Any]) -> Candidate | None:
    symbol = str(payload.get("symbol") or "").strip().upper()
    side = str(payload.get("side") or "").strip().upper()
    score = _to_float(payload.get("score"))
    if not symbol or side not in {"BUY", "SELL"} or score is None:
        return None
    return Candidate(
        symbol=symbol,
        side="BUY" if side == "BUY" else "SELL",
        score=float(score),
        raw_score=_to_float(payload.get("raw_score")),
        portfolio_score=_to_float(payload.get("portfolio_score")),
        portfolio_bucket=str(
            payload.get("portfolio_bucket") or portfolio_bucket_for_symbol(symbol)
        ).strip(),
        volume_quality=_to_float(payload.get("volume_quality")),
        edge_efficiency=_to_float(payload.get("edge_efficiency")),
        spread_penalty=_to_float(payload.get("spread_penalty")),
        correlation_penalty=_to_float(payload.get("correlation_penalty")),
        alpha_id=str(payload.get("alpha_id") or "").strip() or None,
        entry_family=str(payload.get("entry_family") or "").strip() or None,
        reason=str(payload.get("reason") or "").strip() or None,
        entry_price=_to_float(payload.get("entry_price")),
        stop_price_hint=_to_float(payload.get("stop_price_hint")),
        stop_distance_frac=_to_float(payload.get("stop_distance_frac")),
        regime_hint=str(payload.get("regime_hint") or "").strip().upper() or None,
        regime_strength=_to_float(payload.get("regime_strength")),
        risk_per_trade_pct=_to_float(payload.get("risk_per_trade_pct")),
        max_effective_leverage=_to_float(payload.get("max_effective_leverage")),
        expected_move_frac=_to_float(payload.get("expected_move_frac")),
        required_move_frac=_to_float(payload.get("required_move_frac")),
        spread_pct=_to_float(payload.get("spread_pct")),
    )
