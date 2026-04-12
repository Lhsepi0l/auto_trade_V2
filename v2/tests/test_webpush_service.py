from __future__ import annotations

import pytest

from v2.notify import NotificationMessage
from v2.notify.webpush import WebPushService
from v2.storage import RuntimeStorage


def test_webpush_service_generates_and_persists_vapid_keys(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "webpush.sqlite3"))
    storage.ensure_schema()
    service = WebPushService(storage=storage)
    monkeypatch.setattr(service, "_dispatch_dependency_error", lambda: None)

    availability = service.availability_snapshot()
    marker = storage.load_runtime_marker(marker_key="webpush_vapid_keys")

    assert availability["available"] is True
    assert availability["public_key"]
    assert marker is not None
    assert marker["public_key"] == availability["public_key"]
    assert "BEGIN PRIVATE KEY" in str(marker["private_key_pem"])


def test_webpush_service_availability_reports_missing_dispatch_packages(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "webpush.sqlite3"))
    storage.ensure_schema()
    service = WebPushService(storage=storage)
    monkeypatch.setattr(
        service,
        "_dispatch_dependency_error",
        lambda: "webpush_package_missing:py_vapid,pywebpush",
    )

    availability = service.availability_snapshot()

    assert availability["available"] is False
    assert availability["public_key"]
    assert availability["last_error"] == "webpush_package_missing:py_vapid,pywebpush"


def test_webpush_service_deactivates_gone_subscription(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "webpush.sqlite3"))
    storage.ensure_schema()
    service = WebPushService(storage=storage)
    monkeypatch.setattr(service, "_dispatch_dependency_error", lambda: None)
    service.register_subscription(
        subscription={
            "endpoint": "https://example.com/push/1",
            "expirationTime": None,
            "keys": {"p256dh": "abc", "auth": "def"},
        },
        device_id="device-1",
        device_label="민수 iPhone 운영앱",
        user_agent="Mozilla/5.0",
        platform="iPhone",
        standalone=True,
    )

    class _GoneError(Exception):
        def __init__(self) -> None:
            self.response = type("Response", (), {"status_code": 410, "text": "Gone"})()
            super().__init__("410 Gone")

    def _raise_gone(**kwargs) -> None:  # type: ignore[no-untyped-def]
        _ = kwargs
        raise _GoneError()

    monkeypatch.setattr(service, "_dispatch", _raise_gone)

    result = service.send(
        NotificationMessage(title="테스트", body="gone subscription cleanup")
    )
    rows = storage.list_webpush_subscriptions(active_only=False)

    assert result.sent is False
    assert rows[0]["active"] == 0
    assert "Gone" in str(rows[0]["last_error"])


def test_webpush_service_records_successful_send(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "webpush.sqlite3"))
    storage.ensure_schema()
    service = WebPushService(storage=storage)
    monkeypatch.setattr(service, "_dispatch_dependency_error", lambda: None)
    service.register_subscription(
        subscription={
            "endpoint": "https://example.com/push/1",
            "expirationTime": None,
            "keys": {"p256dh": "abc", "auth": "def"},
        },
        device_id="device-1",
        device_label="민수 iPhone 운영앱",
        user_agent="Mozilla/5.0",
        platform="iPhone",
        standalone=True,
    )

    monkeypatch.setattr(service, "_dispatch", lambda **kwargs: None)

    result = service.send_test_notification(device_label="민수 iPhone 운영앱")
    rows = storage.list_webpush_subscriptions(active_only=True)

    assert result.sent is True
    assert result.status == "sent"
    assert rows[0]["last_success_at"] is not None


def test_webpush_dispatch_uses_vapid_instance_for_pywebpush(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    vapid_mod = pytest.importorskip("py_vapid")
    Vapid01 = vapid_mod.Vapid01
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "webpush.sqlite3"))
    storage.ensure_schema()
    service = WebPushService(storage=storage)
    _ = service.availability_snapshot()
    marker = storage.load_runtime_marker(marker_key="webpush_vapid_keys")
    assert marker is not None
    captured: dict[str, object] = {}

    def _fake_webpush(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return None

    monkeypatch.setattr("pywebpush.webpush", _fake_webpush)

    service._dispatch(  # noqa: SLF001
        subscription={"endpoint": "https://example.com/push/1", "keys": {"p256dh": "abc", "auth": "def"}},
        payload='{"title":"테스트"}',
        vapid_private_key=str(marker["private_key_pem"]),
    )

    assert isinstance(captured["vapid_private_key"], Vapid01)


def test_webpush_service_uses_detected_https_subject_for_placeholder_subject(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(WebPushService, "_detect_non_loopback_ip", classmethod(lambda cls: "192.168.50.29"))
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "webpush.sqlite3"))
    storage.ensure_schema()
    service = WebPushService(storage=storage, subject="mailto:autotrader@local.invalid")

    assert service.subject == "https://192.168.50.29"


def test_webpush_notification_payload_keeps_dedupe_tag_for_collapsed_alerts(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "webpush.sqlite3"))
    storage.ensure_schema()
    service = WebPushService(storage=storage)

    payload = service._notification_payload(  # noqa: SLF001
        NotificationMessage(
            title="시장 데이터 지연",
            body="갱신 지연 감지",
            event_type="stale_transition",
            dedupe_key="stale:market_data",
        )
    )

    assert payload["tag"] == "stale:market_data"
    assert payload["renotify"] is True


def test_webpush_notification_payload_uses_unique_tag_for_non_deduped_alerts(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "webpush.sqlite3"))
    storage.ensure_schema()
    service = WebPushService(storage=storage)
    real_datetime = __import__("datetime").datetime
    values = iter(
        [
            real_datetime(2026, 4, 12, 14, 50, 0, 1000, tzinfo=__import__("datetime").timezone.utc),
            real_datetime(2026, 4, 12, 14, 50, 0, 2000, tzinfo=__import__("datetime").timezone.utc),
            real_datetime(2026, 4, 12, 14, 50, 0, 3000, tzinfo=__import__("datetime").timezone.utc),
            real_datetime(2026, 4, 12, 14, 50, 0, 4000, tzinfo=__import__("datetime").timezone.utc),
        ]
    )

    class _FakeDateTime:
        @staticmethod
        def now(tz=None):  # type: ignore[no-untyped-def]
            value = next(values)
            if tz is None:
                return value.replace(tzinfo=None)
            return value.astimezone(tz)

    monkeypatch.setattr("v2.notify.webpush.datetime", _FakeDateTime)

    first = service._notification_payload(  # noqa: SLF001
        NotificationMessage(
            title="웹 푸시 테스트",
            body="현재 기기에서 연결 확인",
            event_type="webpush_test",
        )
    )
    second = service._notification_payload(  # noqa: SLF001
        NotificationMessage(
            title="웹 푸시 테스트",
            body="현재 기기에서 연결 확인",
            event_type="webpush_test",
        )
    )

    assert first["tag"].startswith("webpush_test:")
    assert second["tag"].startswith("webpush_test:")
    assert first["tag"] != second["tag"]
    assert first["renotify"] is True
