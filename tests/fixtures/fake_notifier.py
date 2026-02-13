from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping


@dataclass
class FakeNotifier:
    events: List[Dict[str, Any]] = field(default_factory=list)
    snapshots: List[Dict[str, Any]] = field(default_factory=list)

    async def send_event(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))

    async def send_status_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        self.snapshots.append(dict(snapshot))

    # compatibility for sync call-sites
    def notify(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))

    def notify_status(self, snapshot: Mapping[str, Any]) -> None:
        self.snapshots.append(dict(snapshot))

