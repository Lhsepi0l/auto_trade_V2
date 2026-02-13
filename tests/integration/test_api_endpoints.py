from __future__ import annotations

import asyncio

import httpx
import pytest
from asgi_lifespan import LifespanManager

from apps.trader_engine.main import create_app
from apps.trader_engine.services.strategy_service import StrategyDecision
from tests.fixtures.fake_exchange import FakeBinanceRest
from tests.fixtures.fake_notifier import FakeNotifier


class _OpenOrdersFailExchange(FakeBinanceRest):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.open_orders_calls = 0

    def get_open_orders_usdtm(self, symbols):  # type: ignore[override]
        self.open_orders_calls += 1
        if self.open_orders_calls == 1:
            return super().get_open_orders_usdtm(symbols)
        raise RuntimeError("open_orders_down")


class _PanicCloseFailExchange(FakeBinanceRest):
    def place_order_market(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
        new_client_order_id: str | None = None,
    ):
        if bool(reduce_only):
            raise RuntimeError("panic_close_failed")
        return super().place_order_market(
            symbol=symbol,
            side=side,
            quantity=quantity,
            reduce_only=reduce_only,
            new_client_order_id=new_client_order_id,
        )


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
            body = r.json()
            assert body["engine_state"]["state"] == "PANIC"
            assert bool(body["panic_result"]["ok"]) is True
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

            r = await client.post("/set", json={"key": "max_exposure_pct", "value": "0.2"})
            assert r.status_code == 200
            assert float(r.json()["applied_value"]) == 0.2

            r = await client.post("/set", json={"key": "max_exposure_pct", "value": "20%"})
            assert r.status_code == 200
            assert float(r.json()["applied_value"]) == 0.2

            r = await client.post("/set", json={"key": "max_exposure_pct", "value": "20"})
            assert r.status_code == 422
            detail = r.json().get("detail") or {}
            errs = detail.get("errors") or []
            assert any(str(x.get("field")) == "max_exposure_pct" for x in errs)

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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trade_enter_returns_block_reason_on_precheck_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_DRY_RUN", "false")
    monkeypatch.setenv("DRY_RUN_STRICT", "false")
    ex = _OpenOrdersFailExchange()
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
            r = await client.post("/start")
            assert r.status_code == 200
            r = await client.post(
                "/trade/enter",
                json={"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.1},
            )
            assert r.status_code == 200
            detail = (r.json() or {}).get("detail") or {}
            assert bool(detail.get("blocked")) is True
            assert str(detail.get("block_reason")) == "PRECHECK_OPEN_ORDERS_FAILED"
            assert ex.fills == []
            assert any(str(e.get("kind")) == "BLOCK" and str(e.get("reason")) == "PRECHECK_OPEN_ORDERS_FAILED" for e in notifier.events)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scheduler_tick_respects_blocked_entry_without_extra_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_DRY_RUN", "false")
    monkeypatch.setenv("DRY_RUN_STRICT", "false")
    ex = _OpenOrdersFailExchange()
    app = create_app(
        test_mode=True,
        test_overrides={
            "binance_client": ex,
            "notifier": FakeNotifier(),
            "disable_background_tasks": True,
            "skip_binance_startup": True,
        },
    )
    seq = iter([StrategyDecision(kind="ENTER", reason="forced_enter", enter_symbol="BTCUSDT", enter_direction="LONG")])

    def _decide(**_kwargs):  # type: ignore[no-untyped-def]
        return next(seq)

    async with LifespanManager(app):
        app.state.strategy_service.decide_next_action = _decide
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/start")
            assert r.status_code == 200
            r = await client.post("/debug/tick")
            assert r.status_code == 200
            snap = (r.json() or {}).get("snapshot") or {}
            assert str(snap.get("last_error")) == "PRECHECK_OPEN_ORDERS_FAILED"
            assert "blocked" in str(snap.get("last_action") or "")
            assert ex.open_orders_calls >= 2
            assert ex.fills == []


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
            assert r1.json()["engine_state"]["state"] == "PANIC"
            assert bool(r1.json()["panic_result"]["ok"]) is True

            r2 = await client.post("/panic")
            assert r2.status_code == 200
            assert r2.json()["engine_state"]["state"] == "PANIC"
            assert bool(r2.json()["panic_result"]["ok"]) is True

            assert ex.get_open_positions_any() == {}
            open_orders = ex.get_open_orders_usdtm(["BTCUSDT"]).get("BTCUSDT") or []
            assert len(open_orders) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_panic_returns_partial_failure_with_error_and_notifier_event() -> None:
    ex = _PanicCloseFailExchange()
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
    ex.positions["BTCUSDT"] = {
        "position_amt": 0.02,
        "entry_price": 100.0,
        "unrealized_pnl": 0.0,
        "leverage": 1.0,
    }
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/panic")
            assert r.status_code == 207
            body = r.json()
            assert body["engine_state"]["state"] == "PANIC"
            panic_result = body.get("panic_result") or {}
            assert bool(panic_result.get("ok")) is False
            assert bool(panic_result.get("close_ok")) is False
            errs = panic_result.get("errors") or []
            assert any("panic_close_failed" in str(e) for e in errs)
            assert any(str(e.get("kind")) == "PANIC_RESULT" and (not bool(e.get("ok"))) for e in notifier.events)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_panic_lock_busy_returns_structured_423() -> None:
    ex = FakeBinanceRest()
    app = create_app(
        test_mode=True,
        test_overrides={
            "binance_client": ex,
            "disable_background_tasks": True,
            "skip_binance_startup": True,
        },
    )
    async with LifespanManager(app):
        exe = app.state.execution_service
        await exe._exec_lock.acquire()  # type: ignore[attr-defined]
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/panic")
                assert r.status_code == 423
                body = r.json()
                assert bool(body.get("ok")) is False
                assert str(body.get("code")) == "EXECUTION_LOCK_BUSY"
                assert "retry_after_ms" in body
                assert "engine_state" in body
        finally:
            if exe._exec_lock.locked():  # type: ignore[attr-defined]
                exe._exec_lock.release()  # type: ignore[attr-defined]
