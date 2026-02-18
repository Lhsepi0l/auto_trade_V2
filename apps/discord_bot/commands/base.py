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
                await ch.send("명령 처리 중 세션이 만료되었습니다. 이 메시지는 임시 알림입니다.")
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
        "현재 대시보드 기반으로 손쉽게 제어할 수 있습니다.",
        "1) `/panel`에서 환경 설정 확인",
        f"2) {beginner_buttons} 버튼으로 원하는 기능 실행",
        "3) 실시간 상태 점검과 설정 반영까지 한 번에 확인 가능합니다.",
    ]
    em = discord.Embed(
        title="상태·운영 상태 안내",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    em.add_field(
        name="기본 설명",
        value=(
            "`/panel` : 패널 관련 명령(설정 조회, 토글, 빠른 적용)\n"
            "`/status` : 현재 봇 동작 상태 조회"
        ),
        inline=False,
    )
    em.add_field(
        name="빠른 버튼",
        value=(
            f"{beginner_buttons} : 초보자용 빠른 실행 버튼 (`{', '.join(simple_buttons)}`)\n"
            f"`{SIMPLE_PANEL_BUTTON_LABELS[0]}` : 봇 시작\n"
            f"`{SIMPLE_PANEL_BUTTON_LABELS[1]}` : 봇 중지\n"
            f"`{SIMPLE_PANEL_BUTTON_LABELS[2]}` : 포지션 마감(선택)\n"
            f"`{SIMPLE_PANEL_BUTTON_LABELS[3]}` : 긴급 정리 및 상태확인\n"
            f"`{MARGIN_BUDGET_BUTTON_LABEL}` : 예산 기반 자금 배분 설정\n"
            f"`{ADVANCED_TOGGLE_LABEL}` : 고급 설정 패널 토글(자동/수동)\n"
        ),
        inline=False,
    )
    em.add_field(
        name="주의사항",
        value=(
            "현재 운영 중인 포지션·손익은 `/status`에서 먼저 확인하고 변경하세요.\n"
            "리스크가 높은 동작(`/panel`의 일괄 반영)은 신중히 사용하세요."
        ),
        inline=False,
    )
    if is_admin:
        em.add_field(
            name="운영자 권한 기능",
            value=(
                "`/risk` : 리스크 설정 조회\n"
                "`/set` : 개별 리스크 키 값 수정\n"
                "`/preset` : 프리셋 적용\n"
                "`/profile` : 기본 템플릿 적용\n"
                "`/close` : 단일 포지션 종료(close)\n"
                "`/closeall` : 전체 포지션 종료(closeall)\n"
                "`/cooldown_clear` : 쿨다운 강제 해제\n"
                "`/report` : 일일 상태 리포트 수동 전송\n"
                f"{advanced_buttons_text} : 운영용 고급 패널 보조 기능\n"
            ),
            inline=False,
        )
    else:
        em.add_field(
            name="권한 안내",
            value="권한이 부족합니다. 관리자 권한이 필요합니다: `/set`, `/risk`, `/preset`, `/profile`, `/close`, `/closeall`, `/cooldown_clear`",
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

    @app_commands.command(name="status", description="전체 운영 상태 조회")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.get_status()
            if not isinstance(payload, dict):
                logger.warning("discord_status_payload_invalid_type", extra={"type": type(payload).__name__})
                await interaction.followup.send(
                    f"`/status` 응답이 잘못되었습니다. 타입={type(payload).__name__}\n"
                    "/status endpoint 또는 bot 헬스체크를 점검하세요."
                )
                return
            msg = _fmt_status_payload(payload)
            await interaction.followup.send(f"```text\n{msg}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="risk", description="현재 리스크 설정 조회")
    async def risk(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.get_risk()
            if not isinstance(payload, dict):
                logger.warning("discord_risk_payload_invalid_type", extra={"type": type(payload).__name__})
                await interaction.followup.send(
                    f"`/risk` 응답이 잘못되었습니다. 타입={type(payload).__name__}",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="start", description="봇 시작")
    async def start(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.start()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="stop", description="봇 중지")
    async def stop(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.stop()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="panic", description="긴급 종료 및 비상 정리")
    async def panic(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.panic()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="report", description="일간 리포트 수동 전송")
    async def report(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.send_daily_report()
            if not isinstance(payload, dict):
                await interaction.followup.send(
                    f"`/report` 응답이 잘못되었습니다. 타입={type(payload).__name__}",
                    ephemeral=True,
                )
                return
            summary = payload.get("detail", {})
            day = payload.get("day", "-")
            sent = bool(payload.get("notifier_sent"))
            err = payload.get("notifier_error")
            lines = [
                "일간 리포트 수동 전송 결과",
                f"day: {day}",
                f"sent_to_discord: {sent}",
            ]
            if not sent and err:
                lines.append(f"전송 오류: {err}")
            if isinstance(summary, dict):
                lines.append(
                    (
                        f"entries={summary.get('entries', 0)} closes={summary.get('closes', 0)} "
                        f"errors={summary.get('errors', 0)} canceled={summary.get('canceled', 0)} blocks={summary.get('blocks', 0)}"
                    ).strip()
                )
            await interaction.followup.send("```text\n" + "\n".join(lines) + "\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="help", description="봇 사용 가이드")
    async def help(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            em = _build_help_embed(is_admin=_is_admin(interaction))
            await interaction.followup.send(embed=em, ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="close", description="단일 포지션 종료 (reduceOnly)")
    @app_commands.describe(symbol="심볼: BTCUSDT")
    async def close(self, interaction: discord.Interaction, symbol: str) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.close_position(symbol.strip().upper())
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="closeall", description="전체 포지션 종료")
    async def closeall(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.close_all()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="set", description="리스크 설정 키를 변경합니다")
    @app_commands.describe(
        key="리스크 설정 키",
        value="새로 설정할 값",
    )
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
                    "허용된 key가 아닙니다. 사용 가능한 key는 아래를 참고하세요:\n" + ", ".join(RISK_KEYS[:15]) + ", ...",
                    ephemeral=True,
                )
                return
            payload = await self.api.set_value(k, value)
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="preset", description="프리셋 적용")
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
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)


    @app_commands.command(name="profile", description="사전 저장된 프리셋을 빠르게 적용")
    @app_commands.describe(
        name="프리셋 이름",
        budget_usdt="예산(선택). 입력 시 margin_budget_usdt 설정",
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
            if not isinstance(risk, dict):
                await interaction.followup.send(
                    f"`/profile` 응답이 잘못되었습니다. 타입={type(risk).__name__}",
                    ephemeral=True,
                )
                return
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
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)


async def setup_commands(bot: commands.Bot, api: TraderAPI) -> None:
    await bot.add_cog(RemoteControl(bot, api))
