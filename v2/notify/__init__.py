from __future__ import annotations

from .factory import build_notifier_from_config
from .models import NotificationMessage, WebPushDispatchResult
from .notifier import Notifier
from .runtime_events import RuntimeNotificationContext

__all__ = [
    "NotificationMessage",
    "Notifier",
    "RuntimeNotificationContext",
    "WebPushDispatchResult",
    "WebPushService",
    "build_notifier_from_config",
]


def __getattr__(name: str):
    if name == "WebPushService":
        from .webpush import WebPushService

        return WebPushService
    raise AttributeError(name)
