from __future__ import annotations

import pytest

from apps.trader_engine.services.oplog import OperationalLogger
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.snapshot_service import SnapshotService
from apps.trader_engine.storage.db import connect, migrate
from apps.trader_engine.storage.repositories import PnLStateRepo
from tests.fixtures.fake_exchange import FakeBinanceRest


@pytest.mark.integration
def test_snapshot_insert_fields_with_fake_exchange(tmp_path) -> None:
    db = connect(str(tmp_path / "snapshot.sqlite3"))
    migrate(db)
    pnl = PnLService(repo=PnLStateRepo(db))
    ex = FakeBinanceRest()
    ex.positions["BTCUSDT"] = {"position_amt": 0.5, "entry_price": 100.0, "unrealized_pnl": 0.0, "leverage": 1.0}
    ex.set_book("BTCUSDT", bid=109.5, ask=110.5, mark=110.0)

    svc = SnapshotService(db=db, client=ex, pnl=pnl, oplog=OperationalLogger.create(db=db))
    row = svc.capture_snapshot(reason="integration_test", preferred_symbol="BTCUSDT")
    assert row is not None
    assert row.symbol == "BTCUSDT"
    assert row.position_side == "LONG"
    assert row.qty == pytest.approx(0.5, rel=1e-6)
    assert row.entry_price == pytest.approx(100.0, rel=1e-6)
    assert row.mark_price == pytest.approx(110.0, rel=1e-6)
    assert row.unrealized_pnl_usdt == pytest.approx(5.0, rel=1e-6)
    assert row.unrealized_pnl_pct == pytest.approx(10.0, rel=1e-6)

    db_row = db.query_one(
        """
        SELECT symbol, position_side, qty, entry_price, mark_price,
               unrealized_pnl_usdt, unrealized_pnl_pct, equity_usdt, available_usdt
        FROM pnl_snapshots
        ORDER BY ts DESC
        LIMIT 1
        """.strip()
    )
    assert db_row is not None
    assert str(db_row["symbol"]) == "BTCUSDT"
    assert str(db_row["position_side"]) == "LONG"
    assert float(db_row["qty"]) == pytest.approx(0.5, rel=1e-6)
    assert float(db_row["unrealized_pnl_usdt"]) == pytest.approx(5.0, rel=1e-6)
