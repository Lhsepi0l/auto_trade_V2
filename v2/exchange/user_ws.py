from __future__ import annotations

import asyncio
import heapq
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import websockets

from v2.exchange.rate_limit import BackoffPolicy
from v2.exchange.types import EnvName, ResyncSnapshot


class UserStreamREST(Protocol):
    async def create_listen_key(self) -> str: ...

    async def keepalive_listen_key(self, *, listen_key: str) -> None: ...

    async def close_listen_key(self, *, listen_key: str) -> None: ...

    async def get_open_orders(self) -> list[dict[str, Any]]: ...

    async def get_positions(self) -> list[dict[str, Any]]: ...

    async def get_balances(self) -> list[dict[str, Any]]: ...


def _default_user_ws_base(env: EnvName) -> str:
    return "wss://fstream.binance.com/ws" if env == "prod" else "wss://stream.binancefuture.com/ws"


async def _maybe_await(value: Any) -> None:
    if asyncio.iscoroutine(value):
        await value


@dataclass
class UserStreamManager:
    env: EnvName
    rest: UserStreamREST
    keepalive_interval_sec: int = 30 * 60
    reconnect_min_sec: float = 1.0
    reconnect_max_sec: float = 30.0
    connection_ttl_sec: int = 23 * 60 * 60
    reorder_window_ms: int = 300
    ws_base_url: str | None = None
    ws_connect: Callable[..., Any] | None = None
    backoff_policy: BackoffPolicy = field(default_factory=BackoffPolicy)

    def __post_init__(self) -> None:
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._active_ws: Any | None = None
        self._listen_key: str | None = None
        self._ws_base = self.ws_base_url or _default_user_ws_base(self.env)
        self._ws_connect = self.ws_connect or websockets.connect
        self._heap: list[tuple[int, int, dict[str, Any]]] = []
        self._event_seq = 0
        self._max_event_ms = 0
        self._on_event: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
        self._on_resync: Callable[[ResyncSnapshot], Awaitable[None] | None] | None = None
        self._on_disconnect: Callable[[str], Awaitable[None] | None] | None = None
        self._on_private_ok: Callable[[str], Awaitable[None] | None] | None = None

    def start(
        self,
        *,
        on_event: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        on_resync: Callable[[ResyncSnapshot], Awaitable[None] | None] | None = None,
        on_disconnect: Callable[[str], Awaitable[None] | None] | None = None,
        on_private_ok: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> None:
        if self._task is not None and not self._task.done():
            return
        self._on_event = on_event
        self._on_resync = on_resync
        self._on_disconnect = on_disconnect
        self._on_private_ok = on_private_ok
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever(), name="v2_user_stream")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None

        ws = self._active_ws
        self._active_ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

        if self._listen_key:
            try:
                await self.rest.close_listen_key(listen_key=self._listen_key)
            except Exception:
                pass
            self._listen_key = None

        maybe_aclose = getattr(self.rest, "aclose", None)
        if callable(maybe_aclose):
            try:
                out = maybe_aclose()
                if asyncio.iscoroutine(out):
                    await out
            except Exception:
                pass

    async def _resync(self) -> None:
        snapshot = ResyncSnapshot(
            open_orders=await self.rest.get_open_orders(),
            positions=await self.rest.get_positions(),
            balances=await self.rest.get_balances(),
        )
        if self._on_resync is not None:
            await _maybe_await(self._on_resync(snapshot))
        if self._on_private_ok is not None:
            await _maybe_await(self._on_private_ok("resync"))

    async def _keepalive_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(max(self.keepalive_interval_sec, 1))
            listen_key = self._listen_key
            if listen_key is None:
                return
            try:
                await self.rest.keepalive_listen_key(listen_key=listen_key)
                if self._on_private_ok is not None:
                    await _maybe_await(self._on_private_ok("keepalive"))
            except Exception:
                self._listen_key = None
                ws = self._active_ws
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                return

    def _buffer_event(self, event: dict[str, Any]) -> None:
        event_ms = int(event.get("E") or int(time.time() * 1000))
        self._event_seq += 1
        self._max_event_ms = max(self._max_event_ms, event_ms)
        heapq.heappush(self._heap, (event_ms, self._event_seq, event))

    async def _drain_events(self, *, force: bool = False) -> None:
        while self._heap:
            oldest_ms = self._heap[0][0]
            ready = force
            if not ready and self._max_event_ms - oldest_ms >= self.reorder_window_ms:
                ready = True
            if not ready and len(self._heap) >= 256:
                ready = True
            if not ready:
                break
            _, _, event = heapq.heappop(self._heap)
            if self._on_event is not None:
                await _maybe_await(self._on_event(event))

    async def _run_forever(self) -> None:
        attempt = 1
        while not self._stop.is_set():
            disconnect_reason = "user_stream_disconnected"
            try:
                if self._listen_key is None:
                    self._listen_key = await self.rest.create_listen_key()
                listen_key = self._listen_key
                ws_url = f"{self._ws_base}/{listen_key}"

                self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="v2_user_stream_keepalive")
                connected_at = time.monotonic()
                async with self._ws_connect(ws_url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                    self._active_ws = ws
                    await self._resync()
                    attempt = 1
                    while not self._stop.is_set():
                        if (time.monotonic() - connected_at) >= self.connection_ttl_sec:
                            self._listen_key = None
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except TimeoutError:
                            await self._drain_events(force=False)
                            continue
                        except asyncio.TimeoutError:
                            await self._drain_events(force=False)
                            continue

                        try:
                            msg = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(msg, dict):
                            continue

                        if str(msg.get("e") or "") == "listenKeyExpired":
                            disconnect_reason = "listen_key_expired"
                            self._listen_key = None
                            break

                        self._buffer_event(msg)
                        await self._drain_events(force=False)

                    await self._drain_events(force=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                disconnect_reason = f"user_stream_error:{type(exc).__name__}"
            finally:
                try:
                    await self._drain_events(force=True)
                except Exception:
                    pass
                self._active_ws = None
                if self._keepalive_task is not None:
                    self._keepalive_task.cancel()
                    try:
                        await self._keepalive_task
                    except asyncio.CancelledError:
                        pass
                    self._keepalive_task = None
                if not self._stop.is_set() and self._on_disconnect is not None:
                    await _maybe_await(self._on_disconnect(disconnect_reason))

            if self._stop.is_set():
                break
            await asyncio.sleep(self.backoff_policy.compute_delay(attempt=attempt))
            attempt += 1


class ShadowUserStreamREST:
    async def create_listen_key(self) -> str:
        return "shadow-listen-key"

    async def keepalive_listen_key(self, *, listen_key: str) -> None:
        _ = listen_key

    async def close_listen_key(self, *, listen_key: str) -> None:
        _ = listen_key

    async def get_open_orders(self) -> list[dict[str, Any]]:
        return []

    async def get_positions(self) -> list[dict[str, Any]]:
        return []

    async def get_balances(self) -> list[dict[str, Any]]:
        return []


class ShadowUserStreamManager:
    def start(
        self,
        *,
        on_event: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        on_resync: Callable[[ResyncSnapshot], Awaitable[None] | None] | None = None,
        on_disconnect: Callable[[str], Awaitable[None] | None] | None = None,
        on_private_ok: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> None:
        _ = on_event
        _ = on_resync
        _ = on_disconnect
        _ = on_private_ok

    async def stop(self) -> None:
        return
