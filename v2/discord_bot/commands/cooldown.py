from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from v2.discord_bot.services.api_client import APIError
from v2.discord_bot.services.contracts import TraderAPI
from v2.discord_bot.services.discord_utils import fmt_json as _fmt_json
from v2.discord_bot.services.discord_utils import is_admin as _is_admin
from v2.discord_bot.services.discord_utils import safe_defer as _safe_defer
from v2.discord_bot.services.discord_utils import safe_send_ephemeral as _safe_send_ephemeral


class CooldownControl(commands.Cog):
    def __init__(self, bot: commands.Bot, api: TraderAPI) -> None:
        self.bot: commands.Bot = bot
        self.api: TraderAPI = api

    @app_commands.command(name="cooldown_clear", description="쿨다운/연속손실 카운터 즉시 해제")
    async def cooldown_clear(self, interaction: discord.Interaction) -> None:
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
            payload = await self.api.clear_cooldown()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)
