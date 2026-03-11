from __future__ import annotations

from v2.clean_room.contracts import Candidate, KernelContext, RiskDecision
from v2.clean_room.defaults import DynamicNotionalSizer


def _context() -> KernelContext:
    return KernelContext(mode="live", profile="ra_2026_alpha_v2_expansion_live_candidate", symbol="BTCUSDT", tick=1, dry_run=False)


def test_dynamic_notional_sizer_updates_default_leverage_on_runtime_change() -> None:
    sizer = DynamicNotionalSizer(fallback_notional=20.0, default_leverage=5.0)
    candidate = Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0)
    risk = RiskDecision(allow=True, reason="ok", max_notional=None)

    before = sizer.size(candidate=candidate, risk=risk, context=_context())
    assert before.leverage == 5.0

    sizer.set_leverage_config(symbol_leverage_map={}, max_leverage=12.0)
    after = sizer.size(candidate=candidate, risk=risk, context=_context())

    assert after.leverage == 12.0
    assert after.notional == 20.0
