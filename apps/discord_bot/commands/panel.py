from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from apps.discord_bot.services.api_client import APIError
from apps.discord_bot.services.contracts import TraderAPI
from apps.discord_bot.views.panel import PanelView, _build_embed

logger = logging.getLogger(__name__)


def _is_admin(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    return bool(user.guild_permissions.administrator)


class PanelControl(commands.Cog):
    def __init__(self, bot: commands.Bot, api: TraderAPI) -> None:
        self.bot = bot
        self.api = api
        self._panel_by_channel: dict[int, int] = {}

    @app_commands.command(name="panel", description="운영 패널 열기 (초보자용)")
    async def panel(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message("관리자 권한이 없습니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        ch = interaction.channel
        if ch is None or not hasattr(ch, "send"):
            await interaction.followup.send("현재 채널에서 패널을 생성할 수 없습니다.", ephemeral=True)
            return

        try:
            payload = await self.api.get_status()
            data = payload if isinstance(payload, dict) else {}
            embed = _build_embed(data)

            channel_id = int(ch.id)
            old_mid = self._panel_by_channel.get(channel_id)
            view = PanelView(api=self.api, message_id=old_mid)

            target_msg = None
            if old_mid:
                try:
                    target_msg = await ch.fetch_message(old_mid)
                except Exception:
                    target_msg = None

            if target_msg is not None:
                await target_msg.edit(embed=embed, view=view)
                self._panel_by_channel[channel_id] = int(target_msg.id)
            else:
                m = await ch.send(embed=embed, view=view)
                self._panel_by_channel[channel_id] = int(m.id)

            try:
                await interaction.delete_original_response()
            except Exception:
                # Keep panel UX clean even if interaction response cleanup fails.
                pass
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            logger.exception("panel_command_failed")
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)
