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

