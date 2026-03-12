from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator
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
    discord_bot_token: str = Field(
        default="", validation_alias=AliasChoices("DISCORD_BOT_TOKEN", "DISCORD_TOKEN")
    )

    trader_api_base_url: str = Field(default="http://127.0.0.1:8101")
    trader_api_timeout_sec: float = Field(
        default=20.0,
        validation_alias=AliasChoices("TRADER_API_TIMEOUT_SEC", "trader_api_timeout_sec"),
    )
    trader_api_retry_count: int = Field(default=3)
    trader_api_retry_backoff: float = Field(default=0.25)

    discord_guild_id: int = Field(default=0)

    @field_validator("trader_api_base_url", mode="before")
    @classmethod
    def _normalize_local_trader_api_base_url(cls, value: object) -> object:
        text = str(value).strip()
        if not text:
            return value
        parsed = urlsplit(text)
        host = (parsed.hostname or "").strip().lower()
        if host not in {"localhost", "0.0.0.0", "::", "[::]"}:
            return text

        port = parsed.port
        default_port = 443 if parsed.scheme == "https" else 80
        normalized_netloc = f"127.0.0.1:{port or default_port}"
        return urlunsplit(
            (
                parsed.scheme or "http",
                normalized_netloc,
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )


def load_settings() -> DiscordBotSettings:
    return DiscordBotSettings()
