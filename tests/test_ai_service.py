from __future__ import annotations

from apps.trader_engine.services.ai_service import AiService


def test_ai_stub_returns_hold_when_no_candidate():
    ai = AiService(mode="stub", conf_threshold=0.65)
    sig = ai.get_signal({"candidate": None, "scores": {}})
    assert sig.direction == "HOLD"
    assert 0.0 <= sig.confidence <= 1.0


def test_ai_stub_uses_candidate_and_threshold():
    ai = AiService(mode="stub", conf_threshold=0.65)
    sig_low = ai.get_signal({"candidate": {"symbol": "BTCUSDT", "direction": "LONG", "strength": 0.4}})
    assert sig_low.direction == "HOLD"
    sig_hi = ai.get_signal({"candidate": {"symbol": "BTCUSDT", "direction": "LONG", "strength": 0.9}})
    assert sig_hi.direction == "LONG"
    assert sig_hi.exec_hint == "LIMIT"


def test_ai_manual_risk_tag_overrides():
    ai = AiService(mode="stub", conf_threshold=0.0, manual_risk_tag="NEWS_RISK")
    sig = ai.get_signal({"candidate": {"symbol": "ETHUSDT", "direction": "SHORT", "strength": 0.7}})
    assert sig.risk_tag == "NEWS_RISK"

