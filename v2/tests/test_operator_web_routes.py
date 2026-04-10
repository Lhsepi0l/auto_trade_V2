from __future__ import annotations

from fastapi.testclient import TestClient

from v2.config.loader import load_effective_config
from v2.control import build_runtime_controller, create_control_http_app
from v2.core import EventBus, Scheduler
from v2.engine import EngineStateStore
from v2.kernel import build_default_kernel
from v2.notify import Notifier
from v2.operator import OperatorService
from v2.ops import OpsController
from v2.storage import RuntimeStorage


def _build_operator_app(tmp_path, *, with_webpush: bool = False):  # type: ignore[no-untyped-def]
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
    if with_webpush:
        class _FakeWebPushService:
            def __init__(self) -> None:
                self._subscriptions: list[dict[str, object]] = []

            def availability_snapshot(self) -> dict[str, object]:
                return {
                    "available": True,
                    "public_key": "PUBLIC_KEY",
                    "subscription_count": len(self._subscriptions),
                    "last_error": None,
                }

            def list_subscriptions(self) -> list[dict[str, object]]:
                return list(self._subscriptions)

            def register_subscription(self, **kwargs) -> dict[str, object]:  # type: ignore[no-untyped-def]
                self._subscriptions.append(
                    {
                        "device_label": kwargs.get("device_label") or "현재 기기",
                        "platform": kwargs.get("platform") or "web",
                        "active": True,
                        "standalone": bool(kwargs.get("standalone")),
                        "endpoint_hint": "https://example...",
                        "last_success_at": None,
                        "last_failure_at": None,
                        "last_error": None,
                    }
                )
                return {"ok": True, "subscription_count": len(self._subscriptions)}

            def unregister_subscription(self, *, endpoint: str) -> dict[str, object]:
                _ = endpoint
                self._subscriptions.clear()
                return {"ok": True, "subscription_count": 0}

            def send_test_notification(self, *, device_label: str | None = None):  # type: ignore[no-untyped-def]
                _ = device_label
                from v2.notify import WebPushDispatchResult

                return WebPushDispatchResult(sent=True, error=None, status="sent")

        controller.webpush_service = _FakeWebPushService()
    return create_control_http_app(controller=controller, enable_operator_web=True)


def test_operator_console_route_renders(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_build_operator_app(tmp_path))

    redirect = client.get("/operator", follow_redirects=False)
    response = client.get("/operator/")

    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/operator/"
    assert response.status_code == 200
    assert "웹 운영 콘솔" in response.text
    assert "상태 · 실행 · 리스크" in response.text
    assert ">개요<" in response.text
    assert "mission control" in response.text
    assert "/operator/static/operator.js" in response.text
    assert "/operator/manifest.webmanifest" in response.text


def test_operator_logs_route_renders(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_build_operator_app(tmp_path))

    redirect = client.get("/operator/logs", follow_redirects=False)
    response = client.get("/operator/logs/")

    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/operator/logs/"
    assert response.status_code == 200
    assert "운영 로그" in response.text
    assert "조회 · 검색 · 추출" in response.text
    assert ">필터<" in response.text
    assert "빠른 추출" in response.text
    assert "전체 추출" in response.text
    assert "/operator/api/logs" not in response.text
    assert 'data-operator-page="logs"' in response.text


def test_operator_favicon_route_redirects(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_build_operator_app(tmp_path))

    response = client.get("/favicon.ico", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].startswith("/operator/static/favicon.svg")


def test_operator_manifest_and_push_routes_work(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_build_operator_app(tmp_path, with_webpush=True))

    manifest = client.get("/operator/manifest.webmanifest")
    service_worker = client.get("/operator/sw.js")
    payload = client.get("/operator/api/console")
    subscribe = client.post(
        "/operator/api/push/subscribe",
        json={
            "subscription": {
                "endpoint": "https://example.com/push/1",
                "expirationTime": None,
                "keys": {"p256dh": "abc", "auth": "def"},
            },
            "device_id": "device-1",
            "device_label": "민수 iPhone 운영앱",
            "user_agent": "Mozilla/5.0",
            "platform": "iPhone",
            "standalone": True,
        },
    )
    test_push = client.post("/operator/actions/push-test", json={"device_label": "민수 iPhone 운영앱"})

    assert manifest.status_code == 200
    assert manifest.json()["display"] == "standalone"
    assert manifest.json()["start_url"] == "/operator/"
    assert manifest.json()["scope"] == "/operator/"
    assert service_worker.status_code == 200
    assert "self.addEventListener(\"push\"" in service_worker.text
    assert payload.status_code == 200
    assert payload.json()["push"]["available"] is True
    assert subscribe.status_code == 200
    assert subscribe.json()["action"] == "push_subscribe"
    push_state = client.get("/operator/api/push/state")
    assert push_state.status_code == 200
    assert len(push_state.json()["devices"]) == 1
    assert test_push.status_code == 200
    assert test_push.json()["action"] == "push_test"
    unsubscribe = client.post(
        "/operator/api/push/unsubscribe",
        json={"endpoint": "https://example.com/push/1"},
    )
    assert unsubscribe.status_code == 200
    assert unsubscribe.json()["action"] == "push_unsubscribe"


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
    assert "report" in json_payload
    assert "notification" in json_payload
    assert "guidance" in json_payload
    assert "preset_options" in json_payload["controls"]
    assert "preset_current_state_label" in json_payload["controls"]
    assert "default_symbol" in json_payload["controls"]
    assert "trailing" in json_payload["risk_forms"]
    assert "scoring" in json_payload["risk_forms"]
    assert json_payload["risk_forms"]["scoring"]["score_conf_threshold"] == 0.60
    assert json_payload["risk_forms"]["scoring"]["weights"]["10m"] == 0.25

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
    assert scheduler.json()["result"]["risk_config"]["notify_interval_sec"] == 600
    assert scheduler.json()["result"]["risk_config"]["scheduler_tick_sec"] == 600

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
    assert notify.json()["result"]["risk_config"]["notify_interval_sec"] == 45
    assert notify.json()["result"]["risk_config"]["scheduler_tick_sec"] == 600

    preset = client.post("/operator/actions/preset", json={"name": "normal"})
    assert preset.status_code == 200
    assert preset.json()["action"] == "preset"

    profile = client.post(
        "/operator/actions/profile-template",
        json={"name": "recovery_safe", "budget_usdt": 44.0},
    )
    assert profile.status_code == 200
    assert profile.json()["action"] == "profile_template"

    trailing = client.post(
        "/operator/actions/trailing",
        json={
            "trailing_enabled": True,
            "trailing_mode": "PCT",
            "trail_arm_pnl_pct": 1.2,
            "trail_grace_minutes": 30,
            "trail_distance_pnl_pct": 0.8,
            "atr_trail_timeframe": "1h",
            "atr_trail_k": 2.0,
            "atr_trail_min_pct": 0.6,
            "atr_trail_max_pct": 1.8,
        },
    )
    assert trailing.status_code == 200
    assert trailing.json()["action"] == "trailing_config"

    universe = client.post(
        "/operator/actions/universe",
        json={"symbols_text": "BTCUSDT,ETHUSDT"},
    )
    assert universe.status_code == 200
    assert universe.json()["action"] == "universe_set"

    universe_remove = client.post(
        "/operator/actions/universe/remove",
        json={"symbol": "ETHUSDT"},
    )
    assert universe_remove.status_code == 200
    assert universe_remove.json()["action"] == "universe_remove"

    scoring = client.post(
        "/operator/actions/scoring",
        json={
            "tf_weight_10m": 0.25,
            "tf_weight_15m": 0.0,
            "tf_weight_30m": 0.25,
            "tf_weight_1h": 0.25,
            "tf_weight_4h": 0.25,
            "score_conf_threshold": 0.61,
            "score_gap_threshold": 0.14,
            "donchian_momentum_filter": True,
            "donchian_fast_ema_period": 8,
            "donchian_slow_ema_period": 21,
        },
    )
    assert scoring.status_code == 200
    assert scoring.json()["action"] == "scoring_config"

    report = client.post("/operator/actions/report")
    assert report.status_code == 200
    assert report.json()["action"] == "report"


def test_operator_events_api_returns_persisted_events(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = _build_operator_app(tmp_path)
    controller = app.state.controller
    controller._log_event("runtime_start", running=True, event_time="2026-03-20T00:00:00+00:00")
    client = TestClient(app)

    response = client.get("/operator/api/events?limit=20")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert payload[0]["event_type"] == "runtime_start"


def test_operator_logs_api_supports_filtering(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = _build_operator_app(tmp_path)
    controller = app.state.controller
    controller._log_event("runtime_start", running=True, event_time="2026-03-20T00:00:00+00:00")
    controller._log_event(
        "report_sent",
        status="sent",
        notifier_error=None,
        event_time="2026-03-20T00:01:00+00:00",
    )
    client = TestClient(app)

    response = client.get("/operator/api/logs?limit=20&category=report&query=sent")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    assert payload["has_prev"] is False
    assert payload["has_next"] is False
    assert len(payload["items"]) == 1
    assert payload["items"][0]["event_type"] == "report_sent"


def test_operator_logs_api_supports_offset_pagination(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = _build_operator_app(tmp_path)
    controller = app.state.controller
    for idx in range(5):
        controller._log_event(
            "runtime_start",
            running=True,
            event_time=f"2026-03-20T00:00:0{idx}+00:00",
            symbol=f"TEST{idx}",
        )
    client = TestClient(app)

    response = client.get("/operator/api/logs?limit=2&offset=2&category=action&query=엔진")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 5
    assert payload["offset"] == 2
    assert payload["limit"] == 2
    assert payload["has_prev"] is True
    assert payload["has_next"] is True
    assert len(payload["items"]) == 2


def test_operator_debug_bundle_action_uses_request_base_url(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app = _build_operator_app(tmp_path)
    client = TestClient(app)

    def _fake_export(self, *, base_url: str, include_all: bool = False) -> dict[str, object]:  # type: ignore[no-untyped-def]
        return {
            "ok": True,
            "status": "success",
            "action": "debug_bundle",
            "action_label": "로그 추출",
            "summary": f"로그 번들 추출 완료: {base_url}/SUMMARY.md",
            "context": {"base_url": base_url, "include_all": include_all},
            "result": {
                "ok": True,
                "bundle_dir": "/tmp/runtime_debug",
                "summary_path": "/tmp/runtime_debug/SUMMARY.md",
                "download_url": "http://testserver/operator/api/debug-bundles/runtime_debug.zip",
                "full_export": include_all,
            },
        }

    monkeypatch.setattr(OperatorService, "export_debug_bundle", _fake_export)

    response = client.post("/operator/actions/debug-bundle", json={"mode": "full"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "debug_bundle"
    assert payload["context"]["base_url"] == "http://testserver"
    assert payload["context"]["include_all"] is True
    assert payload["result"]["download_url"].endswith("/operator/api/debug-bundles/runtime_debug.zip")
    assert payload["result"]["full_export"] is True


def test_operator_debug_bundle_download_serves_zip(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app = _build_operator_app(tmp_path)
    client = TestClient(app)
    archive_path = tmp_path / "runtime_debug.zip"
    archive_path.write_bytes(b"zip-bytes")

    from v2.operator_web import router as router_module

    def _fake_resolve(*, archive_name: str):  # type: ignore[no-untyped-def]
        assert archive_name == "runtime_debug.zip"
        return archive_path

    monkeypatch.setattr(router_module, "resolve_runtime_debug_bundle_archive", _fake_resolve)

    response = client.get("/operator/api/debug-bundles/runtime_debug.zip")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.content == b"zip-bytes"
