from __future__ import annotations

import json

from apps.trader_engine.services.oplog import OperationalLogger
from apps.trader_engine.storage.db import connect, migrate


def test_oplog_writes_tables(tmp_path) -> None:
    db_path = tmp_path / "test_oplog.sqlite3"
    db = connect(str(db_path))
    migrate(db)

    oplog = OperationalLogger.create(db=db, component="engine")
    oplog.log_event("TEST_EVENT", {"symbol": "BTCUSDT", "reason": "sanity"})
    oplog.log_decision(
        cycle_id="cycle-1",
        symbol="BTCUSDT",
        direction="LONG",
        confidence=0.8,
        regime_4h="BULL",
        scores_json={"composite": 0.9},
        reason="candidate_selected",
    )
    oplog.log_execution(
        intent_id="intent-1",
        symbol="BTCUSDT",
        side="BUY",
        qty=0.01,
        price=50000.0,
        order_type="LIMIT",
        client_order_id="cid-1",
        status="FILLED",
        reason="entry",
    )
    oplog.log_risk_block(
        intent_id="intent-2",
        symbol="ETHUSDT",
        block_reason="ENTRY_EXCEEDS_BUDGET_CAP",
        details_json={"source": "unit"},
    )

    c1 = db.query_one("SELECT COUNT(*) AS c FROM op_events")["c"]
    c2 = db.query_one("SELECT COUNT(*) AS c FROM decisions")["c"]
    c3 = db.query_one("SELECT COUNT(*) AS c FROM executions")["c"]
    c4 = db.query_one("SELECT COUNT(*) AS c FROM risk_blocks")["c"]

    assert int(c1) >= 4
    assert int(c2) == 1
    assert int(c3) == 1
    assert int(c4) == 1

    row = db.query_one("SELECT json FROM op_events ORDER BY ts DESC LIMIT 1")
    assert row is not None
    payload = json.loads(str(row["json"]))
    assert payload.get("run_id")
