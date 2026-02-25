from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from v2.clean_room import build_default_kernel
from v2.clean_room.contracts import Candidate, ExecutionResult, KernelCycleResult, SizePlan
from v2.config.loader import load_effective_config
from v2.control import build_runtime_controller, create_control_http_app
from v2.core import EventBus, Scheduler
from v2.engine import EngineStateStore
from v2.exchange import BinanceRESTError
from v2.exchange.types import ResyncSnapshot
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


def test_status_loop_emits_while_running_without_cycle(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_status_loop_running.sqlite3")
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

    controller._risk["notify_interval_sec"] = 1
    controller._status_thread_stop.set()
    if controller._status_thread is not None:
        controller._status_thread.join(timeout=1.0)
    controller._status_thread_stop.clear()
    controller._start_status_loop()
    controller._running = True
    controller._last_status_notify_at = datetime.now(timezone.utc)
    notifier.send.reset_mock()

    time.sleep(1.2)
    assert notifier.send.call_count >= 1

    controller._running = False
    controller._status_thread_stop.set()
    if controller._status_thread is not None:
        controller._status_thread.join(timeout=1.0)


def test_scheduler_interval_does_not_override_notify_interval(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_scheduler_notify_split.sqlite3")
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
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    set_notify = client.post("/set", json={"key": "notify_interval_sec", "value": "1800"})
    assert set_notify.status_code == 200
    interval = client.post("/scheduler/interval", json={"tick_sec": 30})
    assert interval.status_code == 200

    risk = client.get("/risk")
    assert risk.status_code == 200
    assert risk.json()["notify_interval_sec"] == 1800
    assert risk.json()["scheduler_tick_sec"] == 30


def test_set_notify_interval_does_not_change_scheduler_tick(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_scheduler_split.sqlite3")
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
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    interval = client.post("/scheduler/interval", json={"tick_sec": 30})
    assert interval.status_code == 200
    set_notify = client.post("/set", json={"key": "notify_interval_sec", "value": "1800"})
    assert set_notify.status_code == 200

    scheduler_resp = client.get("/scheduler")
    assert scheduler_resp.status_code == 200
    assert scheduler_resp.json()["tick_sec"] == 30.0


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


def test_status_summary_translates_prefixed_reason_head(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_translate_prefixed.sqlite3")
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

    controller._last_cycle["last_action"] = "no_candidate"
    controller._last_cycle["last_decision_reason"] = "no_entry:donchian"
    summary = controller._status_summary()
    assert "사유=진입 조건 미충족:donchian" in summary


def test_status_summary_includes_signed_pnl(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_pnl.sqlite3")
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

    state_store.startup_reconcile(
        snapshot=ResyncSnapshot(
            positions=[
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.01",
                    "entryPrice": "100000",
                    "unRealizedProfit": "5.0",
                },
                {
                    "symbol": "ETHUSDT",
                    "positionAmt": "-0.02",
                    "entryPrice": "2000",
                    "unRealizedProfit": "-2.0",
                },
            ]
        ),
        reason="status_pnl_test",
    )
    controller._last_cycle["last_action"] = "no_candidate"
    controller._last_cycle["last_decision_reason"] = "no_candidate"

    summary = controller._status_summary()
    assert "미실현PnL=+3.0000 USDT" in summary
    assert "BTCUSDT:+5.0000" in summary
    assert "ETHUSDT:-2.0000" in summary


def test_status_summary_prefers_live_positions_and_side_labels(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_status_live_positions.sqlite3")
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

    class _LivePosREST:
        async def get_positions(self):  # type: ignore[no-untyped-def]
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.010",
                    "entryPrice": "100000",
                    "markPrice": "101000",
                    "unRealizedProfit": "1.50",
                },
                {
                    "symbol": "ETHUSDT",
                    "positionAmt": "-0.100",
                    "entryPrice": "3000",
                    "markPrice": "2995",
                    "unRealizedProfit": "-0.50",
                },
            ]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_LivePosREST(),
    )
    controller._last_cycle["last_action"] = "no_candidate"
    controller._last_cycle["last_decision_reason"] = "no_candidate"

    summary = controller._status_summary()
    assert "포지션=BTCUSDT[롱], ETHUSDT[숏]" in summary
    assert "미실현PnL=+1.0000 USDT" in summary


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


def test_control_api_tick_places_tpsl_brackets_after_live_execution(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelExecuted:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(
                    symbol="ETHUSDT",
                    side="BUY",
                    score=1.0,
                    reason="test",
                    entry_price=2000.0,
                ),
                size=SizePlan(
                    symbol="ETHUSDT",
                    qty=0.02,
                    leverage=5.0,
                    notional=40.0,
                    reason="size_ok",
                ),
                execution=ExecutionResult(ok=True, order_id="oid-1", reason="live_order_submitted"),
            )

    class _AlgoREST:
        def __init__(self) -> None:
            self.algo_orders: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.algo_orders.append(dict(params))
            return {"clientAlgoId": str(params.get("clientAlgoId") or "")}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

    rest = _AlgoREST()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelExecuted(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    payload = tick.json()
    assert payload["ok"] is True
    assert payload["snapshot"]["last_action"] == "executed"
    assert payload["snapshot"]["bracket"]["state"] == "active"

    assert len(rest.algo_orders) == 2
    algo_types = {str(row.get("type") or "") for row in rest.algo_orders}
    assert algo_types == {"TAKE_PROFIT_MARKET", "STOP_MARKET"}


def test_control_api_tick_reports_bracket_failure_in_last_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_fail.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelExecuted:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(
                    symbol="BTCUSDT",
                    side="BUY",
                    score=1.0,
                    reason="test",
                    entry_price=100000.0,
                ),
                size=SizePlan(
                    symbol="BTCUSDT",
                    qty=0.001,
                    leverage=5.0,
                    notional=100.0,
                    reason="size_ok",
                ),
                execution=ExecutionResult(ok=True, order_id="oid-2", reason="live_order_submitted"),
            )

    class _AlgoRESTFail:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            raise RuntimeError("algo_reject")

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelExecuted(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_AlgoRESTFail(),
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    payload = tick.json()
    assert payload["ok"] is True
    assert payload["snapshot"]["last_action"] == "executed"
    assert str(payload["snapshot"].get("last_error") or "").startswith("bracket_failed:")
    bracket = payload["snapshot"].get("bracket")
    assert isinstance(bracket, dict)
    assert bracket.get("state") == "failed"


def test_control_api_recovers_active_brackets_on_live_boot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_recover.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="CREATED",
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="no_candidate",
                reason="no_candidate",
                candidate=None,
            )

    class _AlgoRESTRecover:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [{"symbol": "BTCUSDT", "clientAlgoId": "tp-1"}]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

    _ = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_AlgoRESTRecover(),
    )

    rows = storage.list_bracket_states()
    assert len(rows) == 1
    assert rows[0]["state"] == "ACTIVE"


def test_control_api_bracket_poller_cleans_counterpart_when_one_leg_missing(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_poller.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="ACTIVE",
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="no_candidate",
                reason="no_candidate",
                candidate=None,
            )

    class _AlgoRESTPoller:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.cancel_calls.append(dict(params))
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [{"symbol": "BTCUSDT", "clientAlgoId": "tp-1"}]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

    rest = _AlgoRESTPoller()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )

    controller._poll_brackets_once()

    rows = storage.list_bracket_states()
    assert len(rows) == 1
    assert rows[0]["state"] == "CLEANED"
    assert len(rest.cancel_calls) == 1
    assert str(rest.cancel_calls[0].get("clientAlgoId") or "") == "tp-1"


def test_control_api_bracket_poller_sends_take_profit_alert_with_realized_pnl(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_tp_alert.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="ACTIVE",
    )
    _ = storage.insert_fill(
        fill_id="fill-tp-1",
        client_id="tp-1",
        exchange_id="1",
        symbol="BTCUSDT",
        side="SELL",
        qty=0.01,
        price=101.0,
        realized_pnl=5.25,
        fill_time_ms=int(time.time() * 1000),
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTTakeProfit:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [{"symbol": "BTCUSDT", "clientAlgoId": "sl-1"}]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

    notifier = Notifier(enabled=True)
    notifier.send = MagicMock()  # type: ignore[method-assign]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=_AlgoRESTTakeProfit(),
    )

    controller._poll_brackets_once()

    rows = storage.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    assert notifier.send.call_count == 1
    message = str(notifier.send.call_args[0][0])
    assert "익절 완료!" in message
    assert "BTCUSDT" in message
    assert "+5.2500 USDT" in message


def test_control_api_bracket_poller_sends_stop_loss_alert_with_realized_pnl(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_sl_alert.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="ACTIVE",
    )
    _ = storage.insert_fill(
        fill_id="fill-sl-1",
        client_id="sl-1",
        exchange_id="2",
        symbol="BTCUSDT",
        side="SELL",
        qty=0.01,
        price=99.0,
        realized_pnl=-1.75,
        fill_time_ms=int(time.time() * 1000),
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTStopLoss:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [{"symbol": "BTCUSDT", "clientAlgoId": "tp-1"}]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

    notifier = Notifier(enabled=True)
    notifier.send = MagicMock()  # type: ignore[method-assign]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=notifier,
        rest_client=_AlgoRESTStopLoss(),
    )

    controller._poll_brackets_once()

    rows = storage.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    assert notifier.send.call_count == 1
    message = str(notifier.send.call_args[0][0])
    assert "손절 완료!" in message
    assert "BTCUSDT" in message
    assert "-1.7500 USDT" in message


def test_control_api_bracket_poller_cleans_open_algos_when_position_flat(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_flat.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="ETHUSDT",
        tp_order_client_id="tp-2",
        sl_order_client_id="sl-2",
        state="ACTIVE",
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="no_candidate",
                reason="no_candidate",
                candidate=None,
            )

    class _AlgoRESTFlat:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.cancel_calls.append(dict(params))
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [
                {"symbol": "ETHUSDT", "clientAlgoId": "tp-2"},
                {"symbol": "ETHUSDT", "clientAlgoId": "sl-2"},
                {"symbol": "ETHUSDT", "clientAlgoId": "extra-3"},
            ]

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "ETHUSDT", "positionAmt": "0"}]

    rest = _AlgoRESTFlat()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )

    controller._poll_brackets_once()

    rows = storage.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    canceled = {str(item.get("clientAlgoId") or "") for item in rest.cancel_calls}
    assert {"tp-2", "sl-2", "extra-3"}.issubset(canceled)


def test_control_api_trailing_exit_closes_position_on_profit_drawdown(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_trailing.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-t",
        sl_order_client_id="sl-t",
        state="ACTIVE",
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="no_candidate",
                reason="no_candidate",
                candidate=None,
            )

    class _AlgoRESTTrailing:
        def __init__(self) -> None:
            self.cancel_calls: list[dict[str, str]] = []
            self.close_calls: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.cancel_calls.append(dict(params))
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return [
                {"symbol": "BTCUSDT", "clientAlgoId": "tp-t"},
                {"symbol": "BTCUSDT", "clientAlgoId": "sl-t"},
            ]

        async def get_positions(self) -> list[dict[str, str]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.01",
                    "entryPrice": "100.0",
                    "markPrice": "101.0",
                    "positionSide": "BOTH",
                }
            ]

        async def close_position_market(
            self,
            *,
            symbol: str,
            side: str,
            quantity: float,
            position_side: str = "BOTH",
        ) -> dict[str, str]:
            self.close_calls.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": str(quantity),
                    "position_side": position_side,
                }
            )
            return {}

    rest = _AlgoRESTTrailing()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelNoop(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    controller._risk["trailing_enabled"] = True
    controller._risk["trailing_mode"] = "PCT"
    controller._risk["trail_arm_pnl_pct"] = 1.0
    controller._risk["trail_distance_pnl_pct"] = 0.5
    controller._risk["trail_grace_minutes"] = 0
    controller._trailing_state["BTCUSDT"] = {
        "first_seen_mono": time.monotonic() - 600.0,
        "peak_pnl_pct": 2.0,
        "armed": True,
    }

    controller._poll_brackets_once()

    assert len(rest.close_calls) == 1
    close = rest.close_calls[0]
    assert close["symbol"] == "BTCUSDT"
    assert close["side"] == "SELL"
    rows = storage.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    assert float(controller._status_snapshot()["watchdog"]["last_trailing_distance_pct"]) == 0.5


def test_control_api_adaptive_regime_tpsl_scales_with_bull_multiplier(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_adaptive.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelExecuted:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(
                    symbol="BTCUSDT",
                    side="BUY",
                    score=1.0,
                    reason="entry_pullback_long",
                    entry_price=100.0,
                    regime_hint="BULL",
                ),
                size=SizePlan(
                    symbol="BTCUSDT",
                    qty=0.01,
                    leverage=3.0,
                    notional=1.0,
                    reason="size_ok",
                ),
                execution=ExecutionResult(
                    ok=True, order_id="oid-adaptive", reason="live_order_submitted"
                ),
            )

    class _AlgoREST:
        def __init__(self) -> None:
            self.algo_orders: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.algo_orders.append(dict(params))
            return {"clientAlgoId": str(params.get("clientAlgoId") or "")}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

    rest = _AlgoREST()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelExecuted(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    assert (
        client.post("/set", json={"key": "tpsl_policy", "value": "adaptive_regime"}).status_code
        == 200
    )
    assert (
        client.post("/set", json={"key": "tpsl_base_take_profit_pct", "value": "0.02"}).status_code
        == 200
    )
    assert (
        client.post("/set", json={"key": "tpsl_base_stop_loss_pct", "value": "0.01"}).status_code
        == 200
    )
    assert (
        client.post("/set", json={"key": "tpsl_regime_mult_bull", "value": "1.2"}).status_code
        == 200
    )

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["ok"] is True

    assert len(rest.algo_orders) == 2
    by_type = {str(row.get("type") or ""): row for row in rest.algo_orders}
    tp_trigger = float(str(by_type["TAKE_PROFIT_MARKET"]["triggerPrice"]))
    sl_trigger = float(str(by_type["STOP_MARKET"]["triggerPrice"]))
    assert tp_trigger == 102.4
    assert sl_trigger == 98.8


def test_control_api_atr_tpsl_method_uses_candidate_volatility_hint(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_atr.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelExecuted:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(
                    symbol="ETHUSDT",
                    side="BUY",
                    score=1.0,
                    reason="entry_pullback_long",
                    entry_price=100.0,
                    volatility_hint=3.0,
                    regime_hint="BULL",
                ),
                size=SizePlan(
                    symbol="ETHUSDT",
                    qty=0.01,
                    leverage=3.0,
                    notional=1.0,
                    reason="size_ok",
                ),
                execution=ExecutionResult(
                    ok=True, order_id="oid-atr", reason="live_order_submitted"
                ),
            )

    class _AlgoREST:
        def __init__(self) -> None:
            self.algo_orders: list[dict[str, str]] = []

        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            self.algo_orders.append(dict(params))
            return {"clientAlgoId": str(params.get("clientAlgoId") or "")}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "ETHUSDT", "positionAmt": "0.01"}]

    rest = _AlgoREST()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=_KernelExecuted(),
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    assert client.post("/set", json={"key": "tpsl_method", "value": "atr"}).status_code == 200
    assert client.post("/set", json={"key": "tpsl_tp_atr", "value": "2"}).status_code == 200
    assert client.post("/set", json={"key": "tpsl_sl_atr", "value": "1"}).status_code == 200

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["ok"] is True

    assert len(rest.algo_orders) == 2
    by_type = {str(row.get("type") or ""): row for row in rest.algo_orders}
    tp_trigger = float(str(by_type["TAKE_PROFIT_MARKET"]["triggerPrice"]))
    sl_trigger = float(str(by_type["STOP_MARKET"]["triggerPrice"]))
    assert tp_trigger == 106.0
    assert sl_trigger == 97.0


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


def test_control_api_status_prefers_live_positions_for_snapshot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(
        tmp_path / "control_status_live_positions_snapshot.sqlite3"
    )
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

    class _LiveREST:
        async def get_balances(self):  # type: ignore[no-untyped-def]
            return [{"asset": "USDT", "availableBalance": "50", "walletBalance": "50"}]

        async def get_positions(self):  # type: ignore[no-untyped-def]
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.002",
                    "entryPrice": "100000",
                    "unRealizedProfit": "2.2",
                }
            ]

    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_LiveREST(),
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    payload = status.json()
    row = payload["binance"]["positions"]["BTCUSDT"]
    assert row["position_amt"] == 0.002
    assert row["position_side"] == "LONG"
    assert row["unrealized_pnl"] == 2.2


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


def test_control_api_status_uses_recent_cached_balance_after_fetch_failure(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_status_balance_cached.sqlite3")
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

    class _FlakyBalanceREST:
        def __init__(self) -> None:
            self.calls = 0

        async def get_balances(self):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return [{"asset": "USDT", "availableBalance": "111.11", "walletBalance": "222.22"}]
            raise BinanceRESTError(
                status_code=500,
                code=-1000,
                message="internal error",
                path="/fapi/v2/balance",
            )

    rest = _FlakyBalanceREST()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    first = client.get("/status")
    assert first.status_code == 200
    p1 = first.json()
    assert p1["binance"]["usdt_balance"]["source"] == "exchange"
    assert p1["binance"]["private_error"] is None

    second = client.get("/status")
    assert second.status_code == 200
    p2 = second.json()
    assert p2["capital_snapshot"]["available_usdt"] == 111.11
    assert p2["binance"]["usdt_balance"]["wallet"] == 222.22
    assert p2["binance"]["usdt_balance"]["source"] in {"exchange", "exchange_cached"}
    assert p2["binance"]["private_error"] is None


def test_control_api_status_retries_balance_fetch_once_before_fallback(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_status_balance_retry.sqlite3")
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

    class _RetryBalanceREST:
        def __init__(self) -> None:
            self.calls = 0

        async def get_balances(self):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary_failure")
            return [{"asset": "USDT", "availableBalance": "77.7", "walletBalance": "88.8"}]

    rest = _RetryBalanceREST()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest,
    )
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    payload = status.json()
    assert rest.calls == 2
    assert payload["binance"]["usdt_balance"]["source"] == "exchange"
    assert payload["binance"]["private_error"] is None


def test_control_api_tick_coalesces_when_background_cycle_completes(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_tick_coalesce.sqlite3")
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

    controller._running = True
    controller._cycle_seq = 3
    controller._cycle_done_seq = 2
    controller._last_cycle["last_action"] = "no_candidate"
    controller._last_cycle["last_decision_reason"] = "no_candidate"

    _ = controller._lock.acquire(timeout=1.0)

    def _finish_cycle() -> None:
        time.sleep(0.25)
        controller._cycle_done_seq = 3
        controller._last_cycle["tick_finished_at"] = "2026-01-01T00:00:00+00:00"

    worker = threading.Thread(target=_finish_cycle, daemon=True)
    worker.start()
    try:
        out = controller.tick_scheduler_now()
    finally:
        controller._lock.release()
        worker.join(timeout=1.0)

    assert out["ok"] is True
    assert out["error"] is None
    assert bool(out["snapshot"].get("coalesced")) is True


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
            self.fallback_notional: float = 0.0
            self.max_notional: float | None = None

        def set_universe_symbols(self, symbols: list[str]) -> None:
            self.symbols = list(symbols)

        def set_symbol_leverage_map(
            self, mapping: dict[str, float], *, max_leverage: float
        ) -> None:
            self.mapping = dict(mapping)
            self.max_leverage = float(max_leverage)

        def set_notional_config(
            self,
            *,
            fallback_notional: float,
            max_notional: float | None,
        ) -> None:
            self.fallback_notional = float(fallback_notional)
            self.max_notional = max_notional

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

    set_budget = client.post("/set", json={"key": "margin_budget_usdt", "value": "35"})
    assert set_budget.status_code == 200
    assert kernel.fallback_notional == 35.0

    set_cap = client.post("/set", json={"key": "max_position_notional_usdt", "value": "120"})
    assert set_cap.status_code == 200
    assert kernel.max_notional == 120.0

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


def test_live_tick_blocks_reentry_when_live_position_exists(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_live_block_reentry.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoCandidate:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _LivePosREST:
        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

    kernel = _KernelNoCandidate()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_LivePosREST(),
    )
    controller._running = True
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["snapshot"]["last_action"] == "blocked"
    assert tick.json()["snapshot"]["last_decision_reason"] == "position_open"
    assert kernel.calls == 0


def test_live_tick_allows_reentry_when_flag_enabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="normal",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_live_allow_reentry.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoCandidate:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _LivePosREST:
        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

    kernel = _KernelNoCandidate()
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=_LivePosREST(),
    )
    controller._running = True
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    enable = client.post("/set", json={"key": "allow_reentry", "value": "true"})
    assert enable.status_code == 200

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["snapshot"]["last_action"] == "no_candidate"
    assert kernel.calls == 1
