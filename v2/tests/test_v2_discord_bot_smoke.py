from __future__ import annotations

from unittest.mock import AsyncMock

import discord
import pytest

from v2.discord_bot.commands.base import RISK_KEYS, _build_help_embed
from v2.discord_bot.config import load_settings
from v2.discord_bot.services.contracts import JSONPayload
from v2.discord_bot.ui_labels import SIMPLE_PANEL_BUTTON_LABELS
from v2.discord_bot.views.panel import PanelView, _build_embed


class _FakeAPI:
    async def get_status(self) -> JSONPayload:
        return {"engine_state": {"state": "RUNNING"}}

    async def get_risk(self) -> JSONPayload:
        return {}

    async def start(self) -> JSONPayload:
        return {}

    async def stop(self) -> JSONPayload:
        return {}

    async def panic(self) -> JSONPayload:
        return {}

    async def tick_scheduler_now(self) -> JSONPayload:
        return {}

    async def send_daily_report(self) -> JSONPayload:
        return {}

    async def set_scheduler_interval(self, tick_sec: float) -> JSONPayload:
        _ = tick_sec
        return {}

    async def set_value(self, key: str, value: str) -> JSONPayload:
        _ = key
        _ = value
        return {}

    async def set_config(self, config: JSONPayload) -> JSONPayload:
        _ = config
        return {}

    async def set_symbol_leverage(self, symbol: str, leverage: float) -> JSONPayload:
        _ = symbol
        _ = leverage
        return {}

    async def close_position(self, symbol: str) -> JSONPayload:
        _ = symbol
        return {}

    async def close_all(self) -> JSONPayload:
        return {}

    async def clear_cooldown(self) -> JSONPayload:
        return {}

    async def preset(self, name: str) -> JSONPayload:
        _ = name
        return {}

    async def aclose(self) -> None:
        return None


def test_v2_discord_settings_default_control_api_port(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TRADER_API_BASE_URL", "http://127.0.0.1:8101")
    settings = load_settings()
    assert settings.trader_api_base_url == "http://127.0.0.1:8101"


def test_v2_help_embed_contains_core_commands() -> None:
    embed = _build_help_embed(is_admin=True)
    text = "\n".join(
        [str(embed.title), str(embed.description), "".join(str(f.value) for f in embed.fields)]
    )
    assert "초보자용 도움말" in str(embed.title)
    assert "/panel" in text
    assert "/status" in text
    assert "/risk" in text


def test_v2_set_risk_keys_include_tpsl_base_fields() -> None:
    assert "tpsl_base_take_profit_pct" in RISK_KEYS
    assert "tpsl_base_stop_loss_pct" in RISK_KEYS


@pytest.mark.asyncio
async def test_v2_panel_view_simple_buttons_render() -> None:
    api = _FakeAPI()
    api.get_status = AsyncMock(return_value={"engine_state": {"state": "RUNNING"}})
    view = PanelView(api=api)
    buttons = [str(item.label) for item in view.children if isinstance(item, discord.ui.Button)]
    assert set(SIMPLE_PANEL_BUTTON_LABELS).issubset(set(buttons))


def test_v2_panel_embed_renders() -> None:
    payload: JSONPayload = {
        "engine_state": {"state": "RUNNING"},
        "scheduler": {"last_action": "hold", "last_error": None},
        "config": {"capital_mode": "MARGIN_BUDGET_USDT", "margin_budget_usdt": 100},
        "capital_snapshot": {"budget_usdt": 100, "notional_usdt": 100},
    }
    embed = _build_embed(payload, mode="simple")
    assert embed.title is not None
    assert "오토트레이더 패널" in str(embed.title)
