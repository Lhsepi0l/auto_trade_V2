from __future__ import annotations

from dataclasses import dataclass

from v2.clean_room import (
    AlwaysAllowedRiskGate,
    Candidate,
    ExecutionResult,
    FixedNotionalSizer,
    KernelContext,
    KernelCycleResult,
    RiskDecision,
    TradeKernel,
    TradeKernelConfig,
    build_default_kernel,
)
from v2.config.loader import load_effective_config
from v2.engine import EngineStateStore
from v2.storage import RuntimeStorage


@dataclass(frozen=True)
class FixedCandidateSelector:
    candidate: Candidate | None

    def select(self, *, context: KernelContext) -> Candidate | None:
        _ = context
        return self.candidate


@dataclass(frozen=True)
class RejectingRiskGate:
    reason: str

    def evaluate(self, *, candidate: Candidate, context: KernelContext) -> RiskDecision:
        _ = candidate
        _ = context
        return RiskDecision(allow=False, reason=self.reason)


class ExecutionCounting:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, candidate: Candidate, size: object, context: KernelContext) -> ExecutionResult:
        _ = context
        _ = candidate
        _ = size
        self.calls += 1
        return ExecutionResult(ok=True, order_id="order-1", reason="ok")


def _state_store(tmp_path) -> EngineStateStore:
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "kernel.sqlite3"))
    storage.ensure_schema()
    return EngineStateStore(storage=storage, mode="shadow")


def test_no_candidate_returns_no_candidate(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _state_store(tmp_path)
    kernel = TradeKernel(
        state_store=store,
        candidate_selector=FixedCandidateSelector(candidate=None),
        risk_gate=AlwaysAllowedRiskGate(),
        sizer=FixedNotionalSizer(),
        executor=ExecutionCounting(),
        config=TradeKernelConfig(mode="shadow", profile="normal", default_symbol="BTCUSDT", dry_run=True),
    )

    result = kernel.run_once()
    assert result.state == "no_candidate"


def test_ops_paused_blocks_execution(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _state_store(tmp_path)
    store.apply_ops_mode(paused=True, reason="test")

    selector = FixedCandidateSelector(
        candidate=Candidate(
            symbol="BTCUSDT",
            side="BUY",
            score=1.0,
            entry_price=100.0,
        )
    )
    executor = ExecutionCounting()
    kernel = TradeKernel(
        state_store=store,
        candidate_selector=selector,
        risk_gate=AlwaysAllowedRiskGate(),
        sizer=FixedNotionalSizer(),
        executor=executor,
        config=TradeKernelConfig(mode="shadow", profile="normal", default_symbol="BTCUSDT", dry_run=True),
    )

    result = kernel.run_once()
    assert result.state == "blocked"
    assert executor.calls == 0


def test_risk_reject_prevents_execute(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _state_store(tmp_path)
    selector = FixedCandidateSelector(
        candidate=Candidate(
            symbol="BTCUSDT",
            side="BUY",
            score=1.0,
            entry_price=100.0,
        )
    )
    executor = ExecutionCounting()

    kernel = TradeKernel(
        state_store=store,
        candidate_selector=selector,
        risk_gate=RejectingRiskGate(reason="policy"),
        sizer=FixedNotionalSizer(),
        executor=executor,
        config=TradeKernelConfig(mode="shadow", profile="normal", default_symbol="BTCUSDT", dry_run=True),
    )

    result = kernel.run_once()
    assert result.state == "risk_rejected"
    assert executor.calls == 0


def test_default_kernel_runs_shadow_as_dry_run(tmp_path) -> None:
    cfg = load_effective_config(profile="normal", mode="shadow")
    cfg.behavior.exchange.default_symbol = "BTCUSDT"
    store = _state_store(tmp_path)

    kernel = build_default_kernel(
        state_store=store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
    )

    result: KernelCycleResult = kernel.run_once()
    assert result.state in {"no_candidate", "dry_run", "executed", "risk_rejected"}


def test_default_kernel_uses_strategy_pack_v1_selector(tmp_path) -> None:
    cfg = load_effective_config(profile="normal", mode="shadow")
    cfg.behavior.exchange.default_symbol = "BTCUSDT"
    store = _state_store(tmp_path)

    kernel = build_default_kernel(
        state_store=store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
    )

    assert kernel._selector.__class__.__name__ == "StrategyPackV1CandidateSelector"
