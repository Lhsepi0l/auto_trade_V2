from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DiscordBotSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = Field(default="dev")

    log_level: str = Field(default="INFO")
    log_dir: str = Field(default="./logs")
    log_json: bool = Field(default=False)

    # Backward compatible: accept DISCORD_BOT_TOKEN (preferred) or DISCORD_TOKEN (legacy).
    discord_bot_token: str = Field(default="", validation_alias=AliasChoices("DISCORD_BOT_TOKEN", "DISCORD_TOKEN"))

    trader_api_base_url: str = Field(default="http://127.0.0.1:8000")
    trader_api_timeout_sec: float = Field(default=8.0)
    trader_api_retry_count: int = Field(default=3)
    trader_api_retry_backoff: float = Field(default=0.25)

    discord_guild_id: int = Field(default=0)


def load_settings() -> DiscordBotSettings:
    return DiscordBotSettings()
