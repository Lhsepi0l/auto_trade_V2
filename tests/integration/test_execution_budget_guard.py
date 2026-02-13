from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import pytest

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.execution_service import ExecutionService
from tests.fixtures.fake_exchange import FakeBinanceRest


@dataclass
class _State:
    state: EngineState


class _Engine:
    def __init__(self, state: EngineState = EngineState.RUNNING) -> None:
        self._state = _State(state=state)

    def get_state(self) -> _State:
        return self._state


class _Risk:
    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> RiskConfig:
        return self._cfg


def _cfg(**overrides: object) -> RiskConfig:
    base = RiskConfig(
        per_trade_risk_pct=1.0,
        max_exposure_pct=0.5,
        max_notional_pct=100.0,
        max_leverage=3.0,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6.0,
        notify_interval_sec=1800,
        capital_mode="PCT_AVAILABLE",
        capital_pct=0.2,
        capital_usdt=100.0,
        margin_use_pct=0.9,
        max_position_notional_usdt=None,
        fee_buffer_pct=0.002,
    )
    return base.model_copy(update=dict(overrides))


def _svc(exchange: FakeBinanceRest, cfg: RiskConfig, *, dry_run: bool = False) -> ExecutionService:
    return ExecutionService(
        client=exchange,  # type: ignore[arg-type]
        engine=_Engine(),
        risk=_Risk(cfg),  # type: ignore[arg-type]
        pnl=None,
        policy=None,
        notifier=None,
        allowed_symbols=["BTCUSDT", "ETHUSDT"],
        dry_run=dry_run,
        dry_run_strict=False,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entry_uses_computed_qty_when_missing_qty_and_notional() -> None:
    ex = FakeBinanceRest()
    ex.available = 1000.0
    svc = _svc(ex, _cfg(capital_mode="PCT_AVAILABLE", capital_pct=0.2, margin_use_pct=0.9, max_leverage=2.0))

    out = await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "MARKET"})
    assert out.get("blocked") is not True
    assert out.get("orders")
    filled = float(out["orders"][0].get("executed_qty") or 0.0)
    assert filled > 0.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entry_block_when_budget_too_small() -> None:
    ex = FakeBinanceRest()
    ex.available = 5.0
    svc = _svc(
        ex,
        _cfg(capital_mode="FIXED_USDT", capital_usdt=5.0, margin_use_pct=0.1, max_leverage=1.0),
    )

    out = await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT"})
    assert out.get("blocked") is True
    assert str(out.get("block_reason")) in {"BUDGET_TOO_SMALL_FOR_MIN_NOTIONAL", "BUDGET_TOO_SMALL_FOR_MIN_QTY"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_blocks_real_orders_but_reports_sizing() -> None:
    ex = FakeBinanceRest()
    ex.available = 1000.0
    svc = _svc(ex, _cfg(capital_mode="PCT_AVAILABLE", capital_pct=0.2), dry_run=True)

    out = await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT"})
    assert out.get("dry_run") is True
    assert float(out.get("qty") or 0.0) > 0.0
    assert float(out.get("notional_usdt_est") or 0.0) > 0.0
    assert ex.fills == []
