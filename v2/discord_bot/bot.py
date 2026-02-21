from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import discord
from discord.ext import commands

from v2.common.logging_setup import LoggingConfig, setup_logging
from v2.discord_bot.commands import setup_commands
from v2.discord_bot.config import load_settings
from v2.discord_bot.services.api_client import TraderAPIClient
from v2.discord_bot.services.contracts import TraderAPI

logger = logging.getLogger(__name__)


class RemoteBot(commands.Bot):
    def __init__(
        self,
        *,
        api: TraderAPI,
        guild_id: int = 0,
    ) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.api = api
        self.guild_id = guild_id

    async def setup_hook(self) -> None:
        await setup_commands(self, self.api)

    async def on_ready(self) -> None:
        # Sync app commands.
        try:
            if self.guild_id:
                guild = discord.Object(id=self.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("discord_commands_synced_guild", extra={"count": len(synced), "guild_id": self.guild_id})
            else:
                synced = await self.tree.sync()
                logger.info("discord_commands_synced_global", extra={"count": len(synced)})
        except Exception:
            logger.exception("discord_command_sync_failed")

        logger.info("discord_ready", extra={"user": str(self.user) if self.user else "unknown"})

    async def close(self) -> None:
        try:
            await self.api.aclose()
        except Exception as e:  # noqa: BLE001
            logger.warning("discord_api_close_failed", extra={"err": type(e).__name__}, exc_info=True)
        await super().close()


async def run() -> None:
    settings = load_settings()
    setup_logging(LoggingConfig(level=settings.log_level, log_dir=settings.log_dir, json=settings.log_json, component="bot"))

    if not settings.discord_bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN is missing in .env")

    api = TraderAPIClient(
        base_url=settings.trader_api_base_url,
        timeout_sec=settings.trader_api_timeout_sec,
        retry_count=settings.trader_api_retry_count,
        retry_backoff=settings.trader_api_retry_backoff,
    )

    bot = RemoteBot(api=api, guild_id=settings.discord_guild_id)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def _shutdown(reason: str) -> None:
        # Idempotent shutdown: multiple signals can arrive.
        if stop_event.is_set():
            return
        logger.info("discord_shutdown_requested", extra={"reason": reason})
        stop_event.set()
        with contextlib.suppress(Exception):
            await bot.close()

    def _handle_signal(sig: int, _frame: object | None = None) -> None:
        # Avoid raising KeyboardInterrupt; request an orderly shutdown instead.
        try:
            signame = signal.Signals(sig).name
        except (TypeError, ValueError):
            signame = str(sig)
        loop.call_soon_threadsafe(lambda: asyncio.create_task(_shutdown(signame)))

    # Windows doesn't support asyncio's add_signal_handler() reliably.
    for s in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if s is None:
            continue
        try:
            signal.signal(s, _handle_signal)
        except (OSError, RuntimeError, ValueError):
            # Best-effort: fall back to default handler if we can't set it.
            pass

    bot_task = asyncio.create_task(bot.start(settings.discord_bot_token))
    await stop_event.wait()

    if not bot_task.done():
        bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot_task


def main() -> int:
    try:
        asyncio.run(run())
        return 0
    except KeyboardInterrupt:
        # Should be rare now (we install a SIGINT handler), but keep it quiet.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
