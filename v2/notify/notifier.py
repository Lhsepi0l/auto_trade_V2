from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import NotificationMessage


@dataclass
class Notifier:
    enabled: bool = False
    provider: str = "none"
    _delivery_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _last_sent_mono_by_key: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _suppressed_count_by_key: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _last_delivery: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    @dataclass(frozen=True)
    class SendResult:
        sent: bool
        error: str | None = None
        status: str = "sent"

    def __post_init__(self) -> None:
        self._last_delivery = {
            "enabled": bool(self.enabled),
            "provider": self.resolved_provider(),
            "periodic_status_enabled": self.supports_periodic_status(),
            "last_status": None,
            "last_attempt_at": None,
            "last_sent_at": None,
            "last_event_type": None,
            "last_title": None,
            "last_body_preview": None,
            "last_error": None,
            "last_dedupe_key": None,
            "last_suppress_window_sec": None,
            "last_suppressed_count": 0,
        }

    def resolved_provider(self) -> str:
        return str(self.provider or "none").strip().lower() or "none"

    def supports_periodic_status(self) -> bool:
        return self.resolved_provider() == "none"

    def delivery_snapshot(self) -> dict[str, Any]:
        with self._delivery_lock:
            snapshot = dict(self._last_delivery)
        snapshot["enabled"] = bool(self.enabled)
        snapshot["provider"] = self.resolved_provider()
        snapshot["periodic_status_enabled"] = self.supports_periodic_status()
        return snapshot

    def send_notification(self, message: NotificationMessage) -> SendResult:
        return self.send_with_result(message)

    def send_with_result(self, message: str | NotificationMessage) -> SendResult:
        notification = self._coerce_message(message)
        provider = self.resolved_provider()
        dedupe_key = self._normalized_dedupe_key(notification.dedupe_key)
        suppress_window_sec = self._normalized_suppress_window(notification.suppress_window_sec)

        if not self.enabled:
            self._record_delivery(
                status="disabled",
                provider=provider,
                message=notification,
                error="disabled",
                dedupe_key=dedupe_key,
                suppress_window_sec=suppress_window_sec,
                suppressed_count=0,
            )
            return Notifier.SendResult(sent=False, error="disabled", status="disabled")

        suppress_result = self._maybe_suppress(
            message=notification,
            dedupe_key=dedupe_key,
            suppress_window_sec=suppress_window_sec,
        )
        if suppress_result is not None:
            return suppress_result

        if provider == "webpush":
            return self._record_send_success(
                provider=provider,
                message=notification,
                dedupe_key=dedupe_key,
                suppress_window_sec=suppress_window_sec,
            )

        self._record_delivery(
            status="failed",
            provider=provider,
            message=notification,
            error="provider_unconfigured",
            dedupe_key=dedupe_key,
            suppress_window_sec=suppress_window_sec,
            suppressed_count=0,
        )
        return Notifier.SendResult(
            sent=False,
            error="provider_unconfigured",
            status="failed",
        )

    def send(self, message: str) -> None:
        _ = self.send_with_result(message)

    @staticmethod
    def _coerce_message(message: str | NotificationMessage) -> NotificationMessage:
        if isinstance(message, NotificationMessage):
            return message
        return NotificationMessage(body=str(message or "").strip())

    @staticmethod
    def _utcnow_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _preview(text: str, limit: int = 160) -> str:
        value = str(text or "").strip().replace("\n", " / ")
        if len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."

    @staticmethod
    def _normalized_dedupe_key(raw: str | None) -> str | None:
        key = str(raw or "").strip()
        return key or None

    @staticmethod
    def _normalized_suppress_window(raw: float | None) -> float | None:
        if raw is None:
            return None
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            return None
        if parsed <= 0.0:
            return None
        return parsed

    def _maybe_suppress(
        self,
        *,
        message: NotificationMessage,
        dedupe_key: str | None,
        suppress_window_sec: float | None,
    ) -> SendResult | None:
        if dedupe_key is None or suppress_window_sec is None:
            return None
        now_mono = time.monotonic()
        with self._delivery_lock:
            last_sent_mono = self._last_sent_mono_by_key.get(dedupe_key)
            if last_sent_mono is None or (now_mono - last_sent_mono) >= suppress_window_sec:
                return None
            suppressed_count = self._suppressed_count_by_key.get(dedupe_key, 0) + 1
            self._suppressed_count_by_key[dedupe_key] = suppressed_count
            self._last_delivery.update(
                {
                    "enabled": bool(self.enabled),
                    "provider": self.resolved_provider(),
                    "periodic_status_enabled": self.supports_periodic_status(),
                    "last_status": "suppressed",
                    "last_attempt_at": self._utcnow_iso(),
                    "last_event_type": message.event_type,
                    "last_title": str(message.title or "").strip() or None,
                    "last_body_preview": self._preview(message.body),
                    "last_error": "suppressed",
                    "last_dedupe_key": dedupe_key,
                    "last_suppress_window_sec": suppress_window_sec,
                    "last_suppressed_count": suppressed_count,
                }
            )
        return Notifier.SendResult(sent=False, error="suppressed", status="suppressed")

    def _record_send_success(
        self,
        *,
        provider: str,
        message: NotificationMessage,
        dedupe_key: str | None,
        suppress_window_sec: float | None,
    ) -> SendResult:
        with self._delivery_lock:
            suppressed_count = 0
            if dedupe_key is not None:
                self._last_sent_mono_by_key[dedupe_key] = time.monotonic()
                suppressed_count = self._suppressed_count_by_key.pop(dedupe_key, 0)
            self._last_delivery.update(
                {
                    "enabled": bool(self.enabled),
                    "provider": provider,
                    "periodic_status_enabled": self.supports_periodic_status(),
                    "last_status": "sent",
                    "last_attempt_at": self._utcnow_iso(),
                    "last_sent_at": self._utcnow_iso(),
                    "last_event_type": message.event_type,
                    "last_title": str(message.title or "").strip() or None,
                    "last_body_preview": self._preview(message.body),
                    "last_error": None,
                    "last_dedupe_key": dedupe_key,
                    "last_suppress_window_sec": suppress_window_sec,
                    "last_suppressed_count": suppressed_count,
                }
            )
        return Notifier.SendResult(sent=True, error=None, status="sent")

    def _record_delivery(
        self,
        *,
        status: str,
        provider: str,
        message: NotificationMessage,
        error: str | None,
        dedupe_key: str | None,
        suppress_window_sec: float | None,
        suppressed_count: int,
    ) -> None:
        with self._delivery_lock:
            self._last_delivery.update(
                {
                    "enabled": bool(self.enabled),
                    "provider": provider,
                    "periodic_status_enabled": self.supports_periodic_status(),
                    "last_status": status,
                    "last_attempt_at": self._utcnow_iso(),
                    "last_event_type": message.event_type,
                    "last_title": str(message.title or "").strip() or None,
                    "last_body_preview": self._preview(message.body),
                    "last_error": error,
                    "last_dedupe_key": dedupe_key,
                    "last_suppress_window_sec": suppress_window_sec,
                    "last_suppressed_count": suppressed_count,
                }
            )
