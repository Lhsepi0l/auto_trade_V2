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
    candidate_symbol: Optional[str] = None
    candidate_direction: Optional[str] = None
    candidate_regime_4h: Optional[str] = None
    candidate_strength: Optional[float] = None
    candidate_confidence: Optional[float] = None
    final_direction: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "enter_symbol": self.enter_symbol,
            "enter_direction": self.enter_direction,
            "close_symbol": self.close_symbol,
            "candidate_symbol": self.candidate_symbol,
            "candidate_direction": self.candidate_direction,
            "candidate_regime_4h": self.candidate_regime_4h,
            "candidate_strength": self.candidate_strength,
            "candidate_confidence": self.candidate_confidence,
            "final_direction": self.final_direction,
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
        def _with_context(
            base: StrategyDecision,
            *,
            context: Optional[Candidate] = None,
            final_direction: Optional[str] = None,
        ) -> StrategyDecision:
            return StrategyDecision(
                kind=base.kind,
                reason=base.reason,
                enter_symbol=base.enter_symbol,
                enter_direction=base.enter_direction,
                close_symbol=base.close_symbol,
                candidate_symbol=context.symbol if context else None,
                candidate_direction=str(context.direction) if context else None,
                candidate_regime_4h=str(context.regime_4h) if context else None,
                candidate_strength=float(context.strength) if context else None,
                candidate_confidence=float(context.confidence) if context else None,
                final_direction=final_direction if final_direction is not None else base.enter_direction,
            )

        ts = now or _utcnow()
        pos_sym = (position.symbol or "").upper() if position.symbol else None

        # No position: try to enter on candidate.
        if not pos_sym:
            if not candidate:
                return _with_context(StrategyDecision(kind="HOLD", reason="no_candidate"), context=candidate)
            if candidate.vol_shock:
                return _with_context(StrategyDecision(kind="HOLD", reason="vol_shock_no_entry"), context=candidate)
            if float(candidate.confidence) < float(cfg.score_conf_threshold):
                return _with_context(
                    StrategyDecision(kind="HOLD", reason="confidence_below_threshold"),
                    context=candidate,
                )
            if candidate.direction == "SHORT" and candidate.regime_4h != "BEAR":
                return _with_context(
                    StrategyDecision(kind="HOLD", reason="short_not_allowed_regime"),
                    context=candidate,
                )
            return _with_context(
                StrategyDecision(
                    kind="ENTER",
                    reason="enter_candidate",
                    enter_symbol=candidate.symbol,
                    enter_direction=candidate.direction,
                ),
                context=candidate,
                final_direction=candidate.direction,
            )

        # Position exists.
        sym_score = scores.get(pos_sym)
        if sym_score and sym_score.vol_shock:
            return _with_context(
                StrategyDecision(kind="CLOSE", reason="vol_shock_close", close_symbol=pos_sym),
                context=candidate,
            )

        # Profit hold rule.
        if float(position.unrealized_pnl or 0.0) > 0.0:
            return _with_context(StrategyDecision(kind="HOLD", reason="profit_hold"), context=candidate)

        # If no candidate, just hold.
        if not candidate:
            return _with_context(StrategyDecision(kind="HOLD", reason="no_candidate"), context=candidate)

        # Candidate might be same symbol; avoid churn.
        if candidate.symbol.upper() == pos_sym:
            return _with_context(StrategyDecision(kind="HOLD", reason="same_symbol"), context=candidate)

        # Min-hold guard before rebalancing (unless shock, handled above).
        min_hold = int(cfg.min_hold_minutes)
        if min_hold > 0 and position.last_entry_at and position.last_entry_symbol:
            if position.last_entry_symbol.upper() == pos_sym:
                held_min = (ts - position.last_entry_at).total_seconds() / 60.0
                if held_min < float(min_hold):
                    return _with_context(
                        StrategyDecision(kind="HOLD", reason=f"min_hold_active:{int(held_min)}/{min_hold}"),
                        context=candidate,
                    )

        # Confidence threshold.
        if float(candidate.confidence) < float(cfg.score_conf_threshold):
            return _with_context(
                StrategyDecision(kind="HOLD", reason="confidence_below_threshold"),
                context=candidate,
            )

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
            return _with_context(
                StrategyDecision(kind="HOLD", reason="gap_below_threshold"),
                context=candidate,
            )

        # Short restriction.
        if candidate.direction == "SHORT" and candidate.regime_4h != "BEAR":
            return _with_context(
                StrategyDecision(kind="HOLD", reason="short_not_allowed_regime"),
                context=candidate,
            )

        return _with_context(
            StrategyDecision(
                kind="REBALANCE",
                reason="rebalance_to_better_candidate",
                close_symbol=pos_sym,
                enter_symbol=candidate.symbol,
                enter_direction=candidate.direction,
            ),
            context=candidate,
            final_direction=candidate.direction,
        )
