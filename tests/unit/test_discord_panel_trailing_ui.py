from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import discord
import pytest

from apps.discord_bot.views.panel import ADMIN_ONLY_MSG, PanelView, TrailingConfigModal


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
        self.channel = None


def _find_button(view: PanelView, label: str) -> discord.ui.Button:
    for item in view.children:
        if isinstance(item, discord.ui.Button) and str(item.label) == label:
            return item
    raise AssertionError(f"button not found: {label}")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trailing_button_exists() -> None:
    api = SimpleNamespace(get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}))
    view = PanelView(api=api)  # type: ignore[arg-type]
    _find_button(view, "트레일링설정")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trailing_modal_submit_calls_set_config(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        set_config=AsyncMock(),
        get_status=AsyncMock(
            return_value={
                "engine_state": {"state": "RUNNING"},
                "risk_config": {
                    "trailing_enabled": True,
                    "trailing_mode": "PCT",
                    "trail_arm_pnl_pct": 1.2,
                    "trail_distance_pnl_pct": 0.8,
                    "trail_grace_minutes": 30,
                    "atr_trail_timeframe": "1h",
                    "atr_trail_k": 2.0,
                    "atr_trail_min_pct": 0.6,
                    "atr_trail_max_pct": 1.8,
                },
            }
        ),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: True)

    it_click = _FakeInteraction()
    await _find_button(view, "트레일링설정").callback(it_click)
    assert isinstance(it_click.response.modal, TrailingConfigModal)

    modal = it_click.response.modal
    assert isinstance(modal, TrailingConfigModal)
    modal.trailing_enabled._value = "yes"  # type: ignore[attr-defined]
    modal.trailing_mode._value = "ATR"  # type: ignore[attr-defined]
    modal.trail_arm_pnl_pct._value = "1.5"  # type: ignore[attr-defined]
    modal.trail_grace_minutes._value = "15"  # type: ignore[attr-defined]
    modal.mode_params._value = "4h,2.5,0.7,1.9"  # type: ignore[attr-defined]

    it_submit = _FakeInteraction()
    await modal.on_submit(it_submit)  # type: ignore[arg-type]
    api.set_config.assert_awaited_once_with(
        {
            "trailing_enabled": True,
            "trailing_mode": "ATR",
            "trail_arm_pnl_pct": 1.5,
            "trail_grace_minutes": 15,
            "atr_trail_timeframe": "4h",
            "atr_trail_k": 2.5,
            "atr_trail_min_pct": 0.7,
            "atr_trail_max_pct": 1.9,
        }
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trailing_permission_gate_denies_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        set_config=AsyncMock(),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}, "risk_config": {}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: False)

    it = _FakeInteraction()
    await _find_button(view, "트레일링설정").callback(it)

    assert any(ADMIN_ONLY_MSG in m for m in it.response.messages + it.followup.messages)
    api.set_config.assert_not_awaited()
