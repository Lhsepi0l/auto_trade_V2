from __future__ import annotations

from fastapi.testclient import TestClient

from v2.clean_room import build_default_kernel
from v2.config.loader import load_effective_config
from v2.control import build_runtime_controller, create_control_http_app
from v2.core import EventBus, Scheduler
from v2.engine import EngineStateStore
from v2.notify import Notifier
from v2.ops import OpsController
from v2.storage import RuntimeStorage


def _build_operator_app(tmp_path):  # type: ignore[no-untyped-def]
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_verified_q070",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    cfg.behavior.storage.sqlite_path = str(tmp_path / "operator_console.sqlite3")
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
    return create_control_http_app(controller=controller, enable_operator_web=True)


def test_operator_console_route_renders(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_build_operator_app(tmp_path))

    response = client.get("/operator")

    assert response.status_code == 200
    assert "웹 운영 콘솔" in response.text
    assert "/operator/static/operator.js" in response.text


def test_operator_console_payload_and_actions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_build_operator_app(tmp_path))

    payload = client.get("/operator/api/console")
    assert payload.status_code == 200
    json_payload = payload.json()
    assert json_payload["engine"]["state"] in {"STOPPED", "PAUSED", "RUNNING", "KILLED"}
    assert "recovery" in json_payload
    assert "controls" in json_payload
    assert "risk_forms" in json_payload
    assert "recent_result" in json_payload

    start = client.post("/operator/actions/start")
    assert start.status_code == 200
    assert start.json()["action"] == "start_resume"

    leverage = client.post(
        "/operator/actions/symbol-leverage",
        json={"symbol": "BTCUSDT", "leverage": 5},
    )
    assert leverage.status_code == 200
    assert leverage.json()["result"]["symbol_leverage_map"]["BTCUSDT"] == 5.0


def test_operator_console_supports_structured_control_actions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_build_operator_app(tmp_path))

    reconcile = client.post("/operator/actions/reconcile")
    assert reconcile.status_code == 200
    assert reconcile.json()["action"] == "reconcile"

    cooldown = client.post("/operator/actions/cooldown-clear")
    assert cooldown.status_code == 200
    assert cooldown.json()["action"] == "cooldown_clear"

    scheduler = client.post("/operator/actions/scheduler-interval", json={"tick_sec": 600})
    assert scheduler.status_code == 200
    assert scheduler.json()["action"] == "scheduler_interval"
    assert scheduler.json()["result"]["tick_sec"] == 600.0

    exec_mode = client.post("/operator/actions/exec-mode", json={"exec_mode": "LIMIT"})
    assert exec_mode.status_code == 200
    assert exec_mode.json()["action"] == "exec_mode"
    assert exec_mode.json()["result"]["applied_value"] == "LIMIT"

    margin = client.post(
        "/operator/actions/margin-budget",
        json={"amount_usdt": 120.0, "leverage": 7.0},
    )
    assert margin.status_code == 200
    assert margin.json()["action"] == "margin_budget"

    risk_basic = client.post(
        "/operator/actions/risk-basic",
        json={
            "max_leverage": 9.0,
            "max_exposure_pct": 0.3,
            "max_notional_pct": 1200.0,
            "per_trade_risk_pct": 12.0,
        },
    )
    assert risk_basic.status_code == 200
    assert risk_basic.json()["action"] == "risk_basic"

    risk_advanced = client.post(
        "/operator/actions/risk-advanced",
        json={
            "daily_loss_limit_pct": -0.04,
            "dd_limit_pct": -0.2,
            "min_hold_minutes": 120,
            "score_conf_threshold": 0.6,
        },
    )
    assert risk_advanced.status_code == 200
    assert risk_advanced.json()["action"] == "risk_advanced"

    notify = client.post("/operator/actions/notify-interval", json={"notify_interval_sec": 45})
    assert notify.status_code == 200
    assert notify.json()["action"] == "notify_interval"
