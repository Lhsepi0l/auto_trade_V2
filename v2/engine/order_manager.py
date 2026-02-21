from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from v2.core.event_bus import Event, EventBus


@dataclass
class OrderManager:
    event_bus: EventBus

    def submit(self, order: dict[str, Any]) -> None:
        self.event_bus.publish(Event(topic="order.submitted", payload=order))
