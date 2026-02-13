from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from apps.discord_bot.services.api_client import APIError, TraderAPIClient

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
        except Exception:
            pass
        try:
            ch = interaction.channel
            if ch is not None and hasattr(ch, "send"):
                await ch.send("응답 시간이 초과되었습니다. 명령어를 다시 시도해 주세요.")
        except Exception:
            pass
        return False


def _truncate(s: str, *, limit: int = 1800) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _fmt_money(x: Any) -> str:
    try:
        return f"{float(x):.4f}"
    except Exception:
        return str(x)


def _fmt_status_payload(payload: Dict[str, Any]) -> str:
    engine = payload.get("engine_state") or {}
    risk = payload.get("risk_config") or {}
    summary = payload.get("config_summary") or {}
    binance = payload.get("binance") or {}
    pnl = payload.get("pnl") or {}
    sched = payload.get("scheduler") or {}
    dry_run = bool(payload.get("dry_run", False))
    dry_run_strict = bool(payload.get("dry_run_strict", False))
    last_error = payload.get("last_error", None)

    state = str(engine.get("state", "UNKNOWN"))
    panic = state.upper() == "PANIC"
    state_line = f"엔진 상태: {state}"
    if panic:
        state_line = f":warning: {state_line} (패닉)"

    enabled = binance.get("enabled_symbols") or []
    disabled = binance.get("disabled_symbols") or []

    bal = binance.get("usdt_balance") or {}
    wallet = _fmt_money(bal.get("wallet", "n/a"))
    available = _fmt_money(bal.get("available", "n/a"))

    positions = binance.get("positions") or {}
    pos_lines: List[str] = []
    if isinstance(positions, dict):
        for sym in sorted(positions.keys()):
            row = positions.get(sym) or {}
            amt = row.get("position_amt", 0)
            pnl = row.get("unrealized_pnl", 0)
            lev = row.get("leverage", 0)
            entry = row.get("entry_price", 0)
            pos_lines.append(
                f"- {sym}: amt={amt} entry={entry} pnl={pnl} lev={lev}"
            )

    open_orders = binance.get("open_orders") or {}
    oo_total = 0
    if isinstance(open_orders, dict):
        for v in open_orders.values():
            if isinstance(v, list):
                oo_total += len(v)

    spread_wide: List[str] = []
    spreads = binance.get("spreads") or {}
    if isinstance(spreads, dict):
        for sym, row in spreads.items():
            if isinstance(row, dict) and row.get("is_wide"):
                spread_wide.append(f"- {sym}: spread_pct={row.get('spread_pct')}")

    lines: List[str] = []
    lines.append(state_line)
    lines.append(f"모의모드(DRY_RUN): {dry_run} (strict={dry_run_strict})")
    lines.append(f"활성 심볼: {', '.join(enabled) if enabled else '(없음)'}")
    if disabled:
        # Show only first few.
        d0 = []
        for d in disabled[:5]:
            if isinstance(d, dict):
                d0.append(f"{d.get('symbol')}({d.get('reason')})")
        lines.append(f"비활성 심볼: {', '.join(d0)}")
    lines.append(f"USDT 잔고: wallet={wallet}, available={available}")
    lines.append(f"오픈 주문 수: {oo_total}")
    if pos_lines:
        lines.append("포지션:")
        lines.extend(pos_lines[:10])
    if spread_wide:
        lines.append("스프레드 과대:")
        lines.extend(spread_wide[:5])

    # Policy guard / PnL snapshot (if available).
    if isinstance(pnl, dict) and pnl:
        dd = pnl.get("drawdown_pct", "n/a")
        dp = pnl.get("daily_pnl_pct", "n/a")
        ls = pnl.get("lose_streak", "n/a")
        cd = pnl.get("cooldown_until", None)
        lbr = pnl.get("last_block_reason", None)
        lines.append(f"PnL: 일간%={dp} DD%={dd} 연속손실={ls}")
        if cd:
            lines.append(f"쿨다운 만료: {cd}")
        if lbr:
            lines.append(f"최근 차단 사유: {lbr}")

    # Scheduler snapshot (if enabled)
    if isinstance(sched, dict) and sched:
        cand = sched.get("candidate") or {}
        ai = sched.get("ai_signal") or {}
        if isinstance(cand, dict) and cand.get("symbol"):
            lines.append(
                f"후보: {cand.get('symbol')} {cand.get('direction')} "
                f"강도={cand.get('strength')} 변동성={cand.get('vol_tag')}"
            )
        if isinstance(ai, dict) and ai.get("target_asset"):
            lines.append(
                f"AI: {ai.get('target_asset')} {ai.get('direction')} "
                f"신뢰도={ai.get('confidence')} 힌트={ai.get('exec_hint')} 태그={ai.get('risk_tag')}"
            )
        la = sched.get("last_action")
        le = sched.get("last_error")
        if la:
            lines.append(f"스케줄러 최근 액션: {la}")
        if le:
            lines.append(f"스케줄러 최근 오류: {le}")
    if last_error:
        lines.append(f"최근 오류: {last_error}")

    # Risk is often useful, but keep it short for /status.
    if isinstance(summary, dict) and summary:
        lines.append(
            "설정: "
            f"symbols={','.join(summary.get('universe_symbols') or [])} "
            f"max_lev={summary.get('max_leverage')} "
            f"dl={summary.get('daily_loss_limit_pct')} "
            f"dd={summary.get('dd_limit_pct')} "
            f"spread={summary.get('spread_max_pct')}"
        )
    elif isinstance(risk, dict):
        lines.append(
            f"리스크: per_trade={risk.get('per_trade_risk_pct')}% "
            f"max_lev={risk.get('max_leverage')} "
            f"notify={risk.get('notify_interval_sec')}s"
        )

    return _truncate("\n".join(lines))


def _fmt_json(payload: Any) -> str:
    import json

    try:
        s = json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True)
    except Exception:
        s = str(payload)
    return _truncate(s, limit=1900)


class RemoteControl(commands.Cog):
    def __init__(self, bot: commands.Bot, api: TraderAPIClient) -> None:
        self.bot = bot
        self.api = api

    @app_commands.command(name="status", description="트레이더 상태 요약 조회")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            payload = await self.api.get_status()
            assert isinstance(payload, dict)
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
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="start", description="엔진 시작")
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

    @app_commands.command(name="stop", description="엔진 중지")
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

    @app_commands.command(name="panic", description="패닉 모드 전환 및 긴급 정리")
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

    @app_commands.command(name="close", description="심볼 단일 청산 (reduceOnly)")
    @app_commands.describe(symbol="심볼 예시: BTCUSDT")
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

    @app_commands.command(name="closeall", description="전체 포지션 청산")
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

    @app_commands.command(name="set", description="리스크 설정 값 변경")
    @app_commands.describe(key="리스크 설정 키", value="새 값(문자열)")
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
                    "잘못된 key입니다. 예시:\n" + ", ".join(RISK_KEYS[:15]) + ", ...",
                    ephemeral=True,
                )
                return
            payload = await self.api.set_value(k, value)
            await interaction.followup.send(f"```json\n{_fmt_json(payload)}\n```")
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"오류: {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="preset", description="리스크 프리셋 적용")
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


async def setup_commands(bot: commands.Bot, api: TraderAPIClient) -> None:
    await bot.add_cog(RemoteControl(bot, api))
