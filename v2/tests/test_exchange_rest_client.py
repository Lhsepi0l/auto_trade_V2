from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from v2.exchange.rate_limit import BackoffPolicy
from v2.exchange.rest_client import BinanceRESTClient


@dataclass
class _Resp:
    status_code: int
    payload: Any
    text: str = ""

    def json(self) -> Any:
        return self.payload


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self._count = 0

    async def request(self, *, method: str, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> _Resp:  # noqa: ARG002
        self.calls.append((method, url, params))
        self._count += 1
        if url.endswith("/fapi/v1/time"):
            return _Resp(200, {"serverTime": 1700000000000})
        if "/fapi/v1/openAlgoOrders" in url:
            return _Resp(200, [{"clientAlgoId": "tp-1"}])
        if "/fapi/v1/allOpenOrders" in url and method == "DELETE":
            return _Resp(200, {"code": 200, "msg": "done"})
        if "/fapi/v1/order" in url and method == "POST":
            return _Resp(200, {"orderId": 777, "status": "NEW"})
        if "/fapi/v1/algoOrder" in url and method == "POST":
            return _Resp(200, {"algoId": 123, "clientAlgoId": "tp-1"})
        if "/fapi/v1/algoOrder" in url and method == "DELETE":
            return _Resp(200, {"code": 200, "msg": "success"})
        if "retry-target" in url:
            if self._count < 3:
                return _Resp(429, {"code": -1003, "msg": "too many requests"})
            return _Resp(200, {"ok": True})
        if "/fapi/v2/balance" in url:
            return _Resp(200, [{"asset": "USDT", "balance": "100.0"}])
        return _Resp(200, {"ok": True})

    async def aclose(self) -> None:
        return


@pytest.mark.asyncio
async def test_signed_request_builds_signature_and_timestamp() -> None:
    transport = _Transport()
    client = BinanceRESTClient(
        env="testnet",
        api_key="k",
        api_secret="s",
        recv_window_ms=5000,
        transport=transport,  # type: ignore[arg-type]
    )

    payload = await client.signed_request("GET", "/fapi/v2/balance")
    assert isinstance(payload, list)
    assert len(transport.calls) >= 2
    _, signed_url, signed_params = transport.calls[-1]
    assert signed_params is None
    assert "timestamp=" in signed_url
    assert "recvWindow=5000" in signed_url
    assert "signature=" in signed_url


@pytest.mark.asyncio
async def test_retry_on_429_with_backoff() -> None:
    transport = _Transport()
    client = BinanceRESTClient(
        env="testnet",
        api_key="k",
        api_secret="s",
        backoff_policy=BackoffPolicy(base_seconds=0.01, cap_seconds=0.02, jitter_ratio=0.0),
        transport=transport,  # type: ignore[arg-type]
    )

    payload = await client.public_request("GET", "/retry-target")
    assert payload == {"ok": True}


@pytest.mark.asyncio
async def test_algo_order_endpoints() -> None:
    transport = _Transport()
    client = BinanceRESTClient(
        env="testnet",
        api_key="k",
        api_secret="s",
        transport=transport,  # type: ignore[arg-type]
    )

    place = await client.place_algo_order(params={"symbol": "BTCUSDT", "algoType": "CONDITIONAL"})
    cancel = await client.cancel_algo_order(params={"symbol": "BTCUSDT", "clientAlgoId": "tp-1"})
    opens = await client.get_open_algo_orders(symbol="BTCUSDT")

    assert place["algoId"] == 123
    assert cancel["msg"] == "success"
    assert opens[0]["clientAlgoId"] == "tp-1"


@pytest.mark.asyncio
async def test_regular_cancel_all_and_reduce_only_market_order() -> None:
    transport = _Transport()
    client = BinanceRESTClient(
        env="testnet",
        api_key="k",
        api_secret="s",
        transport=transport,
    )

    canceled = await client.cancel_all_open_orders(symbol="BTCUSDT")
    closed = await client.place_reduce_only_market_order(
        symbol="BTCUSDT",
        side="SELL",
        quantity=0.01,
    )

    assert canceled["msg"] == "done"
    assert closed["orderId"] == 777
