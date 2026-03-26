from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

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
    daily_loss_limit_pct: float | None = None
    dd_limit_pct: float | None = None
    daily_loss_used_pct: float | None = None
    dd_used_pct: float | None = None
    lose_streak: int | None = None
    cooldown_until: float | None = None
    risk_score_min: float | None = None
    spread_max_pct: float | None = None
    dd_scale_start_pct: float | None = None
    dd_scale_max_pct: float | None = None
    dd_scale_min_factor: float | None = None
    recent_blocks: dict[str, int] | None = None


@dataclass(frozen=True)
class Candidate:
    symbol: str
    side: TradeSide
    score: float
    raw_score: float | None = None
    portfolio_score: float | None = None
    portfolio_bucket: str | None = None
    volume_quality: float | None = None
    edge_efficiency: float | None = None
    spread_penalty: float | None = None
    correlation_penalty: float | None = None
    alpha_id: str | None = None
    entry_family: str | None = None
    reason: str | None = None
    source: str | None = None
    entry_price: float | None = None
    stop_price_hint: float | None = None
    stop_distance_frac: float | None = None
    volatility_hint: float | None = None
    regime_hint: str | None = None
    regime_strength: float | None = None
    risk_per_trade_pct: float | None = None
    max_effective_leverage: float | None = None
    expected_move_frac: float | None = None
    required_move_frac: float | None = None
    spread_pct: float | None = None
    take_profit_hint: float | None = None
    execution_hints: dict[str, float | int | bool] | None = None


@dataclass(frozen=True)
class RiskDecision:
    allow: bool
    reason: str
    max_notional: float | None = None
    size_factor: float | None = None


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


@dataclass(frozen=True)
class PortfolioCycleResult:
    primary_result: KernelCycleResult
    selected_candidates: list[Candidate] = field(default_factory=list)
    results: list[KernelCycleResult] = field(default_factory=list)
    blocked_reasons: dict[str, int] = field(default_factory=dict)
    open_position_count: int = 0
    max_open_positions: int = 1


class CandidateSelector(Protocol):
    def select(self, *, context: KernelContext) -> Candidate | None: ...


@runtime_checkable
class RankedCandidateSelector(Protocol):
    def rank(self, *, context: KernelContext) -> list[Candidate]: ...


class RiskGate(Protocol):
    def evaluate(self, *, candidate: Candidate, context: KernelContext) -> RiskDecision: ...


class Sizer(Protocol):
    def size(
        self,
        *,
        candidate: Candidate,
        risk: RiskDecision,
        context: KernelContext,
    ) -> SizePlan: ...


class ExecutionService(Protocol):
    def execute(
        self,
        *,
        candidate: Candidate,
        size: SizePlan,
        context: KernelContext,
    ) -> ExecutionResult: ...
