from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from v2.common.retry import retry_async
from v2.discord_bot.services.contracts import JSONPayload


@dataclass(frozen=True)
class APIError(Exception):
    status_code: int
    message: str
    details: str | None = None

    def __str__(self) -> str:
        if self.details:
            return f"{self.status_code}: {self.message} ({self.details})"
        return f"{self.status_code}: {self.message}"


class TraderAPIClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_sec: float = 8.0,
        retry_count: int = 3,
        retry_backoff: float = 0.25,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._retry_count = retry_count
        self._retry_backoff = retry_backoff

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_sec),
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _is_retryable_status(self, status_code: int) -> bool:
        return status_code in (408, 425, 429, 500, 502, 503, 504)

    @staticmethod
    def _as_payload(value: object) -> JSONPayload:
        return value if isinstance(value, dict) else {}

    async def _request_json(self, method: str, path: str, *, json_body: JSONPayload | None = None) -> JSONPayload:
        async def _do_once() -> JSONPayload:
            try:
                resp = await self._client.request(method, path, json=json_body)
            except httpx.RequestError as e:
                raise RuntimeError(f"network_error: {type(e).__name__}") from e

            if resp.status_code >= 400:
                # Try to extract FastAPI style error body.
                msg = resp.reason_phrase or "error"
                details: str | None = None
                try:
                    payload = resp.json()
                    if isinstance(payload, dict) and "detail" in payload:
                        details = json.dumps(payload["detail"], ensure_ascii=True)
                    else:
                        details = json.dumps(payload, ensure_ascii=True)[:500]
                except (TypeError, ValueError):
                    details = (resp.text or "")[:500] or None

                if self._is_retryable_status(resp.status_code):
                    raise RuntimeError(f"retryable_http_error: {resp.status_code}")
                raise APIError(status_code=resp.status_code, message=msg, details=details)

            try:
                return self._as_payload(resp.json())
            except ValueError:
                return {}

        return await retry_async(_do_once, attempts=self._retry_count, base_delay_sec=self._retry_backoff)

    async def get_status(self) -> JSONPayload:
        return await self._request_json("GET", "/status")

    async def get_risk(self) -> JSONPayload:
        return await self._request_json("GET", "/risk")

    async def start(self) -> JSONPayload:
        return await self._request_json("POST", "/start")

    async def stop(self) -> JSONPayload:
        return await self._request_json("POST", "/stop")

    async def panic(self) -> JSONPayload:
        return await self._request_json("POST", "/panic")

    async def clear_cooldown(self) -> JSONPayload:
        return await self._request_json("POST", "/cooldown/clear")

    async def set_value(self, key: str, value: str) -> JSONPayload:
        return await self._request_json("POST", "/set", json_body={"key": key, "value": value})

    async def set_config(self, config: JSONPayload) -> JSONPayload:
        out: JSONPayload = {}
        for k, v in config.items():
            out[k] = await self.set_value(str(k), str(v))
        return out

    async def set_symbol_leverage(self, symbol: str, leverage: float) -> JSONPayload:
        return await self._request_json(
            "POST",
            "/symbol-leverage",
            json_body={"symbol": symbol, "leverage": float(leverage)},
        )

    async def get_scheduler(self) -> JSONPayload:
        return await self._request_json("GET", "/scheduler")

    async def set_scheduler_interval(self, tick_sec: float) -> JSONPayload:
        return await self._request_json("POST", "/scheduler/interval", json_body={"tick_sec": tick_sec})

    async def tick_scheduler_now(self) -> JSONPayload:
        return await self._request_json("POST", "/scheduler/tick")

    async def send_daily_report(self) -> JSONPayload:
        return await self._request_json("POST", "/report")

    async def preset(self, name: str) -> JSONPayload:
        return await self._request_json("POST", "/preset", json_body={"name": name})

    async def close_position(self, symbol: str) -> JSONPayload:
        return await self._request_json("POST", "/trade/close", json_body={"symbol": symbol})

    async def close_all(self) -> JSONPayload:
        return await self._request_json("POST", "/trade/close_all")
