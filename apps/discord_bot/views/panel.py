from __future__ import annotations

import logging
import math
from typing import Any, Dict, Optional

import discord

from apps.discord_bot.commands.base import _fmt_status_payload
from apps.discord_bot.services.api_client import APIError, TraderAPIClient

logger = logging.getLogger(__name__)

ADMIN_ONLY_MSG = "관리자만 조작할 수 있습니다."
PLACEHOLDER_EXEC_MODE = "실행 모드"
PLACEHOLDER_BUDGET_MODE = "예산 모드"
PLACEHOLDER_BUDGET_PRESET = "예산 프리셋"


def _is_admin(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if not isinstance(user, (discord.Member,)):
        return False
    return bool(user.guild_permissions.administrator)


def _build_embed(payload: Dict[str, Any]) -> discord.Embed:
    eng = payload.get("engine_state") or {}
    pnl = payload.get("pnl") or {}
    sched = payload.get("scheduler") or {}
    state = str(eng.get("state", "UNKNOWN"))
    dry_run = bool(payload.get("dry_run", False))

    pos = "-"
    upnl = "0"
    if isinstance((payload.get("binance") or {}).get("positions"), dict):
        for sym, row in (payload.get("binance") or {}).get("positions", {}).items():
            amt = float((row or {}).get("position_amt") or 0.0)
            if abs(amt) > 0:
                pos = f"{sym} amt={amt}"
                upnl = str((row or {}).get("unrealized_pnl"))
                break

    dd = pnl.get("drawdown_pct")
    daily = pnl.get("daily_pnl_pct")
    cooldown = pnl.get("cooldown_until")
    last_dec = sched.get("last_decision_reason")
    capital = payload.get("capital_snapshot") or {}
    cap_cfg = payload.get("config") or {}
    risk_cfg = payload.get("risk_config") or {}
    wd = payload.get("watchdog") or {}

    dry_badge = "ON" if dry_run else "OFF"
    em = discord.Embed(title="오토트레이더 패널", description=f"엔진: **{state}** | DRY_RUN: **{dry_badge}**")
    em.add_field(name="포지션", value=str(pos), inline=False)
    em.add_field(name="uPnL / 일간PnL / DD", value=f"{upnl} / {daily} / {dd}", inline=False)
    em.add_field(name="쿨다운", value=str(cooldown or "-"), inline=True)
    em.add_field(name="최근 판단", value=str(last_dec or "-"), inline=True)

    if isinstance(capital, dict) and capital:
        blocked = bool(capital.get("blocked"))
        block_reason = str(capital.get("block_reason") or "-")
        margin_budget = cap_cfg.get("margin_budget_usdt")
        if margin_budget is None:
            margin_budget = cap_cfg.get("capital_usdt")
        cap_lines = [
            f"Avail USDT: {capital.get('available_usdt')}",
            f"설정 예산(USDT): {margin_budget}",
            f"used_margin: {capital.get('used_margin')}",
            f"leverage: {capital.get('leverage')}",
            f"notional: {capital.get('notional_usdt')}",
            f"est_qty: {capital.get('est_qty')}",
            f"blocked: {blocked}",
        ]
        if blocked:
            cap_lines.append(f"block_reason: {block_reason}")
        em.add_field(name="자본 스냅샷", value="\n".join(cap_lines), inline=False)

    if isinstance(cap_cfg, dict) and cap_cfg:
        em.add_field(
            name="예산 설정",
            value=(
                f"mode={cap_cfg.get('capital_mode')} "
                f"pct={cap_cfg.get('capital_pct')} "
                f"usdt={cap_cfg.get('capital_usdt')} "
                f"margin={cap_cfg.get('margin_use_pct')}"
            ),
            inline=False,
        )

    if isinstance(risk_cfg, dict):
        trailing_enabled = bool(risk_cfg.get("trailing_enabled", True))
        trailing_mode = str(risk_cfg.get("trailing_mode") or "PCT").upper()
        arm_pct = risk_cfg.get("trail_arm_pnl_pct")
        grace_min = risk_cfg.get("trail_grace_minutes")
        dist_cfg = risk_cfg.get("trail_distance_pnl_pct")
        dist_last = wd.get("last_trailing_distance_pct") if isinstance(wd, dict) else None
        dist_show = dist_last if trailing_mode == "ATR" and dist_last is not None else dist_cfg
        peak = wd.get("last_peak_pnl_pct") if isinstance(wd, dict) else None
        upnl_pct = payload.get("last_unrealized_pnl_pct")
        tr_lines = [
            f"enabled={trailing_enabled}",
            f"mode={trailing_mode}",
            f"arm%={arm_pct}",
            f"distance%={dist_show}",
            f"grace_min={grace_min}",
            f"last_peak_pnl_pct={peak}",
            f"last_unrealized_pnl_pct={upnl_pct}",
        ]
        em.add_field(name="트레일링", value="\n".join(tr_lines), inline=False)

    em.add_field(name="요약", value=f"```text\n{_fmt_status_payload(payload)}\n```", inline=False)
    return em


def _sanitize_usdt_input(raw: str) -> float:
    txt = str(raw or "").replace(",", "").replace(" ", "").strip()
    if not txt:
        raise ValueError("증거금 값이 비어 있습니다.")
    try:
        v = float(txt)
    except Exception as e:
        raise ValueError("숫자 형식이 아닙니다.") from e
    if not math.isfinite(v):
        raise ValueError("유한한 숫자만 입력할 수 있습니다.")
    if v < 5.0:
        raise ValueError("최소 5.0 USDT 이상이어야 합니다.")
    return v


def _parse_bool_like(raw: str, *, field: str) -> bool:
    s = str(raw or "").strip().lower()
    if s in {"1", "true", "t", "yes", "y", "on", "예", "네", "사용", "켜기", "활성"}:
        return True
    if s in {"0", "false", "f", "no", "n", "off", "아니오", "아니요", "중지", "끄기", "비활성"}:
        return False
    raise ValueError(f"{field}: 예/아니오(yes/no)로 입력하세요.")


def _parse_float_range(raw: str, *, field: str, min_v: float, max_v: float) -> float:
    txt = str(raw or "").replace(",", "").strip()
    try:
        v = float(txt)
    except Exception as e:
        raise ValueError(f"{field}: invalid float") from e
    if not math.isfinite(v) or v < min_v or v > max_v:
        raise ValueError(f"{field}: out_of_range({min_v}..{max_v})")
    return v


def _parse_int_range(raw: str, *, field: str, min_v: int, max_v: int) -> int:
    txt = str(raw or "").replace(",", "").strip()
    try:
        v = int(txt)
    except Exception as e:
        raise ValueError(f"{field}: invalid int") from e
    if v < min_v or v > max_v:
        raise ValueError(f"{field}: out_of_range({min_v}..{max_v})")
    return v


def _budget_mode_options(current: str) -> list[discord.SelectOption]:
    cur = str(current or "PCT_AVAILABLE").upper()
    return [
        discord.SelectOption(label="비율(가용자산)", value="PCT_AVAILABLE", default=(cur == "PCT_AVAILABLE")),
        discord.SelectOption(label="고정(USDT)", value="FIXED_USDT", default=(cur == "FIXED_USDT")),
    ]


def _budget_preset_options(mode: str, current: str) -> list[discord.SelectOption]:
    m = str(mode or "PCT_AVAILABLE").upper()
    cur = str(current or "").upper()
    if m == "FIXED_USDT":
        vals = ["50", "100", "200", "500"]
        return [discord.SelectOption(label=f"{v} USDT", value=v, default=(cur == v)) for v in vals]

    vals = ["0.05", "0.10", "0.20", "0.50"]
    labels = ["5%", "10%", "20%", "50%"]
    return [discord.SelectOption(label=labels[i], value=vals[i], default=(cur == vals[i])) for i in range(len(vals))]


class RiskBasicModal(discord.ui.Modal, title="리스크 기본"):
    max_leverage = discord.ui.TextInput(
        label="최대 레버리지 (max_leverage)",
        placeholder="예: 30",
        required=True,
    )
    max_exposure_pct = discord.ui.TextInput(
        label="최대 노출 비율 (0.2 또는 20%) (max_exposure_pct)",
        placeholder="예: 0.2 또는 20%",
        required=True,
    )
    max_notional_pct = discord.ui.TextInput(
        label="최대 포지션 배수(1배=100) (max_notional_pct)",
        placeholder="예: 10배=10x=1000, 20배=20x=2000",
        required=True,
    )
    per_trade_risk_pct = discord.ui.TextInput(
        label="1회 트레이드 리스크 % 0~100 (per_trade_risk_pct)",
        placeholder="예: 1",
        required=True,
    )

    def __init__(self, *, api: TraderAPIClient, view: "PanelView", defaults: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(timeout=300)
        self._api = api
        self._view = view
        d = defaults or {}
        self.max_leverage.default = str(d.get("max_leverage", ""))
        self.max_exposure_pct.default = str(d.get("max_exposure_pct", ""))
        self.max_notional_pct.default = str(d.get("max_notional_pct", ""))
        self.per_trade_risk_pct.default = str(d.get("per_trade_risk_pct", ""))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        pairs = {
            "max_leverage": str(self.max_leverage),
            "max_exposure_pct": str(self.max_exposure_pct),
            "max_notional_pct": str(self.max_notional_pct),
            "per_trade_risk_pct": str(self.per_trade_risk_pct),
        }
        try:
            for k, v in pairs.items():
                await self._api.set_value(k, v)
            await self._view.refresh_message(interaction)
            await interaction.followup.send("리스크 기본값을 업데이트했습니다.", ephemeral=True)
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)


class RiskAdvancedModal(discord.ui.Modal, title="리스크 고급"):
    daily_loss_limit_pct = discord.ui.TextInput(
        label="일일 손실 제한 -1~0 (daily_loss_limit_pct)",
        placeholder="예: -0.02",
        required=True,
    )
    dd_limit_pct = discord.ui.TextInput(
        label="최대 낙폭 제한 -1~0 (dd_limit_pct)",
        placeholder="예: -0.15",
        required=True,
    )
    min_hold_minutes = discord.ui.TextInput(
        label="최소 보유 시간(분) (min_hold_minutes)",
        placeholder="예: 240",
        required=True,
    )
    score_conf_threshold = discord.ui.TextInput(
        label="신뢰도 임계값 0~1 (score_conf_threshold)",
        placeholder="예: 0.65",
        required=True,
    )

    def __init__(self, *, api: TraderAPIClient, view: "PanelView", defaults: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(timeout=300)
        self._api = api
        self._view = view
        d = defaults or {}
        self.daily_loss_limit_pct.default = str(d.get("daily_loss_limit_pct", ""))
        self.dd_limit_pct.default = str(d.get("dd_limit_pct", ""))
        self.min_hold_minutes.default = str(d.get("min_hold_minutes", ""))
        self.score_conf_threshold.default = str(d.get("score_conf_threshold", ""))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        pairs = {
            "daily_loss_limit_pct": str(self.daily_loss_limit_pct),
            "dd_limit_pct": str(self.dd_limit_pct),
            "min_hold_minutes": str(self.min_hold_minutes),
            "score_conf_threshold": str(self.score_conf_threshold),
        }
        try:
            for k, v in pairs.items():
                await self._api.set_value(k, v)
            await self._view.refresh_message(interaction)
            await interaction.followup.send("리스크 고급값을 업데이트했습니다.", ephemeral=True)
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)


class BudgetCustomModal(discord.ui.Modal, title="예산 상세 설정"):
    capital_pct = discord.ui.TextInput(
        label="가용자산 비율 0.01~1.0 (capital_pct)",
        required=False,
        placeholder="예: 0.20",
    )
    capital_usdt = discord.ui.TextInput(
        label="고정 예산 USDT >=5 (capital_usdt)",
        required=False,
        placeholder="예: 100",
    )
    margin_use_pct = discord.ui.TextInput(
        label="증거금 사용률 0.10~1.0 (margin_use_pct)",
        required=False,
        placeholder="예: 0.90",
    )
    advanced_limits = discord.ui.TextInput(
        label="고급 제한값 (명목,노출,수수료버퍼)",
        required=False,
        placeholder="예: 1000,0.20,0.002 또는 1000,20%,0.002",
    )

    def __init__(self, *, api: TraderAPIClient, view: "PanelView") -> None:
        super().__init__(timeout=300)
        self._api = api
        self._view = view

    async def _apply_config(self, pairs: Dict[str, Any]) -> None:
        fn = getattr(self._api, "set_config", None)
        if callable(fn):
            await fn(pairs)
            return
        for k, v in pairs.items():
            await self._api.set_value(str(k), str(v))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        pairs = {
            "capital_pct": str(self.capital_pct).strip(),
            "capital_usdt": str(self.capital_usdt).strip(),
            "margin_use_pct": str(self.margin_use_pct).strip(),
        }
        try:
            payload: Dict[str, str] = {}
            for k, v in pairs.items():
                if not v:
                    continue
                payload[k] = v

            adv = str(self.advanced_limits).strip()
            if adv:
                parts = [p.strip() for p in adv.split(",")]
                while len(parts) < 3:
                    parts.append("")
                if parts[0]:
                    payload["max_position_notional_usdt"] = parts[0]
                if parts[1]:
                    payload["max_exposure_pct"] = parts[1]
                if parts[2]:
                    payload["fee_buffer_pct"] = parts[2]

            await self._apply_config(payload)
            await self._view.refresh_message(interaction)
            await interaction.followup.send(f"예산 상세 설정 적용 완료 ({len(payload)}개)", ephemeral=True)
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)


class MarginBudgetModal(discord.ui.Modal, title="증거금 설정"):
    amount_usdt = discord.ui.TextInput(
        label="증거금(USDT)",
        placeholder="예: 100 또는 1000",
        required=True,
    )

    def __init__(self, *, api: TraderAPIClient, view: "PanelView") -> None:
        super().__init__(timeout=300)
        self._api = api
        self._view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return
        try:
            amount = _sanitize_usdt_input(str(self.amount_usdt))
        except ValueError as e:
            await interaction.response.send_message(f"입력 오류: {e}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._api.set_config(
                {
                    "capital_mode": "MARGIN_BUDGET_USDT",
                    "margin_budget_usdt": amount,
                }
            )
        except APIError as e:
            await interaction.followup.send(f"설정 실패: {e}", ephemeral=True)
            return

        try:
            await self._view.refresh_message(interaction)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(
                f"증거금 예산 {amount} USDT 적용 완료 (패널 새로고침 경고: {type(e).__name__})",
                ephemeral=True,
            )
            return

        await interaction.followup.send(f"증거금 예산 {amount} USDT 적용 완료", ephemeral=True)


class TrailingConfigModal(discord.ui.Modal, title="트레일링 설정"):
    trailing_enabled = discord.ui.TextInput(
        label="트레일링 사용 여부 (예/아니오)",
        required=True,
        placeholder="예: 예",
    )
    trailing_mode = discord.ui.TextInput(
        label="트레일링 모드 (퍼센트/ATR)",
        required=True,
        placeholder="예: 퍼센트",
    )
    trail_arm_pnl_pct = discord.ui.TextInput(
        label="트레일링 시작 수익률(%)",
        required=True,
        placeholder="예: 1.2",
    )
    trail_grace_minutes = discord.ui.TextInput(
        label="진입 후 유예 시간(분)",
        required=True,
        placeholder="예: 30",
    )
    mode_params = discord.ui.TextInput(
        label="모드 상세값",
        required=False,
        placeholder="퍼센트: 0.8 / ATR: 1h,2.0,0.6,1.8",
    )

    def __init__(self, *, api: TraderAPIClient, view: "PanelView", defaults: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(timeout=300)
        self._api = api
        self._view = view
        d = defaults or {}
        self.trailing_enabled.default = "yes" if bool(d.get("trailing_enabled", True)) else "no"
        self.trailing_mode.default = str(d.get("trailing_mode", "PCT"))
        self.trail_arm_pnl_pct.default = str(d.get("trail_arm_pnl_pct", "1.2"))
        self.trail_grace_minutes.default = str(d.get("trail_grace_minutes", "30"))
        mode = str(d.get("trailing_mode", "PCT")).upper()
        if mode == "ATR":
            self.mode_params.default = (
                f"{d.get('atr_trail_timeframe', '1h')},"
                f"{d.get('atr_trail_k', '2.0')},"
                f"{d.get('atr_trail_min_pct', '0.6')},"
                f"{d.get('atr_trail_max_pct', '1.8')}"
            )
        else:
            self.mode_params.default = str(d.get("trail_distance_pnl_pct", "0.8"))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        try:
            enabled = _parse_bool_like(str(self.trailing_enabled), field="trailing_enabled")
            mode_raw = str(self.trailing_mode).strip().upper()
            mode_alias = {"퍼센트": "PCT", "PERCENT": "PCT", "PCT": "PCT", "ATR": "ATR"}
            mode = mode_alias.get(mode_raw, mode_raw)
            if mode not in {"PCT", "ATR"}:
                raise ValueError("트레일링 모드는 퍼센트(PCT) 또는 ATR만 가능합니다.")
            arm = _parse_float_range(str(self.trail_arm_pnl_pct), field="trail_arm_pnl_pct", min_v=0.0, max_v=100.0)
            grace = _parse_int_range(str(self.trail_grace_minutes), field="trail_grace_minutes", min_v=0, max_v=1440)

            payload: Dict[str, Any] = {
                "trailing_enabled": enabled,
                "trailing_mode": mode,
                "trail_arm_pnl_pct": arm,
                "trail_grace_minutes": grace,
            }

            params = str(self.mode_params or "").strip()
            if mode == "PCT":
                raw_dist = params or "0.8"
                dist = _parse_float_range(raw_dist, field="trail_distance_pnl_pct", min_v=0.0, max_v=100.0)
                payload["trail_distance_pnl_pct"] = dist
            else:
                parts = [p.strip() for p in params.split(",") if p.strip()]
                if len(parts) != 4:
                    raise ValueError("mode_params: ATR 모드는 'timeframe,k,min,max' 형식이어야 합니다.")
                tf = str(parts[0]).lower() or "1h"
                if tf not in {"15m", "1h", "4h"}:
                    raise ValueError("ATR 타임프레임은 15m/1h/4h만 가능합니다.")
                k = _parse_float_range(parts[1], field="atr_trail_k", min_v=0.0, max_v=20.0)
                mn = _parse_float_range(parts[2], field="atr_trail_min_pct", min_v=0.0, max_v=100.0)
                mx = _parse_float_range(parts[3], field="atr_trail_max_pct", min_v=0.0, max_v=100.0)
                if mx < mn:
                    raise ValueError("ATR 최대폭은 ATR 최소폭보다 크거나 같아야 합니다.")
                payload["atr_trail_timeframe"] = tf
                payload["atr_trail_k"] = k
                payload["atr_trail_min_pct"] = mn
                payload["atr_trail_max_pct"] = mx
        except ValueError as e:
            await interaction.response.send_message(f"입력 오류: {e}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._api.set_config(payload)
            await self._view.refresh_message(interaction)
            await interaction.followup.send("트레일링 설정 적용 완료", ephemeral=True)
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)


class PanelView(discord.ui.View):
    def __init__(self, *, api: TraderAPIClient, message_id: Optional[int] = None) -> None:
        super().__init__(timeout=None)
        self.api = api
        self.message_id = message_id
        self._capital_mode: str = "PCT_AVAILABLE"
        self._capital_preset_value: str = "0.20"
        self._sync_budget_controls()

    def _sync_budget_controls(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Select) and str(item.placeholder) == PLACEHOLDER_BUDGET_MODE:
                item.options = _budget_mode_options(self._capital_mode)
            if isinstance(item, discord.ui.Select) and str(item.placeholder) == PLACEHOLDER_BUDGET_PRESET:
                item.options = _budget_preset_options(self._capital_mode, self._capital_preset_value)

    async def refresh_message(self, interaction: discord.Interaction) -> None:
        payload = await self.api.get_status()
        if isinstance(payload, dict):
            cfg = payload.get("config") or {}
            self._capital_mode = str(cfg.get("capital_mode") or self._capital_mode).upper()
            if self._capital_mode == "FIXED_USDT":
                self._capital_preset_value = str(cfg.get("capital_usdt") or self._capital_preset_value)
            else:
                self._capital_preset_value = str(cfg.get("capital_pct") or self._capital_preset_value)
            self._sync_budget_controls()

        em = _build_embed(payload if isinstance(payload, dict) else {})
        msg = interaction.message
        if msg is None and self.message_id and interaction.channel is not None and hasattr(interaction.channel, "fetch_message"):
            try:
                msg = await interaction.channel.fetch_message(self.message_id)
            except Exception:
                msg = None
        if msg is not None:
            if hasattr(msg, "id"):
                try:
                    self.message_id = int(msg.id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("panel_message_id_parse_failed", extra={"err": type(e).__name__}, exc_info=True)
            await msg.edit(embed=em, view=self)

    async def _apply_config(self, pairs: Dict[str, Any]) -> None:
        fn = getattr(self.api, "set_config", None)
        if callable(fn):
            await fn(pairs)
            return
        for k, v in pairs.items():
            await self.api.set_value(str(k), str(v))

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not _is_admin(interaction):
            if interaction.response.is_done():
                await interaction.followup.send(ADMIN_ONLY_MSG, ephemeral=True)
            else:
                await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return False
        return True

    @discord.ui.button(label="시작", style=discord.ButtonStyle.success)
    async def start_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.api.start()
        await self.refresh_message(interaction)
        await interaction.followup.send("엔진을 시작했습니다.", ephemeral=True)

    @discord.ui.button(label="중지", style=discord.ButtonStyle.secondary)
    async def stop_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.api.stop()
        await self.refresh_message(interaction)
        await interaction.followup.send("엔진을 중지했습니다.", ephemeral=True)

    @discord.ui.button(label="패닉", style=discord.ButtonStyle.danger)
    async def panic_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.api.panic()
        await self.refresh_message(interaction)
        await interaction.followup.send("패닉 명령을 전송했습니다.", ephemeral=True)

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.primary)
    async def refresh_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.refresh_message(interaction)
        await interaction.followup.send("상태를 새로고침했습니다.", ephemeral=True)

    @discord.ui.button(label="증거금설정", style=discord.ButtonStyle.primary)
    async def margin_budget_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(MarginBudgetModal(api=self.api, view=self))

    @discord.ui.button(label="트레일링설정", style=discord.ButtonStyle.primary, row=1)
    async def trailing_config_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        defaults: Dict[str, Any] = {}
        try:
            payload = await self.api.get_status()
            if isinstance(payload, dict):
                defaults = dict(payload.get("risk_config") or {})
        except Exception:
            defaults = {}
        await interaction.response.send_modal(TrailingConfigModal(api=self.api, view=self, defaults=defaults))

    @discord.ui.button(label="리스크 기본", style=discord.ButtonStyle.secondary, row=1)
    async def risk_basic_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        defaults: Dict[str, Any] = {}
        try:
            payload = await self.api.get_status()
            if isinstance(payload, dict):
                defaults = dict(payload.get("risk_config") or {})
        except Exception:
            defaults = {}
        await interaction.response.send_modal(RiskBasicModal(api=self.api, view=self, defaults=defaults))

    @discord.ui.button(label="리스크 고급", style=discord.ButtonStyle.secondary, row=1)
    async def risk_adv_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        defaults: Dict[str, Any] = {}
        try:
            payload = await self.api.get_status()
            if isinstance(payload, dict):
                defaults = dict(payload.get("risk_config") or {})
        except Exception:
            defaults = {}
        await interaction.response.send_modal(RiskAdvancedModal(api=self.api, view=self, defaults=defaults))

    @discord.ui.select(
        placeholder=PLACEHOLDER_EXEC_MODE,
        options=[
            discord.SelectOption(label="LIMIT", value="LIMIT", default=True),
            discord.SelectOption(label="MARKET", value="MARKET"),
            discord.SelectOption(label="SPLIT", value="SPLIT"),
        ],
        row=4,
    )
    async def exec_mode_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if not await self._guard(interaction):
            return
        val = str(select.values[0]).upper()
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.api.set_value("exec_mode_default", val)
        await self.refresh_message(interaction)
        await interaction.followup.send(f"실행 모드 변경: {val}", ephemeral=True)

    @discord.ui.select(
        placeholder=PLACEHOLDER_BUDGET_MODE,
        options=[
            discord.SelectOption(label="비율(가용자산)", value="PCT_AVAILABLE", default=True),
            discord.SelectOption(label="고정(USDT)", value="FIXED_USDT"),
        ],
        row=2,
    )
    async def capital_mode_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if not await self._guard(interaction):
            return
        self._capital_mode = str(select.values[0]).upper()
        self._capital_preset_value = "50" if self._capital_mode == "FIXED_USDT" else "0.20"
        self._sync_budget_controls()
        if interaction.message is not None:
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message("모드 선택 업데이트 완료", ephemeral=True)

    @discord.ui.select(
        placeholder=PLACEHOLDER_BUDGET_PRESET,
        options=[
            discord.SelectOption(label="20%", value="0.20", default=True),
            discord.SelectOption(label="10%", value="0.10"),
            discord.SelectOption(label="50%", value="0.50"),
            discord.SelectOption(label="5%", value="0.05"),
        ],
        row=3,
    )
    async def budget_preset_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if not await self._guard(interaction):
            return
        self._capital_preset_value = str(select.values[0]).strip()
        self._sync_budget_controls()
        if interaction.message is not None:
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message("프리셋 선택 업데이트 완료", ephemeral=True)

    @discord.ui.button(label="프리셋 적용", style=discord.ButtonStyle.success, row=1)
    async def apply_budget_preset_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            payload: Dict[str, str] = {"capital_mode": self._capital_mode}
            if self._capital_mode == "FIXED_USDT":
                payload["capital_usdt"] = self._capital_preset_value
            else:
                payload["capital_pct"] = self._capital_preset_value
            await self._apply_config(payload)
            await self.refresh_message(interaction)
            label = f"{self._capital_preset_value} USDT" if self._capital_mode == "FIXED_USDT" else self._capital_preset_value
            await interaction.followup.send(
                f"예산 프리셋 적용 완료: mode={self._capital_mode}, value={label}",
                ephemeral=True,
            )
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)

    @discord.ui.button(label="직접 입력...", style=discord.ButtonStyle.secondary, row=1)
    async def custom_budget_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(BudgetCustomModal(api=self.api, view=self))





