from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.market_data_service import Candle
from apps.trader_engine.services.watchdog_service import (
    WatchdogService,
    _atr_trail_distance_pct,
    _position_pnl_pct,
)


@dataclass
class _State:
    state: EngineState


class _FakeEngine:
    def __init__(self, state: EngineState = EngineState.RUNNING) -> None:
        self._st = _State(state=state)

    def get_state(self) -> _State:
        return self._st


class _FakeRisk:
    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> RiskConfig:
        return self._cfg


class _FakeExecution:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def close_position(self, symbol: str, *, reason: str = "EXIT") -> Dict[str, Any]:
        self.calls.append({"symbol": symbol, "reason": reason})
        return {"symbol": symbol, "closed": True, "reason": reason}


class _FakeNotifier:
    def __init__(self) -> None:
        self.events: List[Mapping[str, Any]] = []

    async def send_event(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))

    async def send_status_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        return None


class _FakeClient:
    def __init__(self) -> None:
        self.positions: Dict[str, Dict[str, float]] = {}
        self.mark: Dict[str, float] = {}
        self.bid: Dict[str, float] = {}
        self.ask: Dict[str, float] = {}

    def get_open_positions_any(self) -> Dict[str, Dict[str, float]]:
        return dict(self.positions)

    def get_mark_price(self, symbol: str) -> Mapping[str, Any]:
        return {"symbol": symbol, "markPrice": str(self.mark.get(symbol, 0.0))}

    def get_book_ticker(self, symbol: str) -> Mapping[str, Any]:
        return {"symbol": symbol, "bidPrice": str(self.bid.get(symbol, 0.0)), "askPrice": str(self.ask.get(symbol, 0.0))}


class _FakeMarketData:
    def __init__(self) -> None:
        self.by_key: Dict[tuple[str, str], list[Any]] = {}

    def get_klines(self, *, symbol: str, interval: str, limit: int = 120):  # type: ignore[no-untyped-def]
        return list(self.by_key.get((symbol.upper(), interval.lower()), []))


def _mk_cfg() -> RiskConfig:
    return RiskConfig(
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
        trailing_enabled=True,
        trailing_mode="PCT",
        trail_arm_pnl_pct=1.2,
        trail_distance_pnl_pct=0.8,
        trail_grace_minutes=30,
    )


def test_watchdog_shock_from_entry_triggers_close() -> None:
    c = _FakeClient()
    c.positions = {"BTCUSDT": {"position_amt": 0.1, "entry_price": 100.0}}
    c.mark = {"BTCUSDT": 98.0}  # -2%
    c.bid = {"BTCUSDT": 97.9}
    c.ask = {"BTCUSDT": 98.1}

    exe = _FakeExecution()
    ntf = _FakeNotifier()
    wd = WatchdogService(
        client=c,  # type: ignore[arg-type]
        engine=_FakeEngine(EngineState.RUNNING),  # type: ignore[arg-type]
        risk=_FakeRisk(_mk_cfg()),  # type: ignore[arg-type]
        execution=exe,  # type: ignore[arg-type]
        notifier=ntf,  # type: ignore[arg-type]
    )
    asyncio.run(wd.tick_once())

    assert exe.calls
    assert exe.calls[0]["reason"] == "WATCHDOG_SHOCK"
    assert wd.metrics.last_shock_reason is not None


def test_watchdog_spread_block_alert_only() -> None:
    c = _FakeClient()
    c.positions = {"ETHUSDT": {"position_amt": 1.0, "entry_price": 100.0}}
    c.mark = {"ETHUSDT": 100.0}
    c.bid = {"ETHUSDT": 90.0}
    c.ask = {"ETHUSDT": 110.0}

    cfg = _mk_cfg().model_copy(update={"allow_market_when_wide_spread": False, "spread_max_pct": 0.001})
    exe = _FakeExecution()
    ntf = _FakeNotifier()
    wd = WatchdogService(
        client=c,  # type: ignore[arg-type]
        engine=_FakeEngine(EngineState.RUNNING),  # type: ignore[arg-type]
        risk=_FakeRisk(cfg),  # type: ignore[arg-type]
        execution=exe,  # type: ignore[arg-type]
        notifier=ntf,  # type: ignore[arg-type]
    )
    asyncio.run(wd.tick_once())

    assert wd.metrics.market_blocked_by_spread is True
    assert not exe.calls
    assert any(str(e.get("kind")) == "BLOCK" for e in ntf.events)


def test_watchdog_no_action_in_panic() -> None:
    c = _FakeClient()
    c.positions = {"XAUUSDT": {"position_amt": 0.5, "entry_price": 100.0}}
    c.mark = {"XAUUSDT": 90.0}
    c.bid = {"XAUUSDT": 89.9}
    c.ask = {"XAUUSDT": 90.1}

    exe = _FakeExecution()
    ntf = _FakeNotifier()
    wd = WatchdogService(
        client=c,  # type: ignore[arg-type]
        engine=_FakeEngine(EngineState.PANIC),  # type: ignore[arg-type]
        risk=_FakeRisk(_mk_cfg()),  # type: ignore[arg-type]
        execution=exe,  # type: ignore[arg-type]
        notifier=ntf,  # type: ignore[arg-type]
    )
    asyncio.run(wd.tick_once())

    assert not exe.calls


def test_position_pnl_pct_short_and_long() -> None:
    assert abs(_position_pnl_pct(side="LONG", entry_price=100.0, mark=102.0) - 2.0) < 1e-9
    assert abs(_position_pnl_pct(side="SHORT", entry_price=100.0, mark=98.0) - 2.0) < 1e-9


def test_atr_trail_distance_clamp_and_trigger_math() -> None:
    d1 = _atr_trail_distance_pct(atr_pct_value=0.1, k=2.0, min_pct=0.6, max_pct=1.8)
    d2 = _atr_trail_distance_pct(atr_pct_value=0.5, k=2.0, min_pct=0.6, max_pct=1.8)
    d3 = _atr_trail_distance_pct(atr_pct_value=2.0, k=2.0, min_pct=0.6, max_pct=1.8)
    assert abs(d1 - 0.6) < 1e-9
    assert abs(d2 - 1.0) < 1e-9
    assert abs(d3 - 1.8) < 1e-9
    peak = 3.2
    trigger_level = peak - d2
    assert abs(trigger_level - 2.2) < 1e-9


def test_watchdog_trailing_grace_arm_peak_trigger_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    c = _FakeClient()
    c.positions = {"BTCUSDT": {"position_amt": 0.1, "entry_price": 100.0}}
    c.bid = {"BTCUSDT": 100.0}
    c.ask = {"BTCUSDT": 100.2}
    cfg = _mk_cfg().model_copy(update={"shock_1m_pct": 0.5, "shock_from_entry_pct": 0.5})

    exe = _FakeExecution()
    wd = WatchdogService(
        client=c,  # type: ignore[arg-type]
        engine=_FakeEngine(EngineState.RUNNING),  # type: ignore[arg-type]
        risk=_FakeRisk(cfg),  # type: ignore[arg-type]
        execution=exe,  # type: ignore[arg-type]
        notifier=None,
    )

    ts = {"v": 1000.0}

    def _time() -> float:
        return float(ts["v"])

    monkeypatch.setattr("apps.trader_engine.services.watchdog_service.time.time", _time)

    c.mark["BTCUSDT"] = 103.0
    asyncio.run(wd.tick_once())  # before grace; should not arm/close
    assert exe.calls == []

    ts["v"] = 1000.0 + (31 * 60)
    c.mark["BTCUSDT"] = 101.3  # arm
    asyncio.run(wd.tick_once())
    assert exe.calls == []

    ts["v"] += 10
    c.mark["BTCUSDT"] = 102.0  # peak
    asyncio.run(wd.tick_once())
    assert exe.calls == []

    ts["v"] += 10
    c.mark["BTCUSDT"] = 101.1  # drop > distance -> trigger
    asyncio.run(wd.tick_once())
    assert len(exe.calls) == 1
    assert exe.calls[0]["reason"] == "TRAILING_PCT"

    ts["v"] += 10
    c.mark["BTCUSDT"] = 100.8
    asyncio.run(wd.tick_once())
    assert len(exe.calls) == 1


def test_watchdog_trailing_short_position(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    c = _FakeClient()
    c.positions = {"ETHUSDT": {"position_amt": -0.2, "entry_price": 100.0}}
    c.bid = {"ETHUSDT": 100.0}
    c.ask = {"ETHUSDT": 100.2}
    cfg = _mk_cfg().model_copy(update={"trail_grace_minutes": 0, "shock_1m_pct": 0.5, "shock_from_entry_pct": 0.5})
    exe = _FakeExecution()
    wd = WatchdogService(
        client=c,  # type: ignore[arg-type]
        engine=_FakeEngine(EngineState.RUNNING),  # type: ignore[arg-type]
        risk=_FakeRisk(cfg),  # type: ignore[arg-type]
        execution=exe,  # type: ignore[arg-type]
        notifier=None,
    )

    c.mark["ETHUSDT"] = 98.0  # +2% for short => armed
    asyncio.run(wd.tick_once())
    assert exe.calls == []

    c.mark["ETHUSDT"] = 99.5  # +0.5% => trigger
    asyncio.run(wd.tick_once())
    assert len(exe.calls) == 1
    assert exe.calls[0]["reason"] == "TRAILING_PCT"


def test_watchdog_trailing_atr_mode_triggers_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    c = _FakeClient()
    c.positions = {"BTCUSDT": {"position_amt": 0.1, "entry_price": 100.0}}
    c.bid = {"BTCUSDT": 100.0}
    c.ask = {"BTCUSDT": 100.2}
    cfg = _mk_cfg().model_copy(
        update={
            "trail_grace_minutes": 0,
            "shock_1m_pct": 0.5,
            "shock_from_entry_pct": 0.5,
            "trailing_mode": "ATR",
            "atr_trail_timeframe": "1h",
            "atr_trail_k": 2.0,
            "atr_trail_min_pct": 0.6,
            "atr_trail_max_pct": 1.8,
        }
    )
    md = _FakeMarketData()
    md.by_key[("BTCUSDT", "1h")] = [
        Candle(open_time_ms=i, open=100.0, high=100.2, low=99.8, close=100.0, volume=1.0, close_time_ms=i + 1)
        for i in range(30)
    ]
    exe = _FakeExecution()
    wd = WatchdogService(
        client=c,  # type: ignore[arg-type]
        engine=_FakeEngine(EngineState.RUNNING),  # type: ignore[arg-type]
        risk=_FakeRisk(cfg),  # type: ignore[arg-type]
        execution=exe,  # type: ignore[arg-type]
        notifier=None,
        market_data=md,  # type: ignore[arg-type]
    )

    c.mark["BTCUSDT"] = 101.3  # arm
    asyncio.run(wd.tick_once())
    assert exe.calls == []

    c.mark["BTCUSDT"] = 101.0  # peak holds at 1.3%
    asyncio.run(wd.tick_once())
    assert exe.calls == []

    c.mark["BTCUSDT"] = 100.4  # pnl=0.4%, dist=min clamp 0.6 -> trigger level=0.7%
    asyncio.run(wd.tick_once())
    assert len(exe.calls) == 1
    assert exe.calls[0]["reason"] == "TRAILING_ATR"

    c.mark["BTCUSDT"] = 100.2
    asyncio.run(wd.tick_once())
    assert len(exe.calls) == 1
