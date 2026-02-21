from __future__ import annotations

# pyright: reportArgumentType=false, reportMissingTypeArgument=false
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import discord
import pytest

from v2.discord_bot.ui_labels import MARGIN_BUDGET_BUTTON_LABEL
from v2.discord_bot.views.panel import ADMIN_ONLY_MSG, MarginBudgetModal, PanelView


class _FakeResponse:
    def __init__(self) -> None:
        self._done = False
        self.modal: discord.ui.Modal | None = None
        self.messages: List[str] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = True, thinking: bool = True) -> None:
        self._done = True

    async def send_message(self, content: str, *, ephemeral: bool = True) -> None:
        self._done = True
        self.messages.append(content)

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self._done = True
        self.modal = modal

    async def edit_message(self, **_kwargs: Any) -> None:
        self._done = True


class _FakeFollowup:
    def __init__(self) -> None:
        self.messages: List[str] = []

    async def send(self, content: str, *, ephemeral: bool = True) -> None:
        self.messages.append(content)


class _FakeMessage:
    def __init__(self) -> None:
        self.edits: List[Dict[str, Any]] = []

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


class _FakeInteraction:
    def __init__(self) -> None:
        self.user = object()
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.message = _FakeMessage()


def _find_button(view: PanelView, label: str) -> discord.ui.Button:
    for item in view.children:
        if isinstance(item, discord.ui.Button) and str(item.label) == label:
            return item
    raise AssertionError(f"button not found: {label}")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_margin_budget_only_button_in_simple_panel() -> None:
    api = SimpleNamespace(get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}))
    view = PanelView(api=api)  # type: ignore[arg-type]

    assert any(isinstance(item, discord.ui.Button) and str(item.label) == MARGIN_BUDGET_BUTTON_LABEL for item in view.children)
    assert not any(
        isinstance(item, discord.ui.Select) and str(item.placeholder) == "예산 모드" for item in view.children
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_admin_click_opens_modal_and_submit_calls_set_config(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        set_config=AsyncMock(),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it_click = _FakeInteraction()
    await _find_button(view, MARGIN_BUDGET_BUTTON_LABEL).callback(it_click)  # type: ignore[arg-type]
    assert isinstance(it_click.response.modal, MarginBudgetModal)

    modal = it_click.response.modal
    assert isinstance(modal, MarginBudgetModal)
    modal.amount_usdt._value = "1,000"  # type: ignore[attr-defined]

    it_submit = _FakeInteraction()
    await modal.on_submit(it_submit)  # type: ignore[arg-type]
    api.set_config.assert_awaited_once_with(
        {"capital_mode": "MARGIN_BUDGET_USDT", "margin_budget_usdt": 1000.0}
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_admin_click_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        set_config=AsyncMock(),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: False)

    it = _FakeInteraction()
    await _find_button(view, MARGIN_BUDGET_BUTTON_LABEL).callback(it)  # type: ignore[arg-type]

    assert ADMIN_ONLY_MSG in (it.response.messages + it.followup.messages)
    api.set_config.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_input_shows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        set_config=AsyncMock(),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    modal = MarginBudgetModal(api=api, view=view)  # type: ignore[arg-type]
    modal.amount_usdt._value = "abc"  # type: ignore[attr-defined]
    it = _FakeInteraction()
    await modal.on_submit(it)  # type: ignore[arg-type]

    assert any("입력 오류" in m for m in it.response.messages + it.followup.messages)
    api.set_config.assert_not_awaited()
