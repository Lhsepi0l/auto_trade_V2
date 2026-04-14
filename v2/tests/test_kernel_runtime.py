from __future__ import annotations

from dataclasses import dataclass

from v2.config.loader import load_effective_config
from v2.engine import EngineStateStore
from v2.exchange.types import ResyncSnapshot
from v2.kernel import (
    AlwaysAllowedRiskGate,
    Candidate,
    ExecutionResult,
    FixedNotionalSizer,
    KernelContext,
    KernelCycleResult,
    LiveRuntimeRiskGate,
    RiskDecision,
    TradeKernel,
    TradeKernelConfig,
    build_default_kernel,
)
from v2.kernel.kernel import _build_overheat_fetcher
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

    def execute(
        self, *, candidate: Candidate, size: object, context: KernelContext
    ) -> ExecutionResult:
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
        config=TradeKernelConfig(
            mode="shadow",
            profile="ra_2026_alpha_v2_expansion_live_candidate",
            default_symbol="BTCUSDT",
            dry_run=True,
        ),
    )

    result = kernel.run_once()
    assert result.state == "no_candidate"


def test_ops_paused_blocks_execution(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _state_store(tmp_path)
    store.apply_ops_mode(paused=True, reason="test")

    selector = FixedCandidateSelector(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0)
    )
    executor = ExecutionCounting()
    kernel = TradeKernel(
        state_store=store,
        candidate_selector=selector,
        risk_gate=AlwaysAllowedRiskGate(),
        sizer=FixedNotionalSizer(),
        executor=executor,
        config=TradeKernelConfig(
            mode="shadow",
            profile="ra_2026_alpha_v2_expansion_live_candidate",
            default_symbol="BTCUSDT",
            dry_run=True,
        ),
    )

    result = kernel.run_once()
    assert result.state == "blocked"
    assert executor.calls == 0


def test_position_open_blocks_same_symbol_when_reentry_disabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _state_store(tmp_path)
    store.startup_reconcile(
        snapshot=ResyncSnapshot(positions=[{"symbol": "BTCUSDT", "positionAmt": "0.01"}]),
        reason="same_symbol_block",
    )

    selector = FixedCandidateSelector(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0)
    )
    executor = ExecutionCounting()
    kernel = TradeKernel(
        state_store=store,
        candidate_selector=selector,
        risk_gate=AlwaysAllowedRiskGate(),
        sizer=FixedNotionalSizer(),
        executor=executor,
        config=TradeKernelConfig(
            mode="shadow",
            profile="ra_2026_alpha_v2_expansion_live_candidate",
            default_symbol="BTCUSDT",
            dry_run=True,
        ),
    )

    result = kernel.run_once()
    assert result.state == "blocked"
    assert result.reason == "position_open"
    assert executor.calls == 0


def test_position_open_allows_other_symbol_scan_when_reentry_disabled(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    store = _state_store(tmp_path)
    store.startup_reconcile(
        snapshot=ResyncSnapshot(positions=[{"symbol": "BTCUSDT", "positionAmt": "0.01"}]),
        reason="cross_symbol_allow",
    )

    selector = FixedCandidateSelector(
        candidate=Candidate(symbol="ETHUSDT", side="BUY", score=1.0, entry_price=100.0)
    )
    executor = ExecutionCounting()
    kernel = TradeKernel(
        state_store=store,
        candidate_selector=selector,
        risk_gate=AlwaysAllowedRiskGate(),
        sizer=FixedNotionalSizer(),
        executor=executor,
        config=TradeKernelConfig(
            mode="shadow",
            profile="ra_2026_alpha_v2_expansion_live_candidate",
            default_symbol="BTCUSDT",
            dry_run=True,
        ),
    )

    result = kernel.run_once()
    assert result.state == "dry_run"
    assert executor.calls == 1


def test_risk_reject_prevents_execute(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _state_store(tmp_path)
    selector = FixedCandidateSelector(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0)
    )
    executor = ExecutionCounting()

    kernel = TradeKernel(
        state_store=store,
        candidate_selector=selector,
        risk_gate=RejectingRiskGate(reason="policy"),
        sizer=FixedNotionalSizer(),
        executor=executor,
        config=TradeKernelConfig(
            mode="shadow",
            profile="ra_2026_alpha_v2_expansion_live_candidate",
            default_symbol="BTCUSDT",
            dry_run=True,
        ),
    )

    result = kernel.run_once()
    assert result.state == "risk_rejected"
    assert executor.calls == 0


def test_live_runtime_risk_gate_blocks_when_daily_trade_cap_reached() -> None:
    gate = LiveRuntimeRiskGate()
    candidate = Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0)
    context = KernelContext(
        mode="shadow",
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        symbol="BTCUSDT",
        tick=1,
        dry_run=True,
        max_trades_per_day_per_symbol=1,
        daily_trade_entry_counts={"BTCUSDT": 1},
    )

    decision = gate.evaluate(candidate=candidate, context=context)

    assert decision.allow is False
    assert decision.reason == "daily_trade_cap"


def test_live_runtime_risk_gate_blocks_when_reward_risk_below_threshold() -> None:
    gate = LiveRuntimeRiskGate()
    candidate = Candidate(
        symbol="BTCUSDT",
        side="BUY",
        score=1.0,
        entry_price=100.0,
        stop_distance_frac=0.02,
        take_profit_hint=102.0,
    )
    context = KernelContext(
        mode="shadow",
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        symbol="BTCUSDT",
        tick=1,
        dry_run=True,
        min_reward_risk_ratio=2.0,
    )

    decision = gate.evaluate(candidate=candidate, context=context)

    assert decision.allow is False
    assert decision.reason == "reward_risk_block"


def test_default_kernel_runs_shadow_as_dry_run(tmp_path) -> None:
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow")
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


def test_default_kernel_uses_alpha_selector_for_default_profile(tmp_path) -> None:
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow")
    cfg.behavior.exchange.default_symbol = "BTCUSDT"
    store = _state_store(tmp_path)

    kernel = build_default_kernel(
        state_store=store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
    )

    assert kernel._selector.__class__.__name__ == "RA2026AlphaV2CandidateSelector"


def test_overheat_fetcher_uses_requested_symbol_and_symbol_cache() -> None:
    class FakeRestClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def public_request(self, method: str, path: str, params: dict[str, object]):
            _ = method
            symbol = str(params.get("symbol") or "")
            self.calls.append(f"{path}:{symbol}")
            if path.endswith("/premiumIndex"):
                return {"lastFundingRate": "0.001" if symbol == "BTCUSDT" else "0.0"}
            return [{"longShortRatio": "1.6" if symbol == "BTCUSDT" else "1.0"}]

    rest = FakeRestClient()
    fetcher = _build_overheat_fetcher(rest_client=rest, symbol="BTCUSDT")
    assert fetcher is not None

    first = fetcher("BTCUSDT")
    second = fetcher("ETHUSDT")
    third = fetcher("BTCUSDT")

    assert first == (0.001, 1.6)
    assert second == (0.0, 1.0)
    assert third == first
    assert rest.calls.count("/fapi/v1/premiumIndex:BTCUSDT") == 1
    assert rest.calls.count("/fapi/v1/premiumIndex:ETHUSDT") == 1
