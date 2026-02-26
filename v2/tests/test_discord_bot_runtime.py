from __future__ import annotations

from types import SimpleNamespace

import pytest

import v2.discord_bot.bot as bot_module


class _FakeAPIClient:
    async def aclose(self) -> None:
        return


class _FakeBot:
    def __init__(self, *, api: object, guild_id: int = 0) -> None:
        self.api = api
        self.guild_id = guild_id

    async def start(self, _token: str) -> None:
        return

    async def close(self) -> None:
        return


class _FailingBot(_FakeBot):
    async def start(self, _token: str) -> None:
        raise RuntimeError("boom")


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        log_level="INFO",
        log_dir="./logs",
        log_json=False,
        discord_bot_token="token",
        trader_api_base_url="http://127.0.0.1:8101",
        trader_api_timeout_sec=1.0,
        trader_api_retry_count=1,
        trader_api_retry_backoff=0.0,
        discord_guild_id=0,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_raises_when_bot_task_exits_without_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bot_module, "load_settings", lambda: _settings())
    monkeypatch.setattr(bot_module, "setup_logging", lambda _cfg: None)
    monkeypatch.setattr(bot_module, "TraderAPIClient", lambda **_kwargs: _FakeAPIClient())
    monkeypatch.setattr(bot_module, "RemoteBot", _FakeBot)
    monkeypatch.setattr(bot_module.signal, "signal", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="discord_bot_task_exited_unexpectedly"):
        await bot_module.run()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_raises_when_bot_task_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_module, "load_settings", lambda: _settings())
    monkeypatch.setattr(bot_module, "setup_logging", lambda _cfg: None)
    monkeypatch.setattr(bot_module, "TraderAPIClient", lambda **_kwargs: _FakeAPIClient())
    monkeypatch.setattr(bot_module, "RemoteBot", _FailingBot)
    monkeypatch.setattr(bot_module.signal, "signal", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="discord_bot_task_failed"):
        await bot_module.run()
