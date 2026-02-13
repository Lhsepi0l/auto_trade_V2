from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.risk_service import Decision, RiskService


@dataclass
class _FakeEngine:
    state: EngineState = EngineState.RUNNING

    def get_state(self):
        class _Row:
            def __init__(self, s: EngineState) -> None:
                self.state = s

        return _Row(self.state)

    def set_state(self, s: EngineState):
        self.state = s
        return self.get_state()

    def panic(self):
        self.state = EngineState.PANIC
        return self.get_state()


class _FakeRiskCfg:
    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> RiskConfig:
        return self._cfg


class _FakePnL:
    def __init__(self) -> None:
        self.cooldown_until: Optional[datetime] = None

    def set_cooldown_until(self, *, cooldown_until: Optional[datetime]):
        self.cooldown_until = cooldown_until
        return None


def _mk(cfg: RiskConfig) -> tuple[RiskService, _FakeEngine, _FakePnL]:
    eng = _FakeEngine()
    pnl = _FakePnL()
    svc = RiskService(
        risk=_FakeRiskCfg(cfg),  # type: ignore[arg-type]
        engine=eng,  # type: ignore[arg-type]
        pnl=pnl,  # type: ignore[arg-type]
        stop_on_daily_loss=False,
    )
    return svc, eng, pnl


def test_cooldown_active_blocks_and_sets_engine_cooldown():
    cfg = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=0.2,
        max_notional_pct=50,
        max_leverage=5,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=120,
    )
    svc, eng, _pnl = _mk(cfg)
    until = datetime.now(tz=timezone.utc) + timedelta(minutes=10)
    dec = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "MARKET"},
        {"equity_usdt": 1000.0, "notional_usdt_est": 10.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"cooldown_until": until, "daily_pnl_pct": 0.0, "drawdown_pct": 0.0, "lose_streak": 0},
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec.kind == "BLOCK"
    assert dec.reason == "cooldown_active"
    assert eng.state == EngineState.COOLDOWN


def test_stuck_cooldown_recovers_to_running_when_no_cooldown_marker():
    cfg = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=0.2,
        max_notional_pct=50,
        max_leverage=5,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=120,
    )
    svc, eng, _pnl = _mk(cfg)
    eng.state = EngineState.COOLDOWN
    dec = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "LIMIT", "notional_usdt_est": 10.0},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"cooldown_until": None, "daily_pnl_pct": 0.0, "drawdown_pct": 0.0, "lose_streak": 0},
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec.kind in ("ALLOW", "BLOCK", "PANIC")
    assert eng.state == EngineState.RUNNING


def test_drawdown_limit_panics_engine():
    cfg = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=0.2,
        max_notional_pct=50,
        max_leverage=5,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.10,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=120,
    )
    svc, eng, _pnl = _mk(cfg)
    dec = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "MARKET", "notional_usdt_est": 10.0},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"cooldown_until": None, "daily_pnl_pct": 0.0, "drawdown_pct": -50.0, "lose_streak": 0},
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec.kind == "PANIC"
    assert eng.state == EngineState.PANIC


def test_spread_guard_blocks_market_only_by_default():
    cfg = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=1.0,
        max_notional_pct=100,
        max_leverage=50,
        daily_loss_limit_pct=-1.0,
        dd_limit_pct=-1.0,
        lose_streak_n=10,
        cooldown_hours=1,
        notify_interval_sec=120,
        spread_max_pct=0.005,  # 0.5%
        allow_market_when_wide_spread=False,
    )
    svc, _eng, _pnl = _mk(cfg)
    wide = {"bid": 100.0, "ask": 101.0}  # ~0.995% spread
    dec1 = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "MARKET", "notional_usdt_est": 10.0},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"cooldown_until": None, "daily_pnl_pct": 0.0, "drawdown_pct": 0.0, "lose_streak": 0},
        wide,
    )
    assert dec1.kind == "BLOCK"
    dec2 = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "LIMIT", "notional_usdt_est": 10.0},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"cooldown_until": None, "daily_pnl_pct": 0.0, "drawdown_pct": 0.0, "lose_streak": 0},
        wide,
    )
    assert dec2.kind == "ALLOW"


def test_constraints_block_on_leverage_above_cap():
    cfg = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=1.0,
        max_notional_pct=100,
        max_leverage=3,
        daily_loss_limit_pct=-1.0,
        dd_limit_pct=-1.0,
        lose_streak_n=10,
        cooldown_hours=1,
        notify_interval_sec=120,
    )
    svc, _eng, _pnl = _mk(cfg)
    dec = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "LIMIT", "notional_usdt_est": 100.0, "leverage": 10},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"cooldown_until": None, "daily_pnl_pct": 0.0, "drawdown_pct": 0.0, "lose_streak": 0},
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec.kind == "BLOCK"
    assert dec.reason == "leverage_above_max_leverage"
