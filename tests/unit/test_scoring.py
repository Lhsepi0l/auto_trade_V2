from __future__ import annotations

import pytest

from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.scoring_service import Candidate, ScoringService, SymbolScore
from tests.fixtures.fake_exchange import fake_candle_series


def _cfg() -> RiskConfig:
    return RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=0.2,
        max_notional_pct=50,
        max_leverage=5,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=1800,
    )


@pytest.mark.unit
def test_scoring_confidence_clamped() -> None:
    s = ScoringService()
    scores = {
        "A": SymbolScore("A", 0.9, 0.9, 0.0, "BULL", False, 0.9, "LONG", {}),
        "B": SymbolScore("B", 0.0, 0.0, 0.0, "CHOPPY", False, 0.0, "HOLD", {}),
    }
    c = s.pick_candidate(scores=scores)
    assert c is not None
    assert 0.0 <= c.confidence <= 1.0


@pytest.mark.unit
def test_multi_tf_weighted_score() -> None:
    s = ScoringService()
    candles = {
        "BTCUSDT": {
            "30m": fake_candle_series("BTCUSDT", "30m", base=100),
            "1h": fake_candle_series("BTCUSDT", "1h", base=120),
            "4h": fake_candle_series("BTCUSDT", "4h", base=140),
        }
    }
    out = s.score_universe(cfg=_cfg(), candles_by_symbol_interval=candles)
    assert "BTCUSDT" in out
    assert -1.0 <= out["BTCUSDT"].composite <= 1.0


@pytest.mark.unit
def test_top_vs_second_gap_confidence() -> None:
    s = ScoringService()
    scores = {
        "A": SymbolScore("A", 0.7, 0.7, 0.0, "BULL", False, 0.7, "LONG", {}),
        "B": SymbolScore("B", 0.5, 0.5, 0.0, "BULL", False, 0.5, "LONG", {}),
    }
    c = s.pick_candidate(scores=scores)
    assert isinstance(c, Candidate)
    assert c.strength > c.second_strength
    assert c.confidence > 0
