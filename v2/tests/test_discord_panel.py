from __future__ import annotations

# pyright: reportArgumentType=false, reportMissingTypeArgument=false
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import discord
import pytest

from v2.discord_bot.services.api_client import APIError
from v2.discord_bot.services.formatting import format_status_payload
from v2.discord_bot.ui_labels import (
    ADVANCED_PANEL_BUTTON_LABELS,
    EXEC_MODE_SELECT_PLACEHOLDER,
    MARGIN_BUDGET_BUTTON_LABEL,
    SCHEDULER_INTERVAL_SELECT_PLACEHOLDER,
    SIMPLE_PANEL_BUTTON_LABELS,
    SIMPLE_TOGGLE_LABEL,
)
from v2.discord_bot.views.panel import (
    AdvancedPanelView,
    MarginBudgetModal,
    PanelView,
    RiskAdvancedModal,
    RiskBasicModal,
    ScoringSetupModal,
    _build_embed,
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
        str(item.placeholder) == EXEC_MODE_SELECT_PLACEHOLDER
        for item in view.children
        if isinstance(item, discord.ui.Select)
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_simple_buttons_call_api_and_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        start=AsyncMock(),
        stop=AsyncMock(),
        panic=AsyncMock(),
        tick_scheduler_now=AsyncMock(
            return_value={"snapshot": {"last_action": "hold", "last_error": None}}
        ),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[0]).callback(it)  # type: ignore[arg-type]
    api.start.assert_awaited_once()

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[1]).callback(it)  # type: ignore[arg-type]
    api.stop.assert_awaited_once()

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[2]).callback(it)  # type: ignore[arg-type]
    api.panic.assert_awaited_once()

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    api.tick_scheduler_now.assert_awaited_once()

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[5]).callback(it)  # type: ignore[arg-type]
    assert any(isinstance(item.get("view"), AdvancedPanelView) for item in it.response.edits)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_simple_embed_description_is_korean() -> None:
    em = _build_embed({"engine_state": {"state": "RUNNING"}}, mode="simple")
    for label in SIMPLE_PANEL_BUTTON_LABELS[:3]:
        assert label in str(em.description)
    assert any("엔진 상태" in str(field.name) for field in em.fields)
    assert any(
        "현재 증거금" in str(field.name)
        or "설정 기준" in str(field.value)
        or "운영 주기" in str(field.value)
        for field in em.fields
    )
    assert any(
        str(field.name) == "마지막 결과" and str(field.value).startswith("마지막 판단:")
        for field in em.fields
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_simple_margin_modal_and_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        set_config=AsyncMock(),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    open_it = _FakeInteraction()
    await _find_button(view, MARGIN_BUDGET_BUTTON_LABEL).callback(open_it)  # type: ignore[arg-type]
    assert isinstance(open_it.response.modal, MarginBudgetModal)

    modal = open_it.response.modal
    assert isinstance(modal, MarginBudgetModal)
    modal.amount_usdt._value = "100"  # type: ignore[attr-defined]

    submit_it = _FakeInteraction()
    await modal.on_submit(submit_it)  # type: ignore[arg-type]
    api.set_config.assert_awaited_once_with(
        {"capital_mode": "MARGIN_BUDGET_USDT", "margin_budget_usdt": 100.0}
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_advanced_panel_has_risk_and_trailing_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = SimpleNamespace(
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
        set_value=AsyncMock(),
        set_scheduler_interval=AsyncMock(),
    )
    view = AdvancedPanelView(  # type: ignore[arg-type]
        api=api,
        initial_payload={"risk_config": {"exec_mode_default": "MARKET", "scheduler_tick_sec": 600}},
    )
    buttons = [str(item.label) for item in view.children if isinstance(item, discord.ui.Button)]

    assert set(ADVANCED_PANEL_BUTTON_LABELS) <= set(buttons)
    assert SIMPLE_TOGGLE_LABEL not in SIMPLE_PANEL_BUTTON_LABELS and SIMPLE_TOGGLE_LABEL in buttons
    assert any(
        isinstance(item, discord.ui.Select)
        and str(item.placeholder) == EXEC_MODE_SELECT_PLACEHOLDER
        for item in view.children
    )
    assert any(
        isinstance(item, discord.ui.Select)
        and str(item.placeholder) == SCHEDULER_INTERVAL_SELECT_PLACEHOLDER
        for item in view.children
    )

    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)
    it = _FakeInteraction()
    exec_select = _find_select(view, EXEC_MODE_SELECT_PLACEHOLDER)
    defaults = {str(option.value): bool(option.default) for option in exec_select.options}
    assert defaults == {"LIMIT": False, "MARKET": True, "SPLIT": False}
    exec_select._values = ["MARKET"]  # type: ignore[attr-defined]
    await exec_select.callback(it)  # type: ignore[arg-type]
    api.set_value.assert_awaited_once_with("exec_mode_default", "MARKET")

    api.set_scheduler_interval.reset_mock()
    it2 = _FakeInteraction()
    interval_select = _find_select(view, SCHEDULER_INTERVAL_SELECT_PLACEHOLDER)
    interval_select._values = ["600"]  # type: ignore[attr-defined]
    await interval_select.callback(it2)  # type: ignore[arg-type]
    api.set_scheduler_interval.assert_awaited_once_with(600.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_interval_options_include_5_10_15_and_60_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = SimpleNamespace(
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
        set_scheduler_interval=AsyncMock(),
    )
    view = AdvancedPanelView(api=api)  # type: ignore[arg-type]
    interval_select = _find_select(view, SCHEDULER_INTERVAL_SELECT_PLACEHOLDER)
    assert [o.label for o in interval_select.options] == ["5분", "10분", "15분", "30분", "60분"]

    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)
    it = _FakeInteraction()
    interval_select._values = ["300"]  # type: ignore[attr-defined]
    await interval_select.callback(it)  # type: ignore[arg-type]
    assert api.set_scheduler_interval.await_count == 1
    assert any("판단 주기를" in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_risk_modals_submit_values(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        set_value=AsyncMock(),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)
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
async def test_scoring_setup_modal_includes_momentum_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = SimpleNamespace(set_value=AsyncMock())
    view = SimpleNamespace(refresh_message=AsyncMock())
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    modal = ScoringSetupModal(
        api=api,
        view=view,  # type: ignore[arg-type]
        defaults={
            "score_conf_threshold": 0.6,
            "score_gap_threshold": 0.15,
            "donchian_momentum_filter": "false",
            "donchian_fast_ema_period": "8",
            "donchian_slow_ema_period": "21",
        },
    )

    assert str(modal.donchian_momentum_filter.default) == "아니오"
    assert str(modal.donchian_momentum_ema.default) == "8,21"
    assert len(modal.children) <= 5

    modal.tf_weights._value = "10m=0.25,15m=0.10,30m=0.25,1h=0.25,4h=0.15"  # type: ignore[attr-defined]
    modal.score_conf_threshold._value = "0.61"  # type: ignore[attr-defined]
    modal.score_gap_threshold._value = "0.14"  # type: ignore[attr-defined]
    modal.donchian_momentum_filter._value = "예"  # type: ignore[attr-defined]
    modal.donchian_momentum_ema._value = "8,21"  # type: ignore[attr-defined]

    it = _FakeInteraction()
    await modal.on_submit(it)  # type: ignore[arg-type]

    submitted_pairs = {
        (str(call.args[0]), str(call.args[1])) for call in api.set_value.await_args_list
    }
    assert ("score_tf_15m_enabled", "True") in submitted_pairs
    assert ("donchian_momentum_filter", "True") in submitted_pairs
    assert ("donchian_fast_ema_period", "8") in submitted_pairs
    assert ("donchian_slow_ema_period", "21") in submitted_pairs
    assert any("판단식 설정 완료!" in msg for msg in it.followup.messages)


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
    assert "차단 - 사유:" in text
    assert "설정 기준 32.0000 USDT" in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_embed_shows_live_available_margin_and_config_budget() -> None:
    payload = {
        "engine_state": {"state": "RUNNING"},
        "scheduler": {
            "last_action": "hold",
            "last_error": None,
        },
        "config": {
            "capital_mode": "MARGIN_BUDGET_USDT",
            "margin_budget_usdt": 32,
        },
        "capital_snapshot": {
            "budget_usdt": 32,
            "notional_usdt": 32,
        },
        "binance": {
            "usdt_balance": {
                "available": 123.45,
                "wallet": 130.12,
                "source": "exchange",
            }
        },
    }
    em = _build_embed(payload, mode="simple")
    text = " ".join(str(v) for field in em.fields for v in [field.value, field.name])
    assert "실시간 사용가능 123.4500 USDT" in text
    assert "설정 기준 32.0000 USDT" in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_embed_keeps_config_budget_when_live_balance_is_fallback() -> None:
    payload = {
        "engine_state": {"state": "RUNNING"},
        "scheduler": {
            "last_action": "hold",
            "last_error": None,
        },
        "config": {
            "capital_mode": "MARGIN_BUDGET_USDT",
            "margin_budget_usdt": 32,
        },
        "capital_snapshot": {
            "budget_usdt": 32,
            "notional_usdt": 32,
        },
        "binance": {
            "usdt_balance": {
                "available": 123.45,
                "wallet": 130.12,
                "source": "fallback",
            }
        },
    }
    em = _build_embed(payload, mode="simple")
    text = " ".join(str(v) for field in em.fields for v in [field.value, field.name])
    assert "실시간 조회 실패" in text
    assert "설정 기준 32.0000 USDT" in text


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
    assert "차단 - 사유:" in text
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
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    assert any("즉시 판단: 대기" in m and "차단 - 사유:" in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_retries_once_when_tick_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(
            side_effect=[
                {
                    "ok": False,
                    "error": "tick_busy",
                    "snapshot": {
                        "last_action": "blocked",
                        "last_error": "tick_busy",
                        "last_decision_reason": "tick_busy",
                    },
                },
                {
                    "ok": True,
                    "snapshot": {
                        "last_action": "no_candidate",
                        "last_error": None,
                        "last_decision_reason": "no_candidate",
                    },
                },
            ]
        ),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    async def _no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr("v2.discord_bot.views.panel.asyncio.sleep", _no_wait)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]

    assert api.tick_scheduler_now.await_count == 2
    assert any("즉시 판단: 대기" in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_shows_no_candidate_korean_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(
            return_value={
                "snapshot": {
                    "last_action": "no_candidate",
                    "last_error": None,
                    "last_decision_reason": "no_candidate",
                },
            }
        ),
        get_status=AsyncMock(
            return_value={
                "engine_state": {"state": "RUNNING"},
                "binance": {"usdt_balance": {"available": 123.45, "wallet": 130.12}},
            }
        ),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    assert any(
        "즉시 판단: 대기" in m
        and "결과: 대기 - 사유:" in m
        and "현재 진입 후보가 없습니다." in m
        for m in it.followup.messages
    )
    assert any("실시간 잔고:" in m and "123.4500" in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_humanizes_no_entry_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(
            return_value={
                "snapshot": {
                    "last_action": "no_candidate",
                    "last_error": None,
                    "last_decision_reason": "no_entry:donchian",
                },
            }
        ),
        get_status=AsyncMock(
            return_value={
                "engine_state": {"state": "RUNNING"},
                "binance": {"usdt_balance": {"available": 10.0, "wallet": 10.0}},
            }
        ),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    assert any("돈치안 진입 조건 미충족" in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_humanizes_strategy_block_reason_and_action(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(
            return_value={
                "snapshot": {
                    "last_action": "no_candidate",
                    "last_error": None,
                    "last_decision_reason": "regime_adx_rising_missing",
                },
            }
        ),
        get_status=AsyncMock(
            return_value={
                "engine_state": {"state": "RUNNING"},
                "binance": {"usdt_balance": {"available": 10.0, "wallet": 10.0}},
            }
        ),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    assert any(
        "즉시 판단: 대기" in m and "레짐 ADX 상승 추세 조건 미충족" in m
        for m in it.followup.messages
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_humanizes_portfolio_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(
            return_value={
                "snapshot": {
                    "last_action": "blocked",
                    "last_error": None,
                    "last_decision_reason": "portfolio_cap_reached",
                },
            }
        ),
        get_status=AsyncMock(
            return_value={
                "engine_state": {"state": "RUNNING"},
                "binance": {"usdt_balance": {"available": 10.0, "wallet": 10.0}},
            }
        ),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    assert any("포트폴리오 최대 동시포지션 수에 도달해 신규 진입을 보류합니다." in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_shows_portfolio_slots_and_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(
            return_value={
                "snapshot": {
                    "last_action": "blocked",
                    "last_error": None,
                    "last_decision_reason": "portfolio_cap_reached",
                    "portfolio": {
                        "slots_used": 2,
                        "slots_total": 2,
                        "blocked_reasons": {
                            "portfolio_cap_reached": 1,
                            "portfolio_bucket_cap": 2,
                        },
                    },
                },
            }
        ),
        get_status=AsyncMock(
            return_value={
                "engine_state": {"state": "RUNNING"},
                "binance": {"usdt_balance": {"available": 10.0, "wallet": 10.0}},
            }
        ),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    assert any("포트폴리오 슬롯: 2/2" in m for m in it.followup.messages)
    assert any("포트폴리오 차단:" in m for m in it.followup.messages)
    assert any("같은 버킷 포지션이 이미 있어 이번 후보는 보류합니다.:2" in m for m in it.followup.messages)


@pytest.mark.unit
def test_status_formatter_humanizes_portfolio_block_counts() -> None:
    rendered = format_status_payload(
        {
            "engine_state": {"state": "RUNNING"},
            "binance": {},
            "pnl": {},
            "scheduler": {
                "last_action": "blocked",
                "last_decision_reason": "portfolio_cap_reached",
                "portfolio": {
                    "slots_used": 2,
                    "slots_total": 2,
                    "blocked_reasons": {
                        "portfolio_cap_reached": 1,
                        "portfolio_bucket_cap": 2,
                    },
                },
            },
        }
    )

    assert "포트폴리오 슬롯: 2/2" in rendered
    assert "포트폴리오 차단:" in rendered
    assert "포트폴리오 최대 동시포지션 수에 도달해 신규 진입을 보류합니다.:1" in rendered
    assert "같은 버킷 포지션이 이미 있어 후보를 보류합니다.:2" in rendered


@pytest.mark.unit
def test_status_formatter_humanizes_strategy_reason_and_action() -> None:
    rendered = format_status_payload(
        {
            "engine_state": {"state": "RUNNING"},
            "binance": {},
            "pnl": {},
            "scheduler": {
                "last_action": "no_candidate",
                "last_decision_reason": "regime_adx_rising_missing",
                "portfolio": {"slots_used": 0, "slots_total": 1},
            },
        }
    )

    assert "이번 결정: 대기 -> 레짐 ADX 상승 추세 조건 미충족" in rendered
    assert "최근 액션: 대기" in rendered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_shows_live_position_and_unrealized_pnl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(
            return_value={
                "snapshot": {
                    "last_action": "no_candidate",
                    "last_error": None,
                    "last_decision_reason": "no_candidate",
                },
            }
        ),
        get_status=AsyncMock(
            return_value={
                "engine_state": {"state": "RUNNING"},
                "binance": {
                    "positions": {
                        "BTCUSDT": {
                            "position_amt": 0.001,
                            "position_side": "LONG",
                            "unrealized_pnl": 1.25,
                        },
                        "ETHUSDT": {
                            "position_amt": -0.01,
                            "position_side": "SHORT",
                            "unrealized_pnl": -0.40,
                        },
                    },
                    "usdt_balance": {"available": 50.0, "wallet": 50.0},
                },
            }
        ),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    assert any(
        "실시간 포지션: BTCUSDT[롱]" in m and "ETHUSDT[숏]" in m for m in it.followup.messages
    )
    assert any("합계 uPnL +0.8500 USDT" in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_shows_live_balance_fetch_failure_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(
            return_value={
                "snapshot": {
                    "last_action": "no_candidate",
                    "last_error": None,
                    "last_decision_reason": "no_candidate",
                },
            }
        ),
        get_status=AsyncMock(
            return_value={
                "engine_state": {"state": "RUNNING"},
                "binance": {
                    "usdt_balance": {"available": 100.0, "wallet": 100.0, "source": "fallback"},
                    "private_error": "balance_fetch_failed",
                },
            }
        ),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    assert any("바이낸스 실시간 조회 실패" in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_shows_cached_live_balance_without_failure_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(
            return_value={
                "snapshot": {
                    "last_action": "no_candidate",
                    "last_error": None,
                    "last_decision_reason": "no_candidate",
                },
            }
        ),
        get_status=AsyncMock(
            return_value={
                "engine_state": {"state": "RUNNING"},
                "binance": {
                    "usdt_balance": {
                        "available": 100.0,
                        "wallet": 100.0,
                        "source": "exchange_cached",
                    },
                    "private_error": "balance_fetch_failed",
                },
            }
        ),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    assert any("실시간 잔고:" in m and "최근 캐시" in m for m in it.followup.messages)
    assert all("바이낸스 실시간 조회 실패" not in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_handles_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(side_effect=RuntimeError("network_error: ConnectError")),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    assert any("즉시 판단 실행 실패:" in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_maps_bracket_failure_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(
            return_value={
                "snapshot": {
                    "last_action": "executed",
                    "last_error": "bracket_failed:RuntimeError:algo_reject",
                    "last_decision_reason": "executed",
                },
            }
        ),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    assert any("브래킷 주문 생성에 실패" in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_once_handles_read_timeout_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(
        tick_scheduler_now=AsyncMock(side_effect=RuntimeError("network_error: ReadTimeout")),
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[3]).callback(it)  # type: ignore[arg-type]
    assert any("API 응답 시간이 초과" in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_button_reports_api_error_instead_of_interaction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = SimpleNamespace(
        start=AsyncMock(side_effect=APIError(status_code=503, message="Service Unavailable")),
        get_status=AsyncMock(return_value={"engine_state": {"state": "STOPPED"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    monkeypatch.setattr("v2.discord_bot.views.panel._is_admin", lambda _i: True)

    it = _FakeInteraction()
    await _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[0]).callback(it)  # type: ignore[arg-type]
    assert any("API 오류:" in m for m in it.followup.messages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_panel_view_on_error_sends_fallback_message() -> None:
    api = SimpleNamespace(
        get_status=AsyncMock(return_value={"engine_state": {"state": "RUNNING"}}),
    )
    view = PanelView(api=api)  # type: ignore[arg-type]
    it = _FakeInteraction()
    item = _find_button(view, SIMPLE_PANEL_BUTTON_LABELS[0])

    await view.on_error(it, RuntimeError("boom"), item)  # type: ignore[arg-type]

    messages = it.response.messages + it.followup.messages
    assert any("실행 실패: 내부 오류" in m for m in messages)
