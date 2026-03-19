from __future__ import annotations

import httpx
import pytest

from v2.discord_bot.services.api_client import TraderAPIClient


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_scheduler_now_does_not_retry_on_timeout() -> None:
    calls = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read timeout", request=request)

    client = TraderAPIClient(
        base_url="http://localhost:8101", timeout_sec=20.0, retry_count=3, retry_backoff=0.0
    )
    await client._client.aclose()  # type: ignore[attr-defined]
    client._client = httpx.AsyncClient(  # type: ignore[attr-defined]
        base_url="http://localhost:8101",
        transport=httpx.MockTransport(_handler),
        headers={"Accept": "application/json"},
    )

    with pytest.raises(RuntimeError, match="network_error: ReadTimeout"):
        await client.tick_scheduler_now()

    assert calls == 1
    await client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_scheduler_now_timeout_respects_configured_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float | int | None] = {}
    client = TraderAPIClient(base_url="http://localhost:8101", timeout_sec=50.0)

    async def _fake_request_json(
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
        timeout_sec: float | None = None,
        retry_count: int | None = None,
    ) -> dict[str, object]:
        _ = method
        _ = path
        _ = json_body
        captured["timeout_sec"] = timeout_sec
        captured["retry_count"] = retry_count
        return {}

    monkeypatch.setattr(client, "_request_json", _fake_request_json)
    await client.tick_scheduler_now()
    assert captured["timeout_sec"] == 300.0
    assert captured["retry_count"] == 1
    await client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_scheduler_now_timeout_has_minimum_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float | int | None] = {}
    client = TraderAPIClient(base_url="http://localhost:8101", timeout_sec=5.0)

    async def _fake_request_json(
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
        timeout_sec: float | None = None,
        retry_count: int | None = None,
    ) -> dict[str, object]:
        _ = method
        _ = path
        _ = json_body
        captured["timeout_sec"] = timeout_sec
        captured["retry_count"] = retry_count
        return {}

    monkeypatch.setattr(client, "_request_json", _fake_request_json)
    await client.tick_scheduler_now()
    assert captured["timeout_sec"] == 300.0
    assert captured["retry_count"] == 1
    await client.aclose()
