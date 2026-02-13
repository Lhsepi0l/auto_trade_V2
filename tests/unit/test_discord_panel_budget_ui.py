from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import discord
import pytest

from apps.discord_bot.views.panel import BudgetCustomModal, PanelView


class _FakeResponse:
    def __init__(self) -> None:
        self._done = False
        self.modal = None
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


def _find_select(view: PanelView, placeholder: str) -> discord.ui.Select:
    for item in view.children:
        if isinstance(item, discord.ui.Select) and str(item.placeholder) == placeholder:
            return item
    raise AssertionError(f"select not found: {placeholder}")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_budget_components_exist() -> None:
    api = SimpleNamespace(get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}))
    view = PanelView(api=api)  # type: ignore[arg-type]
    _find_select(view, "예산 모드")
    _find_select(view, "예산 프리셋")
    _find_button(view, "프리셋 적용")
    _find_button(view, "직접 입력...")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_budget_preset_calls_set_config(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        set_config=AsyncMock(),
        get_status=AsyncMock(
            return_value={
                "engine_state": {"state": "RUNNING"},
                "config": {"capital_mode": "PCT_AVAILABLE", "capital_pct": 0.2, "capital_usdt": 100},
            }
        ),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    mode = _find_select(view, "예산 모드")
    mode._values = ["FIXED_USDT"]  # type: ignore[attr-defined]
    await mode.callback(it)

    it = _FakeInteraction()
    preset = _find_select(view, "예산 프리셋")
    preset._values = ["200"]  # type: ignore[attr-defined]
    await preset.callback(it)

    it = _FakeInteraction()
    await _find_button(view, "프리셋 적용").callback(it)
    api.set_config.assert_awaited_once_with({"capital_mode": "FIXED_USDT", "capital_usdt": "200"})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_budget_modal_calls_set_config(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        set_config=AsyncMock(),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: True)
    it = _FakeInteraction()

    modal = BudgetCustomModal(api=api, view=view)  # type: ignore[arg-type]
    modal.capital_pct._value = "0.2"  # type: ignore[attr-defined]
    modal.capital_usdt._value = "100"  # type: ignore[attr-defined]
    modal.margin_use_pct._value = "0.9"  # type: ignore[attr-defined]
    modal.advanced_limits._value = "500,0.5,0.002"  # type: ignore[attr-defined]
    await modal.on_submit(it)  # type: ignore[arg-type]

    api.set_config.assert_awaited_once_with(
        {
            "capital_pct": "0.2",
            "capital_usdt": "100",
            "margin_use_pct": "0.9",
            "max_position_notional_usdt": "500",
            "max_exposure_pct": "0.5",
            "fee_buffer_pct": "0.002",
        }
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_budget_permission_gate_denies_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(set_config=AsyncMock(), get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}))
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: False)
    it = _FakeInteraction()

    await _find_button(view, "프리셋 적용").callback(it)
    assert any("관리자만 조작할 수 있습니다." in m for m in (it.response.messages + it.followup.messages))
    api.set_config.assert_not_awaited()
