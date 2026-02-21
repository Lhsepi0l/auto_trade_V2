from __future__ import annotations

from v2.engine.state import EngineStateStore
from v2.exchange.types import ResyncSnapshot
from v2.storage import RuntimeStorage


def test_state_reconcile_and_ws_transition_with_reason_journal(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "v2_state.sqlite3"))
    store = EngineStateStore(storage=storage, mode="shadow")

    snapshot = ResyncSnapshot(
        open_orders=[
            {
                "clientOrderId": "cid-1",
                "orderId": 1001,
                "symbol": "BTCUSDT",
                "status": "NEW",
                "type": "LIMIT",
                "side": "BUY",
                "origQty": "0.01",
                "price": "100.0",
            }
        ],
        positions=[{"symbol": "BTCUSDT", "positionAmt": "0", "entryPrice": "0"}],
        balances=[{"asset": "USDT", "balance": "100.0"}],
    )
    store.startup_reconcile(snapshot=snapshot, reason="strategy_boot")

    state = store.get()
    assert "cid-1" in state.open_orders
    assert state.current_position == {}

    store.apply_exchange_event(
        event={
            "e": "ORDER_TRADE_UPDATE",
            "E": 1700000001000,
            "o": {
                "c": "cid-1",
                "i": 1001,
                "s": "BTCUSDT",
                "X": "FILLED",
                "o": "LIMIT",
                "S": "BUY",
                "q": "0.01",
                "l": "0.01",
                "L": "100.0",
                "x": "TRADE",
                "t": 90001,
                "rp": "0.1",
            },
        },
        reason="strategy_enter_signal",
    )
    store.apply_exchange_event(
        event={
            "e": "ACCOUNT_UPDATE",
            "E": 1700000002000,
            "a": {
                "P": [
                    {
                        "s": "BTCUSDT",
                        "pa": "0.01",
                        "ep": "100.0",
                        "up": "0.2",
                    }
                ]
            },
        },
        reason="ws_account_update",
    )

    after = store.get()
    assert "cid-1" not in after.open_orders
    assert len(after.last_fills) == 1
    assert after.current_position["BTCUSDT"].position_amt == 0.01

    journal = storage.list_journal_events(ascending=True)
    reasons = {str(row.get("reason") or "") for row in journal}
    assert "strategy_enter_signal" in reasons
    assert "ws_account_update" in reasons


def test_state_event_idempotency_prevents_duplicate_orders_and_fills(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "v2_idem.sqlite3"))
    store = EngineStateStore(storage=storage, mode="shadow")

    evt = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1700000003000,
        "o": {
            "c": "cid-idem-1",
            "i": 2002,
            "s": "ETHUSDT",
            "X": "FILLED",
            "o": "MARKET",
            "S": "SELL",
            "q": "0.05",
            "l": "0.05",
            "L": "2500.0",
            "x": "TRADE",
            "t": 81234,
            "rp": "-1.2",
        },
    }
    store.apply_exchange_event(event=evt, reason="idempotency-test")
    store.apply_exchange_event(event=evt, reason="idempotency-test")

    assert len(storage.list_journal_events()) == 1
    assert len(storage.list_orders()) == 1
    assert len(storage.recent_fills(limit=10)) == 1


def test_restart_and_replay_do_not_duplicate_orders(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "v2_replay.sqlite3"))
    store1 = EngineStateStore(storage=storage, mode="shadow")

    store1.apply_reconciliation(
        open_orders=[
            {
                "clientOrderId": "cid-r-1",
                "orderId": 3001,
                "symbol": "BTCUSDT",
                "status": "NEW",
                "type": "LIMIT",
                "side": "BUY",
                "origQty": "0.01",
                "price": "99.0",
            },
            {
                "clientOrderId": "cid-r-2",
                "orderId": 3002,
                "symbol": "ETHUSDT",
                "status": "NEW",
                "type": "LIMIT",
                "side": "SELL",
                "origQty": "0.02",
                "price": "2400.0",
            },
        ],
        positions=[],
        balances=[],
        reason="startup_reconcile",
        event_id="reconcile-fixed-1",
    )
    store1.apply_ops_mode(paused=True, safe_mode=True, reason="risk_trip", event_id="ops-fixed-1")
    assert len(storage.list_orders()) == 2

    store2 = EngineStateStore(storage=storage, mode="shadow")
    assert len(store2.get().open_orders) == 2

    replayed = store2.replay_from_journal()
    assert len(replayed.open_orders) == 2
    assert replayed.operational.paused is True
    assert replayed.operational.safe_mode is True

    store2.apply_reconciliation(
        open_orders=[
            {
                "clientOrderId": "cid-r-1",
                "orderId": 3001,
                "symbol": "BTCUSDT",
                "status": "NEW",
                "type": "LIMIT",
                "side": "BUY",
                "origQty": "0.01",
                "price": "99.0",
            },
            {
                "clientOrderId": "cid-r-2",
                "orderId": 3002,
                "symbol": "ETHUSDT",
                "status": "NEW",
                "type": "LIMIT",
                "side": "SELL",
                "origQty": "0.02",
                "price": "2400.0",
            },
        ],
        positions=[],
        balances=[],
        reason="startup_reconcile",
        event_id="reconcile-fixed-1",
    )

    assert len(storage.list_orders()) == 2
    assert len([row for row in storage.list_journal_events() if row.get("event_type") == "reconcile"]) == 1
