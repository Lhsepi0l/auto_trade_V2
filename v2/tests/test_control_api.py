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


def test_control_api_set_notify_interval_emits_immediate_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_set.sqlite3")
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

    resp = client.post("/set", json={"key": "notify_interval_sec", "value": "30"})
    assert resp.status_code == 200
    assert resp.json()["applied_value"] == 30
    assert notifier.send.call_count >= 1


def test_status_summary_translates_action_and_reason_to_korean(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_translate.sqlite3")
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

    notifier = Notifier(enabled=False)
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

    controller._last_cycle["last_action"] = "no_candidate"
    controller._last_cycle["last_decision_reason"] = "no_candidate"
    summary = controller._status_summary()
    assert "마지막판단=대기" in summary
    assert "사유=현재 진입 후보가 없습니다" in summary


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
                    out.append(
                        [
                            idx,
                            str(o),
                            str(h),
                            str(low_price),
                            str(c),
                            "10",
                            idx + 1,
                            "0",
                            "0",
                            "0",
                            "0",
                            "0",
                        ]
                    )
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


def test_control_api_status_uses_live_usdt_balance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_status_balance.sqlite3")
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

    class _BalanceREST:
        async def get_balances(self):  # type: ignore[no-untyped-def]
            return [{"asset": "USDT", "availableBalance": "321.45", "walletBalance": "345.67"}]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_BalanceREST(),
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["capital_snapshot"]["available_usdt"] == 321.45
    assert payload["binance"]["usdt_balance"]["wallet"] == 345.67
    assert payload["binance"]["usdt_balance"]["source"] == "exchange"
    assert payload["binance"]["private_error"] is None


def test_control_api_status_marks_balance_source_as_fallback_when_live_fetch_unavailable(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    app = _build_app(tmp_path)
    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["binance"]["usdt_balance"]["source"] == "fallback"
    assert payload["binance"]["private_error"] == "rest_client_unavailable"


def test_control_api_syncs_kernel_runtime_overrides(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_sync.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelStub:
        def __init__(self) -> None:
            self.symbols: list[str] = []
            self.mapping: dict[str, float] = {}
            self.max_leverage: float = 1.0

        def set_universe_symbols(self, symbols: list[str]) -> None:
            self.symbols = list(symbols)

        def set_symbol_leverage_map(
            self, mapping: dict[str, float], *, max_leverage: float
        ) -> None:
            self.mapping = dict(mapping)
            self.max_leverage = float(max_leverage)

        def run_once(self):  # type: ignore[no-untyped-def]
            from v2.clean_room.contracts import KernelCycleResult

            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    kernel = _KernelStub()
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

    set_uni = client.post("/set", json={"key": "universe_symbols", "value": "BTCUSDT,ETHUSDT"})
    assert set_uni.status_code == 200
    assert kernel.symbols == ["BTCUSDT", "ETHUSDT"]

    _ = client.post("/set", json={"key": "max_leverage", "value": "20"})
    lev = client.post("/symbol-leverage", json={"symbol": "ETHUSDT", "leverage": 7})
    assert lev.status_code == 200
    assert kernel.mapping.get("ETHUSDT") == 7.0
    assert kernel.max_leverage == 20.0


def test_control_api_persists_risk_config_across_restart(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    sqlite_path = str(tmp_path / "control_persist.sqlite3")
    cfg.behavior.storage.sqlite_path = sqlite_path

    storage1 = RuntimeStorage(sqlite_path=sqlite_path)
    storage1.ensure_schema()
    state_store1 = EngineStateStore(storage=storage1, mode=cfg.mode)
    event_bus1 = EventBus()
    scheduler1 = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus1)
    ops1 = OpsController(state_store=state_store1, exchange=None)
    kernel1 = build_default_kernel(
        state_store=state_store1,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    controller1 = build_runtime_controller(
        cfg=cfg,
        state_store=state_store1,
        ops=ops1,
        kernel=kernel1,
        scheduler=scheduler1,
        event_bus=event_bus1,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    app1 = create_control_http_app(controller=controller1)
    client1 = TestClient(app1)

    assert (
        client1.post("/set", json={"key": "margin_budget_usdt", "value": "250"}).status_code == 200
    )
    assert (
        client1.post(
            "/set", json={"key": "universe_symbols", "value": "BTCUSDT,ETHUSDT"}
        ).status_code
        == 200
    )
    assert client1.post("/set", json={"key": "max_leverage", "value": "15"}).status_code == 200
    assert (
        client1.post("/symbol-leverage", json={"symbol": "ETHUSDT", "leverage": 8}).status_code
        == 200
    )

    storage2 = RuntimeStorage(sqlite_path=sqlite_path)
    storage2.ensure_schema()
    state_store2 = EngineStateStore(storage=storage2, mode=cfg.mode)
    event_bus2 = EventBus()
    scheduler2 = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus2)
    ops2 = OpsController(state_store=state_store2, exchange=None)
    kernel2 = build_default_kernel(
        state_store=state_store2,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        dry_run=True,
        rest_client=None,
    )
    controller2 = build_runtime_controller(
        cfg=cfg,
        state_store=state_store2,
        ops=ops2,
        kernel=kernel2,
        scheduler=scheduler2,
        event_bus=event_bus2,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )
    app2 = create_control_http_app(controller=controller2)
    client2 = TestClient(app2)

    risk = client2.get("/risk")
    assert risk.status_code == 200
    payload = risk.json()
    assert payload["margin_budget_usdt"] == 250
    assert payload["max_leverage"] == 15
    assert payload["universe_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert payload["symbol_leverage_map"]["ETHUSDT"] == 8.0
