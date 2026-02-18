from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from apps.discord_bot.commands.base import _is_admin, _safe_defer
from apps.discord_bot.services.api_client import APIError
from apps.discord_bot.services.contracts import TraderAPI
from apps.discord_bot.views.panel import PanelView, _build_embed

logger = logging.getLogger(__name__)


class PanelControl(commands.Cog):
    def __init__(self, bot: commands.Bot, api: TraderAPI) -> None:
        self.bot = bot
        self.api = api

    @app_commands.command(name="panel", description="운영 패널 열기 (초보자용)")
    async def panel(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        if not _is_admin(interaction):
            await interaction.followup.send("관리자 권한이 없습니다.", ephemeral=True)
            return

        try:
            payload = await self.api.get_status()
            data = payload if isinstance(payload, dict) else {}
            embed = _build_embed(data)
            view = PanelView(api=self.api, message_id=None)
            await interaction.followup.send(
                "패널을 새로고침 했습니다.",
                embed=embed,
                view=view,
            )
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            logger.exception("panel_command_failed")
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)
