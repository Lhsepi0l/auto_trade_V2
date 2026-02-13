from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import pytest

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.execution_service import ExecutionRejected, ExecutionService
from tests.fixtures.fake_exchange import FakeBinanceRest


@dataclass
class _State:
    state: EngineState


class _Engine:
    def __init__(self, state: EngineState = EngineState.RUNNING) -> None:
        self._state = _State(state=state)

    def get_state(self) -> _State:
        return self._state


class _RiskCfg:
    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> RiskConfig:
        return self._cfg


class _SlowFakeBinance(FakeBinanceRest):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.place_limit_calls = 0

    def place_order_limit(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.place_limit_calls += 1
        time.sleep(0.15)
        return super().place_order_limit(**kwargs)


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


def _svc(exchange: FakeBinanceRest, cfg: RiskConfig) -> ExecutionService:
    return ExecutionService(
        client=exchange,  # type: ignore[arg-type]
        engine=_Engine(),
        risk=_RiskCfg(cfg),  # type: ignore[arg-type]
        pnl=None,
        policy=None,
        notifier=None,
        allowed_symbols=["BTCUSDT", "ETHUSDT"],
        dry_run=False,
        dry_run_strict=False,
        exec_lock_timeout_sec=2.0,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_enter_is_serialized_single_order_placement() -> None:
    ex = _SlowFakeBinance()
    ex.limit_fill_mode = "fill_immediate"
    svc = _svc(ex, _cfg(exec_limit_timeout_sec=0.01, exec_limit_retries=1))
    intent = {"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.1}

    t1 = asyncio.create_task(svc.enter_position(intent))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(svc.enter_position(intent))
    r1, r2 = await asyncio.wait_for(asyncio.gather(t1, t2, return_exceptions=True), timeout=5.0)

    outcomes = [r1, r2]
    success = [x for x in outcomes if isinstance(x, dict)]
    failures = [x for x in outcomes if isinstance(x, ExecutionRejected)]

    assert ex.place_limit_calls == 1
    assert len(success) == 1
    assert len(failures) == 1
    assert failures[0].message in {"adding_to_position_not_allowed", "single_asset_rule_unresolved"}
