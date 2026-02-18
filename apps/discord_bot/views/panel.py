from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Literal, Optional

import discord

from apps.discord_bot.services.api_client import APIError
from apps.discord_bot.services.contracts import TraderAPI
from apps.discord_bot.services.formatting import format_status_payload
from apps.discord_bot.ui_labels import (
    ADVANCED_TOGGLE_LABEL,
    EXEC_MODE_SELECT_PLACEHOLDER,
    MARGIN_BUDGET_BUTTON_LABEL,
    PANIC_BUTTON_LABEL,
    RISK_ADVANCED_BUTTON_LABEL,
    RISK_BASIC_BUTTON_LABEL,
    SCHEDULER_INTERVAL_SELECT_PLACEHOLDER,
    NOTIFY_INTERVAL_SELECT_PLACEHOLDER,
    SIMPLE_PANEL_BUTTON_LABELS,
    SIMPLE_TOGGLE_LABEL,
    START_BUTTON_LABEL,
    STOP_BUTTON_LABEL,
    UNIVERSE_SYMBOLS_BUTTON_LABEL,
    UNIVERSE_REMOVE_SYMBOL_BUTTON_LABEL,
    TICK_ONCE_BUTTON_LABEL,
    TRAILING_BUTTON_LABEL,
    SYMBOL_LEVERAGE_BUTTON_LABEL,
)

logger = logging.getLogger(__name__)

ADMIN_ONLY_MSG = "관리자만 조작할 수 있습니다."
HELP_SIMPLE = (
    "간단 모드에서 바로 쓸 수 있는 버튼은 "
    + "/".join(
        [
            START_BUTTON_LABEL,
            STOP_BUTTON_LABEL,
            PANIC_BUTTON_LABEL,
            TICK_ONCE_BUTTON_LABEL,
            MARGIN_BUDGET_BUTTON_LABEL,
            ADVANCED_TOGGLE_LABEL,
        ]
    )
    + " 입니다.\n"
    + "판단 주기(스캔 간격)와 상태 알림 주기는 각각 개별 설정 가능합니다."
)
HELP_ADVANCED = (
    "고급 모드 버튼: "
    + "/".join(
        [
            RISK_BASIC_BUTTON_LABEL,
            RISK_ADVANCED_BUTTON_LABEL,
            TRAILING_BUTTON_LABEL,
            SYMBOL_LEVERAGE_BUTTON_LABEL,
            UNIVERSE_SYMBOLS_BUTTON_LABEL,
            UNIVERSE_REMOVE_SYMBOL_BUTTON_LABEL,
            SIMPLE_TOGGLE_LABEL,
        ]
    )
    + " 와 실행모드/판단 주기 선택이 같이 표시됩니다."
    + " 상태알림 간격 선택도 같이 표시됩니다."
)

REASON_HINT_MAP: dict[str, str] = {
    "no_candidate": "현재 진입 후보가 없습니다.",
    "vol_shock_no_entry": "변동성 급등 구간이라 신규 진입을 보류합니다.",
    "confidence_below_threshold": "신뢰도 점수가 기준치보다 낮아 진입하지 않습니다.",
    "short_not_allowed_regime": "현재 구간은 숏 진입이 제한됩니다.",
    "enter_candidate": "진입 조건이 맞아 후보를 선택했습니다.",
    "vol_shock_close": "변동성 급등 구간이라 포지션 종료를 보류합니다.",
    "profit_hold": "익절 신호가 살아있어 대기 상태입니다.",
    "same_symbol": "현재 보유 심볼과 동일한 심볼은 중복 진입할 수 없습니다.",
    "gap_below_threshold": "점수 차이가 기준치보다 작아 대기합니다.",
    "rebalance_to_better_candidate": "더 나은 후보가 나와 재평가해 이동합니다.",
    "close_symbol_missing": "종료할 심볼 정보를 찾을 수 없습니다.",
    "enter_symbol_missing": "진입할 심볼 정보를 찾을 수 없습니다.",
    "price_unavailable": "가격 데이터를 확인할 수 없습니다.",
    "BALANCE_UNAVAILABLE": "잔고 조회에 실패했습니다.",
    "MARKET_DATA_UNAVAILABLE": "마켓 데이터 조회 중 오류가 발생했습니다.",
    "MARK_PRICE_UNAVAILABLE": "마켓 가격 조회 중 오류가 발생했습니다.",
    "BUDGET_TOO_SMALL_FOR_MIN_NOTIONAL": "최소 주문금액보다 작은 값은 사용할 수 없습니다.",
    "BUDGET_TOO_SMALL_FOR_MIN_QTY": "최소 수량보다 작은 값은 사용할 수 없습니다.",
    "BUDGET_BLOCKED": "예산으로 주문 계산이 차단되었습니다.",
    "ENTRY_EXCEEDS_BUDGET_CAP": "주문 크기가 예산 한도보다 큽니다.",
    "cooldown_active": "현재 쿨다운 상태여서 판단을 보류합니다.",
    "daily_loss_limit_reached": "일일 손실 제한에 걸렸습니다.",
    "dd_limit_reached": "DD 제한에 걸렸습니다.",
    "lose_streak_cooldown": "연속 손실 중이라 일시 정지됩니다.",
    "equity_unavailable": "자산 데이터(Equity)를 가져올 수 없습니다.",
    "leverage_above_max_leverage": "레버리지가 허용 범위를 넘었습니다.",
    "single_asset_rule_violation": "단일 자산 모드에서는 동일 방향 중복 진입이 금지됩니다.",
    "exposure_above_max_exposure": "총 노출 한도를 넘어 진입할 수 없습니다.",
    "notional_above_max_notional": "주문 기준금액이 최대 한도를 넘습니다.",
    "notional_unavailable": "주문 기준금액 계산값이 없습니다.",
    "per_trade_risk_exceeded": "1회 위험 기준을 넘는 주문입니다.",
    "book_unavailable_market_disabled": "호가창 데이터가 없습니다.",
}

REASON_PREFIX_HINTS = {
    "min_hold_active:": "최소 보유 시간 조건이 활성화되어 있습니다",
    "sizing_blocked:": "주문 계산이 차단됨",
    "spread_too_wide_market_disabled:": "스프레드가 너무 넓어 시장가 주문이 비활성입니다",
    "daily_loss_limit_reached:": "일일 손실 제한에 걸림",
    "dd_limit_reached:": "DD 제한에 걸림",
    "notional_": "주문 기준금액이 제한 조건을 벗어났습니다",
}

def _is_admin(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if not isinstance(user, (discord.Member,)):
        return False
    return bool(user.guild_permissions.administrator)


def _fmt_money(v: Any, *, digits: int = 4) -> str:
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def _parse_iso_to_aware_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _next_decision_eta(payload: Dict[str, Any]) -> str:
    sched = payload.get("scheduler") or {}
    tick_sec = float(sched.get("tick_sec") or 1800.0)
    base_ts = sched.get("tick_finished_at") or sched.get("tick_started_at")
    base = _parse_iso_to_aware_datetime(base_ts)
    if base is None:
        return "확인 필요"
    next_tick = base + timedelta(seconds=max(tick_sec, 1.0))
    now = datetime.now(timezone.utc)
    remaining = int((next_tick - now).total_seconds())
    if remaining < 0:
        remaining = 0
    return f"{remaining // 60}분 {remaining % 60}초 후"


def _reason_to_human_readable(raw_reason: str) -> str:
    reason = str(raw_reason).strip()
    if not reason:
        return "사유 없음"

    for prefix, msg in REASON_PREFIX_HINTS.items():
        if reason.startswith(prefix):
            detail = reason[len(prefix) :].strip()
            if detail:
                return f"{msg}: {detail}"
            return msg

    if reason in REASON_HINT_MAP:
        return REASON_HINT_MAP[reason]

    return reason


def _normalize_last_action(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value or value == "-":
        return "-"
    return value


def _normalize_last_decision(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value or value == "-":
        return "-"
    return value


def _build_last_result(last_action: str, last_error: str | None, last_decision: str | None = None) -> tuple[str, str]:
    if last_error:
        human = _reason_to_human_readable(last_error)
        return "BLOCKED", f"사유: {human}"

    decision = _normalize_last_decision(last_decision)
    if last_action == "-" and decision != "-":
        return "DECISION", _reason_to_human_readable(decision)

    action = _normalize_last_action(last_action)
    if action != "-":
        return "OK", str(last_action)
    return "-", "-"


def _interval_label(sec: Any) -> str:
    try:
        sec_f = float(sec)
    except Exception:
        return "미지정"
    if sec_f <= 0:
        return "미지정"
    tick_min = sec_f / 60.0
    if tick_min.is_integer():
        return f"{int(tick_min)}분"
    if sec_f < 60:
        return f"{sec_f:.0f}초"
    return f"{tick_min:.1f}분"


def _scan_interval_label(payload: Dict[str, Any]) -> str:
    sched = payload.get("scheduler") or {}
    tick_sec = float(sched.get("tick_sec") or 1800.0)
    return _interval_label(tick_sec)


def _notify_interval_label(payload: Dict[str, Any]) -> str:
    risk = payload.get("risk_config") or {}
    notify_sec = float(risk.get("notify_interval_sec") or 1800)
    return _interval_label(notify_sec)


def _budget_display(payload: Dict[str, Any]) -> tuple[str, str]:
    cfg = payload.get("config") or {}
    cap = payload.get("capital_snapshot") or {}

    budget = cap.get("budget_usdt")
    order_amt = cap.get("notional_usdt")

    if budget is None:
        mode = str(cfg.get("capital_mode") or "").upper()
        if mode == "MARGIN_BUDGET_USDT":
            budget = cfg.get("margin_budget_usdt")
        elif mode == "FIXED_USDT":
            budget = cfg.get("capital_usdt")
        else:
            budget = cfg.get("capital_pct")

    if order_amt is None and isinstance(cap, dict) and cap.get("used_margin") is not None:
        # Fallback if API snapshot is incomplete.
        order_amt = cap.get("budget_usdt")

    return _fmt_money(budget), _fmt_money(order_amt)



def _build_simple_lines(payload: Dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    eng = payload.get("engine_state") or {}
    dry_run = bool(payload.get("dry_run", False))
    state = str(eng.get("state", "UNKNOWN"))

    sched = payload.get("scheduler") or {}
    last_action = _normalize_last_action(str(sched.get("last_action") or "-"))
    last_decision = str(sched.get("last_decision_reason") or "-")
    last_error = sched.get("last_error")
    last_status, last_reason = _build_last_result(
        last_action, str(last_error) if last_error is not None else None, last_decision
    )
    if last_reason:
        last_result = " - ".join([x for x in (last_status, str(last_reason)) if x not in {"-",""}]) or "-"
    else:
        last_result = "아직 판단 없음"

    decision_hint = f"엔진: {state}\n드라이런: {'ON' if dry_run else 'OFF'}"
    next_decision = _next_decision_eta(payload)
    budget, order_amt = _budget_display(payload)
    margin_line = f"현재 증거금: {budget} USDT"
    expect_line = f"예상 주문금액: {order_amt} USDT"

    scan_interval = _scan_interval_label(payload)
    notify_interval = _notify_interval_label(payload)
    interval_line = f"판단 주기: {scan_interval} / 상태 알림: {notify_interval}"
    return (
        decision_hint,
        next_decision,
        margin_line,
        expect_line,
        f"마지막 판단: {last_action}\n마지막 결과: {last_result}",
        interval_line,
    )


def _build_advanced_lines(payload: Dict[str, Any]) -> list[str]:
    risk = payload.get("risk_config") or {}
    cfg = payload.get("config") or {}
    wd = payload.get("watchdog") or {}
    symbol_map = risk.get("symbol_leverage_map") or {}
    symbol_items = [
        f"{str(sym)}={float(lev)}"
        for sym, lev in sorted(symbol_map.items(), key=lambda item: str(item[0]))
    ]

    lines = [
        "리스크: "
        + ", ".join(
            [
                f"레버리지={risk.get('max_leverage')}",
                f"총노출={risk.get('max_exposure_pct')}",
                f"최대노출={risk.get('max_notional_pct')}",
                f"1회위험={risk.get('per_trade_risk_pct')}%",
            ]
        ),
        f"실행모드={cfg.get('exec_mode_default')}",
        f"운영 심볼={_join_symbols_preview(list(risk.get('universe_symbols') or []), limit=180)}",
        "트레일링="
        + (
            f"ON({risk.get('trailing_mode')}, 시작={risk.get('trail_arm_pnl_pct')}%, "
            f"distance={risk.get('trail_distance_pnl_pct') if risk.get('trailing_mode') == 'PCT' else (wd.get('last_trailing_distance_pct') or '-') }%)"
        ),
    ]
    if symbol_items:
        lines.append("심볼 레버리지=" + ", ".join(symbol_items))
    return lines


def _build_embed(payload: Dict[str, Any], *, mode: Literal["simple", "advanced"] = "simple") -> discord.Embed:
    payload = payload if isinstance(payload, dict) else {}
    title = "오토트레이더 패널 (간단)" if mode == "simple" else "오토트레이더 패널 (고급)"
    em = discord.Embed(title=title)
    if mode == "simple":
        em.description = "현재 화면 버튼: " + "/".join(SIMPLE_PANEL_BUTTON_LABELS)
    else:
        em.description = (
            "고급 화면 버튼: "
            + "/".join(
                [
                    RISK_BASIC_BUTTON_LABEL,
                    RISK_ADVANCED_BUTTON_LABEL,
                    TRAILING_BUTTON_LABEL,
                    SYMBOL_LEVERAGE_BUTTON_LABEL,
                    UNIVERSE_SYMBOLS_BUTTON_LABEL,
                    UNIVERSE_REMOVE_SYMBOL_BUTTON_LABEL,
                    EXEC_MODE_SELECT_PLACEHOLDER,
                    SCHEDULER_INTERVAL_SELECT_PLACEHOLDER,
                ]
            )
        )
    line1, next_decision, margin_line, order_line, judge_line, interval_line = _build_simple_lines(payload)
    em.add_field(name="엔진 상태", value=line1, inline=False)
    em.add_field(name="다음 판단", value=next_decision, inline=True)
    em.add_field(name="현재 증거금", value=margin_line, inline=True)
    em.add_field(name="예상 주문금액", value=order_line, inline=True)
    em.add_field(name="운영 주기", value=interval_line, inline=True)
    em.add_field(name="마지막 결과", value=judge_line, inline=False)
    em.add_field(name="한 번에 보기", value=HELP_SIMPLE if mode == "simple" else HELP_ADVANCED, inline=False)

    if mode == "advanced":
        adv = _build_advanced_lines(payload)
        if adv:
            em.add_field(name="고급 상태", value="\n".join(adv), inline=False)
        em.add_field(name="디버그 상태", value=f"```text\n{format_status_payload(payload)}\n```", inline=False)

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
        raise ValueError("최소 5 USDT 이상 입력해야 합니다.")
    return v


def _parse_bool_like(raw: str, *, field: str) -> bool:
    s = str(raw or "").strip().lower()
    if s in {"1", "true", "t", "yes", "y", "on", "예", "네", "켜기"}:
        return True
    if s in {"0", "false", "f", "no", "n", "off", "아니오", "아니요", "끄기"}:
        return False
    raise ValueError(f"{field}: true/false(예/아니오) 형식으로 입력해주세요")


def _parse_universe_symbols(raw: str) -> list[str]:
    s = str(raw or "").strip()
    if not s:
        raise ValueError("심볼 목록이 비어 있습니다. BTCUSDT,ETHUSDT 형식으로 입력하세요.")
    normalized_raw = s.replace("\n", ",").replace(";", ",").replace("，", ",")
    parts = [p.strip().upper() for p in re.split(r"[\s,]+", normalized_raw) if p.strip()]
    if not parts:
        raise ValueError("심볼 형식이 유효하지 않습니다. BTCUSDT,ETHUSDT 형식으로 입력하세요.")

    out: list[str] = []
    seen: set[str] = set()
    for item in parts:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _join_symbols_preview(symbols: list[str], *, limit: int = 150) -> str:
    if not symbols:
        return "-"
    text = ",".join(symbols)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


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


class RiskBasicModal(discord.ui.Modal, title="리스크 기본"):
    max_leverage = discord.ui.TextInput(
        label="최대 레버리지 (max_leverage)",
        placeholder="예: 30",
        required=True,
    )
    max_exposure_pct = discord.ui.TextInput(
        label="최대 노출 비율 (max_exposure_pct)",
        placeholder="예: 0.2 또는 20%",
        required=True,
    )
    max_notional_pct = discord.ui.TextInput(
        label="최대 포지션 배수 (max_notional_pct)",
        placeholder="예: 10 (1000%)",
        required=True,
    )
    per_trade_risk_pct = discord.ui.TextInput(
        label="1회 트레이드 리스크 % (per_trade_risk_pct)",
        placeholder="예: 1",
        required=True,
    )

    def __init__(self, *, api: TraderAPI, view: "PanelViewBase", defaults: Optional[Dict[str, Any]] = None) -> None:
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

        pairs = {
            "max_leverage": str(self.max_leverage),
            "max_exposure_pct": str(self.max_exposure_pct),
            "max_notional_pct": str(self.max_notional_pct),
            "per_trade_risk_pct": str(self.per_trade_risk_pct),
        }
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            for k, v in pairs.items():
                await self._api.set_value(k, v)
            await self._view.refresh_message(interaction)
            await interaction.followup.send("리스크 기본값을 업데이트했습니다.", ephemeral=True)
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)


class RiskAdvancedModal(discord.ui.Modal, title="리스크 고급"):
    daily_loss_limit_pct = discord.ui.TextInput(
        label="일일 손실 제한 (-1 ~ 0)",
        placeholder="예: -0.02",
        required=True,
    )
    dd_limit_pct = discord.ui.TextInput(
        label="최대 낙폭 제한 (-1 ~ 0)",
        placeholder="예: -0.15",
        required=True,
    )
    min_hold_minutes = discord.ui.TextInput(
        label="최소 보유 시간(분)",
        placeholder="예: 240",
        required=True,
    )
    score_conf_threshold = discord.ui.TextInput(
        label="신뢰도 임계값(0~1)",
        placeholder="예: 0.65",
        required=True,
    )

    def __init__(self, *, api: TraderAPI, view: "PanelViewBase", defaults: Optional[Dict[str, Any]] = None) -> None:
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

        pairs = {
            "daily_loss_limit_pct": str(self.daily_loss_limit_pct),
            "dd_limit_pct": str(self.dd_limit_pct),
            "min_hold_minutes": str(self.min_hold_minutes),
            "score_conf_threshold": str(self.score_conf_threshold),
        }
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            for k, v in pairs.items():
                await self._api.set_value(k, v)
            await self._view.refresh_message(interaction)
            await interaction.followup.send("리스크 고급값을 업데이트했습니다.", ephemeral=True)
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)


class MarginBudgetModal(discord.ui.Modal, title="증거금 설정"):
    amount_usdt = discord.ui.TextInput(
        label="증거금 (USDT, 최소 5)",
        placeholder="예: 100",
        required=True,
    )

    def __init__(
        self,
        *,
        api: TraderAPI,
        view: "PanelViewBase",
        current_budget: Optional[float] = None,
    ) -> None:
        super().__init__(timeout=300)
        self._api = api
        self._view = view
        if current_budget is not None:
            pretty = _fmt_money(current_budget, digits=4)
            self.amount_usdt.default = pretty
            self.amount_usdt.placeholder = f"현재값: {pretty} USDT / 예: 100"

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
            await self._view.refresh_message(interaction)
            await interaction.followup.send(f"증거금 설정 완료: {amount:.4f} USDT", ephemeral=True)
        except APIError as e:
            await interaction.followup.send(f"설정 실패: {e}", ephemeral=True)


class TrailingConfigModal(discord.ui.Modal, title="트레일링 설정"):
    trailing_enabled = discord.ui.TextInput(
        label="트레일링 사용(예/아니오)",
        required=True,
        placeholder="예: 예",
    )
    trailing_mode = discord.ui.TextInput(
        label="트레일링 모드 (PCT / ATR)",
        required=True,
        placeholder="예: PCT",
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
        placeholder="PCT: 0.8 / ATR: 1h,2.0,0.6,1.8",
    )

    def __init__(self, *, api: TraderAPI, view: "PanelViewBase", defaults: Optional[Dict[str, Any]] = None) -> None:
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
                raise ValueError("트레일링 모드는 PCT 또는 ATR만 가능합니다.")
            arm = _parse_float_range(str(self.trail_arm_pnl_pct), field="trail_arm_pnl_pct", min_v=0.0, max_v=100.0)
            grace = _parse_int_range(
                str(self.trail_grace_minutes), field="trail_grace_minutes", min_v=0, max_v=1440
            )

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
                    raise ValueError("ATR 모드는 'timeframe,k,min,max' 형식이어야 합니다.")
                tf = str(parts[0]).lower() or "1h"
                if tf not in {"15m", "1h", "4h"}:
                    raise ValueError("ATR 타임프레임은 15m/1h/4h 만 가능합니다.")
                k = _parse_float_range(parts[1], field="atr_trail_k", min_v=0.0, max_v=20.0)
                mn = _parse_float_range(parts[2], field="atr_trail_min_pct", min_v=0.0, max_v=100.0)
                mx = _parse_float_range(parts[3], field="atr_trail_max_pct", min_v=0.0, max_v=100.0)
                if mx < mn:
                    raise ValueError("ATR 최대폭은 ATR 최소폭보다 크거나 같아야 합니다.")
                payload.update(
                    {
                        "atr_trail_timeframe": tf,
                        "atr_trail_k": k,
                        "atr_trail_min_pct": mn,
                        "atr_trail_max_pct": mx,
                    }
                )
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


class SymbolLeverageModal(discord.ui.Modal, title="심볼 레버리지 설정"):
    symbol = discord.ui.TextInput(
        label="심볼",
        required=True,
        placeholder="예: BTCUSDT",
        max_length=16,
    )
    leverage = discord.ui.TextInput(
        label="레버리지 (0 입력 시 해제)",
        required=True,
        placeholder="예: 20 또는 0",
    )

    def __init__(
        self,
        *,
        api: TraderAPI,
        view: "PanelViewBase",
    ) -> None:
        super().__init__(timeout=300)
        self._api = api
        self._view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        symbol = str(self.symbol).strip().upper()
        if not symbol:
            await interaction.response.send_message("심볼을 입력해주세요.", ephemeral=True)
            return

        try:
            lev = _parse_float_range(str(self.leverage), field="leverage", min_v=0.0, max_v=50.0)
        except ValueError as e:
            await interaction.response.send_message(f"입력 오류: {e}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._api.set_symbol_leverage(symbol=symbol, leverage=lev)
            await self._view.refresh_message(interaction)
            if lev <= 0:
                await interaction.followup.send(f"{symbol} 개별 레버리지를 해제했습니다.", ephemeral=True)
            else:
                await interaction.followup.send(f"{symbol} 개별 레버리지를 {lev} 배로 설정했습니다.", ephemeral=True)
        except APIError as e:
            await interaction.followup.send(f"설정 실패: {e}", ephemeral=True)


class UniverseSymbolsModal(discord.ui.Modal, title="운영 심볼 설정"):
    universe_symbols = discord.ui.TextInput(
        label="운영 심볼 목록(콤마 구분)",
        required=True,
        placeholder="예: BTCUSDT,ETHUSDT,XAUUSDT",
        max_length=300,
        style=discord.TextStyle.paragraph,
    )

    def __init__(
        self,
        *,
        api: TraderAPI,
        view: "PanelViewBase",
        defaults: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(timeout=300)
        self._api = api
        self._view = view
        raw: list[str] = []
        cfg = defaults or {}
        symbols = cfg.get("universe_symbols")
        if isinstance(symbols, (list, tuple)):
            raw = [str(x).strip().upper() for x in symbols if str(x).strip()]
        if raw:
            self.universe_symbols.default = ",".join(raw)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        try:
            symbols = _parse_universe_symbols(str(self.universe_symbols))
        except ValueError as e:
            await interaction.response.send_message(f"입력 오류: {e}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._api.set_value("universe_symbols", ",".join(symbols))
            await self._view.refresh_message(interaction)
            await interaction.followup.send(f"운영 심볼을 {len(symbols)}개로 설정했습니다.", ephemeral=True)
        except APIError as e:
            await interaction.followup.send(f"설정 실패: {e}", ephemeral=True)


class UniverseSymbolRemoveModal(discord.ui.Modal, title="운영 심볼 해제"):
    symbol = discord.ui.TextInput(
        label="해제할 심볼",
        required=True,
        placeholder="예: XAUUSDT",
        max_length=16,
    )

    def __init__(
        self,
        *,
        api: TraderAPI,
        view: "PanelViewBase",
        defaults: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(timeout=300)
        self._api = api
        self._view = view
        self._defaults = defaults or {}

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        symbol = str(self.symbol).strip().upper()
        if not symbol:
            await interaction.response.send_message("심볼을 입력해주세요.", ephemeral=True)
            return

        current = self._defaults.get("universe_symbols", [])
        if not isinstance(current, list):
            try:
                payload = await self._api.get_status()
                risk_cfg = payload.get("risk_config") if isinstance(payload, dict) else {}
                current = risk_cfg.get("universe_symbols", [])
            except Exception:
                current = []
        if not isinstance(current, list):
            current = []

        current_set = [str(x).strip().upper() for x in current if str(x).strip()]
        filtered = [x for x in current_set if x != symbol]
        if not filtered:
            await interaction.response.send_message("해제 후 남은 심볼이 0개가 될 수 없습니다.", ephemeral=True)
            return
        if symbol not in current_set:
            await interaction.response.send_message(f"{symbol}는 현재 운영 심볼 목록에 없습니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._api.set_value("universe_symbols", ",".join(filtered))
            await self._view.refresh_message(interaction)
            await interaction.followup.send(f"{symbol}를 운영 심볼에서 해제했습니다.", ephemeral=True)
        except APIError as e:
            await interaction.followup.send(f"설정 실패: {e}", ephemeral=True)


class PanelViewBase(discord.ui.View):
    def __init__(self, *, api: TraderAPI, message_id: Optional[int] = None) -> None:
        super().__init__(timeout=None)
        self.api = api
        self.message_id = message_id

    @property
    def _mode(self) -> Literal["simple", "advanced"]:
        raise NotImplementedError

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not _is_admin(interaction):
            if interaction.response.is_done():
                await interaction.followup.send(ADMIN_ONLY_MSG, ephemeral=True)
            else:
                await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return False
        return True

    async def refresh_message(self, interaction: discord.Interaction) -> None:
        payload = await self.api.get_status()
        if not isinstance(payload, dict):
            payload = {}

        embed = _build_embed(payload, mode=self._mode)

        if interaction.message is not None:
            try:
                msg = interaction.message
            except Exception:
                msg = None
            if msg is not None and hasattr(msg, "edit"):
                try:
                    await msg.edit(embed=embed, view=self)
                    return
                except Exception as e:  # noqa: BLE001
                    logger.warning("panel_message_edit_failed", extra={"err": type(e).__name__})

        if self.message_id is None or interaction.channel is None or not hasattr(interaction.channel, "fetch_message"):
            return
        try:
            msg = await interaction.channel.fetch_message(self.message_id)
            await msg.edit(embed=embed, view=self)
        except Exception:
            pass

    async def _get_risk_config(self) -> Dict[str, Any]:
        try:
            payload = await self.api.get_status()
            if isinstance(payload, dict):
                return dict(payload.get("risk_config") or {})
        except Exception:
            logger.exception("panel_get_status_failed")
        return {}

    async def _swap_view(self, interaction: discord.Interaction, new_view: "PanelViewBase") -> None:
        payload = await self.api.get_status()
        if not isinstance(payload, dict):
            payload = {}
        embed = _build_embed(payload, mode=new_view._mode)
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=new_view)
        else:
            if interaction.message is not None:
                await interaction.message.edit(embed=embed, view=new_view)
            else:
                raise RuntimeError("interaction_message_missing")


class SimplePanelView(PanelViewBase):
    @property
    def _mode(self) -> Literal["simple", "advanced"]:
        return "simple"

    @discord.ui.button(label=START_BUTTON_LABEL, style=discord.ButtonStyle.success)
    async def start_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.api.start()
        await self.refresh_message(interaction)
        await interaction.followup.send("엔진을 시작했습니다.", ephemeral=True)

    @discord.ui.button(label=STOP_BUTTON_LABEL, style=discord.ButtonStyle.secondary)
    async def stop_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.api.stop()
        await self.refresh_message(interaction)
        await interaction.followup.send("엔진을 중지했습니다.", ephemeral=True)

    @discord.ui.button(label=PANIC_BUTTON_LABEL, style=discord.ButtonStyle.danger)
    async def panic_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.api.panic()
        await self.refresh_message(interaction)
        await interaction.followup.send("패닉 명령을 전송했습니다.", ephemeral=True)

    @discord.ui.button(label=TICK_ONCE_BUTTON_LABEL, style=discord.ButtonStyle.primary)
    async def tick_once_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            tick = await self.api.tick_scheduler_now()
        except APIError as e:
            await interaction.followup.send(f"즉시 판단 실행 실패: {e}", ephemeral=True)
            return

        await self.refresh_message(interaction)
        payload = tick if isinstance(tick, dict) else {}
        sched = payload.get("snapshot") if isinstance(payload, dict) else {}
        last_action = "-"
        last_error = ""
        if isinstance(sched, dict):
            last_action = str(sched.get("last_action") or "-")
            last_error = str(sched.get("last_error") or "")
        if last_error:
            reason = _reason_to_human_readable(last_error)
            msg = f"즉시 판단: {last_action}\n결과: BLOCKED - 사유: {reason}"
        else:
            msg = f"즉시 판단 실행 완료: {last_action}"
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label=MARGIN_BUDGET_BUTTON_LABEL, style=discord.ButtonStyle.secondary)
    async def margin_budget_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return

        current_budget = None
        try:
            payload = await self.api.get_status()
            if isinstance(payload, dict):
                cfg = payload.get("config") or {}
                if str(cfg.get("capital_mode") or "").upper() == "MARGIN_BUDGET_USDT":
                    cur = cfg.get("margin_budget_usdt")
                else:
                    cur = cfg.get("margin_budget_usdt")
                if cur is not None:
                    current_budget = float(cur)
        except Exception:
            current_budget = None

        await interaction.response.send_modal(
            MarginBudgetModal(
                api=self.api,
                view=self,
                current_budget=current_budget,
            )
        )

    @discord.ui.button(label=ADVANCED_TOGGLE_LABEL, style=discord.ButtonStyle.primary)
    async def advanced_toggle_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return

        await self._swap_view(interaction, AdvancedPanelView(api=self.api, message_id=self.message_id))


class AdvancedPanelView(PanelViewBase):
    @property
    def _mode(self) -> Literal["simple", "advanced"]:
        return "advanced"

    @discord.ui.button(label=START_BUTTON_LABEL, style=discord.ButtonStyle.success)
    async def start_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.api.start()
        await self.refresh_message(interaction)
        await interaction.followup.send("엔진을 시작했습니다.", ephemeral=True)

    @discord.ui.button(label=STOP_BUTTON_LABEL, style=discord.ButtonStyle.secondary)
    async def stop_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.api.stop()
        await self.refresh_message(interaction)
        await interaction.followup.send("엔진을 중지했습니다.", ephemeral=True)

    @discord.ui.button(label=PANIC_BUTTON_LABEL, style=discord.ButtonStyle.danger)
    async def panic_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.api.panic()
        await self.refresh_message(interaction)
        await interaction.followup.send("패닉 명령을 전송했습니다.", ephemeral=True)

    @discord.ui.button(label=TICK_ONCE_BUTTON_LABEL, style=discord.ButtonStyle.primary)
    async def tick_once_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            tick = await self.api.tick_scheduler_now()
        except APIError as e:
            await interaction.followup.send(f"즉시 판단 실행 실패: {e}", ephemeral=True)
            return

        await self.refresh_message(interaction)
        payload = tick if isinstance(tick, dict) else {}
        sched = payload.get("snapshot") if isinstance(payload, dict) else {}
        last_action = "-"
        last_error = ""
        if isinstance(sched, dict):
            last_action = str(sched.get("last_action") or "-")
            last_error = str(sched.get("last_error") or "")
        if last_error:
            reason = _reason_to_human_readable(last_error)
            msg = f"즉시 판단: {last_action}\n결과: BLOCKED - 사유: {reason}"
        else:
            msg = f"즉시 판단 실행 완료: {last_action}"
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label=MARGIN_BUDGET_BUTTON_LABEL, style=discord.ButtonStyle.secondary)
    async def margin_budget_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return

        current_budget = None
        try:
            payload = await self.api.get_status()
            if isinstance(payload, dict):
                cfg = payload.get("config") or {}
                if str(cfg.get("capital_mode") or "").upper() == "MARGIN_BUDGET_USDT":
                    cur = cfg.get("margin_budget_usdt")
                else:
                    cur = cfg.get("margin_budget_usdt")
                if cur is not None:
                    current_budget = float(cur)
        except Exception:
            current_budget = None

        await interaction.response.send_modal(
            MarginBudgetModal(
                api=self.api,
                view=self,
                current_budget=current_budget,
            )
        )

    @discord.ui.button(label=SIMPLE_TOGGLE_LABEL, style=discord.ButtonStyle.primary)
    async def simple_toggle_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await self._swap_view(interaction, SimplePanelView(api=self.api, message_id=self.message_id))

    @discord.ui.button(label=RISK_BASIC_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=1)
    async def risk_basic_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        defaults: Dict[str, Any] = await self._get_risk_config()
        await interaction.response.send_modal(RiskBasicModal(api=self.api, view=self, defaults=defaults))

    @discord.ui.button(label=RISK_ADVANCED_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=1)
    async def risk_adv_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        defaults: Dict[str, Any] = await self._get_risk_config()
        await interaction.response.send_modal(RiskAdvancedModal(api=self.api, view=self, defaults=defaults))

    @discord.ui.button(label=TRAILING_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=1)
    async def trailing_config_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        defaults: Dict[str, Any] = await self._get_risk_config()
        await interaction.response.send_modal(TrailingConfigModal(api=self.api, view=self, defaults=defaults))

    @discord.ui.button(label=SYMBOL_LEVERAGE_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=1)
    async def symbol_leverage_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(SymbolLeverageModal(api=self.api, view=self))

    @discord.ui.button(label=UNIVERSE_SYMBOLS_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=1)
    async def universe_symbols_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        defaults: Dict[str, Any] = await self._get_risk_config()
        await interaction.response.send_modal(
            UniverseSymbolsModal(
                api=self.api,
                view=self,
                defaults=defaults,
            )
        )

    @discord.ui.button(label=UNIVERSE_REMOVE_SYMBOL_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=4)
    async def universe_remove_symbol_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        defaults: Dict[str, Any] = await self._get_risk_config()
        await interaction.response.send_modal(
            UniverseSymbolRemoveModal(
                api=self.api,
                view=self,
                defaults=defaults,
            )
        )

    @discord.ui.select(
        placeholder=EXEC_MODE_SELECT_PLACEHOLDER,
        options=[
            discord.SelectOption(label="LIMIT", value="LIMIT", default=True),
            discord.SelectOption(label="MARKET", value="MARKET"),
            discord.SelectOption(label="SPLIT", value="SPLIT"),
        ],
        row=3,
    )
    async def exec_mode_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if not await self._guard(interaction):
            return
        val = str(select.values[0]).upper()
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.api.set_value("exec_mode_default", val)
        await self.refresh_message(interaction)
        await interaction.followup.send(f"실행모드가 {val}로 변경되었습니다.", ephemeral=True)

    @discord.ui.select(
        placeholder=SCHEDULER_INTERVAL_SELECT_PLACEHOLDER,
        options=[
            discord.SelectOption(label="5분", value="300"),
            discord.SelectOption(label="10분", value="600"),
            discord.SelectOption(label="15분", value="900"),
            discord.SelectOption(label="30분", value="1800", default=True),
            discord.SelectOption(label="60분", value="3600"),
        ],
        row=2,
    )
    async def scheduler_interval_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if not await self._guard(interaction):
            return
        try:
            tick_sec = float(str(select.values[0]))
        except Exception:
            await interaction.response.send_message("유효하지 않은 간격입니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.api.set_scheduler_interval(tick_sec)
            await self.refresh_message(interaction)
            minutes = int(tick_sec // 60)
            await interaction.followup.send(
                f"판단 주기를 {minutes}분으로 변경했습니다.\n"
                f"상태 알림도 같은 주기로({minutes}분) 맞췄습니다.",
                ephemeral=True,
            )
        except APIError as e:
            await interaction.followup.send(f"스캔 간격 변경 실패: {e}", ephemeral=True)

    @discord.ui.select(
        placeholder=NOTIFY_INTERVAL_SELECT_PLACEHOLDER,
        options=[
            discord.SelectOption(label="10초", value="10"),
            discord.SelectOption(label="30초", value="30"),
            discord.SelectOption(label="1분", value="60"),
            discord.SelectOption(label="5분", value="300"),
            discord.SelectOption(label="10분", value="600"),
            discord.SelectOption(label="15분", value="900"),
            discord.SelectOption(label="30분", value="1800", default=True),
            discord.SelectOption(label="60분", value="3600"),
        ],
        row=3,
    )
    async def notify_interval_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if not await self._guard(interaction):
            return
        try:
            notify_sec = float(str(select.values[0]))
        except Exception:
            await interaction.response.send_message("유효하지 않은 간격입니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._api.set_value("notify_interval_sec", str(int(notify_sec)))
            await self.refresh_message(interaction)
            if notify_sec >= 60:
                minutes = int(notify_sec // 60)
                await interaction.followup.send(f"상태 알림 주기를 {minutes}분으로 변경했습니다.", ephemeral=True)
            else:
                await interaction.followup.send(
                    f"상태 알림 주기를 {int(notify_sec)}초로 변경했습니다.", ephemeral=True
                )
        except APIError as e:
            await interaction.followup.send(f"상태 알림 주기 변경 실패: {e}", ephemeral=True)


PanelView = SimplePanelView


