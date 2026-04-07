from __future__ import annotations

from pathlib import Path

import httpx

from v2.config.loader import load_effective_config
from v2.notify import NotificationMessage, WebPushDispatchResult, build_notifier_from_config
from v2.notify.notifier import Notifier


def test_load_effective_config_reads_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.v2"
    env_file.write_text(
        "\n".join(
            [
                "BINANCE_API_KEY=file-key",
                "BINANCE_API_SECRET=file-secret",
                "DISCORD_WEBHOOK_URL=https://discord.test/webhook",
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
    assert cfg.secrets.notify_webhook_url == "https://discord.test/webhook"


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


def test_load_effective_config_reads_ntfy_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.ntfy"
    env_file.write_text(
        "\n".join(
            [
                "NTFY_ENABLED=true",
                "NTFY_BASE_URL=https://ntfy.sh",
                "NTFY_TOPIC=ops-alerts",
                "NTFY_TOKEN=ntfy-token",
                "NTFY_TAGS=trading,risk",
                "NTFY_PRIORITY=4",
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
        env_file_path=env_file,
    )
    assert cfg.secrets.ntfy_enabled is True
    assert cfg.secrets.ntfy_base_url == "https://ntfy.sh"
    assert cfg.secrets.ntfy_topic == "ops-alerts"
    assert cfg.secrets.ntfy_token == "ntfy-token"
    assert cfg.secrets.ntfy_tags == ("trading", "risk")
    assert cfg.secrets.ntfy_priority == "4"


def test_build_notifier_from_config_prefers_ntfy_when_topic_present() -> None:
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={
            "NTFY_ENABLED": "true",
            "NTFY_BASE_URL": "https://ntfy.sh",
            "NTFY_TOPIC": "ops-alerts",
        },
    )

    notifier = build_notifier_from_config(cfg)

    assert notifier.enabled is True
    assert notifier.provider == "ntfy"
    assert notifier.ntfy_base_url == "https://ntfy.sh"
    assert notifier.ntfy_topic == "ops-alerts"


def test_build_notifier_from_config_prefers_webpush_when_enabled() -> None:
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={
            "WEBPUSH_ENABLED": "true",
        },
    )

    notifier = build_notifier_from_config(
        cfg,
        webpush_send=lambda message: WebPushDispatchResult(  # type: ignore[return-value]
            sent=bool(message.title),
            error=None,
            status="sent",
        ),
        webpush_public_key="PUBLIC_KEY",
    )

    assert notifier.enabled is True
    assert notifier.provider == "webpush"
    assert notifier.webpush_public_key == "PUBLIC_KEY"
    assert notifier.supports_periodic_status() is False


def test_notifier_disabled_notification_path_returns_disabled() -> None:
    notifier = Notifier(enabled=False, provider="ntfy", ntfy_topic="ops-alerts")

    result = notifier.send_notification(
        NotificationMessage(title="엔진 시작", body="ra_profile | shadow/testnet")
    )

    assert result.sent is False
    assert result.error == "disabled"


def test_notifier_discord_http_error_does_not_raise(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _BoomClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            _ = args
            _ = kwargs

        def __enter__(self) -> "_BoomClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            _ = exc_type
            _ = exc
            _ = tb

        def post(self, url: str, json: dict[str, str]):  # type: ignore[no-untyped-def]
            _ = url
            _ = json
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "Client", _BoomClient)
    notifier = Notifier(
        enabled=True, provider="discord", webhook_url="https://discord.test/webhook"
    )
    notifier.send("hello")


def test_notifier_send_with_result_reports_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _BoomClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            _ = args
            _ = kwargs

        def __enter__(self) -> "_BoomClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            _ = exc_type
            _ = exc
            _ = tb

        def post(self, url: str, json: dict[str, str]):  # type: ignore[no-untyped-def]
            _ = url
            _ = json
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "Client", _BoomClient)
    notifier = Notifier(
        enabled=True, provider="discord", webhook_url="https://discord.test/webhook"
    )
    result = notifier.send_with_result("hello")

    assert result.sent is False
    assert result.error is not None
    assert "ConnectError" in result.error


def test_notifier_ntfy_publish_uses_hosted_headers(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    class _OKResponse:
        def raise_for_status(self) -> None:
            return None

    class _CaptureClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            _ = args
            _ = kwargs

        def __enter__(self) -> "_CaptureClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            _ = exc_type
            _ = exc
            _ = tb

        def post(self, url: str, content: bytes, headers: dict[str, str], params: dict[str, str]):  # type: ignore[no-untyped-def]
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = dict(headers)
            captured["params"] = dict(params)
            return _OKResponse()

    monkeypatch.setattr(httpx, "Client", _CaptureClient)
    notifier = Notifier(
        enabled=True,
        provider="ntfy",
        ntfy_base_url="https://ntfy.sh",
        ntfy_topic="ops-alerts",
        ntfy_token="ntfy-token",
        ntfy_tags=("trading",),
        ntfy_priority="3",
    )

    result = notifier.send_notification(
        NotificationMessage(
            title="패닉 청산",
            body="BTCUSDT | 패닉 정리 완료",
            tags=("risk",),
            priority=5,
        )
    )

    assert result.sent is True
    assert captured["url"] == "https://ntfy.sh/ops-alerts"
    assert captured["content"] == "BTCUSDT | 패닉 정리 완료".encode("utf-8")
    headers = captured["headers"]
    assert headers["Authorization"] == "Bearer ntfy-token"
    params = captured["params"]
    assert params["title"] == "패닉 청산"
    assert params["priority"] == "5"
    assert params["tags"] == "trading,risk"
    snapshot = notifier.delivery_snapshot()
    assert snapshot["last_status"] == "sent"
    assert snapshot["last_title"] == "패닉 청산"
    assert snapshot["last_event_type"] is None


def test_notifier_webpush_records_partial_delivery_without_clearing_last_sent_at() -> None:
    notifier = Notifier(
        enabled=True,
        provider="webpush",
        webpush_public_key="PUBLIC_KEY",
        webpush_send=lambda _message: WebPushDispatchResult(
            sent=True,
            error="partial_failure:1",
            status="partial",
        ),
    )

    result = notifier.send_notification(
        NotificationMessage(
            title="웹 푸시 테스트",
            body="현재 기기에서 수신 여부를 확인합니다.",
        )
    )

    assert result.sent is True
    assert result.status == "partial"
    snapshot = notifier.delivery_snapshot()
    assert snapshot["last_status"] == "partial"
    assert snapshot["last_sent_at"] is not None


def test_notifier_suppresses_duplicate_dedupe_key_within_window(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    now = {"value": 100.0}

    class _OKResponse:
        def raise_for_status(self) -> None:
            return None

    class _CaptureClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            _ = args
            _ = kwargs

        def __enter__(self) -> "_CaptureClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            _ = exc_type
            _ = exc
            _ = tb

        def post(self, url: str, content: bytes, headers: dict[str, str], params: dict[str, str]):  # type: ignore[no-untyped-def]
            calls.append(url)
            _ = content
            _ = headers
            _ = params
            return _OKResponse()

    monkeypatch.setattr("v2.notify.notifier.time.monotonic", lambda: now["value"])
    monkeypatch.setattr(httpx, "Client", _CaptureClient)
    notifier = Notifier(
        enabled=True,
        provider="ntfy",
        ntfy_base_url="https://ntfy.sh",
        ntfy_topic="ops-alerts",
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
    assert len(calls) == 2


def test_notifier_ntfy_http_error_snapshot_includes_response_body(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    request = httpx.Request("POST", "https://ntfy.sh/ops-alerts")
    response = httpx.Response(
        429,
        request=request,
        json={
            "code": 42908,
            "http": 429,
            "error": "limit reached: daily message quota reached",
        },
    )

    class _QuotaClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            _ = args
            _ = kwargs

        def __enter__(self) -> "_QuotaClient":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
            _ = exc_type
            _ = exc
            _ = tb

        def post(self, url: str, content: bytes, headers: dict[str, str], params: dict[str, str]):  # type: ignore[no-untyped-def]
            _ = url
            _ = content
            _ = headers
            _ = params
            return response

    monkeypatch.setattr(httpx, "Client", _QuotaClient)
    notifier = Notifier(
        enabled=True,
        provider="ntfy",
        ntfy_base_url="https://ntfy.sh",
        ntfy_topic="ops-alerts",
    )

    result = notifier.send_notification(
        NotificationMessage(
            title="실시간 판단 대기",
            body="거래량 조건 미충족",
        )
    )

    assert result.sent is False
    assert result.status == "failed"
    assert result.error is not None
    assert "status=429" in result.error
    assert "daily message quota reached" in result.error
    snapshot = notifier.delivery_snapshot()
    assert snapshot["last_status"] == "failed"
    assert "daily message quota reached" in str(snapshot["last_error"])
