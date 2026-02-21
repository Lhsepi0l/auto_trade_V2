from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay_sec: float = 0.25,
) -> T:
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if i == attempts - 1:
                break
            await asyncio.sleep(base_delay_sec * (2**i))
    assert last_exc is not None
    raise last_exc


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay_sec: float = 0.25,
) -> T:
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if i == attempts - 1:
                break
            time.sleep(base_delay_sec * (2**i))
    assert last_exc is not None
    raise last_exc
