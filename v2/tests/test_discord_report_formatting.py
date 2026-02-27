from __future__ import annotations

from v2.discord_bot.services.formatting import JSONPayload, format_report_payload


def test_format_report_payload_shows_disabled_when_notifier_disabled() -> None:
    payload: JSONPayload = {
        "kind": "DAILY_REPORT",
        "day": "2026-02-27",
        "engine_state": "RUNNING",
        "reported_at": "2026-02-27T18:30:34.605413+00:00",
        "detail": {
            "entries": 3,
            "closes": 0,
            "errors": 0,
            "canceled": 0,
            "blocks": 79,
            "total_records": 142,
        },
        "notifier_enabled": False,
        "notifier_sent": False,
        "notifier_error": None,
    }

    text = format_report_payload(payload)
    assert "디스코드 전송: 비활성" in text
    assert "디스코드 전송: 실패" not in text


def test_format_report_payload_keeps_send_error_on_separate_line() -> None:
    payload: JSONPayload = {
        "kind": "DAILY_REPORT",
        "day": "2026-02-27",
        "engine_state": "RUNNING",
        "reported_at": "2026-02-27T18:30:34.605413+00:00",
        "detail": {
            "entries": 3,
            "closes": 0,
            "errors": 0,
            "canceled": 0,
            "blocks": 79,
            "total_records": 142,
        },
        "notifier_enabled": True,
        "notifier_sent": False,
        "notifier_error": "ConnectError: timeout",
    }

    text = format_report_payload(payload)
    lines = text.splitlines()
    assert "디스코드 전송: 실패" in lines
    assert "전송 오류: ConnectError: timeout" in lines
