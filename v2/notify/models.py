from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NotificationMessage:
    body: str
    title: str | None = None
    tags: tuple[str, ...] = ()
    priority: str | int | None = None
    event_type: str | None = None
    dedupe_key: str | None = None
    suppress_window_sec: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_text(self) -> str:
        body = str(self.body or "").strip()
        title = str(self.title or "").strip()
        if title and body:
            return f"**{title}**\n{body}"
        return body or title


@dataclass(frozen=True)
class WebPushDispatchResult:
    sent: bool
    error: str | None = None
    status: str = "sent"
