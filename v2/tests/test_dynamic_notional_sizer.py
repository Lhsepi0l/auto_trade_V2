from __future__ import annotations

from v2.clean_room.contracts import Candidate, KernelContext, RiskDecision
from v2.clean_room.defaults import DynamicNotionalSizer, RiskAwareSizer


def _context() -> KernelContext:
    return KernelContext(mode="live", profile="ra_2026_alpha_v2_expansion_live_candidate", symbol="BTCUSDT", tick=1, dry_run=False)


def test_dynamic_notional_sizer_updates_default_leverage_on_runtime_change() -> None:
    sizer = DynamicNotionalSizer(fallback_notional=20.0, default_leverage=5.0)
    candidate = Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0)
    risk = RiskDecision(allow=True, reason="ok", max_notional=None)

    before = sizer.size(candidate=candidate, risk=risk, context=_context())
    assert before.leverage == 5.0
    assert before.notional == 100.0

    sizer.set_leverage_config(symbol_leverage_map={}, max_leverage=12.0)
    after = sizer.size(candidate=candidate, risk=risk, context=_context())

    assert after.leverage == 12.0
    assert after.notional == 240.0


def test_risk_aware_sizer_uses_runtime_max_leverage_when_candidate_cap_missing() -> None:
    sizer = RiskAwareSizer(fallback_notional=20.0, default_leverage=5.0)
    sizer.set_leverage_config(symbol_leverage_map={}, max_leverage=50.0)
    candidate = Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0)
    risk = RiskDecision(allow=True, reason="ok", max_notional=None)

    sized = sizer.size(candidate=candidate, risk=risk, context=_context())

    assert sized.leverage == 50.0
    assert sized.notional == 1000.0


def test_risk_aware_sizer_candidate_leverage_cap_does_not_inflate_margin_usage() -> None:
    sizer = RiskAwareSizer(fallback_notional=20.0, default_leverage=5.0)
    sizer.set_leverage_config(symbol_leverage_map={}, max_leverage=50.0)
    candidate = Candidate(
        symbol="BTCUSDT",
        side="BUY",
        score=1.0,
        entry_price=100.0,
        max_effective_leverage=10.0,
    )
    risk = RiskDecision(allow=True, reason="ok", max_notional=None)

    sized = sizer.size(candidate=candidate, risk=risk, context=_context())

    assert sized.leverage == 10.0
    assert sized.notional == 200.0


def test_risk_aware_sizer_preserves_operator_notional_when_strategy_risk_hints_exist() -> None:
    sizer = RiskAwareSizer(fallback_notional=10.0, default_leverage=10.0)
    sizer.set_leverage_config(symbol_leverage_map={"BTCUSDT": 10.0}, max_leverage=10.0)
    candidate = Candidate(
        symbol="BTCUSDT",
        side="BUY",
        score=1.0,
        entry_price=100.0,
        stop_distance_frac=0.012,
        risk_per_trade_pct=0.012,
    )
    risk = RiskDecision(allow=True, reason="ok", max_notional=None)

    sized = sizer.size(candidate=candidate, risk=risk, context=_context())

    assert sized.leverage == 10.0
    assert sized.notional == 100.0
    assert sized.qty == 1.0
