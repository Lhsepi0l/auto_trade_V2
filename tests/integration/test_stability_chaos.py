from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from apps.trader_engine.main import create_app
from apps.trader_engine.storage.db import close, connect
from apps.trader_engine.storage.repositories import OrderRecordRepo
from tests.fixtures.fake_exchange import FakeBinanceRest


class _TimeoutLimitOnceExchange(FakeBinanceRest):
    def __init__(self) -> None:
        super().__init__()
        self.limit_fill_mode = "never_fill"
        self._timeout_once = True
        self._timeout_order_ids: set[int] = set()

    def place_order_limit(self, **kwargs):  # type: ignore[no-untyped-def]
        order = super().place_order_limit(**kwargs)
        if self._timeout_once:
            self._timeout_once = False
            try:
                self._timeout_order_ids.add(int(order.get("orderId")))
            except Exception:
                pass
            raise TimeoutError("timed out placing limit order")
        return order

    def get_order(self, *, symbol: str, order_id: int):  # type: ignore[override]
        if int(order_id) in self._timeout_order_ids:
            raise TimeoutError("timeout while polling order status")
        return super().get_order(symbol=symbol, order_id=order_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chaos_timeout_restart_reconcile_no_duplicate_entry(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db_path = str(tmp_path / "chaos.sqlite3")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("TRADING_DRY_RUN", "false")
    monkeypatch.setenv("DRY_RUN_STRICT", "false")

    ex = _TimeoutLimitOnceExchange()
    ex.set_book("BTCUSDT", bid=90.0, ask=110.0, mark=100.0)  # wide spread blocks market fallback

    app1 = create_app(
        test_mode=True,
        test_overrides={
            "binance_client": ex,
            "disable_background_tasks": True,
            "skip_binance_startup": False,
        },
    )
    async with LifespanManager(app1):
        transport = httpx.ASGITransport(app=app1)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/start")
            assert r.status_code == 200

            # Keep the chaos run short and deterministic.
            r = await client.post("/set", json={"key": "exec_limit_retries", "value": "1"})
            assert r.status_code == 200

            # a) place LIMIT entry, b) timeout happens during placement/poll.
            r = await client.post(
                "/trade/enter",
                json={
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "exec_hint": "LIMIT",
                    "qty": 0.1,
                },
            )
            assert r.status_code == 409

            assert len(ex.open_orders) == 1
            cid = str(ex.open_orders[0].get("clientOrderId") or "")
            assert cid != ""

    # c) restart engine/services, d) exchange still reports the same open order.
    app2 = create_app(
        test_mode=True,
        test_overrides={
            "binance_client": ex,
            "disable_background_tasks": True,
            "skip_binance_startup": False,
        },
    )
    async with LifespanManager(app2):
        transport = httpx.ASGITransport(app=app2)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Startup reconcile should have normalized order_records with the existing exchange order.
            db = connect(db_path)
            try:
                repo = OrderRecordRepo(db)
                rec = repo.get_by_client_order_id(cid)
            finally:
                close(db)

            assert rec is not None
            assert str(rec.get("status") or "") in {"ACK", "PARTIAL", "SENT"}
            assert len(ex.open_orders) == 1

            # Must not place a fresh entry when an open entry order already exists.
            r = await client.post("/start")
            assert r.status_code == 200
            r = await client.post(
                "/trade/enter",
                json={
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "exec_hint": "LIMIT",
                    "qty": 0.1,
                },
            )
            assert r.status_code == 200
            body = r.json().get("detail") or {}
            assert bool(body.get("blocked")) is True
            assert str(body.get("block_reason")) == "open_entry_order_exists"

            assert len(ex.open_orders) == 1
            assert len({str(o.get("clientOrderId") or "") for o in ex.open_orders}) == 1
