from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

import requests

logger = logging.getLogger(__name__)


class Notifier:
    async def send_event(self, event: Mapping[str, Any]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def send_status_snapshot(self, snapshot: Mapping[str, Any]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def notify(self, event: Mapping[str, Any]) -> None:
        _run_async_compat(self.send_event(dict(event)))

    def notify_status(self, snapshot: Mapping[str, Any]) -> None:
        _run_async_compat(self.send_status_snapshot(dict(snapshot)))


def _run_async_compat(coro: asyncio.coroutines) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        asyncio.run(coro)


@dataclass
class LoggingNotifier(Notifier):
    async def send_event(self, event: Mapping[str, Any]) -> None:
        logger.info("notifier_event_disabled", extra={"msg": _format_event_line(event)})

    async def send_status_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        logger.info("notifier_status_disabled", extra={"msg": _format_status_line(snapshot)})


@dataclass
class DiscordWebhookNotifier(Notifier):
    url: str
    timeout_sec: float = 5.0

    async def send_event(self, event: Mapping[str, Any]) -> None:
        await asyncio.to_thread(self._post, _format_event_line(event))

    async def send_status_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        await asyncio.to_thread(self._post, _format_status_line(snapshot))

    def _post(self, content: str) -> None:
        payload = {"content": content[:1900]}
        try:
            resp = requests.post(self.url, json=payload, timeout=self.timeout_sec)
            if resp.status_code >= 400:
                logger.warning("discord_webhook_failed", extra={"status": resp.status_code})
        except Exception:
            logger.exception("discord_webhook_error")


def _fmt_float(v: Any, digits: int = 3) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def _fmt_signed_float(v: Any, digits: int = 3) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):+.{digits}f}"
    except Exception:
        return str(v)


def _fmt_realized_breakdown(v: float | None) -> str:
    if v is None:
        return ""
    if v > 0:
        return f"익절액={_fmt_signed_float(v, 4)} USDT"
    if v < 0:
        return f"손절액={_fmt_signed_float(v, 4)} USDT"
    return f"실현손익={_fmt_signed_float(v, 4)} USDT"


def _fmt_int(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return str(int(v))
    except Exception:
        return str(v)


def _fmt_dt(v: Any) -> str:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v) if v is not None else "-"


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def _fmt_event_time(v: Any) -> str:
    if v is None:
        return "-"
    s = str(v).replace("T", " ").split(".")[0]
    if s.endswith("+00:00"):
        s = s[:-6]
    return s


def _order_event_type_label(action: str, reduce_only: bool, side: str) -> str:
    s = str(action or "").upper()
    side_u = str(side or "").strip().upper()
    if s in {"ERROR", "FAILED"}:
        return "실패"
    if s in {"CANCELED", "CANCELLED"}:
        return "취소"
    if s == "CLOSE":
        if side_u == "BUY":
            return "숏 청산"
        if side_u == "SELL":
            return "롱 청산"
        return "청산"
    if s == "ENTRY":
        return "진입"
    return s or "-"


def _fmt_report_orders(orders: list[Mapping[str, Any]]) -> list[str]:
    if not isinstance(orders, list):
        return []
    lines: list[str] = []
    for idx, o in enumerate(orders[:12], 1):
        if not isinstance(o, Mapping):
            continue
        action = str(o.get("action") or o.get("status") or "-")
        status = str(o.get("status") or "-")
        symbol = str(o.get("symbol") or "-")
        side = str(o.get("side") or "")
        qty = _to_float(o.get("qty"))
        price = _to_float(o.get("price"))
        realized = _to_float(o.get("realized_pnl"))
        event_time = _fmt_event_time(o.get("ts_updated") or o.get("ts_created"))
        r_only = bool(int(o.get("reduce_only") or 0))
        t = _order_event_type_label(action, r_only, side)
        is_close = "청산" in t
        label = _position_label_from_close_side(side) if is_close else _position_label(side)
        if t in {"실패", "취소"} and r_only:
            label = _position_label_from_close_side(side)
        base = f"{idx:02d}) {event_time} | {symbol} {label} {t}"
        if status:
            base += f" ({status})"
        q = _fmt_float(qty, 4)
        p = _fmt_float(price, 4)
        extra: list[str] = []
        if q != "-":
            extra.append(f"수량={q}")
        if p != "-":
            extra.append(f"가격={p} USDT")
        if realized is not None:
            extra.append(_fmt_realized_breakdown(realized))
        if extra:
            base += " | " + " | ".join(extra)
        err = str(o.get("last_error") or "")
        if err:
            base += f" | 사유={err}"
        lines.append(base)
    return lines


def _side_to_ko(side: str) -> str:
    s = str(side or "").upper()
    if s == "BUY":
        return "매수"
    if s == "SELL":
        return "매도"
    return s or "-"


def _position_to_ko(side: str) -> str:
    s = str(side or "").strip().upper()
    if s in {"BUY", "LONG", "롱"}:
        return "롱"
    if s in {"SELL", "SHORT", "숏"}:
        return "숏"
    return "-"


def _position_label(side: str) -> str:
    p = _position_to_ko(side)
    return f"[{p}]" if p != "-" else "[-]"


def _position_label_from_close_side(side: str) -> str:
    s = str(side or "").strip().upper()
    if s == "BUY":
        return "[숏]"
    if s == "SELL":
        return "[롱]"
    return "[*]"


def _detail_float(detail: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in detail:
            continue
        try:
            return float(detail[key])
        except Exception:
            pass
    return None


def _extract_event_qty(detail: Mapping[str, Any]) -> float | None:
    qty = _detail_float(
        detail,
        "qty",
        "position_amt",
        "position_size",
        "size",
        "filled_qty",
        "closed_qty",
    )
    if qty is not None:
        return qty
    order = _find_first_order(detail)
    return _order_float(
        order,
        "executedQty",
        "executed_qty",
        "origQty",
        "orig_qty",
    )


def _find_first_order(detail: Mapping[str, Any]) -> Mapping[str, Any] | None:
    direct = detail.get("order")
    if isinstance(direct, Mapping):
        return direct
    multi = detail.get("orders")
    if isinstance(multi, list):
        for entry in reversed(multi):
            if isinstance(entry, Mapping):
                return entry
    return None


def _order_float(order: Mapping[str, Any] | None, *keys: str) -> float | None:
    if not isinstance(order, Mapping):
        return None
    for key in keys:
        if key not in order:
            continue
        try:
            return float(order[key])
        except Exception:
            pass
    return None


def _decision_reason_ko(reason: str) -> str:
    table = {
        "no_candidate": "진입 후보가 없어 진입하지 못했습니다.",
        "vol_shock_no_entry": "변동성 급등으로 변동성 필터가 진입을 차단했습니다.",
        "confidence_below_threshold": "신뢰도 점수가 임계값 아래여서 진입을 보류했습니다.",
        "short_not_allowed_regime": "현재 시장 구간에서는 숏 진입이 허용되지 않습니다.",
        "enter_candidate": "진입 후보가 선정되어 주문 준비를 시작했습니다.",
        "vol_shock_close": "변동성 급등으로 청산 동작이 보류되었습니다.",
        "profit_hold": "익절 트리거 미달로 포지션을 유지합니다.",
        "same_symbol": "현재 탐색한 심볼이 이미 보유 중인 심볼과 같아 중복 진입하지 않습니다.",
        "gap_below_threshold": "갭 크기가 임계값 이하라 판단이 생략되었습니다.",
        "rebalance_to_better_candidate": "더 유리한 후보 심볼로 리밸런싱 전환이 예정되어 있습니다.",
    }
    if reason.startswith("min_hold_active:"):
        return "최소 보유 기간 조건 미충족으로 중단합니다."
    return table.get(reason, "알 수 없는 진입 사유입니다.")
def _regime_ko(regime: str) -> str:
    v = str(regime or "").upper()
    if v == "BULL":
        return "상승"
    if v == "BEAR":
        return "하락"
    if v in {"", "-", "N/A", "NONE", "UNKNOWN", "UNKNOWN_REGIME"}:
        return "미판단"
    if v:
        return v
    return "미판단"


def _position_side(position_amt: Any, position_side: Any = None) -> str:
    if position_side is not None:
        s = str(position_side).strip().upper()
        if s in {"LONG", "BUY", "롱"}:
            return "롱"
        if s in {"SHORT", "SELL", "숏"}:
            return "숏"
    try:
        v = float(position_amt or 0.0)
    except Exception:
        return "-"
    if v > 0:
        return "롱"
    if v < 0:
        return "숏"
    return "-"


_ERROR_GUIDE_TABLE: list[tuple[str, str, str]] = [
    ("engine_in_panic", "엔진이 PANIC 상태입니다. 즉시 복구가 필요합니다.", "자동/수동 모드 상태를 확인하고 안전 모드에서 벗어난 뒤 조치하세요."),
    ("recovery_lock_active", "복구 락이 활성화되어 있습니다.", "복구 락이 해제될 때까지 진입/청산이 잠깐 중단됩니다."),
    ("ws_down_safe_mode", "WebSocket이 불안정해 안전 모드가 활성화되었습니다.", "네트워크/API 상태를 확인한 뒤 Binance WebSocket 재연결을 기다립니다."),
    ("multiple_open_positions_detected", "동일 계열 심볼에서 동일 방향 포지션이 2개 이상 감지됨", "동일 종목/방향 중복 포지션은 무시되고 추가 진입이 차단됩니다."),
    ("symbol_not_allowed", "요청한 심볼이 허용 목록에 없습니다.", "설정 파일의 enabled_symbols에 심볼을 추가하거나 심볼 필터를 점검하세요."),
    ("symbol_required", "심볼이 지정되지 않았습니다.", "BTCUSDT 또는 기본 심볼을 명시해 주세요."),
    ("quantity_below_min_qty", "수량이 최소 주문 수량보다 작습니다.", "거래소 LOT_SIZE 최소 수량/단위를 확인해 수량을 상향 조정하세요."),
    ("notional_below_min_notional", "명목가치가 최소 Notional 미달입니다.", "1회 최대 리스크 비중 또는 per_trade_risk_pct를 조정하세요."),
    ("min_qty", "주문 수량이 최소 수량 조건 미달입니다.", "해당 심볼의 minQty 규칙을 반영해 수량을 맞추세요."),
    ("hedge_mode_enabled", "헤지 모드가 활성화되어 있어 단방향 모드가 강제됩니다.", "거래 모드(ONEWAY)로 전환 후 재시도하세요."),
    ("adding_to_position_not_allowed", "포지션 추가 진입이 허용되지 않습니다.", "현재 규칙 또는 증거금 여유를 확인해 재설정하세요."),
    ("single_asset_rule_unresolved", "단일 자산 룰 충돌이 해결되지 않았습니다.", "룰 우선순위와 심볼 설정 충돌을 점검하세요."),
    ("risk_guard_failed", "리스크 가드 조건 미충족입니다.", "리스크 가드 파라미터(DD, drawdown, max_notional)를 완화하거나 조정하세요."),
    ("book_ticker_unavailable", "현재 책정 티커 조회가 불안정합니다.", "API 연결/권한/네트워크를 점검하세요."),
    ("market_fallback_blocked_by_spread_guard", "스프레드 가드로 인해 시장 fallback이 차단됩니다.", "spread_max_pct를 완화하거나 주문 방식을 조정하세요."),
    ("engine_not_running", "엔진이 RUNNING 상태가 아닙니다.", "엔진 로그를 확인 후 재시작 또는 수동 복구하세요."),
    ("binance_auth_error", "바이낸스 인증 오류", "API Key/Secret, IP 제한, 권한 설정을 점검하세요."),
]
def _normalize_error_code(err: str) -> str:
    return " ".join(str(err).strip().split()).lower()


def _error_guidance(err: str) -> tuple[str, str, str] | None:
    text = _normalize_error_code(err)
    if not text:
        return None

    if text.startswith("engine_not_running:"):
        return (
            "engine_not_running",
            "엔진이 RUNNING 상태가 아닙니다.",
            "엔진 상태를 확인하고 재시작 후 재시도하세요."
        )

    m = re.match(r"binance_http_(\d+)_code_(-?\d+)", text)
    if m:
        status = m.group(1)
        code = m.group(2)
        if status == "401":
            return (f"binance_http_{status}_code_{code}", "인증 실패(401)", "API Key/Secret 권한, IP 제한, API 토큰 상태를 점검하세요.")
        if status == "403":
            return (f"binance_http_{status}_code_{code}", "권한 거부(403)", "거래소 API 권한, IP 화이트리스트, 계정 제약 조건을 점검하세요.")
        if status == "429":
            return (f"binance_http_{status}_code_{code}", "요청 제한 초과(429)", "요청 간격을 늘려서 재시도하세요.")
        if status and status.startswith("5"):
            return (f"binance_http_{status}_code_{code}", "바이낸스 서버 오류", "일시 장애 가능성이 있으니 잠시 후 재시도 후, 잔고/계정 상태를 확인하세요.")
        return (f"binance_http_{status}_code_{code}", "예상치 못한 HTTP 오류", "요청 파라미터, 심볼, 네트워크 상태를 함께 점검하세요.")

    for code, issue, action in _ERROR_GUIDE_TABLE:
        if code in text:
            return (code, issue, action)
    return None
def _format_fail_line(symbol: str, raw_error: str) -> str:
    err = str(raw_error)
    g = _error_guidance(err)
    if not g:
        return f"[EVENT] FAIL {symbol} error={err}"
    code, issue, action = g
    return (
        f"[EVENT] FAIL {symbol} code={code}\n"
        f"- 원인: {issue}\n"
        f"- 권장 대응: {action}"
    )


def _format_event_line(event: Mapping[str, Any]) -> str:
    kind = str(event.get("kind") or "EVENT").upper()
    symbol = str(event.get("symbol") or "")
    detail = event.get("detail") if isinstance(event.get("detail"), Mapping) else {}
    event_payload = detail if isinstance(detail, Mapping) and detail else event

    if kind in {"ENTER", "REBALANCE"}:
        d = detail or {}
        side = str(d.get("position_side") or d.get("side") or d.get("direction") or "")
        qty = _extract_event_qty(d)
        price = _detail_float(d, "entry_price", "price_ref", "avg_price", "price")
        qty_f = _to_float(qty)
        if qty_f is not None and price is not None:
            notional = qty_f * price
        else:
            notional = None
        action = "진입" if kind == "ENTER" else "리밸런스 진입"
        label = _position_label(side)
        if notional is not None:
            return (
                f"[이벤트] {action}: {symbol} {label} | "
                f"수량={_fmt_float(qty_f, 4)} | "
                f"진입가={_fmt_float(price, 4)} USDT | "
                f"notional=USD {_fmt_float(notional, 2)}"
            )
        return (
            f"[이벤트] {action}: {symbol} {label} | "
            f"수량={_fmt_float(qty_f, 4)} | "
            f"진입가={_fmt_float(price, 4)} USDT"
        )

    if kind == "STRATEGY_DECISION_JUDGMENT":
        regime_raw = str(event_payload.get("regime_4h") or event_payload.get("candidate_regime_4h") or event_payload.get("regime") or "-")
        regime = _regime_ko(regime_raw)
        regime_code = regime_raw.strip().upper() if regime_raw and regime_raw != "-" else "N/A"
        score = _fmt_float(
            event_payload.get("score")
            if "score" in event_payload
            else event_payload.get("candidate_score"),
            3,
        )
        confidence = _fmt_float(
            event_payload.get("confidence")
            if "confidence" in event_payload
            else event_payload.get("candidate_confidence"),
            3,
        )
        candidate_direction = _position_to_ko(event_payload.get("candidate_direction"))
        final_direction = _position_to_ko(
            event_payload.get("final_direction") or event_payload.get("direction")
        )
        reason = str(event_payload.get("reason") or "-")
        return (
            f"[판단] {symbol} | {regime}({regime_code}) | "
            f"점수={score} 후보={candidate_direction} 최종={final_direction} "
            f"신뢰={confidence} | 사유={reason}"
        )

    if kind == "FILL":
        side = str((detail or {}).get("side") or "")
        qty = (detail or {}).get("qty")
        price = (detail or {}).get("price_ref")
        realized = (detail or {}).get("realized_pnl")
        qty_f = _to_float(qty)
        price_f = _to_float(price)
        notional = (qty_f * price_f) if (qty_f is not None and price_f is not None) else None
        if notional is not None:
            return (
                f"[EVENT] FILL {symbol} {_side_to_ko(side)} | qty={_fmt_float(qty)} | price={_fmt_float(price, 4)} | "
                f"notional=USD {_fmt_float(notional, 2)} | {_fmt_realized_breakdown(_to_float(realized)) or f'실현손익={_fmt_signed_float(realized, 4)} USDT'}"
            )
        return (
            f"[EVENT] FILL {symbol} {_side_to_ko(side)} | qty={_fmt_float(qty)} | price={_fmt_float(price, 4)} | "
            f"{_fmt_realized_breakdown(_to_float(realized)) or f'실현손익={_fmt_signed_float(realized, 4)} USDT'}"
        )

    if kind == "DAILY_REPORT":
        day = str(event.get("day") or (detail or {}).get("day") or "-")
        reported_at = str(event.get("reported_at") or (detail or {}).get("reported_at") or "-")
        engine_state = str(event.get("engine_state") or (detail or {}).get("engine_state") or "-")
        entries = _fmt_int((detail or {}).get("entries"))
        closes = _fmt_int((detail or {}).get("closes"))
        realized_total = _fmt_signed_float((detail or {}).get("realized_pnl"), 4)
        blocks = _fmt_int((detail or {}).get("blocks"))
        errors = _fmt_int((detail or {}).get("errors"))
        canceled = _fmt_int((detail or {}).get("canceled"))
        total_records = _fmt_int((detail or {}).get("total_records"))
        orders = (detail or {}).get("orders", [])
        order_lines = _fmt_report_orders(orders) if isinstance(orders, list) else []
        order_summary = "\n".join(order_lines)
        realized_values = []
        if isinstance(orders, list):
            for o in orders:
                if not isinstance(o, Mapping):
                    continue
                rv = _to_float(o.get("realized_pnl"))
                if rv is not None:
                    realized_values.append(rv)
        realized_profit = sum(v for v in realized_values if v and v > 0)
        realized_loss = sum(v for v in realized_values if v and v < 0)
        realized_profit_text = _fmt_signed_float(realized_profit, 4)
        realized_loss_text = _fmt_signed_float(realized_loss, 4)
        return (
            "[일일 리포트]\n"
            f"일자: {day}\n"
            f"엔진 상태: {engine_state}\n"
            f"보고 시각: {reported_at}\n"
            f"진입/청산: {entries} / {closes}\n"
            f"실현손익: {realized_total} USDT\n"
            f"익절액: {realized_profit_text} USDT\n"
            f"손절액: {realized_loss_text} USDT\n"
            f"오류/취소: {errors} / {canceled}\n"
            f"차단/총건수: {blocks} / {total_records}\n"
            f"주문 상세 (최대 12건):\n"
            + (order_summary or "-")
        )

    if kind == "ACCOUNT_UPDATE":
        positions_count = (detail or {}).get("positions_count")
        balances_count = (detail or {}).get("balances_count")
        return f"[EVENT] ACCOUNT_UPDATE positions={positions_count} balances={balances_count}"

    if kind in {"EXIT", "TAKE_PROFIT", "STOP_LOSS", "WATCHDOG_SHOCK", "TRAILING_PCT", "TRAILING_ATR"}:
        d = detail or {}
        reason = str(d.get("reason") or kind)
        close_reason = _fmt_dt(reason)
        close_side = _position_label_from_close_side(str(d.get("side") or ""))
        pos_side = d.get("position_side")
        if pos_side is None:
            pos_side = d.get("position")
            if pos_side is None:
                pos_side = None
        label = close_side
        if pos_side:
            p = _position_to_ko(pos_side)
            label = f"[{p}]"

        qty = _extract_event_qty(d)
        entry_price = _detail_float(d, "entry_price")
        close_price = _detail_float(d, "close_price", "exit_price")
        if close_price is None:
            close_price = _order_float(_find_first_order(d), "avg_price", "price")
        target_price = _detail_float(
            d,
            "take_profit_price",
            "stop_loss_price",
            "trigger_price",
            "triggered_price",
            "target_price",
        )

        action = "청산"
        if kind == "TAKE_PROFIT":
            action = "익절청산"
        elif kind == "STOP_LOSS":
            action = "손절청산"
        elif kind.startswith("TRAILING"):
            action = "트레일링청산"

        lines = (
            f"[이벤트] {action}: {symbol} {label} | "
            f"수량={_fmt_float(qty, 4)} | "
            f"진입가={_fmt_float(entry_price, 4)} USDT | "
            f"청산가={_fmt_float(close_price, 4)} USDT"
        )
        if target_price is not None:
            if kind == "TAKE_PROFIT":
                lines += f" | 익절가={_fmt_float(target_price, 4)} USDT"
            elif kind == "STOP_LOSS":
                lines += f" | 손절가={_fmt_float(target_price, 4)} USDT"
            else:
                lines += f" | 기준가={_fmt_float(target_price, 4)} USDT"
        lines += f" | 사유={close_reason}"
        return lines

    if kind == "COOLDOWN":
        until = event.get("until") or (detail or {}).get("until")
        hours = event.get("hours") or (detail or {}).get("hours")
        return f"[RISK] COOLDOWN {hours}h until {_fmt_dt(until)}"

    if kind == "PANIC":
        reason = str(event.get("reason") or (detail or {}).get("reason") or "panic")
        return f"[RISK] PANIC reason={reason}"

    if kind == "PANIC_RESULT":
        ok = bool(event.get("ok"))
        cancel_ok = bool(event.get("canceled_orders_ok"))
        close_ok = bool(event.get("close_ok"))
        errs = event.get("errors") if isinstance(event.get("errors"), list) else []
        err_txt = str(errs[0]) if errs else "-"
        return f"[RISK] PANIC_RESULT ok={ok} cancel_ok={cancel_ok} close_ok={close_ok} err={err_txt}"

    if kind == "BLOCK":
        reason = str(event.get("reason") or (detail or {}).get("reason") or "blocked")
        return f"[RISK] ENTRY BLOCKED: {reason} symbol={symbol}"

    if kind == "BUDGET_UPDATED":
        key = str(event.get("key") or (detail or {}).get("key") or "-")
        value = event.get("value") if "value" in event else (detail or {}).get("value")
        return f"[CONFIG] BUDGET UPDATED: {key}={value}"

    if kind == "WS_DOWN_SAFE_MODE":
        reason = str(event.get("reason") or (detail or {}).get("reason") or "user_stream_disconnected")
        return f"[RISK] WS_DOWN_SAFE_MODE reason={reason}"

    if kind == "FAIL":
        err = str(event.get("error") or (detail or {}).get("error") or "unknown")
        op = event.get("op")
        if isinstance(op, str) and op:
            return f"[EVENT] FAIL op={op} symbol={symbol}\n{_format_fail_line(symbol, err)}"
        return _format_fail_line(symbol, err)

    return f"[EVENT] {kind} {symbol}".strip()


def _format_status_line(snapshot: Mapping[str, Any]) -> str:
    engine_state = str(snapshot.get("engine_state") or "UNKNOWN")
    pos = snapshot.get("position_symbol")
    amt_raw = snapshot.get("position_amt")
    side = _position_side(amt_raw, snapshot.get("position_side"))
    try:
        amt = _fmt_float(abs(float(amt_raw or 0.0)), 4)
    except Exception:
        amt = _fmt_float(amt_raw, 4)
    upnl = _fmt_float(snapshot.get("upnl"), 2)
    daily = _fmt_float(snapshot.get("daily_pnl_pct"), 2)
    dd = _fmt_float(snapshot.get("drawdown_pct"), 2)
    regime_raw = str(snapshot.get("regime") or "-")
    regime = _regime_ko(regime_raw)
    regime_code = regime_raw.strip().upper() if regime_raw and regime_raw != "-" else "N/A"
    candidate = snapshot.get("candidate_symbol") or "-"
    decision_regime_raw = str(snapshot.get("last_decision_candidate_regime_4h") or regime_raw)
    decision_regime = _regime_ko(decision_regime_raw)
    decision_regime_code = decision_regime_raw.strip().upper() if decision_regime_raw and decision_regime_raw != "-" else "N/A"
    decision_score = _fmt_float(snapshot.get("last_decision_candidate_score"), 3)
    decision_confidence = _fmt_float(snapshot.get("last_decision_candidate_confidence"), 3)
    decision_candidate_direction = _position_to_ko(snapshot.get("last_decision_candidate_direction"))
    decision_final_direction = _position_to_ko(snapshot.get("last_decision_final_direction"))
    dec = str(snapshot.get("last_decision_reason") or "-")
    dec_ko = _decision_reason_ko(dec)
    if dec == "profit_hold":
        dec_ko = f"익절 트리거 미달로 포지션을 유지합니다. (현재 미실현손익: {upnl} USDT)"
    last_action = snapshot.get("last_action") or "-"
    last_error = snapshot.get("last_error") or "-"

    pos_line = "-"
    if pos:
        side_label = f"[{side}] " if side != "-" else ""
        pos_line = f"{pos} {side_label}(수량 {amt})"

    lines = [
        "[상태 알림]",
        f"엔진 상태: {engine_state}",
        f"현재 포지션: {pos_line}",
        (
            "손익 요약: "
            f"미실현손익(uPnL) {upnl} USDT, "
            f"일일손익 {daily}%, "
            f"DD {dd}%"
        ),
        f"시장 판단: 레짐 {regime}({regime_code}), 후보 심볼 {candidate}",
        f"판단: {decision_regime}({decision_regime_code}) / "
        f"점수={decision_score} / 후보={decision_candidate_direction} / "
        f"최종={decision_final_direction} / 신뢰={decision_confidence}",
        f"이번 결정: {dec} -> {dec_ko}",
        f"최근 액션: {last_action}",
    ]
    if str(last_error) != "-":
        err = str(last_error)
        lines.append(f"오류: {err}")
        guide = _error_guidance(err)
        if guide is not None:
            code, issue, action = guide
            lines.append(f"권장 대응: {code} - {issue}")
            lines.append(f"대응: {action}")
    return "\n".join(lines)


def build_notifier(discord_webhook_url: str) -> Notifier:
    url = (discord_webhook_url or "").strip()
    if not url:
        logger.info("discord_webhook_disabled")
        return LoggingNotifier()
    return DiscordWebhookNotifier(url=url)


