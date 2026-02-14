from __future__ import annotations

import os
from typing import Any, Dict

import pytest

from apps.trader_engine.main import create_app
from tests.fixtures.fake_exchange import FakeBinanceRest
from tests.fixtures.fake_notifier import FakeNotifier


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    monkeypatch.setenv("TEST_MODE", "1")
    monkeypatch.setenv("BINANCE_API_KEY", "")
    monkeypatch.setenv("BINANCE_API_SECRET", "")
    monkeypatch.setenv("TRADING_DRY_RUN", "true")
    monkeypatch.setenv("DRY_RUN_STRICT", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    db_path = tmp_path_factory.mktemp("dbs") / "test.sqlite3"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    yield


@pytest.fixture
def fake_exchange() -> FakeBinanceRest:
    return FakeBinanceRest()


@pytest.fixture
def fake_notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def test_app(fake_exchange: FakeBinanceRest, fake_notifier: FakeNotifier):
    app = create_app(
        test_mode=True,
        test_overrides={
            "binance_client": fake_exchange,
            "notifier": fake_notifier,
            "disable_background_tasks": True,
            "skip_binance_startup": True,
        },
    )
    return app
