from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class TimeOffset:
    offset_ms: int
    measured_at_ms: int


class TimeSync:
    """Utility for computing local<->server time offset.

    Offset is computed as: server_ms - local_ms.
    """

    def __init__(self) -> None:
        self._offset = TimeOffset(offset_ms=0, measured_at_ms=0)

    @property
    def offset_ms(self) -> int:
        return self._offset.offset_ms

    def apply(self, local_ms: int) -> int:
        return local_ms + self._offset.offset_ms

    def measure(self, *, server_time_ms: int) -> TimeOffset:
        now_ms = int(time.time() * 1000)
        self._offset = TimeOffset(offset_ms=server_time_ms - now_ms, measured_at_ms=now_ms)
        return self._offset
