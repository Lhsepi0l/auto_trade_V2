from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from v2.clean_room import build_default_kernel
from v2.config.loader import load_effective_config
from v2.control import build_runtime_controller, create_control_http_app
from v2.core import EventBus, Scheduler
from v2.engine import EngineStateStore
from v2.notify import Notifier
from v2.ops import OpsController
from v2.storage import RuntimeStorage


def _build_app(tmp_path):  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    return create_control_http_app(controller=controller)


def test_control_api_contract(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = _build_app(tmp_path)
    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    assert status.json()["engine_state"]["state"] in {"STOPPED", "PAUSED", "RUNNING", "KILLED"}

    start = client.post("/start")
    assert start.status_code == 200
    assert start.json()["state"] == "RUNNING"

    risk = client.get("/risk")
    assert risk.status_code == 200
    assert "max_leverage" in risk.json()

    set_resp = client.post("/set", json={"key": "margin_budget_usdt", "value": "150"})
    assert set_resp.status_code == 200
    assert set_resp.json()["applied_value"] == 150

    lev = client.post("/symbol-leverage", json={"symbol": "BTCUSDT", "leverage": 7})
    assert lev.status_code == 200
    assert lev.json()["symbol_leverage_map"]["BTCUSDT"] == 7.0

    scheduler = client.get("/scheduler")
    assert scheduler.status_code == 200
    assert scheduler.json()["tick_sec"] >= 1

    interval = client.post("/scheduler/interval", json={"tick_sec": 5})
    assert interval.status_code == 200
    assert interval.json()["tick_sec"] == 5.0

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["ok"] is True

    report = client.post("/report")
    assert report.status_code == 200
    assert report.json()["kind"] == "DAILY_REPORT"

    cooldown = client.post("/cooldown/clear")
    assert cooldown.status_code == 200
    assert cooldown.json()["lose_streak"] == 0

    close = client.post("/trade/close", json={"symbol": "BTCUSDT"})
    assert close.status_code == 200
    assert close.json()["symbol"] == "BTCUSDT"

    close_all = client.post("/trade/close_all")
    assert close_all.status_code == 200
    assert close_all.json()["symbol"] == "ALL"

    panic = client.post("/panic")
    assert panic.status_code == 200
    assert panic.json()["engine_state"]["state"] == "KILLED"

    stop = client.post("/stop")
    assert stop.status_code == 200
    assert stop.json()["state"] == "PAUSED"


def test_control_api_tick_emits_status_notification(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )

    notifier = Notifier(enabled=True)
    notifier.send = MagicMock()  # type: ignore[method-assign]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=None,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    _ = client.post("/set", json={"key": "notify_interval_sec", "value": "1"})
    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["ok"] is True
    assert notifier.send.call_count >= 1


def test_control_api_tick_handles_kernel_exception(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_error.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _BrokenKernel:
        def run_once(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_BrokenKernel(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    payload = tick.json()
    assert payload["ok"] is False
    assert str(payload.get("error", "")).startswith("cycle_failed:")
    assert payload["snapshot"]["last_action"] == "error"


def test_control_api_tick_from_async_endpoint_with_rest_snapshot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_async_snapshot.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _FakeREST:
        async def public_request(self, method: str, path: str, *, params=None):  # type: ignore[no-untyped-def]
            _ = method
            if path == "/fapi/v1/klines":
                out = []
                base = 100.0
                for idx in range(260):
                    o = base + idx * 0.01
                    h = o + 0.05
                    low_price = o - 0.05
                    c = o + 0.01
                    out.append([idx, str(o), str(h), str(low_price), str(c), "10", idx + 1, "0", "0", "0", "0", "0"])
                return out
            if path == "/fapi/v1/premiumIndex":
                return {"lastFundingRate": "0.0001"}
            if path == "/fapi/v1/globalLongShortAccountRatio":
                return [{"longShortRatio": "1.0"}]
            return {}

    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=_FakeREST(),
    )

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    payload = tick.json()
    assert payload["ok"] is True
    assert payload["snapshot"]["last_action"] != "error"
