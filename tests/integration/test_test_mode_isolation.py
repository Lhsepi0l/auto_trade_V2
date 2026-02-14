from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager

from apps.trader_engine.config import SettingsValidationError
from apps.trader_engine.exchange.binance_usdm import BinanceUSDMClient
from apps.trader_engine.main import create_app
from apps.trader_engine.services.binance_service import BinanceService
from apps.trader_engine.services.reconcile_service import ReconcileService
from tests.fixtures.fake_exchange import FakeBinanceRest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_test_mode_without_client_override_forces_strict_dry_run_and_skips_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test_mode_isolation.sqlite3"))
    monkeypatch.setenv("BINANCE_API_KEY", "")
    monkeypatch.setenv("BINANCE_API_SECRET", "")
    monkeypatch.setenv("TRADING_DRY_RUN", "false")
    monkeypatch.setenv("DRY_RUN_STRICT", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")

    startup_calls = {"count": 0}
    reconcile_calls = {"count": 0}

    def _startup_spy(self):  # type: ignore[no-untyped-def]
        startup_calls["count"] += 1
        return None

    def _reconcile_spy(self):  # type: ignore[no-untyped-def]
        reconcile_calls["count"] += 1
        return True

    monkeypatch.setattr(BinanceService, "startup", _startup_spy)
    monkeypatch.setattr(ReconcileService, "startup_reconcile", _reconcile_spy)
    monkeypatch.setattr(BinanceUSDMClient, "get_exchange_info_cached", lambda self: {"symbols": []})  # type: ignore[no-untyped-def]
    monkeypatch.setattr(BinanceUSDMClient, "get_symbol_filters", lambda self, symbol: {})  # type: ignore[no-untyped-def]
    monkeypatch.setattr(  # type: ignore[no-untyped-def]
        BinanceUSDMClient, "get_open_orders_usdtm", lambda self, symbols: {str(s).upper(): [] for s in symbols}
    )
    monkeypatch.setattr(  # type: ignore[no-untyped-def]
        BinanceUSDMClient,
        "get_positions_usdtm",
        lambda self, symbols: {str(s).upper(): {"position_amt": 0.0, "unrealized_pnl": 0.0} for s in symbols},
    )
    monkeypatch.setattr(BinanceUSDMClient, "close", lambda self: None)  # type: ignore[no-untyped-def]

    app = create_app(test_mode=True)
    async with LifespanManager(app):
        exe = app.state.execution_service
        assert bool(getattr(exe, "_dry_run", False)) is True
        assert bool(getattr(exe, "_dry_run_strict", False)) is True
        assert startup_calls["count"] == 0
        assert reconcile_calls["count"] == 0
        assert getattr(app.state.scheduler, "_task", None) is None
        assert getattr(app.state.user_stream, "_task", None) is None
        assert getattr(app.state.watchdog, "_task", None) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_test_mode_with_client_override_respects_configured_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test_mode_override.sqlite3"))
    monkeypatch.setenv("BINANCE_API_KEY", "")
    monkeypatch.setenv("BINANCE_API_SECRET", "")
    monkeypatch.setenv("TRADING_DRY_RUN", "false")
    monkeypatch.setenv("DRY_RUN_STRICT", "false")

    app = create_app(
        test_mode=True,
        test_overrides={
            "binance_client": FakeBinanceRest(),
            "disable_background_tasks": True,
            "skip_binance_startup": True,
        },
    )
    async with LifespanManager(app):
        exe = app.state.execution_service
        assert bool(getattr(exe, "_dry_run", True)) is False
        assert bool(getattr(exe, "_dry_run_strict", True)) is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_test_mode_with_any_api_key_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test_mode_failfast.sqlite3"))
    monkeypatch.setenv("BINANCE_API_KEY", "dummy")
    monkeypatch.setenv("BINANCE_API_SECRET", "")

    app = create_app(test_mode=True)
    with pytest.raises(SettingsValidationError, match="TEST_MODE=true requires BINANCE_API_KEY/BINANCE_API_SECRET to be empty"):
        async with LifespanManager(app):
            pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_non_test_mode_missing_keys_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DB_PATH", str(tmp_path / "non_test_failfast.sqlite3"))
    monkeypatch.setenv("TEST_MODE", "0")
    monkeypatch.setenv("BINANCE_API_KEY", "")
    monkeypatch.setenv("BINANCE_API_SECRET", "")

    app = create_app(test_mode=False)
    with pytest.raises(SettingsValidationError, match="Missing required env vars in non-test mode: BINANCE_API_KEY, BINANCE_API_SECRET"):
        async with LifespanManager(app):
            pass
