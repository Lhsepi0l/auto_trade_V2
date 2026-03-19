from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from v2.discord_bot.services.api_client import APIError
from v2.discord_bot.services.contracts import TraderAPI
from v2.discord_bot.services.discord_utils import fmt_json as _fmt_json
from v2.discord_bot.services.discord_utils import is_admin as _is_admin
from v2.discord_bot.services.discord_utils import safe_defer as _safe_defer
from v2.discord_bot.services.discord_utils import safe_send_ephemeral as _safe_send_ephemeral
from v2.discord_bot.services.formatting import (
    format_report_payload as _fmt_report_payload,
)
from v2.discord_bot.services.formatting import (
    format_status_payload as _fmt_status_payload,
)
from v2.discord_bot.ui_labels import (
    ADVANCED_PANEL_BUTTON_LABELS,
    MARGIN_BUDGET_BUTTON_LABEL,
    SIMPLE_PANEL_BUTTON_LABELS,
)
from v2.operator.presets import (
    PRESETS,
    PROFILE_KEYS,
)
from v2.operator.presets import (
    build_profile_payload as _profile_payload,
)

RISK_KEYS: list[str] = [
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
    "enabled_alphas",
    "trend_adx_min_4h",
    "trend_adx_max_4h",
    "trend_adx_rising_lookback_4h",
    "trend_adx_rising_min_delta_4h",
    "breakout_buffer_bps",
    "expansion_buffer_bps",
    "expansion_range_atr_min",
    "expansion_body_ratio_min",
    "expansion_close_location_min",
    "expansion_width_expansion_min",
    "min_volume_ratio_15m",
    "expected_move_cost_mult",
    "expansion_quality_score_v2_min",
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
    "tpsl_policy",
    "tpsl_method",
    "tpsl_base_take_profit_pct",
    "tpsl_base_stop_loss_pct",
    "tpsl_regime_mult_bull",
    "tpsl_regime_mult_bear",
    "tpsl_regime_mult_sideways",
    "tpsl_regime_mult_unknown",
    "tpsl_volatility_norm_enabled",
    "tpsl_atr_pct_ref",
    "tpsl_vol_mult_min",
    "tpsl_vol_mult_max",
    "tpsl_tp_min_pct",
    "tpsl_tp_max_pct",
    "tpsl_sl_min_pct",
    "tpsl_sl_max_pct",
    "tpsl_rr_min",
    "tpsl_rr_max",
    "tpsl_tp_atr",
    "tpsl_sl_atr",
    "atr_trail_timeframe",
    "atr_trail_k",
    "atr_trail_min_pct",
    "atr_trail_max_pct",
    "tf_weight_4h",
    "tf_weight_1h",
    "tf_weight_30m",
    "tf_weight_10m",
    "tf_weight_15m",
    "score_tf_15m_enabled",
    "vol_shock_atr_mult_threshold",
    "atr_mult_mean_window",
]

JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
JSONPayload = dict[str, JSONValue]


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
        title="초보자용 도움말",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    _ = em.add_field(
        name="기본 설명",
        value=(
            "`/panel` : 패널 관련 명령(설정 조회, 토글, 빠른 적용)\n"
            "`/status` : 현재 봇 동작 상태 조회"
        ),
        inline=False,
    )
    _ = em.add_field(
        name="빠른 버튼",
        value=(
            f"{beginner_buttons} : 초보자용 빠른 실행 버튼 (`{', '.join(simple_buttons)}`)\n"
            f"`{SIMPLE_PANEL_BUTTON_LABELS[0]}` : 봇 시작\n"
            f"`{SIMPLE_PANEL_BUTTON_LABELS[1]}` : 봇 중지\n"
            f"`{SIMPLE_PANEL_BUTTON_LABELS[2]}` : 포지션 마감(선택)\n"
            f"`{SIMPLE_PANEL_BUTTON_LABELS[3]}` : 긴급 정리 및 상태확인\n"
            f"`{MARGIN_BUDGET_BUTTON_LABEL}` : 예산 기반 자금 배분 설정\n"
        ),
        inline=False,
    )
    _ = em.add_field(
        name="주의사항",
        value=(
            "현재 운영 중인 포지션·손익은 `/status`에서 먼저 확인하고 변경하세요.\n"
            "리스크가 높은 동작(`/panel`의 일괄 반영)은 신중히 사용하세요."
        ),
        inline=False,
    )
    if is_admin:
        _ = em.add_field(
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
        _ = em.add_field(
            name="권한 안내",
            value="관리자만 사용 가능합니다. 권한이 부족합니다. 관리자 권한이 필요합니다: `/set`, `/risk`, `/preset`, `/profile`, `/close`, `/closeall`, `/cooldown_clear`",
            inline=False,
        )
    return em


async def _defer_or_notify(interaction: discord.Interaction) -> bool:
    if await _safe_defer(interaction):
        return True
    _ = await _safe_send_ephemeral(
        interaction,
        "명령 응답에 실패했습니다. 잠시 후 다시 시도해주세요.",
    )
    return False


class RemoteControl(commands.Cog):
    def __init__(self, bot: commands.Bot, api: TraderAPI) -> None:
        self.bot: commands.Bot = bot
        self.api: TraderAPI = api

    @app_commands.command(name="status", description="전체 운영 상태 조회")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await _defer_or_notify(interaction):
            return
        try:
            payload = await self.api.get_status()
            msg = _fmt_status_payload(payload)
            await interaction.followup.send(f"```text\n{msg}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="risk", description="현재 리스크 설정 조회")
    async def risk(self, interaction: discord.Interaction) -> None:
        if not await _defer_or_notify(interaction):
            return
        try:
            payload = await self.api.get_risk()
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="start", description="봇 시작")
    async def start(self, interaction: discord.Interaction) -> None:
        if not await _defer_or_notify(interaction):
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
        if not await _defer_or_notify(interaction):
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
        if not await _defer_or_notify(interaction):
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
        if not await _defer_or_notify(interaction):
            return
        try:
            payload = await self.api.send_daily_report()
            msg = _fmt_report_payload(payload)
            await interaction.followup.send(f"```text\n{msg}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="help", description="봇 사용 가이드")
    async def help(self, interaction: discord.Interaction) -> None:
        if not await _defer_or_notify(interaction):
            return
        try:
            em = _build_help_embed(is_admin=_is_admin(interaction))
            await interaction.followup.send(embed=em, ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="close", description="단일 포지션 종료 (reduceOnly)")
    @app_commands.describe(symbol="심볼: BTCUSDT")
    async def close(self, interaction: discord.Interaction, symbol: str) -> None:
        if not await _defer_or_notify(interaction):
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
        if not await _defer_or_notify(interaction):
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
        if not await _defer_or_notify(interaction):
            return
        try:
            k = key.strip()
            if k not in RISK_KEYS:
                await interaction.followup.send(
                    "허용된 key가 아닙니다. 사용 가능한 key는 아래를 참고하세요:\n"
                    + ", ".join(RISK_KEYS[:15])
                    + ", ...",
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
        if not await _defer_or_notify(interaction):
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
        budget_usdt: float | None = None,
    ) -> None:
        if not await _defer_or_notify(interaction):
            return
        try:
            payload = _profile_payload(name.value, budget_usdt)
            _ = await self.api.set_config(payload)
            risk = await self.api.get_risk()
            lines: list[str] = [
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
