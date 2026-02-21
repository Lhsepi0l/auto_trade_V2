from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from v2.exchange.rate_limit import BackoffPolicy, RequestThrottler
from v2.exchange.types import EnvName


class BinanceRESTError(Exception):
    def __init__(self, *, status_code: int, code: int | None, message: str, path: str) -> None:
        super().__init__(f"binance_rest_error status={status_code} code={code} path={path} msg={message}")
        self.status_code = status_code
        self.code = code
        self.path = path
        self.message = message


@dataclass
class TimeOffset:
    offset_ms: int = 0


class AsyncRequestClient(Protocol):
    async def request(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any: ...

    async def aclose(self) -> None: ...


class BinanceRESTClient:
    def __init__(
        self,
        *,
        env: EnvName,
        api_key: str | None,
        api_secret: str | None,
        recv_window_ms: int = 5000,
        time_sync_enabled: bool = True,
        rate_limit_per_sec: float = 5.0,
        backoff_policy: BackoffPolicy | None = None,
        transport: AsyncRequestClient | None = None,
    ) -> None:
        self._env = env
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window_ms = int(recv_window_ms)
        self._time_sync_enabled = bool(time_sync_enabled)
        self._time_offset = TimeOffset(0)
        self._time_synced_once = False
        self._throttler = RequestThrottler(rate_per_sec=rate_limit_per_sec)
        self._backoff = backoff_policy if backoff_policy is not None else BackoffPolicy()

        self._base_url = "https://fapi.binance.com" if env == "prod" else "https://testnet.binancefuture.com"
        self._client: AsyncRequestClient = transport if transport is not None else httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._owns_client = transport is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000) + int(self._time_offset.offset_ms)

    async def sync_server_time(self) -> int:
        payload = await self.public_request("GET", "/fapi/v1/time")
        server = int(payload.get("serverTime") or 0)
        local = int(time.time() * 1000)
        self._time_offset.offset_ms = server - local
        self._time_synced_once = True
        return self._time_offset.offset_ms

    def _signed_query(self, params: dict[str, Any]) -> str:
        if not self._api_secret:
            raise ValueError("missing BINANCE_API_SECRET")
        query = urllib.parse.urlencode(params, doseq=True)
        signature = hmac.new(self._api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, signed: bool = False, api_key_only: bool = False) -> Any:
        if signed and (not self._api_key or not self._api_secret):
            raise ValueError("signed request requires BINANCE_API_KEY and BINANCE_API_SECRET")
        if api_key_only and not self._api_key:
            raise ValueError("api key request requires BINANCE_API_KEY")

        attempt = 1
        while True:
            await self._throttler.acquire()
            query_params = dict(params or {})
            headers: dict[str, str] = {}
            url = f"{self._base_url}{path}"

            if signed:
                headers["X-MBX-APIKEY"] = str(self._api_key)
                query_params["recvWindow"] = self._recv_window_ms
                query_params["timestamp"] = self._timestamp_ms()
                url = f"{url}?{self._signed_query(query_params)}"
                request_params = None
            else:
                if api_key_only:
                    headers["X-MBX-APIKEY"] = str(self._api_key)
                request_params = query_params if query_params else None

            try:
                resp = await self._client.request(method=method, url=url, params=request_params, headers=headers)
            except httpx.RequestError:
                if attempt >= 6:
                    raise
                await asyncio.sleep(self._backoff.compute_delay(attempt=attempt))
                attempt += 1
                continue

            payload: Any
            try:
                payload = resp.json()
            except ValueError:
                payload = {}

            if resp.status_code >= 400:
                code: int | None = None
                if isinstance(payload, dict) and payload.get("code") is not None:
                    raw_code = payload.get("code")
                    try:
                        code = int(raw_code) if raw_code is not None else None
                    except (TypeError, ValueError):
                        code = None
                msg = str(payload.get("msg") or resp.text or "error") if isinstance(payload, dict) else str(resp.text or "error")
                retryable = resp.status_code in {418, 429} or resp.status_code >= 500
                ts_out_of_sync = code == -1021 and signed
                if ts_out_of_sync and self._time_sync_enabled and attempt <= 2:
                    await self.sync_server_time()
                    attempt += 1
                    continue
                if retryable and attempt < 6:
                    await asyncio.sleep(self._backoff.compute_delay(attempt=attempt))
                    attempt += 1
                    continue
                raise BinanceRESTError(status_code=resp.status_code, code=code, message=msg, path=path)

            return payload

    async def public_request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self._request(method, path, params=params, signed=False, api_key_only=False)

    async def signed_request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        if self._time_sync_enabled and not self._time_synced_once:
            await self.sync_server_time()
        return await self._request(method, path, params=params, signed=True, api_key_only=False)

    async def create_listen_key(self) -> str:
        payload = await self._request("POST", "/fapi/v1/listenKey", api_key_only=True)
        listen_key = str(payload.get("listenKey") or "")
        if not listen_key:
            raise ValueError("listenKey missing in response")
        return listen_key

    async def keepalive_listen_key(self, *, listen_key: str) -> None:
        await self._request("PUT", "/fapi/v1/listenKey", params={"listenKey": listen_key}, api_key_only=True)

    async def close_listen_key(self, *, listen_key: str) -> None:
        await self._request("DELETE", "/fapi/v1/listenKey", params={"listenKey": listen_key}, api_key_only=True)

    async def get_open_orders(self) -> list[dict[str, Any]]:
        payload = await self.signed_request("GET", "/fapi/v1/openOrders")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def cancel_all_open_orders(self, *, symbol: str) -> dict[str, Any]:
        payload = await self.signed_request("DELETE", "/fapi/v1/allOpenOrders", params={"symbol": symbol})
        if isinstance(payload, dict):
            return payload
        return {}

    async def place_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
        payload = await self.signed_request("POST", "/fapi/v1/order", params=params)
        if isinstance(payload, dict):
            return payload
        return {}

    async def place_reduce_only_market_order(
        self,
        *,
        symbol: str,
        side: Literal["BUY", "SELL"],
        quantity: float,
        position_side: Literal["BOTH", "LONG", "SHORT"] = "BOTH",
    ) -> dict[str, Any]:
        payload = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": f"{quantity:.8f}",
            "reduceOnly": "true",
            "positionSide": position_side,
        }
        return await self.place_order(params=payload)

    async def get_positions(self) -> list[dict[str, Any]]:
        payload = await self.signed_request("GET", "/fapi/v2/positionRisk")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def get_balances(self) -> list[dict[str, Any]]:
        payload = await self.signed_request("GET", "/fapi/v2/balance")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def place_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
        payload = await self.signed_request("POST", "/fapi/v1/algoOrder", params=params)
        if isinstance(payload, dict):
            return payload
        return {}

    async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
        payload = await self.signed_request("DELETE", "/fapi/v1/algoOrder", params=params)
        if isinstance(payload, dict):
            return payload
        return {}

    async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] | None = None
        if symbol:
            params = {"symbol": symbol}
        payload = await self.signed_request("GET", "/fapi/v1/openAlgoOrders", params=params)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []
