from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from v2.exchange.user_ws import UserStreamManager


class _FakeREST:
    def __init__(self) -> None:
        self.listen_idx = 0
        self.keepalive_calls = 0
        self.close_calls = 0
        self.resync_calls = 0

    async def create_listen_key(self) -> str:
        self.listen_idx += 1
        return f"lk-{self.listen_idx}"

    async def keepalive_listen_key(self, *, listen_key: str) -> None:
        _ = listen_key
        self.keepalive_calls += 1

    async def close_listen_key(self, *, listen_key: str) -> None:
        _ = listen_key
        self.close_calls += 1

    async def get_open_orders(self) -> list[dict[str, Any]]:
        self.resync_calls += 1
        return [{"id": self.resync_calls}]

    async def get_positions(self) -> list[dict[str, Any]]:
        return [{"symbol": "BTCUSDT", "positionAmt": "0"}]

    async def get_balances(self) -> list[dict[str, Any]]:
        return [{"asset": "USDT", "balance": "100"}]


@dataclass
class _FakeWS:
    messages: list[dict[str, Any]]
    delay_sec: float = 0.4
    idx: int = 0
    closed: bool = False

    async def recv(self) -> str:
        await asyncio.sleep(self.delay_sec)
        if self.idx >= len(self.messages):
            raise RuntimeError("socket_closed")
        msg = self.messages[self.idx]
        self.idx += 1
        return json.dumps(msg)

    async def close(self) -> None:
        self.closed = True


class _FakeWSContext:
    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws

    async def __aenter__(self) -> _FakeWS:
        return self._ws

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        await self._ws.close()


class _ConnectFactory:
    def __init__(self, streams: list[list[dict[str, Any]]], *, delay_sec: float = 0.4) -> None:
        self._streams = streams
        self._delay_sec = delay_sec
        self.calls = 0

    def __call__(self, *args, **kwargs) -> _FakeWSContext:  # type: ignore[no-untyped-def]
        _ = args
        _ = kwargs
        idx = min(self.calls, len(self._streams) - 1)
        self.calls += 1
        return _FakeWSContext(_FakeWS(messages=list(self._streams[idx]), delay_sec=self._delay_sec))


async def _wait_until(pred, timeout: float = 3.0) -> None:  # type: ignore[no-untyped-def]
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timeout")


@pytest.mark.asyncio
async def test_user_stream_reorders_events_and_reconnects_with_resync() -> None:
    rest = _FakeREST()
    connect = _ConnectFactory(
        streams=[
            [
                {"e": "ORDER_TRADE_UPDATE", "E": 200, "o": {"s": "BTCUSDT"}},
                {"e": "ORDER_TRADE_UPDATE", "E": 100, "o": {"s": "BTCUSDT"}},
                {"e": "listenKeyExpired", "E": 300},
            ],
            [
                {"e": "ORDER_TRADE_UPDATE", "E": 400, "o": {"s": "BTCUSDT"}},
            ],
        ]
    )

    seen: list[int] = []
    resync_count = 0

    async def _on_event(event: dict[str, Any]) -> None:
        seen.append(int(event.get("E") or 0))

    async def _on_resync(snapshot) -> None:  # type: ignore[no-untyped-def]
        nonlocal resync_count
        _ = snapshot
        resync_count += 1

    svc = UserStreamManager(
        env="testnet",
        rest=rest,
        ws_connect=connect,
        keepalive_interval_sec=1,
        reconnect_min_sec=0.01,
        reconnect_max_sec=0.02,
        connection_ttl_sec=60,
        reorder_window_ms=250,
    )
    svc.start(on_event=_on_event, on_resync=_on_resync)

    await _wait_until(lambda: resync_count >= 2)
    await _wait_until(lambda: len(seen) >= 3)
    await _wait_until(lambda: rest.keepalive_calls >= 1)
    await svc.stop()

    assert seen[:2] == [100, 200]
    assert 400 in seen
    assert rest.listen_idx >= 2
    assert rest.resync_calls >= 2
