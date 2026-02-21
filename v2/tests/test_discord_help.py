from __future__ import annotations

# pyright: reportArgumentType=false, reportCallIssue=false
from types import SimpleNamespace
from typing import List

import discord
import pytest

from v2.discord_bot.commands.base import RemoteControl, _build_help_embed
from v2.discord_bot.ui_labels import ADVANCED_PANEL_BUTTON_LABELS, SIMPLE_PANEL_BUTTON_LABELS


class _FakeResponse:
    def __init__(self) -> None:
        self._done = False
        self.messages: List[str] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = True, thinking: bool = True) -> None:
        self._done = True

    async def send_message(self, content: str, *, ephemeral: bool = True, embed: discord.Embed | None = None) -> None:
        self._done = True
        if content:
            self.messages.append(content)
        if embed is not None:
            self.messages.append(str(embed.title))


class _FakeFollowup:
    def __init__(self) -> None:
        self.contents: List[str | None] = []
        self.embeds: List[discord.Embed] = []

    async def send(self, content: str | None = None, *, embed: discord.Embed | None = None, ephemeral: bool = True) -> None:
        self.contents.append(content)
        if embed is not None:
            self.embeds.append(embed)


class _FakeInteraction:
    def __init__(self) -> None:
        self.user = object()
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_help_command_shows_beginner_guide(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace()
    cog = RemoteControl(bot=object(), api=api)  # type: ignore[arg-type]

    monkeypatch.setattr("v2.discord_bot.commands.base._is_admin", lambda _i: False)

    it = _FakeInteraction()
    await cog.help.callback(cog, it)  # type: ignore[attr-defined]

    assert it.followup.embeds, "help should return embed"
    em = it.followup.embeds[0]
    all_text = "\n".join([str(em.title), str(em.description), "".join(str(v) for f in em.fields for v in [f.name, f.value])])
    assert "초보자용 도움말" in str(em.title)
    assert "/panel" in all_text
    assert "관리자만 사용 가능합니다" in all_text
    for label in SIMPLE_PANEL_BUTTON_LABELS:
        assert f"`{label}`" in all_text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_help_command_shows_admin_features(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace()
    cog = RemoteControl(bot=object(), api=api)  # type: ignore[arg-type]

    monkeypatch.setattr("v2.discord_bot.commands.base._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await cog.help.callback(cog, it)  # type: ignore[attr-defined]

    em = it.followup.embeds[0]
    all_text = "".join(str(v) for f in em.fields for v in [f.name, f.value])
    assert "/risk" in all_text
    assert "/set" in all_text
    assert "고급설정" in all_text
    for label in ADVANCED_PANEL_BUTTON_LABELS:
        assert f"`{label}`" in all_text


def test_help_embed_message_is_readable() -> None:
    em = _build_help_embed(is_admin=False)
    assert em.title == "초보자용 도움말"
    assert em.description
    assert len(em.fields) >= 3
