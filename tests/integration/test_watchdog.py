from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping

import pytest

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.execution_service import ExecutionRejected
from apps.trader_engine.services.watchdog_service import WatchdogService
from apps.trader_engine.storage.db import close, connect, migrate
from apps.trader_engine.storage.repositories import TrailingStateRepo
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


class _Execution:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def close_position(self, symbol: str, *, reason: str = "EXIT") -> Dict[str, Any]:
        self.calls.append({"symbol": symbol, "reason": reason})
        return {"symbol": symbol, "reason": reason, "closed": True}


class _FailThenSuccessExecution:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._failed_once = False

    async def close_position(self, symbol: str, *, reason: str = "EXIT") -> Dict[str, Any]:
        self.calls.append({"symbol": symbol, "reason": reason})
        if not self._failed_once:
            self._failed_once = True
            raise ExecutionRejected("simulated_close_failure")
        return {"symbol": symbol, "reason": reason, "closed": True}


def _cfg(**overrides: object) -> RiskConfig:
    base = RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=0.2,
        max_notional_pct=50,
        max_leverage=5,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=1800,
        enable_watchdog=True,
        watchdog_interval_sec=10,
        shock_1m_pct=0.01,
        shock_from_entry_pct=0.012,
    )
    return base.model_copy(update=dict(overrides))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watchdog_closes_on_1m_shock(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = FakeBinanceRest()
    ex.positions["BTCUSDT"] = {"position_amt": 0.1, "entry_price": 100.0, "unrealized_pnl": 0.0}
    exe = _Execution()
    notifier = FakeNotifier()
    wd = WatchdogService(
        client=ex,  # type: ignore[arg-type]
        engine=_Engine(),
        risk=_RiskCfg(_cfg()),  # type: ignore[arg-type]
        execution=exe,  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
    )

    ts = {"v": 1000.0}
    marks = [100.0, 100.0, 99.9, 99.8, 99.7, 98.8, 98.8]

    def _time() -> float:
        cur = ts["v"]
        ts["v"] += 10.0
        return cur

    monkeypatch.setattr("apps.trader_engine.services.watchdog_service.time.time", _time)
    for m in marks:
        ex.set_book("BTCUSDT", bid=m - 0.05, ask=m + 0.05, mark=m)
        await wd.tick_once()

    assert exe.calls
    assert exe.calls[-1]["reason"] == "WATCHDOG_SHOCK"
    assert wd.metrics.last_shock_reason is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watchdog_closes_on_entry_drop() -> None:
    ex = FakeBinanceRest()
    ex.positions["BTCUSDT"] = {"position_amt": 0.1, "entry_price": 100.0, "unrealized_pnl": 0.0}
    ex.set_book("BTCUSDT", bid=98.7, ask=98.9, mark=98.8)
    exe = _Execution()
    wd = WatchdogService(
        client=ex,  # type: ignore[arg-type]
        engine=_Engine(),
        risk=_RiskCfg(_cfg()),  # type: ignore[arg-type]
        execution=exe,  # type: ignore[arg-type]
        notifier=None,
    )
    await wd.tick_once()
    assert exe.calls
    assert exe.calls[0]["reason"] == "WATCHDOG_SHOCK"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watchdog_spread_wide_sets_block_tag_without_closing() -> None:
    ex = FakeBinanceRest()
    ex.positions["ETHUSDT"] = {"position_amt": 1.0, "entry_price": 50.0, "unrealized_pnl": 0.0}
    ex.set_book("ETHUSDT", bid=45.0, ask=55.0, mark=50.0)
    exe = _Execution()
    notifier = FakeNotifier()
    wd = WatchdogService(
        client=ex,  # type: ignore[arg-type]
        engine=_Engine(),
        risk=_RiskCfg(_cfg(spread_max_pct=0.001, allow_market_when_wide_spread=False)),  # type: ignore[arg-type]
        execution=exe,  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
    )
    await wd.tick_once()

    assert wd.metrics.market_blocked_by_spread is True
    assert exe.calls == []
    assert any(str(e.get("kind")) == "BLOCK" for e in notifier.events)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watchdog_trailing_rise_then_drop_closes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = FakeBinanceRest()
    ex.positions["BTCUSDT"] = {"position_amt": 0.1, "entry_price": 100.0, "unrealized_pnl": 0.0}
    exe = _Execution()
    notifier = FakeNotifier()
    wd = WatchdogService(
        client=ex,  # type: ignore[arg-type]
        engine=_Engine(),
        risk=_RiskCfg(
            _cfg(
                shock_1m_pct=0.5,
                shock_from_entry_pct=0.5,
                trail_grace_minutes=0,
                trail_arm_pnl_pct=1.2,
                trail_distance_pnl_pct=0.8,
                trailing_enabled=True,
                trailing_mode="PCT",
            )
        ),  # type: ignore[arg-type]
        execution=exe,  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
    )

    ts = {"v": 1000.0}

    def _time() -> float:
        cur = ts["v"]
        ts["v"] += 10.0
        return cur

    monkeypatch.setattr("apps.trader_engine.services.watchdog_service.time.time", _time)

    for m in [101.3, 102.0, 101.1, 100.8]:
        ex.set_book("BTCUSDT", bid=m - 0.05, ask=m + 0.05, mark=m)
        await wd.tick_once()

    assert len(exe.calls) == 1
    assert exe.calls[0]["reason"] == "TRAILING_PCT"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watchdog_trailing_atr_mode_known_atr_pct_triggers_once(monkeypatch: pytest.MonkeyPatch) -> None:
    ex = FakeBinanceRest()
    ex.positions["BTCUSDT"] = {"position_amt": 0.1, "entry_price": 100.0, "unrealized_pnl": 0.0}
    exe = _Execution()
    wd = WatchdogService(
        client=ex,  # type: ignore[arg-type]
        engine=_Engine(),
        risk=_RiskCfg(
            _cfg(
                shock_1m_pct=0.5,
                shock_from_entry_pct=0.5,
                trail_grace_minutes=0,
                trailing_enabled=True,
                trailing_mode="ATR",
                atr_trail_timeframe="1h",
                atr_trail_k=2.0,
                atr_trail_min_pct=0.6,
                atr_trail_max_pct=1.8,
            )
        ),  # type: ignore[arg-type]
        execution=exe,  # type: ignore[arg-type]
        notifier=None,
    )

    def _known_atr(*, symbol: str, timeframe: str) -> float:  # type: ignore[no-untyped-def]
        assert symbol == "BTCUSDT"
        assert timeframe == "1h"
        return 0.6  # dist = clamp(2*0.6, 0.6, 1.8) = 1.2

    monkeypatch.setattr(wd, "_get_atr_pct_cached", _known_atr)

    for m in [101.3, 101.0, 100.0, 99.8]:
        ex.set_book("BTCUSDT", bid=m - 0.05, ask=m + 0.05, mark=m)
        await wd.tick_once()

    assert len(exe.calls) == 1
    assert exe.calls[0]["reason"] == "TRAILING_ATR"
    assert "distance_pct=1.200000" in str(wd.metrics.last_trailing_reason or "")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watchdog_trailing_close_failure_retries_after_cooldown() -> None:
    ex = FakeBinanceRest()
    ex.positions["BTCUSDT"] = {"position_amt": 0.1, "entry_price": 100.0, "unrealized_pnl": 0.0}
    exe = _FailThenSuccessExecution()
    wd = WatchdogService(
        client=ex,  # type: ignore[arg-type]
        engine=_Engine(),
        risk=_RiskCfg(
            _cfg(
                shock_1m_pct=0.5,
                shock_from_entry_pct=0.5,
                trail_grace_minutes=0,
                trail_arm_pnl_pct=1.2,
                trail_distance_pnl_pct=0.8,
                trailing_enabled=True,
                trailing_mode="PCT",
            )
        ),  # type: ignore[arg-type]
        execution=exe,  # type: ignore[arg-type]
        notifier=None,
        trailing_retry_base_sec=0.1,
        trailing_retry_max_sec=0.1,
    )

    for m in [101.3, 102.0, 101.1]:
        ex.set_book("BTCUSDT", bid=m - 0.05, ask=m + 0.05, mark=m)
        await wd.tick_once()

    assert len(exe.calls) == 1
    ex.set_book("BTCUSDT", bid=100.95, ask=101.05, mark=101.0)
    await wd.tick_once()
    assert len(exe.calls) == 1

    await asyncio.sleep(0.55)
    await wd.tick_once()
    assert len(exe.calls) == 2
    assert exe.calls[0]["reason"] == "TRAILING_PCT"
    assert exe.calls[1]["reason"] == "TRAILING_PCT"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_watchdog_trailing_state_survives_restart(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = connect(str(tmp_path / "watchdog_trailing.sqlite3"))
    migrate(db)
    try:
        trailing_repo = TrailingStateRepo(db)
        ex = FakeBinanceRest()
        ex.positions["BTCUSDT"] = {"position_amt": 0.1, "entry_price": 100.0, "unrealized_pnl": 0.0}
        cfg = _cfg(
            shock_1m_pct=0.5,
            shock_from_entry_pct=0.5,
            trail_grace_minutes=0,
            trail_arm_pnl_pct=1.2,
            trail_distance_pnl_pct=0.8,
            trailing_enabled=True,
            trailing_mode="PCT",
        )

        wd1 = WatchdogService(
            client=ex,  # type: ignore[arg-type]
            engine=_Engine(),
            risk=_RiskCfg(cfg),  # type: ignore[arg-type]
            execution=_Execution(),  # type: ignore[arg-type]
            notifier=None,
            trailing_state_repo=trailing_repo,
        )
        for m in [101.3, 102.0]:
            ex.set_book("BTCUSDT", bid=m - 0.05, ask=m + 0.05, mark=m)
            await wd1.tick_once()

        before = wd1._trailing  # type: ignore[attr-defined]
        assert before is not None
        assert before.armed is True
        assert before.peak_pnl_pct >= 2.0

        wd2 = WatchdogService(
            client=ex,  # type: ignore[arg-type]
            engine=_Engine(),
            risk=_RiskCfg(cfg),  # type: ignore[arg-type]
            execution=_Execution(),  # type: ignore[arg-type]
            notifier=None,
            trailing_state_repo=trailing_repo,
        )
        ex.set_book("BTCUSDT", bid=101.45, ask=101.55, mark=101.5)
        await wd2.tick_once()
        after = wd2._trailing  # type: ignore[attr-defined]
        assert after is not None
        assert after.armed is True
        assert abs(after.entry_ts - before.entry_ts) < 1e-9
        assert abs(after.peak_pnl_pct - before.peak_pnl_pct) < 1e-9
    finally:
        close(db)
