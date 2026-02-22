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
