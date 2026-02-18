from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import discord
import pytest

from apps.discord_bot.views.panel import (
    AdvancedPanelView,
    MarginBudgetModal,
    PanelView,
    RiskAdvancedModal,
    RiskBasicModal,
    _build_embed,
)
from apps.discord_bot.ui_labels import (
    EXEC_MODE_SELECT_PLACEHOLDER,
    ADVANCED_PANEL_BUTTON_LABELS,
    MARGIN_BUDGET_BUTTON_LABEL,
    SCHEDULER_INTERVAL_SELECT_PLACEHOLDER,
    SIMPLE_PANEL_BUTTON_LABELS,
    SIMPLE_TOGGLE_LABEL,
)


class _FakeResponse:
    def __init__(self) -> None:
        self._done = False
        self.messages: List[str] = []
        self.edits: List[Dict[str, Any]] = []
        self.modal: discord.ui.Modal | None = None

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

    async def edit_message(self, **kwargs: Any) -> None:
        self._done = True
        self.edits.append(kwargs)


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



def _find_button(view: discord.ui.View, label: str) -> discord.ui.Button:
    for item in view.children:
        if isinstance(item, discord.ui.Button) and str(item.label) == label:
            return item
    raise AssertionError(f"button not found: {label}")


def _find_select(view: discord.ui.View, placeholder: str) -> discord.ui.Select:
    for item in view.children:
        if isinstance(item, discord.ui.Select) and str(item.placeholder) == placeholder:
            return item
    raise AssertionError(f"select not found: {placeholder}")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_simple_panel_shows_core_buttons_only(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    buttons = [str(item.label) for item in view.children if isinstance(item, discord.ui.Button)]

    assert set(buttons) == set(SIMPLE_PANEL_BUTTON_LABELS)
    assert not any(
        str(item.placeholder) == EXEC_MODE_SELECT_PLACEHOLDER for item in view.children if isinstance(item, discord.ui.Select)
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_simple_buttons_call_api_and_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        panic=AsyncMock(),
        tick_scheduler_now=AsyncMock(return_value={"snapshot": {"last_action": "hold", "last_error": None}}),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[0]).callback(it)
    api.start.assert_awaited_once()

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[1]).callback(it)
    api.stop.assert_awaited_once()

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[2]).callback(it)
    api.panic.assert_awaited_once()

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)
    api.tick_scheduler_now.assert_awaited_once()

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[5]).callback(it)
    assert any(isinstance(item.get("view"), AdvancedPanelView) for item in it.response.edits)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_simple_embed_description_is_korean() -> None:
    em = _build_embed({"engine_state": {"state": "RUNNING"}}, mode="simple")
    for label in SIMPLE_PANEL_BUTTON_LABELS[:3]:
        assert label in str(em.description)
    assert any("엔진 상태" in str(field.name) for field in em.fields)
    assert any("현재 증거금" in str(field.value) or "운영 주기" in str(field.value) for field in em.fields)
    assert any(str(field.name) == "마지막 결과" and str(field.value).startswith("마지막 판단:") for field in em.fields)

@pytest.mark.unit
@pytest.mark.asyncio
async def test_simple_margin_modal_and_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        set_config=AsyncMock(),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: True)

    open_it = _FakeInteraction()
    await _find_button(view, MARGIN_BUDGET_BUTTON_LABEL).callback(open_it)
    assert isinstance(open_it.response.modal, MarginBudgetModal)

    modal = open_it.response.modal
    assert isinstance(modal, MarginBudgetModal)
    modal.amount_usdt._value = "100"  # type: ignore[attr-defined]

    submit_it = _FakeInteraction()
    await modal.on_submit(submit_it)  # type: ignore[arg-type]
    api.set_config.assert_awaited_once_with({"capital_mode": "MARGIN_BUDGET_USDT", "margin_budget_usdt": 100.0})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_advanced_panel_has_risk_and_trailing_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
        set_value=AsyncMock(),
        set_scheduler_interval=AsyncMock(),
    )
    view = AdvancedPanelView(api=api)  # type: ignore[arg-type]
    buttons = [str(item.label) for item in view.children if isinstance(item, discord.ui.Button)]

    assert set(ADVANCED_PANEL_BUTTON_LABELS) <= set(buttons)
    assert SIMPLE_TOGGLE_LABEL not in SIMPLE_PANEL_BUTTON_LABELS and SIMPLE_TOGGLE_LABEL in buttons
    assert any(isinstance(item, discord.ui.Select) and str(item.placeholder) == EXEC_MODE_SELECT_PLACEHOLDER for item in view.children)
    assert any(isinstance(item, discord.ui.Select) and str(item.placeholder) == SCHEDULER_INTERVAL_SELECT_PLACEHOLDER for item in view.children)

    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: True)
    it = _FakeInteraction()
    exec_select = _find_select(view, EXEC_MODE_SELECT_PLACEHOLDER)
    exec_select._values = ["MARKET"]  # type: ignore[attr-defined]
    await exec_select.callback(it)
    api.set_value.assert_awaited_once_with("exec_mode_default", "MARKET")

    api.set_scheduler_interval.reset_mock()
    it2 = _FakeInteraction()
    interval_select = _find_select(view, SCHEDULER_INTERVAL_SELECT_PLACEHOLDER)
    interval_select._values = ["600"]  # type: ignore[attr-defined]
    await interval_select.callback(it2)
    api.set_scheduler_interval.assert_awaited_once_with(600.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_interval_options_include_5_10_15_and_60_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
        set_scheduler_interval=AsyncMock(),
    )
    view = AdvancedPanelView(api=api)  # type: ignore[arg-type]
    interval_select = _find_select(view, SCHEDULER_INTERVAL_SELECT_PLACEHOLDER)
    assert [o.label for o in interval_select.options] == ["5분", "10분", "15분", "30분", "60분"]

    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: True)
    it = _FakeInteraction()
    interval_select._values = ["300"]  # type: ignore[attr-defined]
    await interval_select.callback(it)
    assert api.set_scheduler_interval.await_count == 1
    assert any("판단 주기를" in m and "상태 알림도 같은 주기로" in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_risk_modals_submit_values(monkeypatch: pytest.MonkeyPatch) -> None:
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

    assert api.set_value.await_count >= 8


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_embed_shows_failure_reason() -> None:
    payload = {
        "engine_state": {"state": "RUNNING"},
        "scheduler": {
            "last_action": "enter:BTCUSDT:LONG",
            "last_error": "engine_in_panic",
        },
        "config": {
            "capital_mode": "MARGIN_BUDGET_USDT",
            "margin_budget_usdt": 32,
        },
        "capital_snapshot": {
            "budget_usdt": 32,
            "notional_usdt": 32,
        },
    }
    em = _build_embed(payload, mode="simple")
    text = " ".join(str(v) for field in em.fields for v in [field.value, field.name])
    assert "BLOCKED - 사유:" in text
    assert "현재 증거금 32.0000 USDT" in text

@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_embed_shows_human_reason_for_known_code() -> None:
    payload = {
        "engine_state": {"state": "RUNNING"},
        "scheduler": {
            "last_action": "hold",
            "last_error": "vol_shock_no_entry",
        },
        "config": {
            "capital_mode": "MARGIN_BUDGET_USDT",
            "margin_budget_usdt": 10,
        },
        "capital_snapshot": {
            "budget_usdt": 10,
            "notional_usdt": 10,
        },
    }
    em = _build_embed(payload, mode="simple")
    text = " ".join(str(v) for field in em.fields for v in [field.value, field.name])
    assert "BLOCKED - 사유:" in text
    assert "변동성 급등 구간이라 신규 진입이 보류됩니다." in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_shows_human_reason_in_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(
            return_value={
                "snapshot": {
                    "last_action": "hold",
                    "last_error": "daily_loss_limit_reached:-0.30<= -0.20",
                },
            }
        ),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("apps.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)
    assert any("즉시 판단: hold" in m and "BLOCKED - 사유:" in m for m in it.followup.messages)

