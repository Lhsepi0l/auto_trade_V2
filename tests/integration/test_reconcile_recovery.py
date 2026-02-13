from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from apps.trader_engine.main import create_app
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.reconcile_service import ReconcileService
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.storage.db import connect, migrate
from apps.trader_engine.storage.repositories import EngineStateRepo, OrderRecordRepo, RiskConfigRepo
from tests.fixtures.fake_exchange import FakeBinanceRest


@pytest.mark.integration
def test_startup_reconcile_updates_pending_order_from_exchange_open(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = connect(str(tmp_path / "reconcile.sqlite3"))
    migrate(db)
    risk = RiskConfigService(risk_config_repo=RiskConfigRepo(db))
    _ = risk.get_config()
    engine = EngineService(engine_state_repo=EngineStateRepo(db))
    repo = OrderRecordRepo(db)
    ex = FakeBinanceRest()
    ex.limit_fill_mode = "never_fill"

    cid = "BOT-PROD-intentA-1"
    ex.place_order_limit(
        symbol="BTCUSDT",
        side="BUY",
        quantity=0.01,
        price=100.0,
        post_only=False,
        reduce_only=False,
        new_client_order_id=cid,
    )
    repo.create_created(
        intent_id="intentA",
        cycle_id="cycleA",
        run_id="runA",
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        reduce_only=False,
        qty=0.01,
        price=100.0,
        time_in_force="GTC",
        client_order_id=cid,
    )

    svc = ReconcileService(client=ex, risk=risk, engine=engine, order_records=repo)  # type: ignore[arg-type]
    ok = svc.startup_reconcile()
    assert ok is True
    rec = repo.get_by_client_order_id(cid)
    assert rec is not None
    assert str(rec.get("status")) in {"ACK", "PARTIAL"}
    assert engine.is_recovery_lock_active() is False
    assert len(ex.open_orders) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restart_with_existing_position_does_not_auto_enter_new_position() -> None:
    ex = FakeBinanceRest()
    ex.positions["BTCUSDT"] = {
        "position_amt": 0.05,
        "entry_price": 100.0,
        "unrealized_pnl": 0.0,
        "leverage": 1.0,
    }
    app = create_app(
        test_mode=True,
        test_overrides={
            "binance_client": ex,
            "disable_background_tasks": True,
            # keep startup to ensure enabled symbols are available to scheduler tick
            "skip_binance_startup": False,
        },
    )
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/start")
            assert r.status_code == 200
            r = await client.post("/debug/tick")
            assert r.status_code == 200
            # Strategy must not place new non-reduce entry while a position already exists.
            assert not any(not bool(f.get("reduce_only")) for f in ex.fills)
