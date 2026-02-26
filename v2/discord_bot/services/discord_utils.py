"""Shared Discord helpers used by command and view modules.

Keeping these helpers in one place avoids duplicated interaction handling and
authorization checks across commands/views.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import discord

logger = logging.getLogger(__name__)


async def safe_defer(interaction: discord.Interaction) -> bool:
    """Safely defer interaction with fallback notification handling.

    Returns ``True`` if the interaction can continue to be handled via follow-up
    messages, ``False`` when the token is already expired.
    """

    try:
        _ = await interaction.response.defer(thinking=True)
        return True
    except discord.InteractionResponded:
        return True
    except discord.NotFound:
        # The interaction token has likely expired. Keep behavior compatible by
        # attempting a best-effort channel notification.
        try:
            cmd = getattr(getattr(interaction, "command", None), "name", None)
            created = getattr(interaction, "created_at", None)
            logger.warning(
                "discord_unknown_interaction", extra={"command": cmd, "created_at": str(created)}
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "discord_unknown_interaction_log_failed",
                extra={"err": type(e).__name__},
                exc_info=True,
            )

        try:
            ch = interaction.channel
            if isinstance(ch, discord.abc.Messageable):
                _ = await ch.send(
                    "명령 처리 중 세션이 만료되었습니다. 이 메시지는 임시 알림입니다."
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "discord_unknown_interaction_notify_failed",
                extra={"err": type(e).__name__},
                exc_info=True,
            )
        return False
    except discord.HTTPException as e:
        logger.warning(
            "discord_interaction_defer_http_failed",
            extra={"err": type(e).__name__},
            exc_info=True,
        )
        _ = await safe_send_ephemeral(
            interaction,
            "명령 응답에 실패했습니다. 잠시 후 다시 시도해주세요.",
        )
        return False
    except Exception:  # noqa: BLE001
        logger.exception("discord_interaction_defer_failed")
        _ = await safe_send_ephemeral(
            interaction,
            "명령 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
        )
        return False


async def safe_send_ephemeral(interaction: object, content: str) -> bool:
    interaction_any: Any = interaction
    try:
        if interaction_any.response.is_done():
            _ = await interaction_any.followup.send(content, ephemeral=True)
        else:
            _ = await interaction_any.response.send_message(content, ephemeral=True)
        return True
    except discord.NotFound:
        logger.warning("discord_interaction_reply_not_found")
        return False
    except discord.HTTPException as e:
        logger.warning(
            "discord_interaction_reply_http_failed",
            extra={"err": type(e).__name__},
            exc_info=True,
        )
        return False
    except Exception:  # noqa: BLE001
        logger.exception("discord_interaction_reply_failed")
        return False


def is_admin(interaction: discord.Interaction) -> bool:
    """Return whether the user is a guild administrator."""

    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    return bool(user.guild_permissions.administrator)


def fmt_json(payload: object, *, limit: int = 1900) -> str:
    """Serialize payload to JSON for compact interaction responses."""

    try:
        s = json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True)
    except (TypeError, ValueError, OverflowError, RecursionError):
        s = str(payload)
    if len(s) > limit:
        return s[: limit - 3] + "..."
    return s
