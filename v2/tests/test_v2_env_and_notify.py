from __future__ import annotations

from pathlib import Path

from v2.config.loader import load_effective_config
from v2.notify import NotificationMessage, build_notifier_from_config
from v2.notify.notifier import Notifier


def test_load_effective_config_reads_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.v2"
    env_file.write_text(
        "\n".join(
            [
                "BINANCE_API_KEY=file-key",
                "BINANCE_API_SECRET=file-secret",
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="prod",
        env_map={},
        env_file_path=env_file,
    )
    assert cfg.secrets.binance_api_key == "file-key"
    assert cfg.secrets.binance_api_secret == "file-secret"


def test_env_map_overrides_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.v2"
    env_file.write_text(
        "\n".join(
            [
                "BINANCE_API_KEY=file-key",
                "BINANCE_API_SECRET=file-secret",
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="live",
        env="prod",
        env_map={
            "BINANCE_API_KEY": "override-key",
            "BINANCE_API_SECRET": "override-secret",
        },
        env_file_path=env_file,
    )
    assert cfg.secrets.binance_api_key == "override-key"
    assert cfg.secrets.binance_api_secret == "override-secret"


def test_build_notifier_from_config_uses_behavior_webpush() -> None:
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    cfg.behavior.notify.enabled = True
    cfg.behavior.notify.provider = "webpush"

    notifier = build_notifier_from_config(cfg)

    assert notifier.enabled is True
    assert notifier.provider == "webpush"
    assert notifier.resolved_provider() == "webpush"


def test_notifier_disabled_notification_path_returns_disabled() -> None:
    notifier = Notifier(enabled=False, provider="webpush")

    result = notifier.send_notification(
        NotificationMessage(title="엔진 시작", body="ra_profile | shadow/testnet")
    )

    assert result.sent is False
    assert result.error == "disabled"


def test_notifier_with_unconfigured_provider_reports_error() -> None:
    notifier = Notifier(enabled=True, provider="none")

    result = notifier.send_with_result("hello")

    assert result.sent is False
    assert result.error == "provider_unconfigured"


def test_notifier_webpush_provider_records_success() -> None:
    notifier = Notifier(enabled=True, provider="webpush")

    result = notifier.send_notification(
        NotificationMessage(
            title="패닉 청산",
            body="BTCUSDT | 패닉 정리 완료",
            event_type="position_closed",
        )
    )

    assert result.sent is True
    snapshot = notifier.delivery_snapshot()
    assert snapshot["last_status"] == "sent"
    assert snapshot["provider"] == "webpush"
    assert snapshot["last_title"] == "패닉 청산"
    assert snapshot["periodic_status_enabled"] is False


def test_notifier_suppresses_duplicate_dedupe_key_within_window(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    now = {"value": 100.0}

    monkeypatch.setattr("v2.notify.notifier.time.monotonic", lambda: now["value"])
    notifier = Notifier(
        enabled=True,
        provider="webpush",
    )
    message = NotificationMessage(
        title="시장 데이터 상태",
        body="stale 감지",
        event_type="stale_transition",
        dedupe_key="stale:market_data",
        suppress_window_sec=180.0,
    )

    first = notifier.send_notification(message)
    now["value"] = 140.0
    second = notifier.send_notification(message)
    suppressed_snapshot = notifier.delivery_snapshot()
    now["value"] = 320.0
    third = notifier.send_notification(message)
    final_snapshot = notifier.delivery_snapshot()

    assert first.sent is True
    assert first.status == "sent"
    assert second.sent is False
    assert second.status == "suppressed"
    assert second.error == "suppressed"
    assert suppressed_snapshot["last_status"] == "suppressed"
    assert suppressed_snapshot["last_event_type"] == "stale_transition"
    assert suppressed_snapshot["last_suppressed_count"] == 1
    assert third.sent is True
    assert third.status == "sent"
    assert final_snapshot["last_status"] == "sent"
    assert final_snapshot["last_suppressed_count"] == 1


def test_notifier_failed_provider_result_rolls_back_webpush_dedupe(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    now = {"value": 100.0}

    monkeypatch.setattr("v2.notify.notifier.time.monotonic", lambda: now["value"])
    notifier = Notifier(enabled=True, provider="webpush")
    message = NotificationMessage(
        title="시장 데이터 상태",
        body="stale 감지",
        event_type="stale_transition",
        dedupe_key="stale:market_data",
        suppress_window_sec=180.0,
    )

    first = notifier.send_notification(message)
    notifier.record_provider_result(
        message=message,
        provider="webpush",
        sent=False,
        error="webpush_no_subscriptions",
        status="failed",
    )
    failed_snapshot = notifier.delivery_snapshot()

    now["value"] = 140.0
    second = notifier.send_notification(message)

    assert first.sent is True
    assert failed_snapshot["last_status"] == "failed"
    assert failed_snapshot["last_error"] == "webpush_no_subscriptions"
    assert failed_snapshot["last_sent_at"] is None
    assert second.sent is True
    assert second.status == "sent"
