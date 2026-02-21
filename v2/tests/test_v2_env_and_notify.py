from __future__ import annotations

from pathlib import Path

import httpx

from v2.config.loader import load_effective_config
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
        profile="normal",
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
        profile="normal",
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
    notifier = Notifier(enabled=True, provider="discord", webhook_url="https://discord.test/webhook")
    notifier.send("hello")
