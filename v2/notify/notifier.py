from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from .models import NotificationMessage


@dataclass
class Notifier:
    enabled: bool = False
    provider: str = "none"
    webhook_url: str | None = None
    ntfy_base_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None
    ntfy_tags: tuple[str, ...] | list[str] | str | None = None
    ntfy_priority: str | int | None = None
    timeout_sec: float = 5.0
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
        provider = str(self.provider or "none").strip().lower()
        if provider == "none":
            if str(self.ntfy_topic or "").strip():
                return "ntfy"
            if str(self.webhook_url or "").strip():
                return "discord"
        return provider

    def supports_periodic_status(self) -> bool:
        return self.resolved_provider() != "ntfy"

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

        if provider == "discord":
            try:
                self._send_discord(notification)
                return self._record_send_success(
                    provider=provider,
                    message=notification,
                    dedupe_key=dedupe_key,
                    suppress_window_sec=suppress_window_sec,
                )
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
                print(f"[notify] discord_send_failed: {notification.as_text()}")
                self._record_delivery(
                    status="failed",
                    provider=provider,
                    message=notification,
                    error=error,
                    dedupe_key=dedupe_key,
                    suppress_window_sec=suppress_window_sec,
                    suppressed_count=0,
                )
                return Notifier.SendResult(sent=False, error=error, status="failed")
        if provider == "ntfy":
            topic = str(self.ntfy_topic or "").strip()
            if not topic:
                self._record_delivery(
                    status="failed",
                    provider=provider,
                    message=notification,
                    error="ntfy_topic_missing",
                    dedupe_key=dedupe_key,
                    suppress_window_sec=suppress_window_sec,
                    suppressed_count=0,
                )
                return Notifier.SendResult(
                    sent=False,
                    error="ntfy_topic_missing",
                    status="failed",
                )
            try:
                self._send_ntfy(notification)
                return self._record_send_success(
                    provider=provider,
                    message=notification,
                    dedupe_key=dedupe_key,
                    suppress_window_sec=suppress_window_sec,
                )
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
                print(f"[notify] ntfy_send_failed: {notification.as_text()}")
                self._record_delivery(
                    status="failed",
                    provider=provider,
                    message=notification,
                    error=error,
                    dedupe_key=dedupe_key,
                    suppress_window_sec=suppress_window_sec,
                    suppressed_count=0,
                )
                return Notifier.SendResult(sent=False, error=error, status="failed")

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

    def _send_discord(self, message: NotificationMessage) -> None:
        url = str(self.webhook_url or "").strip()
        if not url:
            raise httpx.UnsupportedProtocol("discord_webhook_missing")
        with httpx.Client(timeout=httpx.Timeout(self.timeout_sec)) as client:
            response = client.post(url, json={"content": message.as_text()})
            response.raise_for_status()

    @staticmethod
    def _normalize_tags(raw: tuple[str, ...] | list[str] | str | None) -> tuple[str, ...]:
        if raw is None:
            return ()
        if isinstance(raw, str):
            values = raw.split(",")
        else:
            values = list(raw)
        out: list[str] = []
        for value in values:
            tag = str(value or "").strip()
            if tag and tag not in out:
                out.append(tag)
        return tuple(out)

    def _merged_tags(self, extra_tags: Iterable[str]) -> tuple[str, ...]:
        tags: list[str] = []
        for value in [*self._normalize_tags(self.ntfy_tags), *self._normalize_tags(tuple(extra_tags))]:
            if value not in tags:
                tags.append(value)
        return tuple(tags)

    def _send_ntfy(self, message: NotificationMessage) -> None:
        base_url = str(self.ntfy_base_url or "https://ntfy.sh").strip().rstrip("/")
        topic = str(self.ntfy_topic or "").strip().lstrip("/")
        if not topic:
            raise httpx.UnsupportedProtocol("ntfy_topic_missing")

        headers = {"Content-Type": "text/plain; charset=utf-8"}
        params: dict[str, str] = {}
        title = str(message.title or "").strip()
        if title:
            params["title"] = title
        priority = message.priority if message.priority is not None else self.ntfy_priority
        if priority is not None and str(priority).strip():
            params["priority"] = str(priority).strip()
        tags = self._merged_tags(message.tags)
        if tags:
            params["tags"] = ",".join(tags)
        token = str(self.ntfy_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        with httpx.Client(timeout=httpx.Timeout(self.timeout_sec)) as client:
            response = client.post(
                f"{base_url}/{topic}",
                content=str(message.body or "").encode("utf-8"),
                headers=headers,
                params=params,
            )
            response.raise_for_status()

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
