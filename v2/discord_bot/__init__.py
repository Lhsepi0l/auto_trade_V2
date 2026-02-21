from v2.discord_bot.client import APIError, TraderAPIClient
from v2.discord_bot.config import DiscordBotSettings, load_settings

__all__ = [
    "APIError",
    "DiscordBotSettings",
    "TraderAPIClient",
    "load_settings",
    "RemoteBot",
]


def __getattr__(name: str) -> object:
    if name == "RemoteBot":
        from v2.discord_bot.bot import RemoteBot

        return RemoteBot
    raise AttributeError(name)
