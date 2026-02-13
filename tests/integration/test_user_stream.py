from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from apps.trader_engine.domain.enums import Direction, ExecHint
from apps.trader_engine.exchange.binance_usdm import BinanceHTTPError
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.execution_service import ExecutionRejected, ExecutionService
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.services.risk_service import RiskService
from apps.trader_engine.services.user_stream_service import UserStreamService
from apps.trader_engine.storage.db import connect, migrate
from apps.trader_engine.storage.repositories import EngineStateRepo, PnLStateRepo, RiskConfigRepo
from tests.fixtures.fake_exchange import FakeBinanceRest
from tests.fixtures.fake_notifier import FakeNotifier
from tests.fixtures.fake_user_stream import FakeUserStreamServer


class _Client:
    def __init__(self) -> None:
        self.listen_key = "lk-test"
        self.start_calls = 0
        self.keepalive_calls = 0
        self.close_calls = 0
        self.wallet = 1000.0
        self.positions: Dict[str, Dict[str, float]] = {}
        self.fail_keepalive_with_1125 = False

    def start_user_stream(self) -> str:
        self.start_calls += 1
        return self.listen_key

    def keepalive_user_stream(self, *, listen_key: str) -> None:
        if listen_key != self.listen_key:
            raise RuntimeError("invalid_listen_key")
        if self.fail_keepalive_with_1125:
            self.fail_keepalive_with_1125 = False
            raise BinanceHTTPError(status_code=400, path="/fapi/v1/listenKey", code=-1125, msg="invalid listen key")
        self.keepalive_calls += 1

    def close_user_stream(self, *, listen_key: str) -> None:
        if listen_key == self.listen_key:
            self.close_calls += 1

    def get_open_positions_any(self) -> Dict[str, Dict[str, float]]:
        return dict(self.positions)

    def get_account_balance_usdtm(self) -> Dict[str, float]:
        return {"wallet": self.wallet, "available": self.wallet}


@dataclass
class _ReconcileProbe:
    calls: int = 0

    def startup_reconcile(self) -> bool:
        self.calls += 1
        return True


async def _wait_until(pred, timeout: float = 3.0) -> None:  # type: ignore[no-untyped-def]
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timeout")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_stream_start_keepalive_and_event_handling(tmp_path) -> None:
    db = connect(str(tmp_path / "u1.sqlite3"))
    migrate(db)
    engine = EngineService(engine_state_repo=EngineStateRepo(db))
    pnl = PnLService(repo=PnLStateRepo(db))
    client = _Client()
    notifier = FakeNotifier()

    async with FakeUserStreamServer() as ws:
        svc = UserStreamService(
            client=client,  # type: ignore[arg-type]
            engine=engine,
            pnl=pnl,
            execution=object(),  # type: ignore[arg-type]
            notifier=notifier,  # type: ignore[arg-type]
            ws_base_url=ws.ws_url,
            keepalive_interval_sec=1,
            reconnect_backoff_min_sec=0.1,
            reconnect_backoff_max_sec=0.2,
        )
        svc.start()
        await _wait_until(lambda: bool(engine.get_state().ws_connected))
        await _wait_until(lambda: client.keepalive_calls >= 1, timeout=3.0)

        await ws.emit_order_fill(symbol="BTCUSDT", side="SELL", qty=0.01, price=100.0, realized_pnl=-2.0, reduce_only=True)
        await _wait_until(lambda: pnl.get_or_bootstrap().last_fill_symbol == "BTCUSDT")
        st = pnl.get_or_bootstrap()
        assert st.daily_realized_pnl == -2.0
        assert st.lose_streak == 1
        assert any(str(e.get("kind")) == "FILL" for e in notifier.events)

        await ws.emit_account_update(positions_count=1, balances_count=1)
        await _wait_until(lambda: any(str(e.get("kind")) == "ACCOUNT_UPDATE" for e in notifier.events))

        await svc.stop()
        assert client.close_calls >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_stream_reconnect_on_listen_key_expired(tmp_path) -> None:
    db = connect(str(tmp_path / "u2.sqlite3"))
    migrate(db)
    engine = EngineService(engine_state_repo=EngineStateRepo(db))
    pnl = PnLService(repo=PnLStateRepo(db))
    client = _Client()

    async with FakeUserStreamServer() as ws:
        svc = UserStreamService(
            client=client,  # type: ignore[arg-type]
            engine=engine,
            pnl=pnl,
            execution=object(),  # type: ignore[arg-type]
            notifier=None,
            ws_base_url=ws.ws_url,
            keepalive_interval_sec=1,
            reconnect_backoff_min_sec=0.1,
            reconnect_backoff_max_sec=0.2,
        )
        svc.start()
        await _wait_until(lambda: bool(engine.get_state().ws_connected))
        first_calls = client.start_calls
        await ws.emit_listen_key_expired()
        await _wait_until(lambda: client.start_calls > first_calls, timeout=4.0)
        await _wait_until(lambda: bool(engine.get_state().ws_connected), timeout=4.0)
        await svc.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_stream_keepalive_1125_reconnect_and_reconcile(tmp_path) -> None:
    db = connect(str(tmp_path / "u3.sqlite3"))
    migrate(db)
    engine = EngineService(engine_state_repo=EngineStateRepo(db))
    pnl = PnLService(repo=PnLStateRepo(db))
    client = _Client()
    probe = _ReconcileProbe()

    async with FakeUserStreamServer() as ws:
        svc = UserStreamService(
            client=client,  # type: ignore[arg-type]
            engine=engine,
            pnl=pnl,
            execution=object(),  # type: ignore[arg-type]
            notifier=None,
            reconcile=probe,  # type: ignore[arg-type]
            ws_base_url=ws.ws_url,
            keepalive_interval_sec=1,
            reconnect_backoff_min_sec=0.1,
            reconnect_backoff_max_sec=0.2,
            safe_mode_after_sec=1,
        )
        svc.start()
        await _wait_until(lambda: bool(engine.get_state().ws_connected))
        await _wait_until(lambda: probe.calls >= 1)

        first_start_calls = client.start_calls
        client.fail_keepalive_with_1125 = True
        await _wait_until(lambda: client.start_calls > first_start_calls, timeout=4.0)
        await _wait_until(lambda: probe.calls >= 2, timeout=4.0)
        await svc.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_stream_disconnect_enables_safe_mode_and_blocks_entry(tmp_path) -> None:
    db = connect(str(tmp_path / "u4.sqlite3"))
    migrate(db)
    engine = EngineService(engine_state_repo=EngineStateRepo(db))
    pnl = PnLService(repo=PnLStateRepo(db))
    risk = RiskConfigService(risk_config_repo=RiskConfigRepo(db))
    policy = RiskService(risk=risk, engine=engine, pnl=pnl)
    notifier = FakeNotifier()
    client = FakeBinanceRest()

    exe = ExecutionService(
        client=client,  # type: ignore[arg-type]
        engine=engine,
        risk=risk,
        pnl=pnl,
        policy=policy,
        notifier=notifier,  # type: ignore[arg-type]
        allowed_symbols=["BTCUSDT", "ETHUSDT", "XAUUSDT"],
        dry_run=True,
        dry_run_strict=False,
    )

    svc = UserStreamService(
        client=client,  # type: ignore[arg-type]
        engine=engine,
        pnl=pnl,
        execution=exe,
        notifier=notifier,  # type: ignore[arg-type]
        ws_base_url="ws://127.0.0.1:9",
        keepalive_interval_sec=120,
        reconnect_backoff_min_sec=5.0,
        reconnect_backoff_max_sec=5.0,
        safe_mode_after_sec=1,
    )
    svc.start()
    await _wait_until(lambda: bool(svc.safe_mode), timeout=3.0)
    assert engine.is_ws_safe_mode() is True
    assert any(str(e.get("kind")) == "WS_DOWN_SAFE_MODE" for e in notifier.events)

    engine.set_recovery_lock(False)
    engine.start()
    with pytest.raises(ExecutionRejected) as ei:
        await exe.enter_position(
            {
                "symbol": "BTCUSDT",
                "direction": Direction.LONG.value,
                "exec_hint": ExecHint.MARKET.value,
            }
        )
    assert "ws_down_safe_mode" in str(ei.value)
    await svc.stop()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_stream_stale_event_with_exposure_enables_safe_mode_and_blocks_entry(tmp_path) -> None:
    db = connect(str(tmp_path / "u5.sqlite3"))
    migrate(db)
    engine = EngineService(engine_state_repo=EngineStateRepo(db))
    pnl = PnLService(repo=PnLStateRepo(db))
    risk = RiskConfigService(risk_config_repo=RiskConfigRepo(db))
    policy = RiskService(risk=risk, engine=engine, pnl=pnl)
    notifier = FakeNotifier()
    client = FakeBinanceRest()
    client.positions["BTCUSDT"] = {"position_amt": 0.02, "entry_price": 100.0, "unrealized_pnl": 0.0}

    exe = ExecutionService(
        client=client,  # type: ignore[arg-type]
        engine=engine,
        risk=risk,
        pnl=pnl,
        policy=policy,
        notifier=notifier,  # type: ignore[arg-type]
        allowed_symbols=["BTCUSDT", "ETHUSDT", "XAUUSDT"],
        dry_run=True,
        dry_run_strict=False,
    )

    svc = UserStreamService(
        client=client,  # type: ignore[arg-type]
        engine=engine,
        pnl=pnl,
        execution=exe,
        notifier=notifier,  # type: ignore[arg-type]
        stale_msg_after_sec=60,
        stale_event_after_sec=1,
        exposure_probe_interval_sec=0.1,
        tracked_symbols=["BTCUSDT"],
    )
    now = datetime.now(tz=timezone.utc)
    engine.set_ws_status(connected=True, last_event_time=now)
    svc._last_ws_connect_ts = now  # type: ignore[attr-defined]
    svc._last_ws_msg_ts = now  # type: ignore[attr-defined]
    svc._last_ws_event_ts = now - timedelta(seconds=5)  # type: ignore[attr-defined]
    health_task = asyncio.create_task(svc._health_guard_loop())  # type: ignore[attr-defined]
    try:
        await _wait_until(lambda: bool(svc.safe_mode), timeout=3.0)
        assert engine.is_ws_safe_mode() is True
        assert any(str(e.get("kind")) == "WS_DOWN_SAFE_MODE" for e in notifier.events)
    finally:
        svc._stop.set()  # type: ignore[attr-defined]
        health_task.cancel()
        try:
            await health_task
        except asyncio.CancelledError:
            pass

    engine.set_recovery_lock(False)
    engine.start()
    with pytest.raises(ExecutionRejected) as ei:
        await exe.enter_position(
            {
                "symbol": "BTCUSDT",
                "direction": Direction.LONG.value,
                "exec_hint": ExecHint.MARKET.value,
            }
        )
    assert "ws_down_safe_mode" in str(ei.value)
