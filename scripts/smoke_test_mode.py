from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
from asgi_lifespan import LifespanManager

from apps.trader_engine.main import create_app

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures.fake_exchange import FakeBinanceRest
from tests.fixtures.fake_notifier import FakeNotifier


async def _run() -> int:
    # Safety defaults for smoke.
    os.environ["TRADING_DRY_RUN"] = "true"
    os.environ["DRY_RUN_STRICT"] = "false"

    app = create_app(
        test_mode=True,
        test_overrides={
            "binance_client": FakeBinanceRest(),
            "notifier": FakeNotifier(),
            "disable_background_tasks": True,
            "skip_binance_startup": True,
        },
    )

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/status")
            assert r.status_code == 200, f"/status failed: {r.status_code}"

            r = await client.get("/doctor")
            if r.status_code not in {200, 404}:
                raise AssertionError(f"/doctor unexpected status: {r.status_code}")

            r = await client.post("/start")
            assert r.status_code == 200, f"/start failed: {r.status_code}"

            r = await client.post("/debug/tick")
            assert r.status_code == 200, f"/debug/tick failed: {r.status_code}"

    print("SMOKE_OK test_mode endpoints completed without exceptions")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
