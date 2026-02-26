from __future__ import annotations

from typing import List

import pytest

from v2.discord_bot.services.discord_utils import safe_send_ephemeral


class _FakeResponse:
    def __init__(self, *, done: bool = False, fail_send: bool = False) -> None:
        self._done = done
        self._fail_send = fail_send
        self.messages: List[str] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content: str, *, ephemeral: bool = True) -> None:
        _ = ephemeral
        if self._fail_send:
            raise RuntimeError("send_failed")
        self._done = True
        self.messages.append(content)


class _FakeFollowup:
    def __init__(self, *, fail_send: bool = False) -> None:
        self._fail_send = fail_send
        self.messages: List[str] = []

    async def send(self, content: str, *, ephemeral: bool = True) -> None:
        _ = ephemeral
        if self._fail_send:
            raise RuntimeError("followup_failed")
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self, *, response_done: bool = False, fail_response: bool = False) -> None:
        self.response = _FakeResponse(done=response_done, fail_send=fail_response)
        self.followup = _FakeFollowup()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_send_ephemeral_uses_response_when_not_done() -> None:
    interaction = _FakeInteraction(response_done=False)

    ok = await safe_send_ephemeral(interaction, "hello")

    assert ok is True
    assert interaction.response.messages == ["hello"]
    assert interaction.followup.messages == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_send_ephemeral_uses_followup_when_already_responded() -> None:
    interaction = _FakeInteraction(response_done=True)

    ok = await safe_send_ephemeral(interaction, "hello")

    assert ok is True
    assert interaction.response.messages == []
    assert interaction.followup.messages == ["hello"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_send_ephemeral_returns_false_when_response_send_fails() -> None:
    interaction = _FakeInteraction(response_done=False, fail_response=True)

    ok = await safe_send_ephemeral(interaction, "hello")

    assert ok is False
