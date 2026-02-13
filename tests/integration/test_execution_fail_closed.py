from __future__ import annotations

from dataclasses import dataclass

import pytest

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.execution_service import ExecutionService
from tests.fixtures.fake_exchange import FakeBinanceRest
from tests.fixtures.fake_notifier import FakeNotifier


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


class _OpenOrdersFailExchange(FakeBinanceRest):
    def get_open_orders_usdtm(self, symbols):  # type: ignore[override]
        raise RuntimeError("open_orders_down")


class _PositionFailExchange(FakeBinanceRest):
    def get_open_positions_any(self):  # type: ignore[override]
        raise RuntimeError("positions_down")


class _MarketDataFailExchange(FakeBinanceRest):
    def get_mark_price(self, symbol: str):  # type: ignore[override]
        raise RuntimeError("mark_down")

    def get_book_ticker(self, symbol: str):  # type: ignore[override]
        raise RuntimeError("book_down")


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


def _svc(exchange: FakeBinanceRest, notifier: FakeNotifier | None = None) -> ExecutionService:
    return ExecutionService(
        client=exchange,  # type: ignore[arg-type]
        engine=_Engine(),
        risk=_RiskCfg(_cfg()),  # type: ignore[arg-type]
        pnl=None,
        policy=None,
        notifier=notifier,
        allowed_symbols=["BTCUSDT", "ETHUSDT"],
        dry_run=False,
        dry_run_strict=False,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entry_fail_closed_when_open_orders_precheck_fails() -> None:
    ex = _OpenOrdersFailExchange()
    notifier = FakeNotifier()
    svc = _svc(ex, notifier=notifier)

    out = await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.1})
    assert bool(out.get("blocked")) is True
    assert str(out.get("block_reason")) == "PRECHECK_OPEN_ORDERS_FAILED"
    assert ex.fills == []
    assert all(str(o.get("status") or "") != "FILLED" for o in ex.open_orders)
    assert any(str(e.get("kind")) == "BLOCK" and str(e.get("reason")) == "PRECHECK_OPEN_ORDERS_FAILED" for e in notifier.events)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entry_fail_closed_when_position_precheck_fails() -> None:
    ex = _PositionFailExchange()
    svc = _svc(ex, notifier=None)

    out = await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.1})
    assert bool(out.get("blocked")) is True
    assert str(out.get("block_reason")) == "PRECHECK_POSITION_FAILED"
    assert ex.fills == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entry_fail_closed_when_market_data_unavailable() -> None:
    ex = _MarketDataFailExchange()
    svc = _svc(ex, notifier=None)

    out = await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.1})
    assert bool(out.get("blocked")) is True
    assert str(out.get("block_reason")) == "MARKET_DATA_UNAVAILABLE"
    assert ex.fills == []
