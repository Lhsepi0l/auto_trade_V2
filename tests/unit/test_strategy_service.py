from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.scoring_service import Candidate, SymbolScore
from apps.trader_engine.services.strategy_service import PositionState, StrategyService


def _cfg(**overrides: object) -> RiskConfig:
    base = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=0.2,
        max_notional_pct=50,
        max_leverage=5,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=1800,
    )
    return base.model_copy(update=dict(overrides))


def _score(
    symbol: str,
    *,
    long_score: float = 0.5,
    short_score: float = 0.0,
    direction: str = "LONG",
    regime_4h: str = "BULL",
    vol_shock: bool = False,
) -> SymbolScore:
    return SymbolScore(
        symbol=symbol,
        composite=long_score - short_score,
        long_score=long_score,
        short_score=short_score,
        regime_4h=regime_4h,  # type: ignore[arg-type]
        vol_shock=vol_shock,
        strength=max(long_score, short_score),
        direction=direction,  # type: ignore[arg-type]
        timeframes={},
    )


@pytest.mark.unit
def test_short_gated_when_regime_not_bear() -> None:
    svc = StrategyService()
    dec = svc.decide_next_action(
        cfg=_cfg(score_conf_threshold=0.5),
        now=datetime.now(tz=timezone.utc),
        candidate=Candidate(
            symbol="BTCUSDT",
            direction="SHORT",
            confidence=0.9,
            strength=0.9,
            second_strength=0.1,
            regime_4h="BULL",
            vol_shock=False,
        ),
        scores={},
        position=PositionState(
            symbol=None,
            position_amt=0.0,
            unrealized_pnl=0.0,
            last_entry_symbol=None,
            last_entry_at=None,
        ),
    )
    assert dec.kind == "HOLD"
    assert dec.reason == "short_not_allowed_regime"


@pytest.mark.unit
def test_min_hold_blocks_rebalance_before_expiry() -> None:
    svc = StrategyService()
    now = datetime.now(tz=timezone.utc)
    pos = PositionState(
        symbol="BTCUSDT",
        position_amt=1.0,
        unrealized_pnl=-1.0,
        last_entry_symbol="BTCUSDT",
        last_entry_at=now - timedelta(minutes=30),
    )
    scores = {"BTCUSDT": _score("BTCUSDT", long_score=0.2, short_score=0.0)}
    dec = svc.decide_next_action(
        cfg=_cfg(min_hold_minutes=240, score_conf_threshold=0.5),
        now=now,
        candidate=Candidate(
            symbol="ETHUSDT",
            direction="LONG",
            confidence=0.9,
            strength=0.9,
            second_strength=0.1,
            regime_4h="BULL",
            vol_shock=False,
        ),
        scores=scores,
        position=pos,
    )
    assert dec.kind == "HOLD"
    assert dec.reason.startswith("min_hold_active:")


@pytest.mark.unit
def test_vol_shock_forces_immediate_close_even_in_profit() -> None:
    svc = StrategyService()
    dec = svc.decide_next_action(
        cfg=_cfg(),
        now=datetime.now(tz=timezone.utc),
        candidate=None,
        scores={"BTCUSDT": _score("BTCUSDT", vol_shock=True)},
        position=PositionState(
            symbol="BTCUSDT",
            position_amt=1.0,
            unrealized_pnl=15.0,
            last_entry_symbol="BTCUSDT",
            last_entry_at=datetime.now(tz=timezone.utc) - timedelta(hours=6),
        ),
    )
    assert dec.kind == "CLOSE"
    assert dec.reason == "vol_shock_close"
    assert dec.close_symbol == "BTCUSDT"


@pytest.mark.unit
def test_profit_defaults_to_hold() -> None:
    svc = StrategyService()
    dec = svc.decide_next_action(
        cfg=_cfg(min_hold_minutes=0, score_gap_threshold=0.1),
        now=datetime.now(tz=timezone.utc),
        candidate=Candidate(
            symbol="ETHUSDT",
            direction="SHORT",
            confidence=1.0,
            strength=1.0,
            second_strength=0.0,
            regime_4h="BEAR",
            vol_shock=False,
        ),
        scores={"BTCUSDT": _score("BTCUSDT", long_score=0.2)},
        position=PositionState(
            symbol="BTCUSDT",
            position_amt=1.0,
            unrealized_pnl=5.0,
            last_entry_symbol="BTCUSDT",
            last_entry_at=datetime.now(tz=timezone.utc) - timedelta(hours=10),
        ),
    )
    assert dec.kind == "HOLD"
    assert dec.reason == "profit_hold"


def test_decision_attaches_candidate_judgment_context() -> None:
    svc = StrategyService()
    dec = svc.decide_next_action(
        cfg=_cfg(min_hold_minutes=0, score_gap_threshold=0.1),
        now=datetime.now(tz=timezone.utc),
        candidate=Candidate(
            symbol="BTCUSDT",
            direction="SHORT",
            confidence=0.91,
            strength=0.71,
            second_strength=0.41,
            regime_4h="BEAR",
            vol_shock=False,
        ),
        scores={"BTCUSDT": _score("BTCUSDT", long_score=0.2, short_score=0.7)},
        position=PositionState(
            symbol=None,
            position_amt=0.0,
            unrealized_pnl=0.0,
            last_entry_symbol=None,
            last_entry_at=None,
        ),
    )
    assert dec.kind == "ENTER"
    assert dec.candidate_symbol == "BTCUSDT"
    assert dec.candidate_direction == "SHORT"
    assert dec.candidate_regime_4h == "BEAR"
    assert dec.candidate_strength == 0.71
    assert dec.candidate_confidence == 0.91
    assert dec.final_direction == "SHORT"
