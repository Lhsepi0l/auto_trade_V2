from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from asgi_lifespan import LifespanManager

from apps.trader_engine.main import create_app
from tests.fixtures.fake_exchange import FakeBinanceRest
from tests.fixtures.fake_notifier import FakeNotifier


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sqlite_concurrent_reads_writes_do_not_raise_lock_errors(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "sqlite_concurrency.sqlite3"))
    monkeypatch.setenv("TRADING_DRY_RUN", "true")
    monkeypatch.setenv("DRY_RUN_STRICT", "false")

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
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=20.0) as client:
            async def _status_spam(n: int) -> None:
                for _ in range(n):
                    r = await client.get("/status")
                    assert r.status_code == 200

            async def _set_spam(n: int) -> None:
                for i in range(n):
                    r = await client.post("/set", json={"key": "notify_interval_sec", "value": str(60 + (i % 60))})
                    assert r.status_code == 200

            async def _tick_spam(n: int) -> None:
                for _ in range(n):
                    r = await client.post("/debug/tick")
                    assert r.status_code == 200

            started = time.monotonic()
            await asyncio.gather(
                _status_spam(40),
                _status_spam(40),
                _set_spam(30),
                _set_spam(30),
                _tick_spam(20),
            )
            elapsed = time.monotonic() - started
            # Guardrail only: catch severe regressions while remaining CI-friendly.
            assert elapsed < 30.0
