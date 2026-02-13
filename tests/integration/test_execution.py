from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping

import pytest

from apps.trader_engine.domain.enums import Direction, EngineState, ExecHint
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.execution_service import ExecutionRejected, ExecutionService
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

    def panic(self) -> _State:
        self._state = _State(state=EngineState.PANIC)
        return self._state


class _RiskCfg:
    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> RiskConfig:
        return self._cfg


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


def _svc(
    exchange: FakeBinanceRest,
    cfg: RiskConfig,
    *,
    dry_run: bool = False,
    notifier: FakeNotifier | None = None,
) -> ExecutionService:
    return ExecutionService(
        client=exchange,  # type: ignore[arg-type]
        engine=_Engine(),
        risk=_RiskCfg(cfg),  # type: ignore[arg-type]
        pnl=None,
        policy=None,
        notifier=notifier,
        allowed_symbols=["BTCUSDT", "ETHUSDT"],
        dry_run=dry_run,
        dry_run_strict=False,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_limit_fills_immediately_one_order() -> None:
    ex = FakeBinanceRest()
    ex.limit_fill_mode = "fill_immediate"
    svc = _svc(ex, _cfg(exec_limit_timeout_sec=0.01, exec_limit_retries=2))

    out = await svc.enter_position(
        {"symbol": "BTCUSDT", "direction": Direction.LONG, "exec_hint": ExecHint.LIMIT, "qty": 0.1}
    )
    limit_orders = [o for o in out["orders"] if str(o.get("type")) == "LIMIT"]
    market_orders = [o for o in out["orders"] if str(o.get("type")) == "MARKET"]
    assert len(limit_orders) >= 1
    assert len(market_orders) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_limit_timeout_retry_twice_then_market_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = FakeBinanceRest()
    ex.limit_fill_mode = "never_fill"
    svc = _svc(ex, _cfg(exec_limit_timeout_sec=5.0, exec_limit_retries=2, spread_max_pct=0.05))

    clock = {"t": 0.0}

    def _mono() -> float:
        clock["t"] += 10.0
        return clock["t"]

    monkeypatch.setattr("apps.trader_engine.services.execution_service.time.monotonic", _mono)
    monkeypatch.setattr("apps.trader_engine.services.execution_service.time.sleep", lambda _s: None)

    out = await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.1})
    limit_orders = [o for o in out["orders"] if str(o.get("type")) == "LIMIT"]
    limit_order_ids = {int(o["order_id"]) for o in limit_orders if o.get("order_id") is not None}
    market_orders = [o for o in out["orders"] if str(o.get("type")) == "MARKET"]
    assert len(limit_order_ids) == 2
    assert len(market_orders) == 1
    assert out.get("market_fallback_used") is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_market_fallback_blocked_when_spread_guard_forbids() -> None:
    ex = FakeBinanceRest()
    ex.limit_fill_mode = "never_fill"
    ex.set_book("BTCUSDT", bid=90.0, ask=110.0, mark=100.0)
    svc = _svc(
        ex,
        _cfg(exec_limit_timeout_sec=0.01, exec_limit_retries=2, spread_max_pct=0.001, allow_market_when_wide_spread=False),
    )

    with pytest.raises(ExecutionRejected) as ei:
        await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.1})
    assert "market_fallback_blocked_by_spread_guard" in ei.value.message


@pytest.mark.integration
@pytest.mark.asyncio
async def test_partial_fill_handles_remaining_without_infinite_loop() -> None:
    ex = FakeBinanceRest()
    ex.limit_fill_mode = "partial_then_fill"
    svc = _svc(ex, _cfg(exec_limit_timeout_sec=0.5, exec_limit_retries=2))
    out = await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.2})
    assert out["symbol"] == "BTCUSDT"
    assert float(out.get("remaining_qty") or 0.0) == 0.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_asset_rule_closes_other_symbol_before_entry() -> None:
    ex = FakeBinanceRest()
    ex.positions["ETHUSDT"] = {"position_amt": 0.02, "entry_price": 50.0, "unrealized_pnl": 0.0, "leverage": 1.0}
    svc = _svc(ex, _cfg(exec_limit_timeout_sec=0.01, exec_limit_retries=2))

    out = await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.1})
    assert out["symbol"] == "BTCUSDT"
    assert "ETHUSDT" not in ex.get_open_positions_any()
    assert "BTCUSDT" in ex.get_open_positions_any()
    assert any(bool(fill["reduce_only"]) for fill in ex.fills)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_blocks_entry_orders_but_emits_simulation_event() -> None:
    ex = FakeBinanceRest()
    notifier = FakeNotifier()
    svc = _svc(ex, _cfg(), dry_run=True, notifier=notifier)

    out = await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.1})
    assert out["dry_run"] is True
    assert ex.fills == []
    assert any(str(e.get("kind")) == "ENTER" and bool(e.get("dry_run")) for e in notifier.events)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entry_blocked_when_request_exceeds_budget_cap() -> None:
    ex = FakeBinanceRest()
    ex.available = 1000.0
    svc = _svc(ex, _cfg(capital_mode="FIXED_USDT", capital_usdt=10.0, margin_use_pct=1.0, max_leverage=1))
    out = await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 1.0})
    assert out.get("blocked") is True
    assert str(out.get("block_reason")) == "ENTRY_EXCEEDS_BUDGET_CAP"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entry_uses_live_sizing_when_qty_and_notional_missing() -> None:
    ex = FakeBinanceRest()
    svc = _svc(ex, _cfg(capital_mode="PCT_AVAILABLE", capital_pct=0.2, margin_use_pct=0.9, max_leverage=2))
    out = await svc.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT"})
    assert out.get("symbol") == "BTCUSDT"
    assert not bool(out.get("blocked"))
