from __future__ import annotations

import json
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from apps.discord_bot.services.api_client import APIError, TraderAPIClient


def _fmt_json(payload: Any) -> str:
    try:
        s = json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True)
    except Exception:
        s = str(payload)
    if len(s) > 1900:
        s = s[:1897] + "..."
    return s


class CooldownControl(commands.Cog):
    def __init__(self, bot: commands.Bot, api: TraderAPIClient) -> None:
        self.bot = bot
        self.api = api

    @app_commands.command(name="cooldown_clear", description="쿨다운/연속손실 카운터 즉시 해제")
    async def cooldown_clear(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer(thinking=True)
        except Exception:
            pass
        try:
            payload = await self.api.clear_cooldown()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

