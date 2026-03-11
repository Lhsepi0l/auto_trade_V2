from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
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


def _build_app(tmp_path, *, profile: str = "ra_2026_alpha_v2_expansion_live_candidate"):  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile=profile, mode="shadow", env="testnet", env_map={})
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


def _build_controller(tmp_path, *, profile: str = "ra_2026_alpha_v2_expansion_live_candidate"):  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile=profile, mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_controller.sqlite3")
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
    return build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=None,
    )


def _build_live_controller(  # type: ignore[no-untyped-def]
    tmp_path,
    *,
    profile: str = "ra_2026_alpha_v2_expansion_live_candidate",
    rest_client: Any | None,
    kernel: Any | None = None,
    user_stream_manager: Any | None = None,
    market_data_state: dict[str, Any] | None = None,
    runtime_lock_active: bool = True,
    dirty_restart_detected: bool = False,
):
    cfg = load_effective_config(
        profile=profile,
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_live_controller.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)
    if kernel is None:
        kernel = build_default_kernel(
            state_store=state_store,
            behavior=cfg.behavior,
            profile=cfg.profile,
            mode=cfg.mode,
            dry_run=False,
            rest_client=rest_client,
        )
    controller = build_runtime_controller(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        event_bus=event_bus,
        notifier=Notifier(enabled=False),
        rest_client=rest_client,
        user_stream_manager=user_stream_manager,
        market_data_state=market_data_state,
        runtime_lock_active=runtime_lock_active,
        dirty_restart_detected=dirty_restart_detected,
    )
    return controller, state_store, ops


def test_control_api_contract(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = _build_app(tmp_path)
    client = TestClient(app)

    status = client.get("/status")
    assert status.status_code == 200
    assert status.json()["engine_state"]["state"] in {"STOPPED", "PAUSED", "RUNNING", "KILLED"}
    assert "last_alpha_id" in status.json()["pnl"]

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
    assert report.json()["notifier_enabled"] is False
    assert report.json()["notifier_sent"] is False
    assert report.json()["notifier_error"] is None

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


def test_alpha_live_candidate_profile_seeds_runtime_defaults_and_readiness(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    app = _build_app(tmp_path, profile="ra_2026_alpha_v2_expansion_live_candidate")
    client = TestClient(app)

    risk = client.get("/risk")
    assert risk.status_code == 200
    risk_payload = risk.json()
    assert risk_payload["margin_use_pct"] == 0.1
    assert risk_payload["risk_score_min"] == 0.6
    assert risk_payload["lose_streak_n"] == 2
    assert risk_payload["cooldown_hours"] == 4
    assert risk_payload["universe_symbols"] == ["BTCUSDT"]

    readiness = client.get("/readiness")
    assert readiness.status_code == 200
    payload = readiness.json()
    assert payload["target"] == "alpha_expansion_live_candidate"
    assert payload["profile"] == "ra_2026_alpha_v2_expansion_live_candidate"
    assert payload["enabled_symbols"] == ["BTCUSDT"]
    assert payload["overall"] == "caution"
    assert payload["checks"]["profile"]["status"] == "pass"
    assert payload["checks"]["strategy"]["status"] == "pass"
    assert payload["checks"]["symbols"]["status"] == "pass"
    assert payload["checks"]["margin_use_pct"]["status"] == "pass"
    assert payload["checks"]["max_leverage"]["status"] == "pass"
    assert payload["checks"]["mode"]["status"] == "warn"

    status = client.get("/status")
    assert status.status_code == 200
    status_payload = status.json()
    assert status_payload["live_readiness"]["profile"] == "ra_2026_alpha_v2_expansion_live_candidate"


def test_set_strategy_runtime_values_syncs_kernel_runtime_params(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_runtime_params.sqlite3")
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
    kernel.set_strategy_runtime_params = MagicMock()  # type: ignore[method-assign]

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

    resp = client.post("/set", json={"key": "trend_enter_adx_4h", "value": "24"})
    assert resp.status_code == 200
    kernel.set_strategy_runtime_params.assert_called()
    kwargs = kernel.set_strategy_runtime_params.call_args.kwargs
    assert kwargs["trend_enter_adx_4h"] == 24.0
    assert kwargs["trend_exit_adx_4h"] == 18.0
    assert kwargs["regime_hold_bars_4h"] == 2
    assert kwargs["breakout_buffer_bps"] == 8.0
    assert kwargs["breakout_bar_size_atr_max"] == 1.6
    assert kwargs["min_volume_ratio_15m"] == 1.2
    assert kwargs["range_enabled"] is False
    assert kwargs["overheat_funding_abs"] == 0.0008
    assert kwargs["overheat_long_short_ratio_cap"] == 1.8
    assert kwargs["overheat_long_short_ratio_floor"] == 0.56


def test_set_value_waits_for_controller_lock(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)

    entered = threading.Event()
    finished = threading.Event()

    def _worker() -> None:
        entered.set()
        controller.set_value(key="breakout_buffer_bps", value="11")
        finished.set()

    controller._lock.acquire()
    t = threading.Thread(target=_worker)
    try:
        t.start()
        assert entered.wait(timeout=1.0)
        assert not finished.wait(timeout=0.1)
    finally:
        controller._lock.release()

    t.join(timeout=1.0)
    assert finished.is_set()
    assert controller.get_risk().get("breakout_buffer_bps") == 11


def test_control_api_tick_emits_status_notification(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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


def test_control_api_report_reflects_notifier_send_result(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_report_notify.sqlite3")
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
    notifier = Notifier(
        enabled=True, provider="discord", webhook_url="https://discord.test/webhook"
    )
    notifier.send_with_result = MagicMock(  # type: ignore[method-assign]
        return_value=Notifier.SendResult(sent=False, error="ConnectError: boom")
    )

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

    report = client.post("/report")
    assert report.status_code == 200
    payload = report.json()
    assert payload["notifier_enabled"] is True
    assert payload["notifier_sent"] is False
    assert payload["notifier_error"] == "ConnectError: boom"


def test_control_api_set_notify_interval_emits_immediate_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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


def test_status_summary_translates_portfolio_reason(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_notify_translate_portfolio.sqlite3")
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

    controller._last_cycle["last_action"] = "blocked"
    controller._last_cycle["last_decision_reason"] = "portfolio_cap_reached"
    summary = controller._status_summary()
    assert "사유=포트폴리오 최대 포지션 도달" in summary


def test_status_summary_includes_signed_pnl(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
        profile="ra_2026_alpha_v2_expansion_live_candidate",
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
        profile="ra_2026_alpha_v2_expansion_live_candidate",
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
        profile="ra_2026_alpha_v2_expansion_live_candidate",
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
        profile="ra_2026_alpha_v2_expansion_live_candidate",
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
        profile="ra_2026_alpha_v2_expansion_live_candidate",
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
        profile="ra_2026_alpha_v2_expansion_live_candidate",
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
    messages = [str(call.args[0]) for call in notifier.send.call_args_list]
    assert any(
        "손절 완료!" in message and "BTCUSDT" in message and "-1.7500 USDT" in message
        for message in messages
    )


def test_control_api_sl_symbol_flatten_cooldown_scopes_by_symbol(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_sl_flatten_once.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTNoop:
        async def place_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def cancel_algo_order(self, *, params: dict[str, str]) -> dict[str, str]:
            _ = params
            return {}

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, str]]:
            _ = symbol
            return []

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
        rest_client=_AlgoRESTNoop(),
    )
    controller._running = True
    controller._risk["sl_flatten_cooldown_sec"] = 3600

    calls: list[str] = []

    async def _fake_close_position(*, symbol: str) -> dict[str, object]:
        calls.append(symbol)
        return {"symbol": symbol, "detail": {}}

    controller.close_position = _fake_close_position  # type: ignore[method-assign]

    controller._maybe_trigger_symbol_sl_flatten(trigger_symbol="BTCUSDT")
    controller._running = True
    controller._maybe_trigger_symbol_sl_flatten(trigger_symbol="BTCUSDT")
    controller._maybe_trigger_symbol_sl_flatten(trigger_symbol="ETHUSDT")

    assert calls == ["BTCUSDT", "ETHUSDT"]
    assert controller._watchdog_state["last_sl_flatten_symbol"] == "ETHUSDT"
    assert state_store.get().status == "STOPPED"


def test_control_api_bracket_poller_uses_realized_pnl_sign_for_alert_headline(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_sign_alert.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="ACTIVE",
    )
    _ = storage.insert_fill(
        fill_id="fill-sl-positive-1",
        client_id="sl-1",
        exchange_id="3",
        symbol="BTCUSDT",
        side="SELL",
        qty=0.01,
        price=100.3,
        realized_pnl=0.5631,
        fill_time_ms=int(time.time() * 1000),
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTStopLegMissing:
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
        rest_client=_AlgoRESTStopLegMissing(),
    )

    controller._poll_brackets_once()

    messages = [str(call.args[0]) for call in notifier.send.call_args_list]
    assert any("익절 완료!" in message and "+0.5631 USDT" in message for message in messages)


def test_control_api_bracket_poller_uses_breakeven_headline_for_zero_realized_pnl(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="testnet",
        env_map={"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "control_brackets_zero_alert.sqlite3")
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    storage.set_bracket_state(
        symbol="BTCUSDT",
        tp_order_client_id="tp-1",
        sl_order_client_id="sl-1",
        state="ACTIVE",
    )
    _ = storage.insert_fill(
        fill_id="fill-sl-zero-1",
        client_id="sl-1",
        exchange_id="4",
        symbol="BTCUSDT",
        side="SELL",
        qty=0.01,
        price=100.0,
        realized_pnl=0.0,
        fill_time_ms=int(time.time() * 1000),
    )

    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    event_bus = EventBus()
    scheduler = Scheduler(tick_seconds=cfg.behavior.scheduler.tick_seconds, event_bus=event_bus)
    ops = OpsController(state_store=state_store, exchange=None)

    class _KernelNoop:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    class _AlgoRESTStopLegMissing:
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
        rest_client=_AlgoRESTStopLegMissing(),
    )

    controller._poll_brackets_once()

    messages = [str(call.args[0]) for call in notifier.send.call_args_list]
    assert any("손익없음 청산!" in message and "0.0000 USDT" in message for message in messages)


def test_control_api_bracket_poller_cleans_open_algos_when_position_flat(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
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
        profile="ra_2026_alpha_v2_expansion_live_candidate",
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
        profile="ra_2026_alpha_v2_expansion_live_candidate",
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
        profile="ra_2026_alpha_v2_expansion_live_candidate",
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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


def test_control_api_status_notional_tracks_effective_budget_leverage(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = _build_app(tmp_path)
    client = TestClient(app)

    assert (
        client.post(
            "/set", json={"key": "universe_symbols", "value": "BTCUSDT,ETHUSDT"}
        ).status_code
        == 200
    )
    assert client.post("/set", json={"key": "margin_budget_usdt", "value": "35"}).status_code == 200
    assert client.post("/set", json={"key": "max_leverage", "value": "20"}).status_code == 200
    assert (
        client.post("/symbol-leverage", json={"symbol": "ETHUSDT", "leverage": 7}).status_code
        == 200
    )

    status = client.get("/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["capital_snapshot"]["budget_usdt"] == 3.5
    assert payload["capital_snapshot"]["leverage"] == 7.0
    assert payload["capital_snapshot"]["notional_usdt"] == 24.5


def test_control_api_status_uses_recent_cached_balance_after_fetch_failure(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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
    assert kernel.fallback_notional == 17.5

    set_cap = client.post("/set", json={"key": "max_position_notional_usdt", "value": "120"})
    assert set_cap.status_code == 200
    assert kernel.max_notional == 120.0
    assert kernel.fallback_notional == 17.5

    _ = client.post("/set", json={"key": "max_leverage", "value": "20"})
    assert kernel.fallback_notional == 70.0
    lev = client.post("/symbol-leverage", json={"symbol": "ETHUSDT", "leverage": 7})
    assert lev.status_code == 200
    assert kernel.fallback_notional == 24.5
    assert kernel.mapping.get("ETHUSDT") == 7.0
    assert kernel.max_leverage == 20.0


def test_control_api_persists_risk_config_across_restart(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(profile="ra_2026_alpha_v2_expansion_live_candidate", mode="shadow", env="testnet", env_map={})
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


def test_live_tick_does_not_preblock_multi_symbol_scan_when_live_position_exists(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
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
    controller._risk["universe_symbols"] = ["BTCUSDT", "ETHUSDT"]
    app = create_control_http_app(controller=controller)
    client = TestClient(app)

    tick = client.post("/scheduler/tick")
    assert tick.status_code == 200
    assert tick.json()["snapshot"]["last_action"] == "no_candidate"
    assert tick.json()["snapshot"]["last_decision_reason"] == "no_candidate"
    assert tick.json()["snapshot"]["last_error"] is None
    assert tick.json().get("error") is None
    assert kernel.calls == 1


def test_live_tick_allows_reentry_when_flag_enabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
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


def test_live_startup_reconcile_populates_exchange_position_and_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.010",
                    "entryPrice": "100000",
                    "unRealizedProfit": "5.0",
                }
            ]

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, state_store, ops = _build_live_controller(tmp_path, rest_client=_ReconREST())

    state = state_store.get()
    assert state.current_position["BTCUSDT"].position_amt == 0.01
    assert state.last_reconcile_at is not None
    assert ops.can_open_new_entries() is True

    status = controller._status_snapshot()
    assert status["state_uncertain"] is False
    assert status["startup_reconcile_ok"] is True
    assert status["last_reconcile_at"] is not None


def test_live_startup_reconcile_failure_sets_uncertainty_and_safe_mode(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _FailREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return []

    controller, _state_store, ops = _build_live_controller(tmp_path, rest_client=_FailREST())

    status = controller._status_snapshot()
    assert status["state_uncertain"] is True
    assert status["startup_reconcile_ok"] is False
    assert str(status["state_uncertain_reason"]).startswith("startup_reconcile:")
    assert ops.can_open_new_entries() is False


def test_live_uncertainty_blocks_new_entries_before_kernel_execution(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _FailREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return []

    class _KernelExecuted:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(
                state="executed",
                reason="executed",
                candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100000.0),
                size=SizePlan(symbol="BTCUSDT", qty=0.01, leverage=2.0, notional=1000.0),
                execution=ExecutionResult(ok=True, order_id="oid-1", reason="live_order_submitted"),
            )

    kernel = _KernelExecuted()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_FailREST(),
        kernel=kernel,
    )

    out = controller.tick_scheduler_now()
    assert out["ok"] is True
    assert out["snapshot"]["last_action"] == "blocked"
    assert out["snapshot"]["last_decision_reason"] == "state_uncertain"
    assert kernel.calls == 0


def test_live_user_stream_disconnect_resync_and_event_flow_update_state(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.010", "entryPrice": "100000"}]

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _FakeUserStreamManager:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False
            self.on_event = None
            self.on_resync = None
            self.on_disconnect = None
            self.on_private_ok = None

        def start(  # type: ignore[no-untyped-def]
            self, *, on_event=None, on_resync=None, on_disconnect=None, on_private_ok=None
        ):
            self.started = True
            self.on_event = on_event
            self.on_resync = on_resync
            self.on_disconnect = on_disconnect
            self.on_private_ok = on_private_ok

        async def stop(self) -> None:
            self.stopped = True

    manager = _FakeUserStreamManager()
    controller, state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_ReconREST(),
        user_stream_manager=manager,
    )

    app = create_control_http_app(controller=controller)
    with TestClient(app):
        assert manager.started is True
        assert controller._status_snapshot()["state_uncertain"] is False

        asyncio.run(manager.on_disconnect("socket_closed"))
        assert controller._status_snapshot()["state_uncertain"] is True

        asyncio.run(
            manager.on_resync(
                ResyncSnapshot(
                    open_orders=[],
                    positions=[
                        {"symbol": "ETHUSDT", "positionAmt": "0.050", "entryPrice": "2500.0"}
                    ],
                    balances=[{"asset": "USDT", "availableBalance": "1000"}],
                )
            )
        )
        status = controller._status_snapshot()
        assert status["state_uncertain"] is False
        assert state_store.get().current_position["ETHUSDT"].position_amt == 0.05

        asyncio.run(
            manager.on_event(
                {
                    "e": "ACCOUNT_UPDATE",
                    "E": 1700000002000,
                    "a": {"P": [{"s": "ETHUSDT", "pa": "0", "ep": "2500.0", "up": "0"}]},
                }
            )
        )
        assert "ETHUSDT" not in state_store.get().current_position

    assert manager.stopped is True


def test_live_user_stream_resync_failure_sets_uncertainty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _FakeUserStreamManager:
        def __init__(self) -> None:
            self.on_resync = None
            self.on_private_ok = None

        def start(  # type: ignore[no-untyped-def]
            self, *, on_event=None, on_resync=None, on_disconnect=None, on_private_ok=None
        ):
            _ = on_event
            _ = on_disconnect
            self.on_resync = on_resync
            self.on_private_ok = on_private_ok

        async def stop(self) -> None:
            return None

    manager = _FakeUserStreamManager()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_ReconREST(),
        user_stream_manager=manager,
    )

    original = controller.state_store.startup_reconcile

    def _broken_startup_reconcile(*, snapshot, reason):  # type: ignore[no-untyped-def]
        _ = snapshot
        _ = reason
        raise RuntimeError("resync_boom")

    controller.state_store.startup_reconcile = _broken_startup_reconcile  # type: ignore[method-assign]
    try:
        asyncio.run(controller.start_live_services())
        try:
            asyncio.run(
                manager.on_resync(
                    ResyncSnapshot(
                        open_orders=[],
                        positions=[],
                        balances=[{"asset": "USDT", "availableBalance": "1000"}],
                    )
                )
            )
        except RuntimeError as exc:
            assert str(exc) == "resync_boom"
        status = controller._status_snapshot()
        assert status["state_uncertain"] is True
        assert status["state_uncertain_reason"] == "resync_failed:RuntimeError"
    finally:
        controller.state_store.startup_reconcile = original  # type: ignore[method-assign]
        asyncio.run(controller.stop_live_services())


def test_live_uncertainty_reconcile_action_clears_state_when_exchange_recovers(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _FailREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return []

    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, str]]:
            return [{"symbol": "BTCUSDT", "positionAmt": "0.010", "entryPrice": "100000"}]

        async def get_balances(self) -> list[dict[str, str]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, state_store, _ops = _build_live_controller(tmp_path, rest_client=_FailREST())
    assert controller._status_snapshot()["state_uncertain"] is True

    controller.rest_client = _HealthyREST()
    out = asyncio.run(controller.reconcile_now())

    assert out["ok"] is True
    assert out["state_uncertain"] is False
    assert out["startup_reconcile_ok"] is True
    assert state_store.get().current_position["BTCUSDT"].position_amt == 0.01


def test_live_uncertainty_still_allows_flatten_reduce_only_recovery(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _FailREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            raise RuntimeError("boom")

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return []

    class _FlattenExchange:
        def __init__(self) -> None:
            self.reduce_only_calls: list[dict[str, Any]] = []

        async def cancel_all_open_orders(self, *, symbol: str) -> dict[str, Any]:
            _ = symbol
            return {}

        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
            _ = symbol
            return []

        async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            _ = params
            return {}

        async def get_positions(self) -> list[dict[str, Any]]:
            if self.reduce_only_calls:
                return [{"symbol": "BTCUSDT", "positionAmt": "0"}]
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

        async def place_reduce_only_market_order(
            self,
            *,
            symbol: str,
            side: str,
            quantity: float,
            position_side: str = "BOTH",
        ) -> dict[str, Any]:
            self.reduce_only_calls.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "position_side": position_side,
                }
            )
            return {}

    controller, _state_store, _ops = _build_live_controller(tmp_path, rest_client=_FailREST())
    exchange = _FlattenExchange()
    controller.ops.exchange = exchange

    out = asyncio.run(controller.close_position(symbol="BTCUSDT"))

    assert out["symbol"] == "BTCUSDT"
    assert len(exchange.reduce_only_calls) == 1
    assert exchange.reduce_only_calls[0]["side"] == "SELL"


def test_live_market_data_stale_blocks_new_entries_and_unblocks_when_fresh(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _KernelNoCandidate:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    market_data_state = {
        "last_market_data_at": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
        "last_market_symbol_count": 1,
    }
    kernel = _KernelNoCandidate()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        kernel=kernel,
        market_data_state=market_data_state,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    controller._user_stream_started = True
    controller._user_stream_started_at = now_iso
    controller._last_private_stream_ok_at = now_iso
    controller._risk["market_data_stale_sec"] = 5.0
    controller._risk["user_ws_stale_sec"] = 60.0

    blocked = controller.tick_scheduler_now()
    assert blocked["snapshot"]["last_decision_reason"] == "market_data_stale"
    assert kernel.calls == 0

    controller._market_data_state["last_market_data_at"] = datetime.now(timezone.utc).isoformat()
    recovered = controller.tick_scheduler_now()
    assert recovered["snapshot"]["last_decision_reason"] == "no_candidate"
    assert kernel.calls == 1


def test_live_user_stream_stale_blocks_new_entries_and_private_ok_unblocks(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _KernelNoCandidate:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(self) -> KernelCycleResult:
            self.calls += 1
            return KernelCycleResult(state="no_candidate", reason="no_candidate", candidate=None)

    market_data_state = {
        "last_market_data_at": datetime.now(timezone.utc).isoformat(),
        "last_market_symbol_count": 1,
    }
    kernel = _KernelNoCandidate()
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        kernel=kernel,
        market_data_state=market_data_state,
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = (
        datetime.now(timezone.utc) - timedelta(seconds=120)
    ).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at
    controller._risk["market_data_stale_sec"] = 60.0
    controller._risk["user_ws_stale_sec"] = 5.0

    blocked = controller.tick_scheduler_now()
    assert blocked["snapshot"]["last_decision_reason"] == "user_ws_stale"
    assert kernel.calls == 0

    asyncio.run(controller._handle_user_stream_private_ok("test"))
    recovered = controller.tick_scheduler_now()
    assert recovered["snapshot"]["last_decision_reason"] == "no_candidate"
    assert kernel.calls == 1


def test_live_stale_state_still_allows_flatten_and_reconcile(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _FlattenExchange:
        def __init__(self) -> None:
            self.reduce_only_calls = 0

        async def cancel_all_open_orders(self, *, symbol: str) -> dict[str, Any]:
            _ = symbol
            return {}

        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
            _ = symbol
            return []

        async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            _ = params
            return {}

        async def get_positions(self) -> list[dict[str, Any]]:
            if self.reduce_only_calls > 0:
                return [{"symbol": "BTCUSDT", "positionAmt": "0"}]
            return [{"symbol": "BTCUSDT", "positionAmt": "0.01"}]

        async def place_reduce_only_market_order(
            self,
            *,
            symbol: str,
            side: str,
            quantity: float,
            position_side: str = "BOTH",
        ) -> dict[str, Any]:
            _ = symbol
            _ = side
            _ = quantity
            _ = position_side
            self.reduce_only_calls += 1
            return {}

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        market_data_state={
            "last_market_data_at": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
            "last_market_symbol_count": 1,
        },
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at
    controller._risk["market_data_stale_sec"] = 5.0
    controller.ops.exchange = _FlattenExchange()

    out = asyncio.run(controller.close_position(symbol="BTCUSDT"))
    assert out["symbol"] == "BTCUSDT"

    reconcile = asyncio.run(controller.reconcile_now())
    assert reconcile["ok"] is True


def test_readyz_and_healthz_reflect_uncertainty_stale_and_recovery(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        rest_client=_HealthyREST(),
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
        },
        dirty_restart_detected=True,
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at
    app = create_control_http_app(controller=controller)

    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["ready"] is False

        blocked = client.get("/readyz")
        assert blocked.status_code == 503
        assert blocked.json()["recovery_required"] is True

        reconcile = client.post("/reconcile")
        assert reconcile.status_code == 200

        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True

        status = client.get("/status")
        assert status.status_code == 200
        assert status.json()["health"]["ready"] is True


def test_readyz_fails_for_uncertainty_and_stale_freshness(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    market_data_state = {
        "last_market_data_at": datetime.now(timezone.utc).isoformat(),
        "last_market_symbol_count": 1,
    }
    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        market_data_state=market_data_state,
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at
    app = create_control_http_app(controller=controller)

    with TestClient(app) as client:
        healthy = client.get("/readyz")
        assert healthy.status_code == 200

        controller._set_state_uncertain(reason="manual_test", engage_safe_mode=False)
        uncertain = client.get("/readyz")
        assert uncertain.status_code == 503
        assert uncertain.json()["state_uncertain"] is True

        controller._clear_state_uncertain()
        controller._last_private_stream_ok_at = (
            datetime.now(timezone.utc) - timedelta(seconds=120)
        ).isoformat()
        controller._risk["user_ws_stale_sec"] = 5.0
        stale_user = client.get("/readyz")
        assert stale_user.status_code == 503
        assert stale_user.json()["user_ws_stale"] is True

        controller._last_private_stream_ok_at = datetime.now(timezone.utc).isoformat()
        controller._market_data_state["last_market_data_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=120)
        ).isoformat()
        controller._risk["market_data_stale_sec"] = 5.0
        stale_market = client.get("/readyz")
        assert stale_market.status_code == 503
        assert stale_market.json()["market_data_stale"] is True


def test_controller_structured_logs_include_phase_b_fields(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
        },
    )
    with caplog.at_level(logging.INFO):
        controller._set_state_uncertain(reason="phase_b_test", engage_safe_mode=False)

    record = next(
        rec for rec in caplog.records if getattr(rec, "event", "") == "uncertainty_transition"
    )
    assert record.mode == "live"
    assert record.profile == controller.cfg.profile
    assert record.state_uncertain is True
    assert hasattr(record, "safe_mode")


def test_submit_review_required_blocks_until_manual_reconcile_resolves(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_ReconREST(),
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at
    storage = state_store.runtime_storage()
    storage.upsert_submission_intent(
        intent_id="intent-1",
        client_order_id="cid-1",
        symbol="BTCUSDT",
        side="BUY",
        status="REVIEW_REQUIRED",
    )

    blocked = controller.tick_scheduler_now()
    assert blocked["snapshot"]["last_decision_reason"] == "submit_recovery_required"

    out = asyncio.run(controller.reconcile_now())
    assert out["ok"] is True
    status = controller._status_snapshot()
    assert status["submission_recovery"]["pending_review_count"] == 0
    assert status["health"]["submission_recovery_ok"] is True


def test_manual_recovery_runs_bracket_recovery_path(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _ReconREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_ReconREST(),
        dirty_restart_detected=True,
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at

    async def _recover() -> dict[str, Any]:
        return {"recovered": 1}

    controller._bracket_service.recover = _recover  # type: ignore[method-assign]
    out = asyncio.run(controller.reconcile_now())

    assert out["ok"] is True
    assert controller._status_snapshot()["boot_recovery"]["bracket_recovery"]["ok"] is True


def test_ambiguous_submit_failure_enters_uncertainty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    class _KernelAmbiguousFailure:
        def run_once(self) -> KernelCycleResult:
            return KernelCycleResult(
                state="execution_failed",
                reason="live_order_review_required:not_found_after_submit_error",
                candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100000.0),
            )

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        rest_client=_HealthyREST(),
        kernel=_KernelAmbiguousFailure(),
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller._last_private_stream_ok_at = controller._user_stream_started_at

    out = controller.tick_scheduler_now()
    assert out["snapshot"]["last_decision_reason"] == "live_order_review_required:not_found_after_submit_error"
    assert controller._status_snapshot()["state_uncertain"] is True


def test_status_exposes_runtime_identity_fields(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _HealthyREST:
        async def get_open_orders(self) -> list[dict[str, Any]]:
            return []

        async def get_positions(self) -> list[dict[str, Any]]:
            return []

        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "1000"}]

    controller, _state_store, _ops = _build_live_controller(
        tmp_path,
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        rest_client=_HealthyREST(),
        market_data_state={
            "last_market_data_at": datetime.now(timezone.utc).isoformat(),
            "last_market_symbol_count": 1,
            "last_market_data_source_ok_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    status = controller._status_snapshot()
    assert status["profile"] == "ra_2026_alpha_v2_expansion_live_candidate"
    assert status["mode"] == "live"
    assert status["env"] == "testnet"
    assert status["runtime_identity"]["live_trading_enabled"] is False
