from __future__ import annotations

from v2.control.position_management_runtime import build_position_management_plan
from v2.kernel.contracts import Candidate, ExecutionResult, KernelCycleResult, SizePlan
from v2.management import (
    PositionLifecycleEvent,
    PositionLifecycleState,
    PositionManagementSpec,
    advance_position_lifecycle,
    normalize_management_policy,
)


def test_position_management_spec_round_trips_runner_policy() -> None:
    spec = PositionManagementSpec(
        management_policy="tp1_runner",
        time_stop_bars=18,
        reward_risk_reference_r=2.0,
        tp_partial_ratio=0.25,
        tp_partial_at_r=1.2,
        move_stop_to_be_at_r=1.0,
        entry_quality_score_v2=0.82,
        entry_regime_strength=0.71,
        entry_bias_strength=0.66,
    )

    encoded = spec.to_execution_hints()
    decoded = PositionManagementSpec.from_execution_hints(encoded)

    assert decoded == spec
    assert decoded.uses_runner_management() is True


def test_normalize_management_policy_quarantines_unknown_policies() -> None:
    assert normalize_management_policy("tp1_runner") == "tp1_runner"
    assert normalize_management_policy("single_exit_2r") is None
    assert normalize_management_policy("unknown") is None


def test_position_lifecycle_state_machine_advances_runner_flow() -> None:
    state = advance_position_lifecycle(None, PositionLifecycleEvent.ENTRY_RECORDED)
    assert state == PositionLifecycleState.ENTRY_ARMED.value

    state = advance_position_lifecycle(state, PositionLifecycleEvent.TP1_COMPLETED)
    assert state == PositionLifecycleState.RUNNER_ACTIVE.value

    state = advance_position_lifecycle(state, PositionLifecycleEvent.EXIT_REQUESTED)
    assert state == PositionLifecycleState.EXIT_PENDING.value

    state = advance_position_lifecycle(state, PositionLifecycleEvent.EXIT_CONFIRMED)
    assert state == PositionLifecycleState.EXITED.value


def test_position_management_plan_builder_round_trips_shared_spec() -> None:
    spec = PositionManagementSpec(
        management_policy="tp1_runner",
        time_stop_bars=18,
        progress_check_bars=6,
        progress_min_mfe_r=0.35,
        progress_extend_trigger_r=1.0,
        progress_extend_bars=6,
        reward_risk_reference_r=2.0,
        tp_partial_ratio=0.25,
        tp_partial_at_r=1.2,
        move_stop_to_be_at_r=1.0,
        entry_quality_score_v2=0.82,
        entry_regime_strength=0.74,
        entry_bias_strength=0.68,
    )

    cycle = KernelCycleResult(
        state="executed",
        reason="executed",
        candidate=Candidate(
            symbol="BTCUSDT",
            side="BUY",
            score=0.91,
            entry_price=100.0,
            stop_price_hint=98.0,
            take_profit_hint=104.0,
            execution_hints=spec.to_execution_hints(),
        ),
        size=SizePlan(symbol="BTCUSDT", qty=0.01, leverage=3.0, notional=1.0),
        execution=ExecutionResult(ok=True, order_id="oid-1", reason="live_order_submitted"),
    )

    plan = build_position_management_plan(cycle=cycle)

    assert plan is not None
    assert plan["lifecycle_state"] == PositionLifecycleState.ENTRY_ARMED.value
    decoded = PositionManagementSpec.from_plan(plan)
    assert decoded == spec
