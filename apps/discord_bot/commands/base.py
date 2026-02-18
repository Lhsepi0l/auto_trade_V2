from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from apps.discord_bot.services.api_client import APIError
from apps.discord_bot.services.contracts import TraderAPI
from apps.discord_bot.services.formatting import format_status_payload as _fmt_status_payload
from apps.discord_bot.ui_labels import (
    ADVANCED_PANEL_BUTTON_LABELS,
    ADVANCED_TOGGLE_LABEL,
    MARGIN_BUDGET_BUTTON_LABEL,
    SIMPLE_PANEL_BUTTON_LABELS,
)

logger = logging.getLogger(__name__)

RISK_KEYS: List[str] = [
    "per_trade_risk_pct",
    "max_exposure_pct",
    "max_notional_pct",
    "max_leverage",
    "daily_loss_limit_pct",
    "dd_limit_pct",
    "lose_streak_n",
    "cooldown_hours",
    "min_hold_minutes",
    "score_conf_threshold",
    "score_gap_threshold",
    "exec_mode_default",
    "exec_limit_timeout_sec",
    "exec_limit_retries",
    "notify_interval_sec",
    "spread_max_pct",
    "allow_market_when_wide_spread",
    "capital_mode",
    "capital_pct",
    "capital_usdt",
    "margin_budget_usdt",
    "margin_use_pct",
    "max_position_notional_usdt",
    "fee_buffer_pct",
    "universe_symbols",
    "enable_watchdog",
    "watchdog_interval_sec",
    "shock_1m_pct",
    "shock_from_entry_pct",
    "trailing_enabled",
    "trailing_mode",
    "trail_arm_pnl_pct",
    "trail_distance_pnl_pct",
    "trail_grace_minutes",
    "atr_trail_timeframe",
    "atr_trail_k",
    "atr_trail_min_pct",
    "atr_trail_max_pct",
    "tf_weight_4h",
    "tf_weight_1h",
    "tf_weight_30m",
    "vol_shock_atr_mult_threshold",
    "atr_mult_mean_window",
]

PRESETS: List[str] = ["conservative", "normal", "aggressive"]
PROFILE_KEYS: List[str] = ["recovery_safe", "balanced_20x", "aggressive_50x"]


def _profile_payload(name: str, budget_usdt: Optional[float]) -> Dict[str, str]:
    # One-shot presets for users who want quick batch tuning in Discord.
    profiles: Dict[str, Dict[str, str]] = {
        "recovery_safe": {
            "capital_mode": "MARGIN_BUDGET_USDT",
            "margin_use_pct": "0.8",
            "max_leverage": "20",
            "max_exposure_pct": "0.5",
            "max_notional_pct": "300",
            "per_trade_risk_pct": "15",
            "score_conf_threshold": "0.3",
            "score_gap_threshold": "0.15",
            "daily_loss_limit_pct": "-0.03",
            "dd_limit_pct": "-0.12",
            "cooldown_hours": "6",
        },
        "balanced_20x": {
            "capital_mode": "MARGIN_BUDGET_USDT",
            "margin_use_pct": "0.9",
            "max_leverage": "20",
            "max_exposure_pct": "null",
            "max_notional_pct": "1000",
            "per_trade_risk_pct": "50",
            "score_conf_threshold": "0.2",
            "score_gap_threshold": "0.1",
            "daily_loss_limit_pct": "-0.05",
            "dd_limit_pct": "-0.2",
            "cooldown_hours": "2",
        },
        "aggressive_50x": {
            "capital_mode": "MARGIN_BUDGET_USDT",
            "margin_use_pct": "0.9",
            "max_leverage": "50",
            "max_exposure_pct": "null",
            "max_notional_pct": "2000",
            "per_trade_risk_pct": "100",
            "score_conf_threshold": "0.1",
            "score_gap_threshold": "0.1",
            "daily_loss_limit_pct": "-0.15",
            "dd_limit_pct": "-0.35",
            "cooldown_hours": "0",
        },
    }
    payload = dict(profiles[name])
    if budget_usdt is not None:
        payload["margin_budget_usdt"] = str(float(budget_usdt))
    return payload


async def _safe_defer(interaction: discord.Interaction) -> bool:
    """Acknowledge the interaction quickly.

    Discord requires an initial response within a short deadline (~3s). If our
    event loop is busy or the user retries quickly, the interaction token can
    expire and defer() raises NotFound (10062).
    """
    try:
        await interaction.response.defer(thinking=True)
        return True
    except discord.InteractionResponded:
        return True
    except discord.NotFound:
        # Can't respond via interaction token anymore. Best-effort: post to channel.
        try:
            cmd = getattr(getattr(interaction, "command", None), "name", None)
            created = getattr(interaction, "created_at", None)
            logger.warning("discord_unknown_interaction", extra={"command": cmd, "created_at": str(created)})
        except Exception as e:  # noqa: BLE001
            logger.warning("discord_unknown_interaction_log_failed", extra={"err": type(e).__name__}, exc_info=True)
        try:
            ch = interaction.channel
            if ch is not None and hasattr(ch, "send"):
                await ch.send("?묐떟 ?쒓컙??珥덇낵?섏뿀?듬땲?? 紐낅졊?대? ?ㅼ떆 ?쒕룄??二쇱꽭??")
        except Exception as e:  # noqa: BLE001
            logger.warning("discord_unknown_interaction_notify_failed", extra={"err": type(e).__name__}, exc_info=True)
        return False


def _is_admin(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    return bool(user.guild_permissions.administrator)


def _build_help_embed(*, is_admin: bool) -> discord.Embed:
    simple_buttons = list(SIMPLE_PANEL_BUTTON_LABELS)
    beginner_buttons = " / ".join(f"`{b}`" for b in simple_buttons)
    advanced_buttons = list(ADVANCED_PANEL_BUTTON_LABELS)
    advanced_buttons_text = " / ".join(f"`{b}`" for b in advanced_buttons)
    lines = [
        "泥섏쓬 ?쒖옉?대㈃ ???쒖꽌濡쒕쭔 ?ъ슜?섏꽭??",
        "1) `/panel`濡??꾩옱 ?곹깭 ?뺤씤",
        f"2) {beginner_buttons} 踰꾪듉?쇰줈 利됱떆 議곗옉",
        "3) ?ㅻ쪟媛 ?⑤㈃ ?먯씤??癒쇱? ?쎄퀬 ?ㅼ젙???ы솗?명븯?몄슂.",
    ]
    em = discord.Embed(
        title="상태·운영 상태 안내",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    em.add_field(
        name="媛??癒쇱?",
        value=(
            "`/panel` : ?댁쁺 ?⑤꼸 ?ㅽ뵂(?꾩옱 ?곹깭, ?ㅼ쓬 ?먮떒 ?쒓컖, ?ㅽ뙣 ?ъ쑀 ?쒖떆)\n"
            "`/status` : ?띿뒪?몃줈 ?먯꽭???곹깭 ?뺤씤"
        ),
        inline=False,
    )
    em.add_field(
        name="?듭떖 踰꾪듉",
        value=(
            f"{beginner_buttons} : ?붾㈃???쒖떆?섎뒗 ?쒖꽌??`{', '.join(simple_buttons)}`\n"
            f"`{SIMPLE_PANEL_BUTTON_LABELS[0]}` : ?붿쭊 ?먮룞留ㅻℓ ?쒖옉\n"
            f"`{SIMPLE_PANEL_BUTTON_LABELS[1]}` : ?붿쭊 ?먮룞留ㅻℓ 以묒?\n"
            f"`{SIMPLE_PANEL_BUTTON_LABELS[2]}` : ?섎룞 ?뺣━ 紐⑤뱶(鍮꾩긽)\n"
            f"`{SIMPLE_PANEL_BUTTON_LABELS[3]}` : 吏湲?諛붾줈 ??踰??ㅼ틪 ?ㅽ뻾\n"
            f"`{MARGIN_BUDGET_BUTTON_LABEL}` : 二쇰Ц 湲곗? ?덉궛 議곗젙\n"
            f"`{ADVANCED_TOGGLE_LABEL}` : 由ъ뒪???몃젅?쇰쭅/?ㅽ뻾紐⑤뱶 ?뺤옣 ?ㅼ젙 ?닿린\n"
        ),
        inline=False,
    )
    em.add_field(
        name="李멸퀬",
        value=(
            "遊뉗? 罹붾뱾 李⑦듃 吏?쒕? 湲곗??쇰줈 吏꾩엯 ?꾨낫瑜?怨꾩궛???먮떒?⑸땲??\n"
            "蹂듭옟???꾨왂?앹? ???꾩슂 ?놁씠 `/panel`???ㅽ뙣 ?ъ쑀瑜??곕씪媛硫??⑸땲??"
        ),
        inline=False,
    )
    if is_admin:
        em.add_field(
            name="愿由ъ옄 ?꾩슜 怨좉툒?ㅼ젙",
            value=(
                "`/risk` : 由ъ뒪???ㅼ젙 議고쉶\n"
                "`/set` : ?⑥씪 由ъ뒪??媛?蹂寃?n"
                "`/preset` : ?꾨━???곸슜\n"
                "`/profile` : ?꾨줈???쇨큵 ?곸슜\n"
                "`/close` : ?뱀젙 ?щ낵 ?ъ????뺣━\n"
                "`/closeall` : ?꾩껜 ?ъ????뺣━\n"
                "`/cooldown_clear` : 荑⑤떎???먯떎 ?쒗븳 ?댁젣\n"
                "`/report` : 1??由ы룷??利됱떆 ?꾩넚\n"
                f"{advanced_buttons_text} : 怨좉툒 ?붾㈃?먯꽌 異붽? ?몄텧\n"
            ),
            inline=False,
        )
    else:
        em.add_field(
            name="愿由ъ옄 ?꾩슜",
            value="愿由ъ옄留??ъ슜 媛?ν빀?덈떎: `/set`, `/risk`, `/preset`, `/profile`, `/close`, `/closeall`, `/cooldown_clear`",
            inline=False,
        )
    return em


def _truncate(s: str, *, limit: int = 1800) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _fmt_json(payload: Any) -> str:
    import json

    try:
        s = json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True)
    except Exception:
        s = str(payload)
    return _truncate(s, limit=1900)


class RemoteControl(commands.Cog):
    def __init__(self, bot: commands.Bot, api: TraderAPI) -> None:
        self.bot = bot
        self.api = api

    @app_commands.command(name="status", description="?몃젅?대뜑 ?곹깭 ?붿빟 議고쉶")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.get_status()
            if not isinstance(payload, dict):
                logger.warning("discord_status_payload_invalid_type", extra={"type": type(payload).__name__})
                await interaction.followup.send(
                    f"`/status` 응답이 잘못되었습니다. 타입={type(payload).__name__}`n`/status` endpoint 또는 bot 헬스체크를 점검하세요."
                )
                return
            msg = _fmt_status_payload(payload)
            await interaction.followup.send(f"```text\n{msg}\n```")
        except APIError as e:
            await interaction.followup.send(f"API ?ㅻ쪟: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"?ㅻ쪟: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="risk", description="?꾩옱 由ъ뒪???ㅼ젙 議고쉶")
    async def risk(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.get_risk()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API ?ㅻ쪟: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"?ㅻ쪟: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="start", description="?붿쭊 ?쒖옉")
    async def start(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.start()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API ?ㅻ쪟: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"?ㅻ쪟: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="stop", description="?붿쭊 以묒?")
    async def stop(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.stop()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API ?ㅻ쪟: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"?ㅻ쪟: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="panic", description="?⑤땳 紐⑤뱶 ?꾪솚 諛?湲닿툒 ?뺣━")
    async def panic(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.panic()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API ?ㅻ쪟: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"?ㅻ쪟: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="report", description="?쇱씪 由ы룷??利됱떆 ?꾩넚")
    async def report(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.send_daily_report()
            summary = payload.get("detail", {})
            day = payload.get("day", "-")
            sent = bool(payload.get("notifier_sent"))
            err = payload.get("notifier_error")
            lines = [
                "?쇱씪 由ы룷???앹꽦 ?꾨즺",
                f"day: {day}",
                f"sent_to_discord: {sent}",
            ]
            if not sent and err:
                lines.append(f"?꾩넚 ?ㅻ쪟: {err}")
            if isinstance(summary, dict):
                lines.append(
                    (
                        f"entries={summary.get('entries', 0)} closes={summary.get('closes', 0)} "
                        f"errors={summary.get('errors', 0)} canceled={summary.get('canceled', 0)} blocks={summary.get('blocks', 0)}"
                    ).strip()
                )
            await interaction.followup.send("```text\n" + "\n".join(lines) + "\n```")
        except APIError as e:
            await interaction.followup.send(f"API ?먮윭: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"?덉쇅: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="help", description="珥덈낫?먯슜 ?ъ슜踰?蹂닿린")
    async def help(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            em = _build_help_embed(is_admin=_is_admin(interaction))
            await interaction.followup.send(embed=em, ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"?ㅻ쪟: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="close", description="?щ낵 ?⑥씪 泥?궛 (reduceOnly)")
    @app_commands.describe(symbol="?щ낵 ?덉떆: BTCUSDT")
    async def close(self, interaction: discord.Interaction, symbol: str) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.close_position(symbol.strip().upper())
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API ?ㅻ쪟: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"?ㅻ쪟: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="closeall", description="?꾩껜 ?ъ???泥?궛")
    async def closeall(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.close_all()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API ?ㅻ쪟: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"?ㅻ쪟: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="set", description="由ъ뒪???ㅼ젙 媛?蹂寃?)
    @app_commands.describe(key="由ъ뒪???ㅼ젙 ??, value="??媛?臾몄옄??")
    async def set_value(
        self,
        interaction: discord.Interaction,
        key: str,
        value: str,
    ) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            k = key.strip()
            if k not in RISK_KEYS:
                await interaction.followup.send(
                    "?섎せ??key?낅땲?? ?덉떆:\n" + ", ".join(RISK_KEYS[:15]) + ", ...",
                    ephemeral=True,
                )
                return
            payload = await self.api.set_value(k, value)
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API ?ㅻ쪟: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"?ㅻ쪟: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="preset", description="由ъ뒪???꾨━???곸슜")
    @app_commands.choices(name=[app_commands.Choice(name=p, value=p) for p in PRESETS])
    async def preset(
        self,
        interaction: discord.Interaction,
        name: app_commands.Choice[str],
    ) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.preset(name.value)
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API ?ㅻ쪟: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"?ㅻ쪟: {type(e).__name__}: {e}", ephemeral=True)


    @app_commands.command(name="profile", description="由ъ뒪???덉궛 ?꾨줈???쇨큵 ?곸슜")
    @app_commands.describe(
        name="?곸슜???꾨줈??,
        budget_usdt="利앷굅湲??덉궛(?좏깮). ?낅젰 ??margin_budget_usdt 媛숈씠 ?곸슜",
    )
    @app_commands.choices(name=[app_commands.Choice(name=p, value=p) for p in PROFILE_KEYS])
    async def profile(
        self,
        interaction: discord.Interaction,
        name: app_commands.Choice[str],
        budget_usdt: Optional[float] = None,
    ) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = _profile_payload(name.value, budget_usdt)
            await self.api.set_config(payload)
            risk = await self.api.get_risk()
            lines: List[str] = [
                f"profile={name.value}",
                f"max_leverage={risk.get('max_leverage')}",
                f"margin_budget_usdt={risk.get('margin_budget_usdt')}",
                f"max_notional_pct={risk.get('max_notional_pct')}",
                f"per_trade_risk_pct={risk.get('per_trade_risk_pct')}",
                f"score_conf_threshold={risk.get('score_conf_threshold')}",
                f"daily_loss_limit_pct={risk.get('daily_loss_limit_pct')}",
            ]
            await interaction.followup.send("```text\n" + "\n".join(lines) + "\n```")
        except APIError as e:
            await interaction.followup.send(f"API ?ㅻ쪟: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"?ㅻ쪟: {type(e).__name__}: {e}", ephemeral=True)


async def setup_commands(bot: commands.Bot, api: TraderAPI) -> None:
    await bot.add_cog(RemoteControl(bot, api))
