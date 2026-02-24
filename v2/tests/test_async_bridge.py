from __future__ import annotations

import asyncio
from typing import Any

from v2.common.async_bridge import run_async_blocking
from v2.exchange.rest_client import BinanceRESTClient


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = "{}"

    def json(self) -> dict[str, Any]:
        return dict(self._payload)


class _LoopBoundTransport:
    def __init__(self) -> None:
        self.loop_id: int | None = None
        self.calls = 0

    async def request(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        _ = method
        _ = url
        _ = headers

        current_loop_id = id(asyncio.get_running_loop())
        if self.loop_id is None:
            self.loop_id = current_loop_id
        elif self.loop_id != current_loop_id:
            raise RuntimeError("transport_used_on_different_event_loop")

        self.calls += 1
        symbol = None if params is None else params.get("symbol")
        return _FakeResponse({"ok": True, "symbol": symbol})

    async def aclose(self) -> None:
        return None


def test_run_async_blocking_reuses_single_event_loop() -> None:
    async def _loop_id() -> int:
        return id(asyncio.get_running_loop())

    ids = {run_async_blocking(_loop_id) for _ in range(4)}
    assert len(ids) == 1


def test_rest_client_is_safe_across_repeated_blocking_calls() -> None:
    transport = _LoopBoundTransport()
    client = BinanceRESTClient(
        env="prod",
        api_key=None,
        api_secret=None,
        transport=transport,
    )

    payload1 = run_async_blocking(
        lambda: client.public_request(
            "GET",
            "/fapi/v1/premiumIndex",
            params={"symbol": "BTCUSDT"},
        )
    )
    payload2 = run_async_blocking(
        lambda: client.public_request(
            "GET",
            "/fapi/v1/premiumIndex",
            params={"symbol": "ETHUSDT"},
        )
    )

    assert payload1 == {"ok": True, "symbol": "BTCUSDT"}
    assert payload2 == {"ok": True, "symbol": "ETHUSDT"}
    assert transport.calls == 2
