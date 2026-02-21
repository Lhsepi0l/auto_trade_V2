from __future__ import annotations

from dataclasses import dataclass

from v2.core.event_bus import Event, EventBus


@dataclass
class Scheduler:
    tick_seconds: int
    event_bus: EventBus

    def run_once(self) -> None:
        self.event_bus.publish(Event(topic="scheduler.tick", payload={"tick_seconds": self.tick_seconds}))
