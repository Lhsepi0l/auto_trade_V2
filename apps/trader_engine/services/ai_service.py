from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Literal, Mapping, Optional

logger = logging.getLogger(__name__)


TargetAsset = Literal["BTCUSDT", "ETHUSDT", "XAUUSDT"]
SignalDirection = Literal["LONG", "SHORT", "HOLD"]
SignalExecHint = Literal["MARKET", "LIMIT", "SPLIT"]
RiskTag = Literal["NORMAL", "VOL_SHOCK", "NEWS_RISK"]


@dataclass(frozen=True)
class AiSignal:
    """AI signal output (signal only, no execution authority)."""

    target_asset: TargetAsset
    direction: SignalDirection
    confidence: float  # 0..1
    exec_hint: SignalExecHint
    risk_tag: RiskTag
    notes: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "target_asset": self.target_asset,
            "direction": self.direction,
            "confidence": float(self.confidence),
            "exec_hint": self.exec_hint,
            "risk_tag": self.risk_tag,
            "notes": self.notes,
        }


class AiService:
    """AI interface layer.

    IMPORTANT SECURITY BOUNDARY:
    - This service returns signals only.
    - It must never call execution / place orders.
    - Actual execution remains owned by Risk Guard + ExecutionService.
    """

    def __init__(
        self,
        *,
        mode: str = "stub",
        conf_threshold: float = 0.65,
        manual_risk_tag: str = "",
    ) -> None:
        self._mode = str(mode or "stub").strip().lower()
        self._conf_threshold = float(conf_threshold)
        self._manual_risk_tag = str(manual_risk_tag or "").strip().upper()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def conf_threshold(self) -> float:
        return self._conf_threshold

    def get_signal(self, context: Mapping[str, Any]) -> AiSignal:
        """Return an AI recommendation signal based on the provided context.

        Context is a plain mapping to keep the interface stable.
        """
        if self._mode == "stub":
            return self._stub_signal(context)
        if self._mode == "openai":
            raise NotImplementedError("AI_MODE=openai is not wired in MVP; use stub mode")
        if self._mode == "local":
            raise NotImplementedError("AI_MODE=local is not wired in MVP; use stub mode")
        # Fail closed: unknown modes fall back to stub.
        logger.warning("ai_mode_unknown_fallback_to_stub", extra={"mode": self._mode})
        return self._stub_signal(context)

    def _stub_signal(self, context: Mapping[str, Any]) -> AiSignal:
        # Deterministic: derive from scheduler candidate + scores.
        cand = context.get("candidate") or {}
        sym = str((cand.get("symbol") or "BTCUSDT")).upper()
        if sym not in ("BTCUSDT", "ETHUSDT", "XAUUSDT"):
            sym = "BTCUSDT"

        # Candidate fields (from DecisionService.pick_candidate)
        direction = str(cand.get("direction") or "HOLD").upper()
        strength = float(cand.get("strength") or 0.0)
        vol_tag = str(cand.get("vol_tag") or "NORMAL").upper()

        # Manual override tag if provided.
        risk_tag: RiskTag
        if self._manual_risk_tag == "NEWS_RISK":
            risk_tag = "NEWS_RISK"
        elif vol_tag == "VOL_SHOCK":
            risk_tag = "VOL_SHOCK"
        else:
            risk_tag = "NORMAL"

        if direction not in ("LONG", "SHORT"):
            return AiSignal(
                target_asset=sym,  # type: ignore[return-value]
                direction="HOLD",
                confidence=0.0,
                exec_hint="LIMIT",
                risk_tag=risk_tag,
                notes="no_candidate",
            )

        conf = max(0.0, min(1.0, strength))
        if conf < self._conf_threshold:
            return AiSignal(
                target_asset=sym,  # type: ignore[return-value]
                direction="HOLD",
                confidence=conf,
                exec_hint="LIMIT",
                risk_tag=risk_tag,
                notes=f"below_threshold:{self._conf_threshold}",
            )

        # In stub: recommend LIMIT by default (safer with spread guards).
        return AiSignal(
            target_asset=sym,  # type: ignore[return-value]
            direction="LONG" if direction == "LONG" else "SHORT",
            confidence=conf,
            exec_hint="LIMIT",
            risk_tag=risk_tag,
            notes="stub_from_rule_candidate",
        )
