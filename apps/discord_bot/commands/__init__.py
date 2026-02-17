from __future__ import annotations

from discord.ext import commands

from apps.discord_bot.services.contracts import TraderAPI


async def setup_commands(bot: commands.Bot, api: TraderAPI) -> None:
    # Import lazily to avoid circular imports with panel/view modules.
    from apps.discord_bot.commands.base import RemoteControl
    from apps.discord_bot.commands.cooldown import CooldownControl
    from apps.discord_bot.commands.panel import PanelControl

    await bot.add_cog(RemoteControl(bot, api))
    await bot.add_cog(CooldownControl(bot, api))
    await bot.add_cog(PanelControl(bot, api))
