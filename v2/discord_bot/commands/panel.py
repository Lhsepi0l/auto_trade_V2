from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from v2.discord_bot.services.api_client import APIError
from v2.discord_bot.services.contracts import TraderAPI
from v2.discord_bot.services.discord_utils import is_admin as _is_admin
from v2.discord_bot.services.discord_utils import safe_defer as _safe_defer
from v2.discord_bot.services.discord_utils import safe_send_ephemeral as _safe_send_ephemeral
from v2.discord_bot.views.panel import PanelView, build_embed

logger = logging.getLogger(__name__)


class PanelControl(commands.Cog):
    def __init__(self, bot: commands.Bot, api: TraderAPI) -> None:
        self.bot: commands.Bot = bot
        self.api: TraderAPI = api

    @app_commands.command(name="panel", description="운영 패널 열기 (초보자용)")
    async def panel(self, interaction: discord.Interaction) -> None:
        logger.info("panel_command_received")
        if not await _safe_defer(interaction):
            _ = await _safe_send_ephemeral(
                interaction,
                "명령 응답에 실패했습니다. 잠시 후 다시 시도해주세요.",
            )
            return
        if not _is_admin(interaction):
            await interaction.followup.send("관리자 권한이 없습니다.", ephemeral=True)
            return

        try:
            logger.info("panel_command_status_fetch_started")
            payload = await self.api.get_status()
            logger.info("panel_command_status_fetch_completed")
            embed = build_embed(payload)
            view = PanelView(api=self.api, message_id=None, initial_payload=payload)
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
