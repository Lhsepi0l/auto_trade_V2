from __future__ import annotations

from v2.operator.actions import wrap_operator_action
from v2.operator.read_models import build_operator_console_payload


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
            },
            "watchdog": {"enabled": True, "last_ok_at": "2026-03-19T00:00:01+00:00"},
            "submission_recovery": {"ok": True},
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
    assert payload["recovery"]["startup_reconcile_ok"] is True
    assert payload["risk_forms"]["margin_budget"]["margin_use_pct"] == 0.8
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
