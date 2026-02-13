from __future__ import annotations

import pytest

from apps.trader_engine.services.snapshot_service import SnapshotService


@pytest.mark.unit
def test_calc_unrealized_long() -> None:
    side, upnl, upnl_pct = SnapshotService.calc_unrealized(qty=2.0, entry_price=100.0, mark_price=110.0)
    assert side == "LONG"
    assert upnl == pytest.approx(20.0, rel=1e-6)
    assert upnl_pct == pytest.approx(10.0, rel=1e-6)


@pytest.mark.unit
def test_calc_unrealized_short() -> None:
    side, upnl, upnl_pct = SnapshotService.calc_unrealized(qty=-2.0, entry_price=100.0, mark_price=90.0)
    assert side == "SHORT"
    assert upnl == pytest.approx(20.0, rel=1e-6)
    assert upnl_pct == pytest.approx(10.0, rel=1e-6)
