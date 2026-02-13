from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.user_stream_service import UserStreamService
from apps.trader_engine.storage.db import connect, migrate
from apps.trader_engine.storage.repositories import PnLStateRepo


class _FakeEngine:
    def __init__(self) -> None:
        self.connected: Optional[bool] = None
        self.last_event: Optional[datetime] = None

    def set_ws_status(self, *, connected: bool, last_event_time: datetime | None = None):  # type: ignore[override]
        self.connected = connected
        if last_event_time is not None:
            self.last_event = last_event_time
        return None


class _FakeExecution:
    pass


class _FakeNotifier:
    def __init__(self) -> None:
        self.events: list[Mapping[str, Any]] = []

    async def send_event(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))

    async def send_status_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        return None


class _FakeClient:
    def get_open_positions_any(self) -> Dict[str, Dict[str, float]]:
        return {}

    def get_account_balance_usdtm(self) -> Dict[str, float]:
        return {"wallet": 1000.0, "available": 1000.0}


def test_order_trade_update_reduce_only_updates_pnl(tmp_path) -> None:
    db = connect(str(tmp_path / "t.sqlite3"))
    migrate(db)
    pnl = PnLService(repo=PnLStateRepo(db))
    eng = _FakeEngine()
    ntf = _FakeNotifier()
    svc = UserStreamService(
        client=_FakeClient(),  # type: ignore[arg-type]
        engine=eng,  # type: ignore[arg-type]
        pnl=pnl,
        execution=_FakeExecution(),  # type: ignore[arg-type]
        notifier=ntf,  # type: ignore[arg-type]
    )

    msg = {
        "e": "ORDER_TRADE_UPDATE",
        "E": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
        "o": {
            "x": "TRADE",
            "X": "FILLED",
            "s": "BTCUSDT",
            "S": "SELL",
            "l": "0.010",
            "L": "50000",
            "rp": "-3.5",
            "R": True,
            "T": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
        },
    }
    asyncio.run(svc._handle_order_trade_update(msg))

    st = pnl.get_or_bootstrap()
    assert st.daily_realized_pnl == -3.5
    assert st.lose_streak == 1
    assert st.last_fill_symbol == "BTCUSDT"
    assert st.last_fill_side == "SELL"
    assert st.last_fill_realized_pnl == -3.5
    assert any(str(e.get("kind")) == "FILL" for e in ntf.events)


def test_on_message_updates_engine_ws_status(tmp_path) -> None:
    db = connect(str(tmp_path / "u.sqlite3"))
    migrate(db)
    pnl = PnLService(repo=PnLStateRepo(db))
    eng = _FakeEngine()
    svc = UserStreamService(
        client=_FakeClient(),  # type: ignore[arg-type]
        engine=eng,  # type: ignore[arg-type]
        pnl=pnl,
        execution=_FakeExecution(),  # type: ignore[arg-type]
        notifier=None,
    )
    asyncio.run(svc._on_message('{"e":"ACCOUNT_UPDATE","E":123,"a":{"B":[],"P":[]}}'))
    assert eng.connected is True
    assert eng.last_event is not None

