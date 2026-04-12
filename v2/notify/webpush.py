from __future__ import annotations

import base64
import importlib.util
import json
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from v2.storage import RuntimeStorage

from .models import NotificationMessage, WebPushDispatchResult

_VAPID_MARKER_KEY = "webpush_vapid_keys"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _mask_endpoint(endpoint: str) -> str:
    value = str(endpoint or "").strip()
    if len(value) <= 20:
        return value
    return f"{value[:12]}...{value[-8:]}"


@dataclass
class WebPushService:
    storage: RuntimeStorage
    subject: str | None = None
    target_path: str = "/operator/"
    icon_url: str = "/operator/static/operator-icon.svg"
    badge_url: str = "/operator/static/operator-icon.svg"

    def __post_init__(self) -> None:
        self.subject = self._normalized_subject(self.subject)

    def availability_snapshot(self) -> dict[str, Any]:
        public_key, error = self._public_key_snapshot()
        dependency_error = self._dispatch_dependency_error()
        return {
            "available": public_key is not None and dependency_error is None,
            "public_key": public_key,
            "subscription_count": self.storage.count_webpush_subscriptions(active_only=True),
            "last_error": dependency_error or error,
            "subject": self.subject,
        }

    def list_subscriptions(self) -> list[dict[str, Any]]:
        rows = self.storage.list_webpush_subscriptions(active_only=False)
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "endpoint_hint": _mask_endpoint(str(row.get("endpoint") or "")),
                    "device_id": row.get("device_id"),
                    "device_label": row.get("device_label"),
                    "platform": row.get("platform"),
                    "standalone": bool(row.get("standalone")),
                    "active": bool(row.get("active")),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "last_success_at": row.get("last_success_at"),
                    "last_failure_at": row.get("last_failure_at"),
                    "last_error": row.get("last_error"),
                }
            )
        return out

    @staticmethod
    def _detect_non_loopback_ip() -> str | None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                ip = str(sock.getsockname()[0] or "").strip()
                if ip and not ip.startswith("127."):
                    return ip
        except Exception:  # noqa: BLE001
            return None
        return None

    @classmethod
    def _normalized_subject(cls, raw: str | None) -> str:
        value = str(raw or "").strip()
        if value and "local.invalid" not in value:
            return value
        detected_ip = cls._detect_non_loopback_ip()
        if detected_ip:
            return f"https://{detected_ip}"
        return "mailto:autotrader@example.com"

    def register_subscription(
        self,
        *,
        subscription: dict[str, Any],
        device_id: str | None,
        device_label: str | None,
        user_agent: str | None,
        platform: str | None,
        standalone: bool,
    ) -> dict[str, Any]:
        normalized = self._normalize_subscription(subscription)
        endpoint = str(normalized["endpoint"])
        self.storage.upsert_webpush_subscription(
            endpoint=endpoint,
            subscription_json=json.dumps(
                normalized, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            ),
            device_id=device_id,
            device_label=device_label,
            user_agent=user_agent,
            platform=platform,
            standalone=standalone,
        )
        return {
            "ok": True,
            "endpoint_hint": _mask_endpoint(endpoint),
            "subscription_count": self.storage.count_webpush_subscriptions(active_only=True),
        }

    def unregister_subscription(self, *, endpoint: str) -> dict[str, Any]:
        ok = self.storage.deactivate_webpush_subscription(
            endpoint=str(endpoint or "").strip(),
            reason="client_unsubscribe",
        )
        return {
            "ok": bool(ok),
            "subscription_count": self.storage.count_webpush_subscriptions(active_only=True),
        }

    def send_test_notification(self, *, device_label: str | None = None) -> WebPushDispatchResult:
        label = str(device_label or "").strip() or "현재 기기"
        return self.send(
            NotificationMessage(
                title="웹 푸시 테스트",
                body=f"{label}에서 Auto Trader 운영 푸시가 연결되었습니다.",
                event_type="webpush_test",
                metadata={"path": self.target_path},
            )
        )

    def send(self, message: NotificationMessage) -> WebPushDispatchResult:
        public_key, error = self._public_key_snapshot()
        if public_key is None:
            return WebPushDispatchResult(sent=False, error=error or "webpush_key_unavailable", status="failed")
        dependency_error = self._dispatch_dependency_error()
        if dependency_error is not None:
            return WebPushDispatchResult(sent=False, error=dependency_error, status="failed")
        keys = self.storage.load_runtime_marker(marker_key=_VAPID_MARKER_KEY) or {}
        private_key_pem = str(keys.get("private_key_pem") or "").strip()
        if not private_key_pem:
            return WebPushDispatchResult(sent=False, error="webpush_private_key_missing", status="failed")
        rows = self.storage.list_webpush_subscriptions(active_only=True)
        if not rows:
            return WebPushDispatchResult(sent=False, error="webpush_no_subscriptions", status="failed")

        payload = json.dumps(
            self._notification_payload(message),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        success_count = 0
        error_messages: list[str] = []
        for row in rows:
            endpoint = str(row.get("endpoint") or "").strip()
            raw_subscription = str(row.get("subscription_json") or "").strip()
            try:
                subscription = json.loads(raw_subscription)
            except (TypeError, ValueError, json.JSONDecodeError):
                self.storage.record_webpush_subscription_failure(
                    endpoint=endpoint,
                    error="webpush_subscription_json_invalid",
                )
                self.storage.deactivate_webpush_subscription(
                    endpoint=endpoint,
                    reason="webpush_subscription_json_invalid",
                )
                error_messages.append("webpush_subscription_json_invalid")
                continue

            try:
                self._dispatch(
                    subscription=subscription,
                    payload=payload,
                    vapid_private_key=private_key_pem,
                )
            except Exception as exc:  # noqa: BLE001
                error_text = self._dispatch_error_text(exc)
                self.storage.record_webpush_subscription_failure(
                    endpoint=endpoint,
                    error=error_text,
                )
                status_code = self._dispatch_status_code(exc)
                if status_code in {404, 410}:
                    self.storage.deactivate_webpush_subscription(
                        endpoint=endpoint,
                        reason=error_text,
                    )
                error_messages.append(error_text)
                continue

            success_count += 1
            self.storage.record_webpush_subscription_success(endpoint=endpoint)

        if success_count <= 0:
            return WebPushDispatchResult(
                sent=False,
                error=error_messages[0] if error_messages else "webpush_send_failed",
                status="failed",
            )
        if error_messages:
            return WebPushDispatchResult(
                sent=True,
                error=f"partial_failure:{len(error_messages)}",
                status="partial",
            )
        return WebPushDispatchResult(sent=True, error=None, status="sent")

    def _public_key_snapshot(self) -> tuple[str | None, str | None]:
        try:
            keys = self._ensure_vapid_keys()
        except RuntimeError as exc:
            return None, str(exc)
        public_key = str(keys.get("public_key") or "").strip()
        return (public_key or None, None if public_key else "webpush_public_key_missing")

    @staticmethod
    def _dispatch_dependency_error() -> str | None:
        missing = [
            name for name in ("py_vapid", "pywebpush") if importlib.util.find_spec(name) is None
        ]
        if not missing:
            return None
        return f"webpush_package_missing:{','.join(missing)}"

    def _ensure_vapid_keys(self) -> dict[str, Any]:
        current = self.storage.load_runtime_marker(marker_key=_VAPID_MARKER_KEY) or {}
        public_key = str(current.get("public_key") or "").strip()
        private_key_pem = str(current.get("private_key_pem") or "").strip()
        if public_key and private_key_pem:
            return current

        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"webpush_crypto_missing: {type(exc).__name__}") from exc

        private_key = ec.generate_private_key(ec.SECP256R1())
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        public_numbers = private_key.public_key().public_numbers()
        public_bytes = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
        payload = {
            "public_key": _b64url_encode(public_bytes),
            "private_key_pem": private_pem,
            "created_at": _utcnow_iso(),
        }
        self.storage.save_runtime_marker(marker_key=_VAPID_MARKER_KEY, payload=payload)
        return payload

    def _normalize_subscription(self, subscription: dict[str, Any]) -> dict[str, Any]:
        endpoint = str(subscription.get("endpoint") or "").strip()
        keys = subscription.get("keys")
        if not endpoint or not isinstance(keys, dict):
            raise ValueError("webpush_subscription_invalid")
        p256dh = str(keys.get("p256dh") or "").strip()
        auth = str(keys.get("auth") or "").strip()
        if not p256dh or not auth:
            raise ValueError("webpush_subscription_keys_missing")
        expiration = subscription.get("expirationTime")
        return {
            "endpoint": endpoint,
            "expirationTime": expiration,
            "keys": {
                "p256dh": p256dh,
                "auth": auth,
            },
        }

    def _notification_payload(self, message: NotificationMessage) -> dict[str, Any]:
        raw_data = dict(message.metadata or {})
        path = str(raw_data.get("path") or self.target_path).strip() or self.target_path
        return {
            "title": str(message.title or "운영 알림").strip() or "운영 알림",
            "body": str(message.body or "").strip(),
            "path": path,
            "icon": str(raw_data.get("icon") or self.icon_url),
            "badge": str(raw_data.get("badge") or self.badge_url),
            "tag": str(message.dedupe_key or message.event_type or "operator-update"),
            "event_type": message.event_type,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }

    def _dispatch(
        self,
        *,
        subscription: dict[str, Any],
        payload: str,
        vapid_private_key: str,
    ) -> None:
        try:
            from py_vapid import Vapid01
            from pywebpush import webpush
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"webpush_package_missing: {type(exc).__name__}") from exc

        vapid = Vapid01.from_pem(vapid_private_key.encode("utf-8"))
        _ = webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=vapid,
            vapid_claims={"sub": self.subject},
            ttl=60,
        )

    @staticmethod
    def _dispatch_status_code(exc: Exception) -> int | None:
        response = getattr(exc, "response", None)
        if response is None:
            return None
        try:
            status_code = getattr(response, "status_code", None)
            return int(status_code) if status_code is not None else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _dispatch_error_text(exc: Exception) -> str:
        response = getattr(exc, "response", None)
        parts = [type(exc).__name__]
        if response is not None:
            status_code = getattr(response, "status_code", None)
            if status_code is not None:
                parts.append(f"status={status_code}")
            body = ""
            try:
                body = str(getattr(response, "text", "") or "").strip()
            except Exception:  # noqa: BLE001
                body = ""
            if body:
                parts.append(body[:240])
        message = str(exc).strip()
        if message:
            parts.append(message)
        return ": ".join(parts)
