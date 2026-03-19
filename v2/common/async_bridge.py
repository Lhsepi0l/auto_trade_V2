from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import queue
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

_bridge_lock = threading.Lock()
_bridge_ready = threading.Event()
_bridge_job_active = threading.Event()
_bridge_job_context: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "v2_async_bridge_job_context",
    default=False,
)
_bridge_loop: asyncio.AbstractEventLoop | None = None
_bridge_thread: threading.Thread | None = None
_bridge_jobs: queue.Queue[tuple[Callable[[], Coroutine[Any, Any, Any]] | None, concurrent.futures.Future[Any] | None]] = queue.Queue()


def _loop_worker() -> None:
    global _bridge_loop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with _bridge_lock:
        _bridge_loop = loop
        _bridge_ready.set()

    try:
        while True:
            thunk, result_future = _bridge_jobs.get()
            if thunk is None:
                break
            if result_future is None or result_future.cancelled():
                continue
            _bridge_job_active.set()
            token = _bridge_job_context.set(True)
            try:
                result = loop.run_until_complete(thunk())
            except Exception as exc:  # noqa: BLE001
                if not result_future.cancelled():
                    result_future.set_exception(exc)
            else:
                if not result_future.cancelled():
                    result_future.set_result(result)
            finally:
                _bridge_job_context.reset(token)
                _bridge_job_active.clear()
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


def _run_in_helper_thread(
    thunk: Callable[[], Coroutine[Any, Any, Any]], *, timeout_sec: float | None
) -> Any:
    future: concurrent.futures.Future[Any] = concurrent.futures.Future()

    def _worker() -> None:
        try:
            result = asyncio.run(thunk())
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)
        else:
            future.set_result(result)

    thread = threading.Thread(target=_worker, name="v2-async-bridge-reentrant", daemon=True)
    thread.start()
    return future.result(timeout=timeout_sec)


def run_async_blocking(
    thunk: Callable[[], Coroutine[Any, Any, Any]], *, timeout_sec: float | None = None
) -> Any:
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if (
        running_loop is not None
        and _bridge_thread is not None
        and threading.get_ident() == _bridge_thread.ident
    ):
        return _run_in_helper_thread(thunk, timeout_sec=timeout_sec)

    if (
        _bridge_job_context.get()
        and _bridge_thread is not None
        and threading.get_ident() != _bridge_thread.ident
    ):
        return _run_in_helper_thread(thunk, timeout_sec=timeout_sec)

    _ = _ensure_bridge_loop()
    future: concurrent.futures.Future[Any] = concurrent.futures.Future()
    _bridge_jobs.put((thunk, future))
    try:
        return future.result(timeout=timeout_sec)
    except FutureTimeoutError:
        future.cancel()
        raise
