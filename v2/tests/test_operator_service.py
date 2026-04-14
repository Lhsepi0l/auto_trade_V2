from __future__ import annotations

from types import SimpleNamespace

from v2.control.operator_events import build_operator_event_payload
from v2.operator import OperatorService
from v2.operator.actions import wrap_operator_action
from v2.operator.read_models import build_operator_console_payload
from v2.tests.test_control_api import _build_controller


def test_build_operator_console_payload_humanizes_blocking_state() -> None:
    payload = build_operator_console_payload(
        {
            "profile": "normal",
            "mode": "shadow",
            "env": "testnet",
            "runtime_identity": {"surface_label": "모의/테스트 또는 비실거래"},
            "engine_state": {"state": "PAUSED", "updated_at": "2026-03-19T00:00:00+00:00"},
            "health": {"ready": False},
            "scheduler": {
                "tick_sec": 5.0,
                "running": False,
                "last_action": "blocked",
                "last_decision_reason": "tick_busy",
            },
            "capital_snapshot": {"blocked": True, "block_reason": "portfolio_cap_reached"},
            "pnl": {
                "daily_pnl_pct": 1.2,
                "drawdown_pct": 0.8,
                "lose_streak": 2,
                "last_strategy_block_reason": "portfolio_cap_reached",
                "last_alpha_reject_metrics": {"confidence": 0.41},
                "last_alpha_blocks": {"volume_missing": 3},
            },
            "binance": {
                "positions": {
                    "BTCUSDT": {
                        "position_amt": 0.01,
                        "entry_price": 51000.0,
                        "unrealized_pnl": 1.5,
                        "position_side": "LONG",
                    }
                },
                "usdt_balance": {"wallet": 100.0, "available": 90.0, "source": "live"},
            },
            "live_readiness": {"summary": "준비 대기"},
            "risk_config": {
                "exec_mode_default": "MARKET",
                "notify_interval_sec": 30,
                "margin_budget_usdt": 100.0,
                "max_leverage": 8.0,
                "margin_use_pct": 0.8,
                "max_exposure_pct": 0.2,
                "max_notional_pct": 1000.0,
                "per_trade_risk_pct": 15.0,
                "daily_loss_limit_pct": -0.03,
                "dd_limit_pct": -0.18,
                "min_hold_minutes": 0,
                "score_conf_threshold": 0.5,
                "score_gap_threshold": 0.15,
                "universe_symbols": ["BTCUSDT", "ETHUSDT"],
                "tf_weight_10m": 0.25,
                "tf_weight_15m": 0.0,
                "tf_weight_30m": 0.25,
                "tf_weight_1h": 0.25,
                "tf_weight_4h": 0.25,
                "score_tf_15m_enabled": False,
                "donchian_momentum_filter": True,
                "donchian_fast_ema_period": 8,
                "donchian_slow_ema_period": 21,
                "symbol_leverage_map": {"BTCUSDT": 7.0},
            },
            "watchdog": {"enabled": True, "last_ok_at": "2026-03-19T00:00:01+00:00"},
            "submission_recovery": {"ok": True},
            "report": {
                "reported_at": "2026-03-19T00:10:00+00:00",
                "status": "success",
                "notifier_sent": False,
                "notifier_error": None,
                "summary": "[DAILY_REPORT]\n일자: 2026-03-19",
                "detail": {"entries": 1, "closes": 0},
            },
            "notification": {
                "enabled": True,
                "provider": "webpush",
                "periodic_status_enabled": True,
                "last_status": "sent",
                "last_attempt_at": "2026-03-19T00:09:00+00:00",
                "last_sent_at": "2026-03-19T00:09:00+00:00",
                "last_event_type": "risk_trip",
                "last_title": "자동 리스크 트립",
                "last_body_preview": "일일 손실 제한 도달 / normal | shadow/testnet",
                "last_error": None,
                "last_dedupe_key": "risk_trip:daily_loss_limit",
                "last_suppressed_count": 0,
            },
            "user_ws_stale": True,
            "market_data_stale": False,
            "recovery_required": False,
            "state_uncertain": False,
            "startup_reconcile_ok": True,
            "last_reconcile_at": "2026-03-19T00:00:02+00:00",
        }
    )

    assert payload["engine"]["state_label"] == "일시정지"
    assert payload["health"]["busy"] is True
    assert payload["health"]["blocked_reason_label"] == "포트폴리오 최대 포지션 도달"
    assert payload["positions"][0]["symbol"] == "BTCUSDT"
    assert "프라이빗 스트림 stale" in payload["health"]["stale_items"]
    assert payload["controls"]["exec_mode_default"] == "MARKET"
    assert payload["controls"]["preset_options"] == ["conservative", "normal", "aggressive"]
    assert payload["controls"]["universe_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert payload["controls"]["default_symbol"] == "BTCUSDT"
    assert payload["controls"]["current_symbol_leverage"] == 7.0
    assert payload["controls"]["preset_current_state_label"] == "현재 active 프리셋 개념 없음 (일회성 적용)"
    assert payload["recovery"]["startup_reconcile_ok"] is True
    assert payload["report"]["status"] == "success"
    assert payload["notification"]["provider"] == "webpush"
    assert payload["notification"]["last_title"] == "자동 리스크 트립"
    assert payload["risk_forms"]["margin_budget"]["margin_budget_usdt"] == 80.0
    assert payload["guidance"]["panel_scope"]
    assert payload["risk_forms"]["margin_budget"]["margin_use_pct"] == 0.8
    assert payload["risk_forms"]["trailing"]["trailing_mode"] == "PCT"
    assert payload["risk_forms"]["scoring"]["weights"]["10m"] == 0.25
    assert payload["recent_result"]["last_reason_label"] == "이미 판단 작업이 진행중"


def test_build_operator_console_payload_prefills_effective_margin_budget_not_base_budget() -> None:
    payload = build_operator_console_payload(
        {
            "profile": "normal",
            "mode": "live",
            "env": "prod",
            "engine_state": {"state": "RUNNING", "updated_at": None},
            "health": {"ready": True},
            "scheduler": {"tick_sec": 30.0, "running": True},
            "capital_snapshot": {"symbol": "BTCUSDT", "budget_usdt": 22.0},
            "risk_config": {
                "capital_mode": "MARGIN_BUDGET_USDT",
                "margin_budget_usdt": 220.0,
                "margin_use_pct": 0.1,
                "max_leverage": 50.0,
                "universe_symbols": ["BTCUSDT"],
            },
            "binance": {"positions": {}, "usdt_balance": {}},
        }
    )

    assert payload["risk_forms"]["margin_budget"]["margin_budget_usdt"] == 22.0


def test_operator_service_push_state_exposes_devices_and_runtime_provider() -> None:
    controller = SimpleNamespace(
        notifier=SimpleNamespace(
            delivery_snapshot=lambda: {
                "enabled": True,
                "provider": "webpush",
            }
        ),
        webpush_service=SimpleNamespace(
            availability_snapshot=lambda: {
                "available": True,
                "public_key": "PUBLIC_KEY",
                "subscription_count": 1,
                "last_error": None,
            },
            list_subscriptions=lambda: [
                {
                    "device_label": "민수 iPhone 운영앱",
                    "platform": "iPhone",
                    "active": True,
                    "standalone": True,
                }
            ],
        ),
    )

    payload = OperatorService(controller=controller).push_state()

    assert payload["runtime_provider"] == "webpush"
    assert payload["runtime_provider_enabled"] is True
    assert payload["devices"] == payload["subscriptions"]
    assert payload["devices"][0]["device_label"] == "민수 iPhone 운영앱"


def test_wrap_operator_action_builds_consistent_response() -> None:
    wrapped = wrap_operator_action(
        action="symbol_leverage",
        raw_result={"symbol_leverage_map": {"BTCUSDT": 7.0}},
        context={"symbol": "BTCUSDT"},
    )

    assert wrapped["ok"] is True
    assert wrapped["action"] == "symbol_leverage"
    assert wrapped["summary"] == "BTCUSDT 레버리지 7x 적용"


def test_operator_event_payload_humanizes_position_entry_and_close() -> None:
    entry = build_operator_event_payload(
        event="position_entry_opened",
        fields={
            "symbol": "BTCUSDT",
            "side": "BUY",
            "alpha_id": "alpha_expansion",
            "entry_family": "expansion",
            "qty": 0.0123,
            "leverage": 45.0,
            "notional": 22.0,
            "entry_price": 101234.5,
            "event_time": "2026-03-27T00:00:00+00:00",
        },
    )
    assert entry is not None
    assert entry["category"] == "position"
    assert entry["title"] == "BTCUSDT LONG 진입"
    assert entry["main_text"] == "alpha_expansion / expansion"
    assert "qty=0.012300" in str(entry["sub_text"])


def test_operator_event_payload_preserves_client_log_fields() -> None:
    payload = build_operator_event_payload(
        event="client_log",
        fields={
            "category": "action",
            "title": "push_subscribe_error",
            "main_text": "TypeError",
            "sub_text": "sw_ready_timeout",
            "event_time": "2026-04-14T00:00:00+00:00",
        },
    )

    assert payload is not None
    assert payload["event_type"] == "client_log"
    assert payload["category"] == "action"
    assert payload["title"] == "push_subscribe_error"
    assert payload["main_text"] == "TypeError"
    assert payload["sub_text"] == "sw_ready_timeout"

    close = build_operator_event_payload(
        event="position_closed",
        fields={
            "symbol": "BTCUSDT",
            "reason": "take_profit",
            "realized_pnl": 5.25,
            "closed_qty": 0.01,
            "outcome": "TP",
            "event_time": "2026-03-27T00:10:00+00:00",
        },
    )
    assert close is not None
    assert close["category"] == "position"
    assert close["title"] == "BTCUSDT 익절 청산"
    assert close["main_text"] == "익절 청산"
    assert "realized=5.25" in str(close["sub_text"])


def test_operator_event_payload_humanizes_partial_reduce() -> None:
    payload = build_operator_event_payload(
        event="position_reduced",
        fields={
            "symbol": "BTCUSDT",
            "reason": "partial_reduce_executed",
            "reduced_qty": 0.01,
            "remaining_qty": 0.03,
            "current_r": 1.1,
            "event_time": "2026-03-27T00:05:00+00:00",
        },
    )
    assert payload is not None
    assert payload["category"] == "position"
    assert payload["title"] == "BTCUSDT 부분청산"
    assert payload["main_text"] == "부분청산 실행"
    assert "remain=0.03" in str(payload["sub_text"])


def test_operator_event_payload_humanizes_alpha_drift_lifecycle() -> None:
    queued = build_operator_event_payload(
        event="alpha_drift_setup_queued",
        fields={
            "symbol": "BTCUSDT",
            "setup_open_time_ms": 1234567890000,
            "setup_expiry_bars": 8,
            "event_time": "2026-04-04T00:00:00+00:00",
        },
    )
    assert queued is not None
    assert queued["category"] == "decision"
    assert queued["title"] == "BTCUSDT drift setup 대기"
    assert queued["main_text"] == "alpha_drift / setup queued"
    assert "expiry=8" in str(queued["sub_text"])

    confirmed = build_operator_event_payload(
        event="alpha_drift_confirmed",
        fields={
            "symbol": "BTCUSDT",
            "side": "BUY",
            "action": "executed",
            "score": 0.81,
            "entry_price": 101234.5,
            "setup_open_time_ms": 1234567890000,
            "event_time": "2026-04-04T00:05:00+00:00",
        },
    )
    assert confirmed is not None
    assert confirmed["category"] == "decision"
    assert confirmed["title"] == "BTCUSDT drift confirm"
    assert confirmed["main_text"] == "alpha_drift / LONG confirm"
    assert "setup_open_time_ms=1234567890000" in str(confirmed["sub_text"])


def test_wrap_operator_action_classifies_busy_response() -> None:
    wrapped = wrap_operator_action(
        action="tick_now",
        raw_result={
            "ok": False,
            "error": "tick_busy",
            "snapshot": {"last_action": "blocked", "last_decision_reason": "tick_busy"},
        },
    )

    assert wrapped["status"] == "busy"
    assert wrapped["ok"] is False


def test_operator_service_applies_profile_template_and_trailing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    service = OperatorService(controller=controller)

    profile = service.apply_profile_template(name="recovery_safe", budget_usdt=55.0)
    assert profile["action"] == "profile_template"
    risk_after_profile = controller.get_risk()
    assert risk_after_profile["max_leverage"] == 20.0
    assert risk_after_profile["margin_budget_usdt"] == 55.0

    trailing = service.set_trailing_config(
        trailing_enabled=True,
        trailing_mode="PCT",
        trail_arm_pnl_pct=1.2,
        trail_grace_minutes=30,
        trail_distance_pnl_pct=0.8,
    )
    assert trailing["action"] == "trailing_config"
    risk_after_trailing = controller.get_risk()
    assert risk_after_trailing["trailing_enabled"] is True
    assert risk_after_trailing["trailing_mode"] == "PCT"
    assert risk_after_trailing["trail_distance_pnl_pct"] == 0.8


def test_operator_service_updates_universe_and_scoring(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    service = OperatorService(controller=controller)

    universe = service.set_universe_symbols(symbols_text="BTCUSDT,ETHUSDT")
    assert universe["action"] == "universe_set"
    risk_after_universe = controller.get_risk()
    assert risk_after_universe["universe_symbols"] == ["BTCUSDT", "ETHUSDT"]

    scoring = service.set_scoring_config(
        tf_weight_10m=0.25,
        tf_weight_15m=0.0,
        tf_weight_30m=0.25,
        tf_weight_1h=0.25,
        tf_weight_4h=0.25,
        score_conf_threshold=0.61,
        score_gap_threshold=0.14,
        donchian_momentum_filter=True,
        donchian_fast_ema_period=8,
        donchian_slow_ema_period=21,
    )
    assert scoring["action"] == "scoring_config"
    risk_after_scoring = controller.get_risk()
    assert risk_after_scoring["score_conf_threshold"] == 0.61
    assert risk_after_scoring["score_gap_threshold"] == 0.14
    assert risk_after_scoring["score_tf_15m_enabled"] is False


def test_operator_service_notify_interval_does_not_change_scheduler_tick(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    service = OperatorService(controller=controller)
    before = controller.get_risk()["scheduler_tick_sec"]

    out = service.set_notify_interval(notify_interval_sec=600)
    risk = controller.get_risk()

    assert out["action"] == "notify_interval"
    assert out["result"]["applied_value"] == 600
    assert risk["notify_interval_sec"] == 600
    assert risk["scheduler_tick_sec"] == before


def test_operator_service_scheduler_interval_updates_notify_interval_together(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    service = OperatorService(controller=controller)

    out = service.set_scheduler_interval(tick_sec=300)
    risk = controller.get_risk()

    assert out["action"] == "scheduler_interval"
    assert out["result"]["tick_sec"] == 300.0
    assert risk["scheduler_tick_sec"] == 300
    assert risk["notify_interval_sec"] == 300


def test_operator_service_export_debug_bundle_hydrates_control_snapshots(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    service = OperatorService(controller=controller)
    bundle_dir = tmp_path / "logs" / "runtime_debug" / "20260327T000000Z_operator_logs"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "SUMMARY.md").write_text("# Runtime Debug Bundle\n", encoding="utf-8")

    def _fake_export(*, label: str, base_url: str, include_all: bool = False) -> dict[str, object]:
        return {
            "ok": True,
            "bundle_dir": str(bundle_dir),
            "summary_path": str(bundle_dir / "SUMMARY.md"),
            "archive_path": str(bundle_dir.with_suffix(".zip")),
            "archive_name": "20260327T000000Z_operator_logs.zip",
            "download_url": f"{base_url}/operator/api/debug-bundles/20260327T000000Z_operator_logs.zip",
            "full_export": include_all,
        }

    monkeypatch.setattr("v2.operator.service.export_runtime_debug_bundle", _fake_export)

    out = service.export_debug_bundle(base_url="http://testserver", include_all=False)

    assert out["action"] == "debug_bundle"
    assert out["result"]["hydrated_control_snapshots"] == ["healthz", "readyz", "readiness", "status"]
    assert (bundle_dir / "control" / "status.json").exists()
    assert (bundle_dir / "control" / "readyz.json").exists()


def test_read_model_marks_unset_scoring_fields_clearly() -> None:
    payload = build_operator_console_payload(
        {
            "profile": "normal",
            "mode": "shadow",
            "env": "testnet",
            "engine_state": {"state": "STOPPED", "updated_at": None},
            "health": {"ready": True},
            "scheduler": {"tick_sec": 30.0, "running": False},
            "capital_snapshot": {"symbol": "BTCUSDT"},
            "risk_config": {
                "universe_symbols": ["BTCUSDT"],
                "score_conf_threshold": None,
                "score_gap_threshold": None,
                "tf_weight_10m": None,
                "tf_weight_15m": None,
                "tf_weight_30m": None,
                "tf_weight_1h": None,
                "tf_weight_4h": None,
                "donchian_momentum_filter": None,
                "donchian_fast_ema_period": None,
                "donchian_slow_ema_period": None,
            },
            "binance": {"positions": {}, "usdt_balance": {}},
        }
    )

    assert payload["risk_forms"]["scoring"]["score_conf_threshold"] == 0.60
    assert payload["risk_forms"]["scoring"]["score_gap_threshold"] == 0.15
    assert payload["risk_forms"]["scoring"]["weights"]["10m"] == 0.25
    assert payload["risk_forms"]["scoring"]["weights"]["15m"] == 0.0
    assert payload["risk_forms"]["scoring"]["donchian_fast_ema_period"] == 8
    assert payload["risk_forms"]["scoring"]["state_label"] == "runtime override 없음, 기본값 표시"


def test_missing_market_is_translated_and_not_shown_as_true_block() -> None:
    payload = build_operator_console_payload(
        {
            "profile": "normal",
            "mode": "shadow",
            "env": "testnet",
            "engine_state": {"state": "STOPPED", "updated_at": None},
            "health": {"ready": True},
            "scheduler": {
                "tick_sec": 30.0,
                "running": False,
                "last_action": "no_candidate",
                "last_decision_reason": "missing_market",
            },
            "capital_snapshot": {"symbol": "BTCUSDT", "blocked": False, "block_reason": "missing_market"},
            "pnl": {"last_strategy_block_reason": "missing_market"},
            "risk_config": {"universe_symbols": ["BTCUSDT"]},
            "binance": {"positions": {}, "usdt_balance": {}},
        }
    )

    assert payload["health"]["blocked"] is False
    assert payload["health"]["blocked_reason_label"] is None
    assert payload["recent_result"]["last_reason_label"] == "시장 컨텍스트 데이터 미도착"


def test_operator_service_triggers_report_and_updates_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    service = OperatorService(controller=controller)

    report = service.trigger_report()

    assert report["action"] == "report"
    assert report["result"]["kind"] == "DAILY_REPORT"
    assert controller._status_snapshot()["report"]["reported_at"] is not None


def test_operator_service_lists_persisted_operator_events(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    service = OperatorService(controller=controller)

    controller._log_event("runtime_start", running=True, event_time="2026-03-20T00:00:00+00:00")
    events = service.list_operator_events(limit=20)

    assert events
    assert events[0]["event_type"] == "runtime_start"


def test_operator_service_hides_legacy_client_logs_from_event_views(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    service = OperatorService(controller=controller)
    storage = controller.state_store.runtime_storage()

    storage.append_operator_event(
        event_type="client_log",
        category="action",
        title="push_subscribe_error",
        main_text="TypeError",
        sub_text="sw_ready_timeout",
        event_time="2026-04-14T00:00:00+00:00",
        context={},
    )
    controller._log_event("runtime_start", running=True, event_time="2026-04-14T00:00:01+00:00")

    events = service.list_operator_events(limit=20)

    assert events
    assert all(row["event_type"] != "client_log" for row in events)
    assert service.count_operator_events() == len(events)


def test_operator_service_record_client_log_does_not_persist_operator_event(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    service = OperatorService(controller=controller)

    result = service.record_client_log(
        category="action",
        title="push_subscribe_error",
        main_text="TypeError",
        sub_text="sw_ready_timeout",
        context={"error": "TypeError: sw_ready_timeout"},
    )

    assert result["ok"] is True
    events = controller.state_store.runtime_storage().list_operator_events(limit=20)
    assert all(row["event_type"] != "client_log" for row in events)


def test_operator_event_payload_humanizes_boot_and_readiness_transitions() -> None:
    ready = build_operator_event_payload(
        event="ready_transition",
        fields={
            "ready": False,
            "recovery_required": True,
            "submission_recovery_ok": False,
            "user_ws_stale": True,
            "market_data_stale": False,
        },
    )
    stale = build_operator_event_payload(
        event="stale_transition",
        fields={"stale_type": "market_data", "stale": False, "age_sec": 12.4},
    )
    initialized = build_operator_event_payload(
        event="controller_initialized",
        fields={"dirty_restart_detected": True, "recovery_required": False},
    )

    assert ready is not None
    assert ready["title"] == "운영 준비도 전환"
    assert ready["main_text"] == "운영 준비 미완료"
    assert ready["sub_text"] == "복구 필요, 주문 복구 확인 필요, 프라이빗 스트림 stale"

    assert stale is not None
    assert stale["title"] == "시장 데이터 상태"
    assert stale["main_text"] == "정상 복귀"
    assert stale["sub_text"] == "age=12.4초"

    assert initialized is not None
    assert initialized["title"] == "컨트롤러 초기화 완료"
    assert initialized["main_text"] == "운영 컨트롤러 초기화가 완료되었습니다."
    assert initialized["sub_text"] == "이전 런타임 종료 흔적 감지"
