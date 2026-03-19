from __future__ import annotations

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
    assert payload["recovery"]["startup_reconcile_ok"] is True
    assert payload["report"]["status"] == "success"
    assert payload["guidance"]["panel_scope"]
    assert payload["risk_forms"]["margin_budget"]["margin_use_pct"] == 0.8
    assert payload["risk_forms"]["trailing"]["trailing_mode"] == "PCT"
    assert payload["risk_forms"]["scoring"]["weights"]["10m"] == 0.25
    assert payload["recent_result"]["last_reason_label"] == "이미 판단 작업이 진행중"


def test_wrap_operator_action_builds_consistent_response() -> None:
    wrapped = wrap_operator_action(
        action="symbol_leverage",
        raw_result={"symbol_leverage_map": {"BTCUSDT": 7.0}},
        context={"symbol": "BTCUSDT"},
    )

    assert wrapped["ok"] is True
    assert wrapped["action"] == "symbol_leverage"
    assert wrapped["summary"] == "BTCUSDT 레버리지 7x 적용"


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


def test_operator_service_triggers_report_and_updates_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    controller = _build_controller(tmp_path)
    service = OperatorService(controller=controller)

    report = service.trigger_report()

    assert report["action"] == "report"
    assert report["result"]["kind"] == "DAILY_REPORT"
    assert controller._status_snapshot()["report"]["reported_at"] is not None
