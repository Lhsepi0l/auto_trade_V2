from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Literal

import discord

from v2.discord_bot.services.api_client import APIError
from v2.discord_bot.services.contracts import TraderAPI
from v2.discord_bot.services.discord_utils import is_admin as _is_admin
from v2.discord_bot.services.discord_utils import safe_defer as _safe_defer
from v2.discord_bot.services.formatting import format_status_payload
from v2.discord_bot.ui_labels import (
    ADVANCED_TOGGLE_LABEL,
    EXEC_MODE_SELECT_PLACEHOLDER,
    MARGIN_BUDGET_BUTTON_LABEL,
    PANIC_BUTTON_LABEL,
    RISK_ADVANCED_BUTTON_LABEL,
    RISK_BASIC_BUTTON_LABEL,
    SCHEDULER_INTERVAL_SELECT_PLACEHOLDER,
    SCORING_SETUP_BUTTON_LABEL,
    SIMPLE_PANEL_BUTTON_LABELS,
    SIMPLE_TOGGLE_LABEL,
    START_BUTTON_LABEL,
    STOP_BUTTON_LABEL,
    SYMBOL_LEVERAGE_BUTTON_LABEL,
    TICK_ONCE_BUTTON_LABEL,
    TRAILING_BUTTON_LABEL,
    UNIVERSE_REMOVE_SYMBOL_BUTTON_LABEL,
    UNIVERSE_SYMBOLS_BUTTON_LABEL,
)

logger = logging.getLogger(__name__)

JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
JSONPayload = dict[str, JSONValue]


def _as_dict(value: object) -> JSONPayload:
    return value if isinstance(value, dict) else {}


def _coerce_float(value: JSONValue, *, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _as_str_list(value: JSONValue) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    return []


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
            SCORING_SETUP_BUTTON_LABEL,
            SIMPLE_TOGGLE_LABEL,
        ]
    )
    + " 와 실행모드/판단 주기 선택이 같이 표시됩니다."
    + " 상태알림 주기도 같이 설정 가능합니다."
)

REASON_HINT_MAP: dict[str, str] = {
    "no_candidate": "현재 진입 후보가 없습니다.",
    "vol_shock_no_entry": "변동성 급등 구간이라 신규 진입이 보류됩니다.",
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
    "tick_busy": "이전 판단(자동 스캔/즉시 판단)이 아직 진행 중입니다. 1~2초 후 다시 시도해주세요.",
}

REASON_PREFIX_HINTS = {
    "min_hold_active:": "최소 보유 시간 조건이 활성화되어 있습니다",
    "sizing_blocked:": "주문 계산이 차단됨",
    "spread_too_wide_market_disabled:": "스프레드가 너무 넓어 시장가 주문이 비활성입니다",
    "daily_loss_limit_reached:": "일일 손실 제한에 걸림",
    "dd_limit_reached:": "DD 제한에 걸림",
    "notional_": "주문 기준금액이 제한 조건을 벗어났습니다",
    "cycle_failed:": "즉시 판단 실행 중 내부 오류가 발생했습니다",
    "bracket_failed:": "진입 후 TP/SL 브래킷 주문 생성에 실패했습니다",
}

BALANCE_ERROR_HINT_MAP: dict[str, str] = {
    "rest_client_unavailable": "실시간 잔고 연결이 비활성 상태입니다 (키/모드 확인).",
    "balance_fetch_timeout": "바이낸스 응답이 지연되고 있습니다. 잠시 후 다시 시도해주세요.",
    "balance_auth_failed": "API 키 권한/유효성/IP 화이트리스트를 확인해주세요.",
    "balance_rate_limited": "요청 제한에 걸렸습니다. 잠시 후 다시 시도해주세요.",
    "balance_fetch_failed": "바이낸스 잔고 조회가 일시 실패했습니다. 잠시 후 자동 재시도됩니다.",
    "balance_payload_invalid": "잔고 응답 형식이 올바르지 않습니다.",
    "usdt_asset_missing": "잔고 응답에 USDT 항목이 없습니다.",
}

_SYMBOL_LEVERAGE_ERROR_MESSAGES: dict[str, str] = {
    "symbol_leverage_exceeds_max_leverage": "개별 레버리지는 계정의 max_leverage 이하로 설정해야 합니다.",
    "symbol_leverage_must_be_between_0_and_50": "개별 레버리지는 0~50 사이(0은 해제)로 입력해야 합니다.",
    "symbol_leverage_must_be_float": "개별 레버리지는 숫자(실수)로 입력해야 합니다.",
    "symbol_is_required": "심볼을 입력해주세요.",
}


def _extract_api_validation_message(error: APIError) -> tuple[str, list[dict[str, str]]]:
    raw = error.details
    if not raw:
        return str(error), []
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return str(error), []

    if not isinstance(payload, dict):
        return str(error), []
    if payload.get("message") != "validation_failed":
        return str(error), []

    errors = payload.get("errors")
    if not isinstance(errors, list):
        return str(error), []

    parsed: list[dict[str, str]] = []
    for item in errors:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        msg = item.get("message")
        if isinstance(field, str) and isinstance(msg, str):
            parsed.append({"field": field, "message": msg})
    if not parsed:
        return str(error), []
    return "validation_failed", parsed


def _format_symbol_leverage_error(
    error: APIError, *, symbol: str, max_leverage: float | None = None
) -> str:
    base, errors = _extract_api_validation_message(error)
    if base != "validation_failed" or not errors:
        return f"설정 실패: {error}"

    hints: list[str] = []
    for item in errors:
        msg_key = str(item.get("message") or "")
        if msg_key in _SYMBOL_LEVERAGE_ERROR_MESSAGES:
            hint = _SYMBOL_LEVERAGE_ERROR_MESSAGES[msg_key]
            if msg_key == "symbol_leverage_exceeds_max_leverage" and max_leverage is not None:
                hint = f"{hint} (현재 max_leverage = {max_leverage:g})"
            hints.append(f"{symbol}: {hint}")
        else:
            hints.append(f"{symbol}: {msg_key or '입력값이 유효하지 않습니다.'}")

    if not hints:
        return f"설정 실패: {error}"
    return "\n".join(hints)


def _fmt_money(v: JSONValue, *, digits: int = 4) -> str:
    try:
        return f"{float(str(v)):.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _parse_iso_to_aware_datetime(value: JSONValue) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return datetime(
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
                tzinfo=timezone.utc,
            )
        return value

    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return datetime(
            dt.year,
            dt.month,
            dt.day,
            dt.hour,
            dt.minute,
            dt.second,
            dt.microsecond,
            tzinfo=timezone.utc,
        )
    return dt


def _next_decision_eta(payload: JSONPayload) -> str:
    sched = _as_dict(payload.get("scheduler"))
    tick_sec = _coerce_float(sched.get("tick_sec"), default=1800.0)
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


def _build_last_result(
    last_action: str, last_error: str | None, last_decision: str | None = None
) -> tuple[str, str]:
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


def _build_tick_once_message(payload: JSONPayload) -> str:
    sched = payload.get("snapshot") if isinstance(payload, dict) else {}
    sched_map = _as_dict(sched)

    last_action = _normalize_last_action(str(sched_map.get("last_action") or "-"))
    last_error = str(sched_map.get("last_error") or "")
    last_decision = _normalize_last_decision(str(sched_map.get("last_decision_reason") or "-"))

    if last_error:
        reason = _reason_to_human_readable(last_error)
        return f"즉시 판단: {last_action}\n결과: BLOCKED - 사유: {reason}"

    if last_action == "no_candidate":
        decision_reason = last_decision if last_decision != "-" else "no_candidate"
        reason = _reason_to_human_readable(decision_reason)
        return f"즉시 판단: {last_action}\n결과: 대기 - 사유: {reason}"

    mapped_reason = ""
    if last_decision != "-" and last_decision != last_action:
        mapped_reason = _reason_to_human_readable(last_decision)
    elif last_action in REASON_HINT_MAP:
        mapped_reason = _reason_to_human_readable(last_action)
    else:
        for prefix in REASON_PREFIX_HINTS:
            if last_action.startswith(prefix):
                mapped_reason = _reason_to_human_readable(last_action)
                break

    if mapped_reason:
        return f"즉시 판단: {last_action}\n사유: {mapped_reason}"

    return f"즉시 판단 실행 완료: {last_action}"


def _format_tick_runtime_error(err: RuntimeError) -> str:
    raw = str(err)
    if "network_error: ReadTimeout" in raw:
        return (
            "즉시 판단 실행 실패: API 응답 시간이 초과되었습니다. "
            "잠시 후 다시 시도하거나 TRADER_API_TIMEOUT_SEC 값을 늘려주세요."
        )
    return f"즉시 판단 실행 실패: {raw}"


def _build_live_balance_line(payload: JSONPayload) -> str:
    data = payload if isinstance(payload, dict) else {}
    binance = _as_dict(data.get("binance"))
    usdt = _as_dict(binance.get("usdt_balance"))
    source = str(usdt.get("source") or "").strip().lower()
    private_error = str(binance.get("private_error") or "").strip()
    private_error_detail = str(binance.get("private_error_detail") or "").strip()
    available = usdt.get("available")
    wallet = usdt.get("wallet")

    if source and source not in {"exchange", "exchange_cached"}:
        hint = BALANCE_ERROR_HINT_MAP.get(private_error) or "연결/API 권한 확인 필요"
        if private_error_detail and private_error in {
            "balance_auth_failed",
            "balance_rate_limited",
        }:
            hint = f"{hint} ({private_error_detail})"
        return f"실시간 잔고: 바이낸스 실시간 조회 실패 ({hint})"
    if available is None and wallet is None:
        hint = BALANCE_ERROR_HINT_MAP.get(private_error) or "연결/API 권한 확인 필요"
        if private_error_detail and private_error in {
            "balance_auth_failed",
            "balance_rate_limited",
        }:
            hint = f"{hint} ({private_error_detail})"
        return f"실시간 잔고: 바이낸스 실시간 조회 실패 ({hint})"

    suffix = " (최근 캐시)" if source == "exchange_cached" else ""
    return (
        f"실시간 잔고: 사용가능 {_fmt_money(available)} USDT / "
        f"지갑 {_fmt_money(wallet)} USDT{suffix}"
    )


def _interval_label(sec: JSONScalar) -> str:
    sec_f = _coerce_float(sec, default=-1.0)
    if sec_f <= 0:
        return "미지정"
    tick_min = sec_f / 60.0
    if tick_min.is_integer():
        return f"{int(tick_min)}분"
    if sec_f < 60:
        return f"{sec_f:.0f}초"
    return f"{tick_min:.1f}분"


def _scan_interval_label(payload: JSONPayload) -> str:
    sched = _as_dict(payload.get("scheduler"))
    tick_sec_raw = sched.get("tick_sec")
    tick_sec = _coerce_float(tick_sec_raw, default=1800.0)
    return _interval_label(tick_sec)


def _notify_interval_label(payload: JSONPayload) -> str:
    risk = _as_dict(payload.get("risk_config"))
    notify_raw = risk.get("notify_interval_sec")
    notify_sec = _coerce_float(notify_raw, default=1800.0)
    return _interval_label(notify_sec)


def _budget_display(payload: JSONPayload) -> tuple[str, str]:
    cfg = _as_dict(payload.get("config"))
    cap = _as_dict(payload.get("capital_snapshot"))

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


def _build_simple_lines(payload: JSONPayload) -> tuple[str, str, str, str, str, str]:
    eng = _as_dict(payload.get("engine_state"))
    dry_run = bool(payload.get("dry_run", False))
    state = str(eng.get("state", "UNKNOWN"))

    sched = _as_dict(payload.get("scheduler"))
    last_action = _normalize_last_action(str(sched.get("last_action") or "-"))
    last_decision = str(sched.get("last_decision_reason") or "-")
    last_error = sched.get("last_error")
    last_status, last_reason = _build_last_result(
        last_action, str(last_error) if last_error is not None else None, last_decision
    )
    if last_reason:
        last_result = (
            " - ".join([x for x in (last_status, str(last_reason)) if x not in {"-", ""}]) or "-"
        )
    else:
        last_result = "아직 판단 없음"

    decision_hint = f"엔진: {state}\n드라이런: {'ON' if dry_run else 'OFF'}"
    next_decision = _next_decision_eta(payload)
    budget, order_amt = _budget_display(payload)
    margin_line = f"현재 증거금 {budget} USDT"
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


def _build_advanced_lines(payload: JSONPayload) -> list[str]:
    risk = _as_dict(payload.get("risk_config"))
    cfg = _as_dict(payload.get("config"))
    wd = _as_dict(payload.get("watchdog"))
    symbol_map = _as_dict(risk.get("symbol_leverage_map"))
    symbol_items = [
        f"{str(sym)}={_fmt_money(lev)}"
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
        f"운영 심볼={_join_symbols_preview(_as_str_list(risk.get('universe_symbols')), limit=180)}",
        "트레일링="
        + (
            f"ON({risk.get('trailing_mode')}, 시작={risk.get('trail_arm_pnl_pct')}%, "
            f"distance={risk.get('trail_distance_pnl_pct') if risk.get('trailing_mode') == 'PCT' else (wd.get('last_trailing_distance_pct') or '-')}%)"
        ),
    ]
    if symbol_items:
        lines.append("심볼 레버리지=" + ", ".join(symbol_items))
    return lines


def build_embed(
    payload: JSONPayload, *, mode: Literal["simple", "advanced"] = "simple"
) -> discord.Embed:
    payload = payload if isinstance(payload, dict) else {}
    title = "오토트레이더 패널 (간단)" if mode == "simple" else "오토트레이더 패널 (고급)"
    em = discord.Embed(title=title)
    if mode == "simple":
        em.description = "현재 화면 버튼: " + "/".join(SIMPLE_PANEL_BUTTON_LABELS)
    else:
        em.description = "고급 화면 버튼: " + "/".join(
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
    line1, next_decision, margin_line, order_line, judge_line, interval_line = _build_simple_lines(
        payload
    )
    _ = em.add_field(name="엔진 상태", value=line1, inline=False)
    _ = em.add_field(name="다음 판단", value=next_decision, inline=True)
    _ = em.add_field(name="현재 증거금", value=margin_line, inline=True)
    _ = em.add_field(name="예상 주문금액", value=order_line, inline=True)
    _ = em.add_field(name="운영 주기", value=interval_line, inline=True)
    _ = em.add_field(name="마지막 결과", value=judge_line, inline=False)
    _ = em.add_field(
        name="한 번에 보기", value=HELP_SIMPLE if mode == "simple" else HELP_ADVANCED, inline=False
    )

    if mode == "advanced":
        adv = _build_advanced_lines(payload)
        if adv:
            _ = em.add_field(name="고급 상태", value="\n".join(adv), inline=False)
        _ = em.add_field(
            name="디버그 상태",
            value=f"```text\n{format_status_payload(payload)}\n```",
            inline=False,
        )

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

    def __init__(
        self, *, api: TraderAPI, view: "PanelViewBase", defaults: JSONPayload | None = None
    ) -> None:
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
            _ = await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        pairs = {
            "max_leverage": str(self.max_leverage),
            "max_exposure_pct": str(self.max_exposure_pct),
            "max_notional_pct": str(self.max_notional_pct),
            "per_trade_risk_pct": str(self.per_trade_risk_pct),
        }
        _ = await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            for k, v in pairs.items():
                _ = await self._api.set_value(k, v)
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

    def __init__(
        self, *, api: TraderAPI, view: "PanelViewBase", defaults: JSONPayload | None = None
    ) -> None:
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
            _ = await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        pairs = {
            "daily_loss_limit_pct": str(self.daily_loss_limit_pct),
            "dd_limit_pct": str(self.dd_limit_pct),
            "min_hold_minutes": str(self.min_hold_minutes),
            "score_conf_threshold": str(self.score_conf_threshold),
        }
        _ = await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            for k, v in pairs.items():
                _ = await self._api.set_value(k, v)
            await self._view.refresh_message(interaction)
            await interaction.followup.send("리스크 고급값을 업데이트했습니다.", ephemeral=True)
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)


class ScoringSetupModal(discord.ui.Modal, title="판단식 설정"):
    tf_weights = discord.ui.TextInput(
        label="시간봉 가중치(콤마 구분)",
        placeholder="예: 10m=0.25,15m=0.0,30m=0.25,1h=0.25,4h=0.25",
        required=True,
        max_length=140,
    )
    score_tf_15m_enabled = discord.ui.TextInput(
        label="15m 사용 (예/아니오)",
        placeholder="예: 예",
        required=True,
    )
    score_conf_threshold = discord.ui.TextInput(
        label="신뢰도 임계값(0~1)",
        placeholder="예: 0.60",
        required=True,
    )
    score_gap_threshold = discord.ui.TextInput(
        label="점수 갭 임계값(0~1)",
        placeholder="예: 0.15",
        required=True,
    )

    def __init__(
        self, *, api: TraderAPI, view: "PanelViewBase", defaults: JSONPayload | None = None
    ) -> None:
        super().__init__(timeout=300)
        self._api = api
        self._view = view
        d = defaults or {}
        weights = {
            "10m": d.get("tf_weight_10m", 0.25),
            "15m": d.get("tf_weight_15m", 0.0),
            "30m": d.get("tf_weight_30m", 0.25),
            "1h": d.get("tf_weight_1h", 0.25),
            "4h": d.get("tf_weight_4h", 0.25),
        }
        self.tf_weights.default = ",".join(
            [f"{k}={weights[k]}" for k in ("10m", "15m", "30m", "1h", "4h")]
        )
        self.score_tf_15m_enabled.default = (
            "예" if bool(d.get("score_tf_15m_enabled", False)) else "아니오"
        )
        self.score_conf_threshold.default = str(d.get("score_conf_threshold", "0.60"))
        self.score_gap_threshold.default = str(d.get("score_gap_threshold", "0.15"))

    @staticmethod
    def _parse_weight_text(raw: str) -> dict[str, float]:
        parts = [p.strip() for p in str(raw).split(",") if p.strip()]
        parsed = {}
        order = ["10m", "15m", "30m", "1h", "4h"]
        if not parts:
            return parsed

        has_key = all("=" in p for p in parts)
        if has_key:
            for part in parts:
                if "=" not in part:
                    raise ValueError("시간봉 가중치는 key=value 형태 또는 숫자 5개 이어야 합니다.")
                k, v = part.split("=", 1)
                key = k.strip().lower().replace(" ", "")
                if key not in {"10m", "15m", "30m", "1h", "4h"}:
                    raise ValueError("지원하지 않는 시간봉이 포함되어 있습니다.")
                parsed[key] = _parse_float_range(v, field=f"tf_weight_{key}", min_v=0.0, max_v=1.0)
            return parsed

        if len(parts) != 5:
            raise ValueError("숫자 입력은 5개(10m,15m,30m,1h,4h)를 모두 넣어주세요.")
        for key, val in zip(order, parts, strict=True):
            parsed[key] = _parse_float_range(val, field=f"tf_weight_{key}", min_v=0.0, max_v=1.0)
        return parsed

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            _ = await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        try:
            parsed_weights = self._parse_weight_text(str(self.tf_weights))
            weight_10m = parsed_weights.get("10m", 0.25)
            weight_15m = parsed_weights.get("15m", 0.0)
            weight_30m = parsed_weights.get("30m", 0.25)
            weight_1h = parsed_weights.get("1h", 0.25)
            weight_4h = parsed_weights.get("4h", 0.25)
            conf = _parse_float_range(
                str(self.score_conf_threshold), field="score_conf_threshold", min_v=0.0, max_v=1.0
            )
            gap = _parse_float_range(
                str(self.score_gap_threshold), field="score_gap_threshold", min_v=0.0, max_v=1.0
            )
            enabled_15m = _parse_bool_like(
                str(self.score_tf_15m_enabled), field="score_tf_15m_enabled"
            )
        except ValueError as e:
            _ = await interaction.response.send_message(f"입력 오류: {e}", ephemeral=True)
            return

        if weight_10m + weight_15m + weight_30m + weight_1h + weight_4h <= 0:
            _ = await interaction.response.send_message(
                "입력 오류: 가중치 합계는 0보다 커야 합니다.", ephemeral=True
            )
            return
        if not enabled_15m:
            weight_15m = 0.0

        _ = await interaction.response.defer(ephemeral=True, thinking=True)
        pairs = {
            "tf_weight_10m": str(weight_10m),
            "tf_weight_15m": str(weight_15m),
            "tf_weight_30m": str(weight_30m),
            "tf_weight_1h": str(weight_1h),
            "tf_weight_4h": str(weight_4h),
            "score_tf_15m_enabled": str(enabled_15m),
            "score_conf_threshold": str(conf),
            "score_gap_threshold": str(gap),
        }
        try:
            for k, v in pairs.items():
                _ = await self._api.set_value(k, v)
            await self._view.refresh_message(interaction)
            await interaction.followup.send(
                "판단식(시간봉/가중치/임계값) 설정을 업데이트했습니다.",
                ephemeral=True,
            )
        except APIError as e:
            await interaction.followup.send(f"판단식 설정 실패: {e}", ephemeral=True)


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
        current_budget: float | None = None,
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
            _ = await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        try:
            amount = _sanitize_usdt_input(str(self.amount_usdt))
        except ValueError as e:
            _ = await interaction.response.send_message(f"입력 오류: {e}", ephemeral=True)
            return

        _ = await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _ = await self._api.set_config(
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

    def __init__(
        self, *, api: TraderAPI, view: "PanelViewBase", defaults: JSONPayload | None = None
    ) -> None:
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
            _ = await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        try:
            enabled = _parse_bool_like(str(self.trailing_enabled), field="trailing_enabled")
            mode_raw = str(self.trailing_mode).strip().upper()
            mode_alias = {"퍼센트": "PCT", "PERCENT": "PCT", "PCT": "PCT", "ATR": "ATR"}
            mode = mode_alias.get(mode_raw, mode_raw)
            if mode not in {"PCT", "ATR"}:
                raise ValueError("트레일링 모드는 PCT 또는 ATR만 가능합니다.")
            arm = _parse_float_range(
                str(self.trail_arm_pnl_pct), field="trail_arm_pnl_pct", min_v=0.0, max_v=100.0
            )
            grace = _parse_int_range(
                str(self.trail_grace_minutes), field="trail_grace_minutes", min_v=0, max_v=1440
            )

            payload: JSONPayload = {
                "trailing_enabled": enabled,
                "trailing_mode": mode,
                "trail_arm_pnl_pct": arm,
                "trail_grace_minutes": grace,
            }

            params = str(self.mode_params or "").strip()
            if mode == "PCT":
                raw_dist = params or "0.8"
                dist = _parse_float_range(
                    raw_dist, field="trail_distance_pnl_pct", min_v=0.0, max_v=100.0
                )
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
            _ = await interaction.response.send_message(f"입력 오류: {e}", ephemeral=True)
            return

        _ = await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _ = await self._api.set_config(payload)
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
            _ = await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        symbol = str(self.symbol).strip().upper()
        if not symbol:
            _ = await interaction.response.send_message("심볼을 입력해주세요.", ephemeral=True)
            return

        max_leverage = None
        try:
            status_payload = await self._api.get_status()
            risk_cfg = _as_dict(status_payload.get("risk_config"))
            raw = risk_cfg.get("max_leverage")
            if raw is not None:
                max_leverage = _coerce_float(raw, default=0.0)
        except (TypeError, ValueError):
            max_leverage = None

        try:
            lev = _parse_float_range(str(self.leverage), field="leverage", min_v=0.0, max_v=50.0)
            if max_leverage is not None and max_leverage > 0 and lev > max_leverage:
                raise ValueError(
                    f"symbol_leverage_exceeds_max_leverage (현재 max_leverage={max_leverage:g})"
                )
        except ValueError as e:
            msg = str(e)
            if msg.startswith("symbol_leverage_exceeds_max_leverage"):
                _ = await interaction.response.send_message(
                    f"입력 제한: {symbol} 개별 레버리지는 max_leverage 이하로만 설정 가능합니다."
                    + (
                        f" (현재 max_leverage={max_leverage:g})" if max_leverage is not None else ""
                    ),
                    ephemeral=True,
                )
            else:
                _ = await interaction.response.send_message(f"입력 오류: {e}", ephemeral=True)
            return

        _ = await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _ = await self._api.set_symbol_leverage(symbol=symbol, leverage=lev)
            await self._view.refresh_message(interaction)
            if lev <= 0:
                await interaction.followup.send(
                    f"{symbol} 개별 레버리지를 해제했습니다.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"{symbol} 개별 레버리지를 {lev} 배로 설정했습니다.", ephemeral=True
                )
        except APIError as e:
            await interaction.followup.send(
                _format_symbol_leverage_error(error=e, symbol=symbol, max_leverage=max_leverage),
                ephemeral=True,
            )


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
        defaults: JSONPayload | None = None,
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
            _ = await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        try:
            symbols = _parse_universe_symbols(str(self.universe_symbols))
        except ValueError as e:
            _ = await interaction.response.send_message(f"입력 오류: {e}", ephemeral=True)
            return

        _ = await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _ = await self._api.set_value("universe_symbols", ",".join(symbols))
            await self._view.refresh_message(interaction)
            await interaction.followup.send(
                f"운영 심볼을 {len(symbols)}개로 설정했습니다.", ephemeral=True
            )
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
        defaults: JSONPayload | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self._api = api
        self._view = view
        self._defaults = defaults or {}

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            _ = await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        symbol = str(self.symbol).strip().upper()
        if not symbol:
            _ = await interaction.response.send_message("심볼을 입력해주세요.", ephemeral=True)
            return

        current = self._defaults.get("universe_symbols", [])
        if not isinstance(current, list):
            payload = await self._api.get_status()
            if isinstance(payload, dict):
                risk_cfg = payload.get("risk_config")
                if isinstance(risk_cfg, dict):
                    current = risk_cfg.get("universe_symbols", [])
        if not isinstance(current, list):
            current = []

        current_set = [str(x).strip().upper() for x in current if str(x).strip()]
        filtered = [x for x in current_set if x != symbol]
        if not filtered:
            _ = await interaction.response.send_message(
                "해제 후 남은 심볼이 0개가 될 수 없습니다.", ephemeral=True
            )
            return
        if symbol not in current_set:
            _ = await interaction.response.send_message(
                f"{symbol}는 현재 운영 심볼 목록에 없습니다.", ephemeral=True
            )
            return

        _ = await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _ = await self._api.set_value("universe_symbols", ",".join(filtered))
            await self._view.refresh_message(interaction)
            await interaction.followup.send(
                f"{symbol}를 운영 심볼에서 해제했습니다.", ephemeral=True
            )
        except APIError as e:
            await interaction.followup.send(f"설정 실패: {e}", ephemeral=True)


class NotifyIntervalModal(discord.ui.Modal, title="상태 알림 주기 설정"):
    notify_interval = discord.ui.TextInput(
        label="알림 주기 (초/분)",
        placeholder="예: 60, 5m, 1m, 30초, 1분",
        required=True,
    )

    def __init__(
        self, *, api: TraderAPI, view: "PanelViewBase", defaults: JSONPayload | None = None
    ) -> None:
        super().__init__(timeout=300)
        self._api = api
        self._view = view
        cur = None
        if defaults is not None:
            risk_cfg = _as_dict(defaults.get("risk_config"))
            cur = risk_cfg.get("notify_interval_sec")
        if cur is None and defaults is not None:
            cur = defaults.get("notify_interval_sec")
        if cur is not None:
            self.notify_interval.default = str(cur)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            _ = await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return

        raw = str(self.notify_interval).strip().replace(" ", "").replace(",", "").lower()
        if not raw:
            _ = await interaction.response.send_message("알림 주기를 입력해주세요.", ephemeral=True)
            return

        sec = None
        try:
            if raw.endswith("분"):
                sec = float(raw[:-1]) * 60
            elif raw.endswith("초"):
                sec = float(raw[:-1])
            elif raw.endswith("m"):
                sec = float(raw[:-1]) * 60
            elif raw.endswith("s"):
                sec = float(raw[:-1])
            else:
                sec = float(raw)
        except (TypeError, ValueError):
            sec = None

        if sec is None or sec < 1:
            _ = await interaction.response.send_message(
                "알림 주기를 초 단위 정수 또는 분(m)로 입력해주세요.", ephemeral=True
            )
            return

        sec_i = int(sec)
        _ = await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _ = await self._api.set_value("notify_interval_sec", str(sec_i))
            await self._view.refresh_message(interaction)
            if sec_i % 60 == 0 and sec_i >= 60:
                await interaction.followup.send(
                    f"상태 알림 주기를 {sec_i // 60}분으로 변경했습니다.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"상태 알림 주기를 {sec_i}초로 변경했습니다.", ephemeral=True
                )
        except APIError as e:
            await interaction.followup.send(f"상태 알림 주기 설정 실패: {e}", ephemeral=True)


class PanelViewBase(discord.ui.View):
    def __init__(
        self,
        *,
        api: TraderAPI,
        message_id: int | None = None,
        initial_payload: JSONPayload | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.api = api
        self.message_id = message_id
        self._status_cache: JSONPayload = (
            initial_payload if isinstance(initial_payload, dict) else {}
        )
        self._status_cache_at: float = time.monotonic() if self._status_cache else 0.0

    @property
    def _mode(self) -> Literal["simple", "advanced"]:
        raise NotImplementedError

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not _is_admin(interaction):
            if interaction.response.is_done():
                await interaction.followup.send(ADMIN_ONLY_MSG, ephemeral=True)
            else:
                _ = await interaction.response.send_message(ADMIN_ONLY_MSG, ephemeral=True)
            return False
        return True

    def _update_status_cache(self, payload: JSONPayload) -> JSONPayload:
        self._status_cache = payload if isinstance(payload, dict) else {}
        self._status_cache_at = time.monotonic() if self._status_cache else 0.0
        return self._status_cache

    def _get_cached_status(self, *, max_age_sec: float) -> JSONPayload | None:
        if not self._status_cache:
            return None
        if (time.monotonic() - self._status_cache_at) <= max_age_sec:
            return self._status_cache
        return None

    async def _fetch_status_cached(
        self,
        *,
        max_age_sec: float = 1.2,
        timeout_sec: float = 2.0,
        force: bool = False,
    ) -> JSONPayload:
        if not force:
            cached = self._get_cached_status(max_age_sec=max_age_sec)
            if cached is not None:
                return cached
        try:
            status = await asyncio.wait_for(self.api.get_status(), timeout=timeout_sec)
            if isinstance(status, dict):
                return self._update_status_cache(status)
        except (RuntimeError, APIError, asyncio.TimeoutError):
            logger.exception("panel_get_status_failed")
        except Exception:  # noqa: BLE001
            logger.exception("panel_get_status_failed")
        return self._get_cached_status(max_age_sec=3600.0) or {}

    async def _run_action(
        self,
        interaction: discord.Interaction,
        *,
        action: Callable[[], Awaitable[object]],
        success_message: str,
    ) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            _ = await action()
            _ = await self.refresh_message(interaction, force_status=True)
            await interaction.followup.send(success_message, ephemeral=True)
        except APIError as e:
            await interaction.followup.send(f"API 오류: {e}", ephemeral=True)
        except RuntimeError as e:
            await interaction.followup.send(f"실행 실패: {e}", ephemeral=True)
        except Exception:  # noqa: BLE001
            logger.exception("panel_action_failed")
            await interaction.followup.send(
                "실행 실패: 내부 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )

    async def _run_tick_once(self, interaction: discord.Interaction) -> None:
        if not await _safe_defer(interaction):
            return
        try:
            tick = await self.api.tick_scheduler_now()
        except (APIError, RuntimeError) as e:
            if isinstance(e, RuntimeError):
                await interaction.followup.send(_format_tick_runtime_error(e), ephemeral=True)
            else:
                await interaction.followup.send(f"즉시 판단 실행 실패: {e}", ephemeral=True)
            return
        except Exception:  # noqa: BLE001
            logger.exception("panel_tick_once_failed")
            await interaction.followup.send(
                "즉시 판단 실행 실패: 내부 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )
            return

        payload = tick if isinstance(tick, dict) else {}
        if str(payload.get("error") or "").strip() == "tick_busy":
            await asyncio.sleep(1.2)
            try:
                tick_retry = await self.api.tick_scheduler_now()
                if isinstance(tick_retry, dict):
                    payload = tick_retry
            except Exception:  # noqa: BLE001
                logger.exception("panel_tick_once_retry_failed")

        status_payload = await self.refresh_message(
            interaction,
            force_status=False,
            max_age_sec=20.0,
        )
        msg = _build_tick_once_message(payload)
        balance_line = _build_live_balance_line(status_payload)
        if balance_line:
            msg = f"{msg}\n{balance_line}"
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except Exception:  # noqa: BLE001
            logger.exception("panel_tick_once_followup_send_failed")

    async def _open_margin_budget_modal(self, interaction: discord.Interaction) -> None:
        current_budget = None
        payload = await self._fetch_status_cached(max_age_sec=2.0, timeout_sec=1.5)
        cfg = _as_dict(payload.get("config"))
        cur = cfg.get("margin_budget_usdt")
        if cur is not None:
            current_budget = _coerce_float(cur, default=0.0)

        _ = await interaction.response.send_modal(
            MarginBudgetModal(api=self.api, view=self, current_budget=current_budget)
        )

    async def refresh_message(
        self,
        interaction: discord.Interaction,
        *,
        force_status: bool = False,
        max_age_sec: float = 1.2,
    ) -> JSONPayload:
        payload = await self._fetch_status_cached(force=force_status, max_age_sec=max_age_sec)

        embed = build_embed(payload, mode=self._mode)

        if interaction.message is not None:
            msg = interaction.message
            if msg is not None and hasattr(msg, "edit"):
                try:
                    _ = await msg.edit(embed=embed, view=self)
                    return payload
                except discord.HTTPException as e:
                    logger.warning("panel_message_edit_failed", extra={"err": type(e).__name__})

        if self.message_id is None or interaction.channel is None:
            return payload
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            return payload
        try:
            msg = await interaction.channel.fetch_message(self.message_id)
            _ = await msg.edit(embed=embed, view=self)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        return payload

    async def _get_risk_config(self) -> JSONPayload:
        payload = await self._fetch_status_cached(max_age_sec=2.0, timeout_sec=1.8)
        cfg = _as_dict(payload.get("risk_config"))
        if cfg:
            return cfg
        return {}

    async def _get_scoring_setup_defaults(self) -> JSONPayload:
        payload = await self._fetch_status_cached(max_age_sec=2.0, timeout_sec=1.8)
        cfg = _as_dict(payload.get("config"))
        if cfg:
            return cfg
        risk_cfg = _as_dict(payload.get("risk_config"))
        if risk_cfg:
            return risk_cfg
        return {}

    async def _swap_view(self, interaction: discord.Interaction, new_view: "PanelViewBase") -> None:
        payload = self._get_cached_status(max_age_sec=30.0) or {}
        if payload:
            new_view._update_status_cache(payload)
        embed = build_embed(payload, mode=new_view._mode)
        if not interaction.response.is_done():
            try:
                _ = await interaction.response.edit_message(embed=embed, view=new_view)
            except (discord.HTTPException, RuntimeError):
                logger.warning("panel_swap_view_response_edit_failed")
        if interaction.message is not None and interaction.response.is_done():
            try:
                _ = await interaction.message.edit(embed=embed, view=new_view)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("panel_swap_view_message_edit_failed")

        latest = await self._fetch_status_cached(force=True, timeout_sec=2.0)
        if latest:
            new_view._update_status_cache(latest)
            latest_embed = build_embed(latest, mode=new_view._mode)
            try:
                if interaction.message is not None:
                    _ = await interaction.message.edit(embed=latest_embed, view=new_view)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("panel_swap_view_refresh_failed")


class SimplePanelView(PanelViewBase):
    @property
    def _mode(self) -> Literal["simple", "advanced"]:
        return "simple"

    @discord.ui.button(label=START_BUTTON_LABEL, style=discord.ButtonStyle.success)
    async def start_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        await self._run_action(
            interaction, action=self.api.start, success_message="엔진을 시작했습니다."
        )

    @discord.ui.button(label=STOP_BUTTON_LABEL, style=discord.ButtonStyle.secondary)
    async def stop_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        await self._run_action(
            interaction, action=self.api.stop, success_message="엔진을 중지했습니다."
        )

    @discord.ui.button(label=PANIC_BUTTON_LABEL, style=discord.ButtonStyle.danger)
    async def panic_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        await self._run_action(
            interaction, action=self.api.panic, success_message="패닉 명령을 전송했습니다."
        )

    @discord.ui.button(label=TICK_ONCE_BUTTON_LABEL, style=discord.ButtonStyle.primary)
    async def tick_once_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        await self._run_tick_once(interaction)

    @discord.ui.button(label=MARGIN_BUDGET_BUTTON_LABEL, style=discord.ButtonStyle.secondary)
    async def margin_budget_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        await self._open_margin_budget_modal(interaction)

    @discord.ui.button(label=ADVANCED_TOGGLE_LABEL, style=discord.ButtonStyle.primary)
    async def advanced_toggle_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return

        await self._swap_view(
            interaction,
            AdvancedPanelView(
                api=self.api,
                message_id=self.message_id,
                initial_payload=self._get_cached_status(max_age_sec=30.0),
            ),
        )


class AdvancedPanelView(PanelViewBase):
    @property
    def _mode(self) -> Literal["simple", "advanced"]:
        return "advanced"

    @discord.ui.button(label=START_BUTTON_LABEL, style=discord.ButtonStyle.success)
    async def start_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        await self._run_action(
            interaction, action=self.api.start, success_message="엔진을 시작했습니다."
        )

    @discord.ui.button(label=STOP_BUTTON_LABEL, style=discord.ButtonStyle.secondary)
    async def stop_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        await self._run_action(
            interaction, action=self.api.stop, success_message="엔진을 중지했습니다."
        )

    @discord.ui.button(label=PANIC_BUTTON_LABEL, style=discord.ButtonStyle.danger)
    async def panic_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        await self._run_action(
            interaction, action=self.api.panic, success_message="패닉 명령을 전송했습니다."
        )

    @discord.ui.button(label=TICK_ONCE_BUTTON_LABEL, style=discord.ButtonStyle.primary)
    async def tick_once_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        await self._run_tick_once(interaction)

    @discord.ui.button(label=MARGIN_BUDGET_BUTTON_LABEL, style=discord.ButtonStyle.secondary)
    async def margin_budget_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        await self._open_margin_budget_modal(interaction)

    @discord.ui.button(label=SIMPLE_TOGGLE_LABEL, style=discord.ButtonStyle.primary, row=1)
    async def simple_toggle_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        await self._swap_view(
            interaction,
            SimplePanelView(
                api=self.api,
                message_id=self.message_id,
                initial_payload=self._get_cached_status(max_age_sec=30.0),
            ),
        )

    @discord.ui.button(label=RISK_BASIC_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=1)
    async def risk_basic_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        defaults: JSONPayload = await self._get_risk_config()
        _ = await interaction.response.send_modal(
            RiskBasicModal(api=self.api, view=self, defaults=defaults)
        )

    @discord.ui.button(label=RISK_ADVANCED_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=1)
    async def risk_adv_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        defaults: JSONPayload = await self._get_risk_config()
        _ = await interaction.response.send_modal(
            RiskAdvancedModal(api=self.api, view=self, defaults=defaults)
        )

    @discord.ui.button(label=TRAILING_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=1)
    async def trailing_config_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        defaults: JSONPayload = await self._get_risk_config()
        _ = await interaction.response.send_modal(
            TrailingConfigModal(api=self.api, view=self, defaults=defaults)
        )

    @discord.ui.button(
        label=SYMBOL_LEVERAGE_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=1
    )
    async def symbol_leverage_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        _ = await interaction.response.send_modal(SymbolLeverageModal(api=self.api, view=self))

    @discord.ui.button(
        label=UNIVERSE_SYMBOLS_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=2
    )
    async def universe_symbols_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        defaults: JSONPayload = await self._get_risk_config()
        _ = await interaction.response.send_modal(
            UniverseSymbolsModal(
                api=self.api,
                view=self,
                defaults=defaults,
            )
        )

    @discord.ui.button(
        label=UNIVERSE_REMOVE_SYMBOL_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=2
    )
    async def universe_remove_symbol_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        defaults: JSONPayload = await self._get_risk_config()
        _ = await interaction.response.send_modal(
            UniverseSymbolRemoveModal(
                api=self.api,
                view=self,
                defaults=defaults,
            )
        )

    @discord.ui.button(label=SCORING_SETUP_BUTTON_LABEL, style=discord.ButtonStyle.secondary, row=2)
    async def scoring_setup_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        defaults = await self._get_scoring_setup_defaults()
        _ = await interaction.response.send_modal(
            ScoringSetupModal(
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
    async def exec_mode_select(
        self, interaction: discord.Interaction, select: discord.ui.Select[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        val = str(select.values[0]).upper()
        await self._run_action(
            interaction,
            action=lambda: self.api.set_value("exec_mode_default", val),
            success_message=f"실행모드가 {val}로 변경되었습니다.",
        )

    @discord.ui.select(
        placeholder=SCHEDULER_INTERVAL_SELECT_PLACEHOLDER,
        options=[
            discord.SelectOption(label="5분", value="300"),
            discord.SelectOption(label="10분", value="600"),
            discord.SelectOption(label="15분", value="900"),
            discord.SelectOption(label="30분", value="1800", default=True),
            discord.SelectOption(label="60분", value="3600"),
        ],
        row=4,
    )
    async def scheduler_interval_select(
        self, interaction: discord.Interaction, select: discord.ui.Select[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        try:
            tick_sec = float(str(select.values[0]))
        except (TypeError, ValueError):
            _ = await interaction.response.send_message("유효하지 않은 간격입니다.", ephemeral=True)
            return
        minutes = int(tick_sec // 60)
        await self._run_action(
            interaction,
            action=lambda: self.api.set_scheduler_interval(tick_sec),
            success_message=f"판단 주기를 {minutes}분으로 변경했습니다.",
        )

    @discord.ui.button(label="상태 알림 주기 설정", style=discord.ButtonStyle.secondary, row=2)
    async def notify_interval_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if not await self._guard(interaction):
            return
        defaults = await self._get_risk_config()
        _ = await interaction.response.send_modal(
            NotifyIntervalModal(api=self.api, view=self, defaults={"risk_config": defaults})
        )


PanelView = SimplePanelView
_build_embed = build_embed
