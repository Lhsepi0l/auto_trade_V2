from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from apps.trader_engine.main import create_app
from tests.fixtures.fake_exchange import FakeBinanceRest
from tests.fixtures.fake_notifier import FakeNotifier


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_start_stop_panic_set_flow() -> None:
    ex = FakeBinanceRest()
    notifier = FakeNotifier()
    app = create_app(
        test_mode=True,
        test_overrides={
            "binance_client": ex,
            "notifier": notifier,
            "disable_background_tasks": True,
            "skip_binance_startup": True,
        },
    )

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/status")
            assert r.status_code == 200
            body = r.json()
            assert "dry_run" in body
            assert "ws_connected" in body
            assert "listenKey_last_keepalive_ts" in body
            assert "last_ws_event_ts" in body
            assert "safe_mode" in body
            assert "watchdog" in body
            assert "capital_snapshot" in body
            assert "config" in body
            assert "filters_last_refresh_time" in body
            assert "last_snapshot_time" in body
            assert "last_unrealized_pnl_usdt" in body
            assert "last_unrealized_pnl_pct" in body

            r = await client.post("/start")
            assert r.status_code == 200
            assert r.json()["state"] == "RUNNING"

            r = await client.post("/start")
            assert r.status_code == 200
            assert r.json()["state"] == "RUNNING"

            r = await client.post("/stop")
            assert r.status_code == 200
            assert r.json()["state"] == "STOPPED"

            ex.positions["BTCUSDT"] = {"position_amt": 0.02, "entry_price": 100.0, "unrealized_pnl": 1.0, "leverage": 1.0}
            r = await client.post("/panic")
            assert r.status_code == 200
            assert r.json()["state"] == "PANIC"
            assert any(bool(f.get("reduce_only")) for f in ex.fills)

            r = await client.post("/set", json={"key": "max_leverage", "value": "4"})
            assert r.status_code == 200
            assert str(r.json()["key"]) == "max_leverage"
            assert float(r.json()["applied_value"]) == 4.0

            r = await client.post("/set", json={"key": "capital_mode", "value": "FIXED_USDT"})
            assert r.status_code == 200
            assert str(r.json()["applied_value"]) == "FIXED_USDT"

            r = await client.post("/set", json={"key": "margin_budget_usdt", "value": "125"})
            assert r.status_code == 200
            assert float(r.json()["applied_value"]) == 125.0

            r = await client.post("/set", json={"key": "capital_mode", "value": "MARGIN_BUDGET_USDT"})
            assert r.status_code == 200
            assert str(r.json()["applied_value"]) == "MARGIN_BUDGET_USDT"

            r = await client.post("/set", json={"key": "capital_pct", "value": "0.35"})
            assert r.status_code == 200
            assert float(r.json()["applied_value"]) == 0.35

            r = await client.post("/set", json={"key": "max_position_notional_usdt", "value": "250"})
            assert r.status_code == 200
            assert float(r.json()["applied_value"]) == 250.0

            r = await client.post("/set", json={"key": "capital_pct", "value": "1.5"})
            assert r.status_code == 422
            detail = r.json().get("detail") or {}
            errs = detail.get("errors") or []
            assert any(str(x.get("field")) == "capital_pct" for x in errs)

            r = await client.post("/set", json={"key": "trailing_mode", "value": "ATR"})
            assert r.status_code == 200
            assert str(r.json()["applied_value"]) == "ATR"
            r = await client.post("/set", json={"key": "atr_trail_timeframe", "value": "15m"})
            assert r.status_code == 200
            assert str(r.json()["applied_value"]) == "15m"

            r = await client.get("/status")
            assert r.status_code == 200
            assert float(r.json()["risk_config"]["max_leverage"]) == 4.0
            assert str(r.json()["risk_config"]["capital_mode"]) == "MARGIN_BUDGET_USDT"
            assert float(r.json()["risk_config"]["capital_pct"]) == 0.35
            assert float(r.json()["risk_config"]["margin_budget_usdt"]) == 125.0
            assert float(r.json()["risk_config"]["max_position_notional_usdt"]) == 250.0
            assert str(r.json()["risk_config"]["trailing_mode"]) == "ATR"
            assert str(r.json()["risk_config"]["atr_trail_timeframe"]) == "15m"
            assert str(r.json()["config_summary"]["trailing_mode"]) == "ATR"
            assert str(r.json()["config_summary"]["atr_trail_timeframe"]) == "15m"
            assert float(r.json()["config"]["margin_budget_usdt"]) == 125.0
            assert str(r.json()["config"]["capital_mode"]) == "MARGIN_BUDGET_USDT"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_panic_is_idempotent_and_ends_flat() -> None:
    ex = FakeBinanceRest()
    app = create_app(
        test_mode=True,
        test_overrides={
            "binance_client": ex,
            "disable_background_tasks": True,
            "skip_binance_startup": True,
        },
    )

    ex.positions["BTCUSDT"] = {
        "position_amt": 0.02,
        "entry_price": 100.0,
        "unrealized_pnl": 0.0,
        "leverage": 1.0,
    }
    ex.place_order_limit(
        symbol="BTCUSDT",
        side="BUY",
        quantity=0.01,
        price=99.9,
        post_only=False,
        reduce_only=False,
        new_client_order_id="panic-test-open-order",
    )

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.post("/panic")
            assert r1.status_code == 200
            assert r1.json()["state"] == "PANIC"

            r2 = await client.post("/panic")
            assert r2.status_code == 200
            assert r2.json()["state"] == "PANIC"

            assert ex.get_open_positions_any() == {}
            open_orders = ex.get_open_orders_usdtm(["BTCUSDT"]).get("BTCUSDT") or []
            assert len(open_orders) == 0
