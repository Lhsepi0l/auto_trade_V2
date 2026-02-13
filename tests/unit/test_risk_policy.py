from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.risk_service import RiskService


@dataclass
class _Engine:
    state: EngineState = EngineState.RUNNING

    def get_state(self):  # noqa: ANN201
        class _Row:
            def __init__(self, state: EngineState) -> None:
                self.state = state

        return _Row(self.state)

    def set_state(self, state: EngineState):  # noqa: ANN201
        self.state = state
        return self.get_state()

    def panic(self):  # noqa: ANN201
        self.state = EngineState.PANIC
        return self.get_state()


class _RiskCfg:
    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> RiskConfig:
        return self._cfg


class _PnL:
    def __init__(self) -> None:
        self.cooldown_until: Optional[datetime] = None

    def set_cooldown_until(self, *, cooldown_until: Optional[datetime]):  # noqa: ANN201
        self.cooldown_until = cooldown_until
        return None


def _cfg(**overrides: object) -> RiskConfig:
    base = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=1.0,
        max_notional_pct=50,
        max_leverage=5,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=1800,
    )
    return base.model_copy(update=dict(overrides))


def _svc(cfg: RiskConfig) -> tuple[RiskService, _Engine, _PnL]:
    eng = _Engine()
    pnl = _PnL()
    svc = RiskService(
        risk=_RiskCfg(cfg),  # type: ignore[arg-type]
        engine=eng,  # type: ignore[arg-type]
        pnl=pnl,  # type: ignore[arg-type]
    )
    return svc, eng, pnl


@pytest.mark.unit
def test_leverage_cap_enforced() -> None:
    svc, _eng, _pnl = _svc(_cfg(max_leverage=3))
    dec = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "LIMIT", "notional_usdt_est": 100.0, "leverage": 10},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"daily_pnl_pct": 0.0, "drawdown_pct": 0.0, "lose_streak": 0, "cooldown_until": None},
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec.kind == "BLOCK"
    assert dec.reason == "leverage_above_max_leverage"


@pytest.mark.unit
def test_daily_loss_limit_blocks_entries() -> None:
    svc, _eng, _pnl = _svc(_cfg(daily_loss_limit_pct=-0.02))
    dec = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "LIMIT", "notional_usdt_est": 10.0},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"daily_pnl_pct": -2.1, "drawdown_pct": 0.0, "lose_streak": 0, "cooldown_until": None},
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec.kind == "BLOCK"
    assert "daily_loss_limit_reached" in str(dec.reason)


@pytest.mark.unit
def test_dd_limit_triggers_panic() -> None:
    svc, eng, _pnl = _svc(_cfg(dd_limit_pct=-0.10))
    dec = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "LIMIT", "notional_usdt_est": 10.0},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"daily_pnl_pct": 0.0, "drawdown_pct": -15.0, "lose_streak": 0, "cooldown_until": None},
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec.kind == "PANIC"
    assert eng.state == EngineState.PANIC


@pytest.mark.unit
def test_lose_streak_cooldown_blocks_then_unblocks_after_time() -> None:
    svc, eng, _pnl = _svc(_cfg(lose_streak_n=3, cooldown_hours=6))
    dec1 = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "LIMIT", "notional_usdt_est": 10.0},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {"daily_pnl_pct": 0.0, "drawdown_pct": 0.0, "lose_streak": 3, "cooldown_until": None},
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec1.kind == "BLOCK"
    assert dec1.reason == "lose_streak_cooldown"
    assert dec1.until is not None
    assert eng.state == EngineState.COOLDOWN

    dec2 = svc.evaluate_pre_trade(
        {"symbol": "BTCUSDT", "exec_hint": "LIMIT", "notional_usdt_est": 10.0},
        {"equity_usdt": 1000.0, "open_symbols": [], "total_exposure_notional_usdt": 0.0},
        {
            "daily_pnl_pct": 0.0,
            "drawdown_pct": 0.0,
            "lose_streak": 0,
            "cooldown_until": datetime.now(tz=timezone.utc) - timedelta(seconds=1),
        },
        {"bid": 100.0, "ask": 100.1},
    )
    assert dec2.kind == "ALLOW"
    assert eng.state == EngineState.RUNNING
