from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import discord
import pytest

from apps.discord_bot.views.panel import BudgetCustomModal, PanelView, RiskAdvancedModal, RiskBasicModal


class _FakeResponse:
    def __init__(self) -> None:
        self._done = False
        self.modal = None

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = True, thinking: bool = True) -> None:
        self._done = True

    async def send_message(self, _content: str, *, ephemeral: bool = True) -> None:
        self._done = True

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
async def test_panel_buttons_call_api(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        panic=AsyncMock(),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, "시작").callback(it)
    api.start.assert_awaited_once()

    it = _FakeInteraction()
    await _find_button(view, "중지").callback(it)
    api.stop.assert_awaited_once()

    it = _FakeInteraction()
    await _find_button(view, "패닉").callback(it)
    api.panic.assert_awaited_once()

    it = _FakeInteraction()
    await _find_button(view, "새로고침").callback(it)
    api.get_status.assert_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_panel_selects_call_api(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        set_value=AsyncMock(),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    exec_mode = _find_select(view, "실행 모드")
    exec_mode._values = ["MARKET"]  # type: ignore[attr-defined]
    await exec_mode.callback(it)
    api.set_value.assert_awaited_with("exec_mode_default", "MARKET")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_budget_controls_apply_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        preset=AsyncMock(),
        set_value=AsyncMock(),
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
    api.set_value.assert_any_await("capital_mode", "FIXED_USDT")
    api.set_value.assert_any_await("capital_usdt", "200")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modal_submit_calls_set_value(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        set_value=AsyncMock(),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: True)
    it = _FakeInteraction()

    basic = RiskBasicModal(api=api, view=view)  # type: ignore[arg-type]
    basic.max_leverage._value = "5"  # type: ignore[attr-defined]
    basic.max_exposure_pct._value = "20%"  # type: ignore[attr-defined]
    basic.max_notional_pct._value = "50"  # type: ignore[attr-defined]
    basic.per_trade_risk_pct._value = "1"  # type: ignore[attr-defined]
    await basic.on_submit(it)  # type: ignore[arg-type]

    adv = RiskAdvancedModal(api=api, view=view)  # type: ignore[arg-type]
    adv.daily_loss_limit_pct._value = "-0.02"  # type: ignore[attr-defined]
    adv.dd_limit_pct._value = "-0.15"  # type: ignore[attr-defined]
    adv.min_hold_minutes._value = "240"  # type: ignore[attr-defined]
    adv.score_conf_threshold._value = "0.65"  # type: ignore[attr-defined]
    await adv.on_submit(it)  # type: ignore[arg-type]

    custom = BudgetCustomModal(api=api, view=view)  # type: ignore[arg-type]
    custom.capital_pct._value = "0.2"  # type: ignore[attr-defined]
    custom.capital_usdt._value = "100"  # type: ignore[attr-defined]
    custom.margin_use_pct._value = "0.9"  # type: ignore[attr-defined]
    custom.advanced_limits._value = "500,0.5,0.002"  # type: ignore[attr-defined]
    await custom.on_submit(it)  # type: ignore[arg-type]

    assert api.set_value.await_count >= 14
