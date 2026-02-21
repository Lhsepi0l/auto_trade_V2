from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict

TradeSide = Literal["BUY", "SELL", "NONE"]
Regime = Literal["BULL", "BEAR", "SIDEWAYS", "UNKNOWN"]
AllowedSide = Literal["LONG", "SHORT", "BOTH", "NONE"]
Intent = Literal["LONG", "SHORT", "NONE"]


class DesiredPositionDict(TypedDict, total=False):
    symbol: str
    intent: Intent
    side: str
    score: float
    reason: str
    entry_price: float
    stop_hint: float
    management_hint: str
    regime: str
    allowed_side: str
    signals: dict[str, bool]
    blocks: list[str]
    indicators: dict[str, Any]


@dataclass(frozen=True)
class DesiredPosition:
    symbol: str
    intent: Intent = "NONE"
    side: TradeSide = "NONE"
    score: float = 0.0
    reason: str = ""
    entry_price: float | None = None
    stop_hint: float | None = None
    management_hint: str | None = None
    regime: Regime = "UNKNOWN"
    allowed_side: AllowedSide = "NONE"
    signals: dict[str, bool] = field(default_factory=dict)
    blocks: list[str] = field(default_factory=list)
    indicators: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> DesiredPositionDict:
        payload: DesiredPositionDict = {
            "symbol": self.symbol,
            "intent": self.intent,
            "side": self.side,
            "score": self.score,
            "reason": self.reason,
            "regime": self.regime,
            "allowed_side": self.allowed_side,
            "signals": self.signals,
            "blocks": self.blocks,
            "indicators": self.indicators,
        }
        if self.entry_price is not None:
            payload["entry_price"] = self.entry_price
        if self.stop_hint is not None:
            payload["stop_hint"] = self.stop_hint
        if self.management_hint is not None:
            payload["management_hint"] = self.management_hint
        return payload


class StrategyPlugin(Protocol):
    name: str

    def decide(self, market_snapshot: dict[str, Any]) -> dict[str, Any]: ...
