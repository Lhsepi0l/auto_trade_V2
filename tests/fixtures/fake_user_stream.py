from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import websockets


@dataclass
class FakeUserStreamServer:
    host: str = "127.0.0.1"
    port: int = 0
    _server: Optional[Any] = None
    _clients: List[Any] = field(default_factory=list)

    async def __aenter__(self) -> "FakeUserStreamServer":
        async def _handler(ws):
            self._clients.append(ws)
            try:
                await ws.wait_closed()
            finally:
                try:
                    self._clients.remove(ws)
                except ValueError:
                    pass

        self._server = await websockets.serve(_handler, self.host, self.port)
        sock = self._server.sockets[0]
        self.port = int(sock.getsockname()[1])
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    async def emit(self, payload: Dict[str, Any]) -> None:
        msg = json.dumps(payload)
        alive = []
        for ws in list(self._clients):
            if getattr(ws, "closed", False):
                continue
            try:
                await ws.send(msg)
                alive.append(ws)
            except Exception:
                continue
        self._clients = alive

    async def emit_order_fill(
        self,
        *,
        symbol: str,
        side: str = "BUY",
        qty: float = 0.01,
        price: float = 100.0,
        realized_pnl: float = 0.0,
        reduce_only: bool = False,
    ) -> None:
        await self.emit(
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": 1700000000000,
                "o": {
                    "x": "TRADE",
                    "X": "FILLED",
                    "s": symbol,
                    "S": side,
                    "l": str(qty),
                    "L": str(price),
                    "rp": str(realized_pnl),
                    "R": bool(reduce_only),
                    "T": 1700000000000,
                },
            }
        )

    async def emit_account_update(self, *, positions_count: int = 1, balances_count: int = 1) -> None:
        await self.emit(
            {
                "e": "ACCOUNT_UPDATE",
                "E": 1700000000001,
                "a": {
                    "B": [{} for _ in range(balances_count)],
                    "P": [{} for _ in range(positions_count)],
                },
            }
        )

    async def emit_listen_key_expired(self) -> None:
        await self.emit({"e": "listenKeyExpired", "E": 1700000000002})
