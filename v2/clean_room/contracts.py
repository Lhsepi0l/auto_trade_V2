from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

TradeSide = Literal["BUY", "SELL"]
CycleState = Literal[
    "blocked",
    "no_candidate",
    "risk_rejected",
    "size_invalid",
    "executed",
    "dry_run",
    "execution_failed",
]


@dataclass(frozen=True)
class KernelContext:
    mode: str
    profile: str
    symbol: str
    tick: int
    dry_run: bool


@dataclass(frozen=True)
class Candidate:
    symbol: str
    side: TradeSide
    score: float
    reason: str | None = None
    entry_price: float | None = None
    volatility_hint: float | None = None


@dataclass(frozen=True)
class RiskDecision:
    allow: bool
    reason: str
    max_notional: float | None = None


@dataclass(frozen=True)
class SizePlan:
    symbol: str
    qty: float
    leverage: float
    notional: float
    reason: str | None = None


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    order_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class KernelCycleResult:
    state: CycleState
    reason: str
    candidate: Candidate | None
    risk: RiskDecision | None = None
    size: SizePlan | None = None
    execution: ExecutionResult | None = None


class CandidateSelector(Protocol):
    def select(self, *, context: KernelContext) -> Candidate | None:
        ...


class RiskGate(Protocol):
    def evaluate(self, *, candidate: Candidate, context: KernelContext) -> RiskDecision:
        ...


class Sizer(Protocol):
    def size(
        self,
        *,
        candidate: Candidate,
        risk: RiskDecision,
        context: KernelContext,
    ) -> SizePlan:
        ...


class ExecutionService(Protocol):
    def execute(
        self,
        *,
        candidate: Candidate,
        size: SizePlan,
        context: KernelContext,
    ) -> ExecutionResult:
        ...
