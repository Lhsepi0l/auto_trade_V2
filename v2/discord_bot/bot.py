from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import signal
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from v2.common.logging_setup import LoggingConfig, setup_logging
from v2.discord_bot.commands import setup_commands
from v2.discord_bot.config import load_settings
from v2.discord_bot.services.api_client import TraderAPIClient
from v2.discord_bot.services.contracts import TraderAPI
from v2.discord_bot.services.discord_utils import safe_send_ephemeral as _safe_send_ephemeral

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

        @self.tree.error
        async def _on_tree_error(
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
        ) -> None:
            logger.exception("discord_app_command_error", exc_info=error)
            _ = await _safe_send_ephemeral(
                interaction,
                "명령 실행 실패: 내부 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
            )

    async def on_ready(self) -> None:
        # Sync app commands.
        try:
            if self.guild_id:
                guild = discord.Object(id=self.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info(
                    "discord_commands_synced_guild",
                    extra={"count": len(synced), "guild_id": self.guild_id},
                )
            else:
                synced = await self.tree.sync()
                logger.info("discord_commands_synced_global", extra={"count": len(synced)})
        except Exception:
            logger.exception("discord_command_sync_failed")

        logger.info("discord_ready", extra={"user": str(self.user) if self.user else "unknown"})

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        data = interaction.data if isinstance(interaction.data, dict) else {}
        interaction_type = getattr(interaction, "type", None)
        logger.info(
            "discord_interaction_received",
            extra={
                "interaction_type": getattr(interaction_type, "name", str(interaction_type)),
                "command_name": str(data.get("name") or "") or None,
            },
        )
        super_bot: Any = super()
        handler = getattr(super_bot, "on_interaction", None)
        if callable(handler):
            result = handler(interaction)
            if inspect.isawaitable(result):
                await result

    async def close(self) -> None:
        try:
            await self.api.aclose()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "discord_api_close_failed", extra={"err": type(e).__name__}, exc_info=True
            )
        await super().close()


async def run() -> None:
    settings = load_settings()
    setup_logging(
        LoggingConfig(
            level=settings.log_level,
            log_dir=settings.log_dir,
            json=settings.log_json,
            component="bot",
        )
    )

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
    stop_wait_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            {bot_task, stop_wait_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if bot_task in done and not stop_event.is_set():
            with contextlib.suppress(Exception):
                await _shutdown("bot_task_stopped")

            if bot_task.cancelled():
                raise RuntimeError("discord_bot_task_cancelled_unexpectedly")

            exc = bot_task.exception()
            if exc is not None:
                logger.exception("discord_bot_task_failed", exc_info=exc)
                raise RuntimeError("discord_bot_task_failed") from exc
            raise RuntimeError("discord_bot_task_exited_unexpectedly")
    finally:
        if not stop_wait_task.done():
            stop_wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_wait_task

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
