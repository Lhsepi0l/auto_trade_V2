from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import websockets

from v2.exchange.types import EnvName


def _default_market_ws_base(env: EnvName) -> str:
    return "wss://fstream.binance.com/stream" if env == "prod" else "wss://stream.binancefuture.com/stream"


class BinanceMarketWS:
    def __init__(
        self,
        *,
        env: EnvName,
        ws_base_url: str | None = None,
        ws_connect: Callable[..., Any] | None = None,
    ) -> None:
        self._env = env
        self._base_url = ws_base_url or _default_market_ws_base(env)
        self._ws_connect = ws_connect or websockets.connect

    async def stream_klines(
        self,
        *,
        symbols: list[str],
        intervals: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        active_intervals = intervals if intervals is not None else ["15m", "1h", "4h"]
        streams: list[str] = []
        for symbol in symbols:
            sym = str(symbol).lower()
            for interval in active_intervals:
                streams.append(f"{sym}@kline_{interval}")
        stream_query = "/".join(streams)
        url = f"{self._base_url}?streams={stream_query}"

        async with self._ws_connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                payload = data.get("data")
                if isinstance(payload, dict) and payload.get("e") == "kline":
                    yield payload
