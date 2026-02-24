from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

_bridge_lock = threading.Lock()
_bridge_ready = threading.Event()
_bridge_loop: asyncio.AbstractEventLoop | None = None
_bridge_thread: threading.Thread | None = None


def _loop_worker() -> None:
    global _bridge_loop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with _bridge_lock:
        _bridge_loop = loop
        _bridge_ready.set()

    try:
        loop.run_forever()
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


def _ensure_bridge_loop() -> asyncio.AbstractEventLoop:
    global _bridge_thread

    with _bridge_lock:
        if (
            _bridge_loop is not None
            and not _bridge_loop.is_closed()
            and _bridge_thread is not None
            and _bridge_thread.is_alive()
        ):
            return _bridge_loop

        _bridge_ready.clear()
        _bridge_thread = threading.Thread(target=_loop_worker, name="v2-async-bridge", daemon=True)
        _bridge_thread.start()

    if not _bridge_ready.wait(timeout=5.0):
        raise RuntimeError("async bridge loop bootstrap timeout")

    with _bridge_lock:
        if _bridge_loop is None or _bridge_loop.is_closed():
            raise RuntimeError("async bridge loop unavailable")
        return _bridge_loop


def run_async_blocking(
    thunk: Callable[[], Coroutine[Any, Any, Any]], *, timeout_sec: float | None = None
) -> Any:
    loop = _ensure_bridge_loop()
    future = asyncio.run_coroutine_threadsafe(thunk(), loop)
    try:
        return future.result(timeout=timeout_sec)
    except FutureTimeoutError:
        future.cancel()
        raise
