from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from apps.trader_engine.main import create_app
from apps.trader_engine.services.strategy_service import StrategyDecision
from tests.fixtures.fake_exchange import FakeBinanceRest
from tests.fixtures.fake_notifier import FakeNotifier


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_smoke_dry_run_tick_sequence() -> None:
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

    seq = iter(
        [
            StrategyDecision(kind="ENTER", reason="enter_candidate", enter_symbol="BTCUSDT", enter_direction="LONG"),
            StrategyDecision(kind="HOLD", reason="min_hold_active:10/240"),
            StrategyDecision(kind="CLOSE", reason="vol_shock_close", close_symbol="BTCUSDT"),
        ]
    )

    def _decide(**_kwargs):  # type: ignore[no-untyped-def]
        return next(seq)

    async with LifespanManager(app):
        app.state.strategy_service.decide_next_action = _decide

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/start")

            reasons = []
            for _ in range(3):
                r = await client.post("/debug/tick")
                assert r.status_code == 200
                reasons.append(r.json()["snapshot"]["last_decision_reason"])

            assert reasons == ["enter_candidate", "min_hold_active:10/240", "vol_shock_close"]
            assert any(str(e.get("kind")) == "ENTER" for e in notifier.events)
            assert ex.fills == []
