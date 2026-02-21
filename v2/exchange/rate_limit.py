from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass


@dataclass
class BackoffPolicy:
    base_seconds: float = 0.5
    cap_seconds: float = 10.0
    jitter_ratio: float = 0.2

    def compute_delay(self, *, attempt: int) -> float:
        exp = min(self.base_seconds * (2**max(attempt - 1, 0)), self.cap_seconds)
        jitter = exp * self.jitter_ratio
        return max(0.0, exp + random.uniform(-jitter, jitter))


class RequestThrottler:
    def __init__(self, *, rate_per_sec: float) -> None:
        self._interval_sec = 1.0 / max(rate_per_sec, 0.001)
        self._lock = asyncio.Lock()
        self._next_allowed_ts = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next_allowed_ts:
                await asyncio.sleep(self._next_allowed_ts - now)
            now2 = time.monotonic()
            self._next_allowed_ts = now2 + self._interval_sec
