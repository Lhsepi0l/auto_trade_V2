from __future__ import annotations

from v2.discord_bot.config import DiscordBotSettings


def test_trader_api_timeout_reads_uppercase_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TRADER_API_TIMEOUT_SEC", "42")
    cfg = DiscordBotSettings(_env_file=None)
    assert cfg.trader_api_timeout_sec == 42.0


def test_trader_api_base_url_normalizes_localhost(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TRADER_API_BASE_URL", "http://localhost:8101")
    cfg = DiscordBotSettings(_env_file=None)
    assert cfg.trader_api_base_url == "http://127.0.0.1:8101"


def test_trader_api_base_url_normalizes_zero_bind_host(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TRADER_API_BASE_URL", "http://0.0.0.0:8101")
    cfg = DiscordBotSettings(_env_file=None)
    assert cfg.trader_api_base_url == "http://127.0.0.1:8101"
