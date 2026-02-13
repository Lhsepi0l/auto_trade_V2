from __future__ import annotations

import asyncio
import logging
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
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def _fmt_dt(v: Any) -> str:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v) if v is not None else "-"


def _decision_reason_ko(reason: str) -> str:
    table = {
        "no_candidate": "진입 후보가 없어 대기합니다.",
        "vol_shock_no_entry": "변동성 충격 구간이라 신규 진입을 보류합니다.",
        "confidence_below_threshold": "신뢰도 점수가 기준치보다 낮아 진입하지 않습니다.",
        "short_not_allowed_regime": "현재 레짐에서 숏 조건이 충족되지 않아 진입하지 않습니다.",
        "enter_candidate": "조건 충족으로 신규 진입 신호가 발생했습니다.",
        "vol_shock_close": "변동성 충격 감지로 포지션을 청산합니다.",
        "profit_hold": "수익 포지션 유지 조건으로 홀드합니다.",
        "same_symbol": "현재 보유 심볼과 후보가 같아 재진입/리밸런싱을 생략합니다.",
        "gap_below_threshold": "후보 우위 점수 격차가 기준 미달이라 교체하지 않습니다.",
        "rebalance_to_better_candidate": "더 나은 후보로 리밸런싱 조건이 충족되었습니다.",
    }
    if reason.startswith("min_hold_active:"):
        return "최소 보유 시간 규칙이 아직 끝나지 않아 대기합니다."
    return table.get(reason, "전략 규칙에 따라 대기 또는 조건 확인 중입니다.")


def _regime_ko(regime: str) -> str:
    v = str(regime or "").upper()
    if v == "BULL":
        return "상승(BULL)"
    if v == "BEAR":
        return "하락(BEAR)"
    if v:
        return v
    return "-"


def _format_event_line(event: Mapping[str, Any]) -> str:
    kind = str(event.get("kind") or "EVENT").upper()
    symbol = str(event.get("symbol") or "")
    detail = event.get("detail") if isinstance(event.get("detail"), Mapping) else {}

    if kind in {"ENTER", "REBALANCE"}:
        side = str((detail or {}).get("side") or (detail or {}).get("direction") or "")
        qty = (detail or {}).get("qty")
        price = (detail or {}).get("price_ref")
        return f"[EVENT] {kind} {symbol} {side} qty={_fmt_float(qty)} price={_fmt_float(price, 4)}"

    if kind in {"EXIT", "TAKE_PROFIT", "STOP_LOSS", "WATCHDOG_SHOCK", "TRAILING_PCT", "TRAILING_ATR"}:
        reason = str((detail or {}).get("reason") or kind)
        return f"[EVENT] {kind} {symbol} reason={reason}"

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
        return f"[EVENT] FAIL {symbol} error={err}"

    return f"[EVENT] {kind} {symbol}".strip()


def _format_status_line(snapshot: Mapping[str, Any]) -> str:
    engine_state = str(snapshot.get("engine_state") or "UNKNOWN")
    pos = snapshot.get("position_symbol")
    amt = _fmt_float(snapshot.get("position_amt"), 4)
    upnl = _fmt_float(snapshot.get("upnl"), 2)
    daily = _fmt_float(snapshot.get("daily_pnl_pct"), 2)
    dd = _fmt_float(snapshot.get("drawdown_pct"), 2)
    regime = _regime_ko(str(snapshot.get("regime") or "-"))
    candidate = snapshot.get("candidate_symbol") or "-"
    dec = str(snapshot.get("last_decision_reason") or "-")
    dec_ko = _decision_reason_ko(dec)
    last_action = snapshot.get("last_action") or "-"
    last_error = snapshot.get("last_error") or "-"

    pos_line = "없음"
    if pos:
        pos_line = f"{pos} (수량 {amt})"

    lines = [
        "[상태 알림]",
        f"- 엔진 상태: {engine_state}",
        f"- 현재 포지션: {pos_line}",
        f"- 손익 요약: 미실현손익(uPnL) {upnl} USDT, 일일손익 {daily}%, DD {dd}%",
        f"- 시장 판단: 레짐 {regime}, 후보 심볼 {candidate}",
        f"- 이번 결정: {dec} -> {dec_ko}",
        f"- 최근 액션: {last_action}",
    ]
    if str(last_error) != "-":
        lines.append(f"- 최근 오류: {last_error}")
    return "\n".join(lines)


def build_notifier(discord_webhook_url: str) -> Notifier:
    url = (discord_webhook_url or "").strip()
    if not url:
        logger.info("discord_webhook_disabled")
        return LoggingNotifier()
    return DiscordWebhookNotifier(url=url)
