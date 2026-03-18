from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from starlette.testclient import TestClient as StarletteTestClient

from v2.common.async_bridge import run_async_blocking


def _compat_start_control_app(client: StarletteTestClient) -> bool:
    controller = getattr(getattr(client.app, "state", None), "controller", None)
    if controller is None:
        return False
    if controller.cfg.mode != "live" or controller.user_stream_manager is None:
        return True
    if controller._user_stream_started:
        return True

    controller._user_stream_started = True
    controller._user_stream_started_at = datetime.now(timezone.utc).isoformat()
    controller.user_stream_manager.start(
        on_event=controller._handle_user_stream_event,
        on_resync=controller._handle_user_stream_resync,
        on_disconnect=controller._handle_user_stream_disconnect,
        on_private_ok=controller._handle_user_stream_private_ok,
    )
    controller._maybe_probe_market_data()
    controller._update_stale_transitions()
    return True


def _compat_stop_control_app(client: StarletteTestClient) -> bool:
    controller = getattr(getattr(client.app, "state", None), "controller", None)
    if controller is None:
        return False
    if controller.user_stream_manager is not None and controller._user_stream_started:
        run_async_blocking(lambda: controller.user_stream_manager.stop(), timeout_sec=30.0)
        controller._user_stream_started = False
    return True


def _compat_ensure_started(client: StarletteTestClient) -> None:
    if getattr(client, "_compat_started", False):
        return

    if not _compat_start_control_app(client):
        client._compat_lifespan_cm = None  # type: ignore[attr-defined]
    client._compat_started = True  # type: ignore[attr-defined]


def _compat_close(client: StarletteTestClient) -> None:
    if not getattr(client, "_compat_started", False):
        return

    if not _compat_stop_control_app(client):
        lifespan_cm = getattr(client, "_compat_lifespan_cm", None)
        if lifespan_cm is not None:
            async def _shutdown():
                await lifespan_cm.__aexit__(None, None, None)

            run_async_blocking(_shutdown, timeout_sec=30.0)
    client._compat_lifespan_cm = None  # type: ignore[attr-defined]
    client._compat_started = False  # type: ignore[attr-defined]


def _compat_enter(self: StarletteTestClient) -> StarletteTestClient:
    _compat_ensure_started(self)
    return self


def _compat_exit(
    self: StarletteTestClient,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: Any,
) -> None:
    _ = exc_type
    _ = exc
    _ = tb
    _compat_close(self)


def _compat_request(self: StarletteTestClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    _compat_ensure_started(self)
    follow_redirects = kwargs.pop("follow_redirects", kwargs.pop("allow_redirects", None))

    async def _send() -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=str(self.base_url),
            headers=self.headers,
            follow_redirects=(
                bool(follow_redirects)
                if follow_redirects is not None
                else bool(getattr(self, "follow_redirects", False))
            ),
        ) as client:
            return await client.request(method, url, **kwargs)

    return run_async_blocking(_send, timeout_sec=30.0)


StarletteTestClient.__enter__ = _compat_enter  # type: ignore[assignment]
StarletteTestClient.__exit__ = _compat_exit  # type: ignore[assignment]
StarletteTestClient.close = _compat_close  # type: ignore[assignment]
StarletteTestClient.request = _compat_request  # type: ignore[assignment]
