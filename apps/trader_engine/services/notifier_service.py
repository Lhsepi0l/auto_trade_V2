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
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def _fmt_int(v: Any) -> str:
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


def _side_to_ko(side: str) -> str:
    s = str(side or "").upper()
    if s == "BUY":
        return "濡?
    if s == "SELL":
        return "??
    return s or "-"


def _decision_reason_ko(reason: str) -> str:
    table = {
        "no_candidate": "?꾩옱 吏꾩엯 ?꾨낫媛 ?놁뒿?덈떎.",
        "vol_shock_no_entry": "蹂?숈꽦 湲됰벑 援ш컙?대씪 ?좉퇋 吏꾩엯??蹂대쪟?⑸땲??",
        "confidence_below_threshold": "?좊ː???먯닔媛 湲곗?移섎낫????뒿?덈떎.",
        "short_not_allowed_regime": "?꾩옱 援ш컙?먯꽌 ??吏꾩엯???쒗븳?⑸땲??",
        "enter_candidate": "吏꾩엯 ?꾨낫媛 ?좏깮?섏뿀?듬땲??",
        "vol_shock_close": "蹂?숈꽦 湲됰벑 援ш컙?대씪 ?ъ???醫낅즺瑜?蹂대쪟?⑸땲??",
        "profit_hold": "?듭젅 ?좏샇媛 ?댁븘?덉뼱 ?湲??곹깭?낅땲??",
        "same_symbol": "?꾩옱 蹂댁쑀 ?щ낵怨??숈씪???щ낵? 以묐났 吏꾩엯?????놁뒿?덈떎.",
        "gap_below_threshold": "?먯닔 李⑥씠媛 湲곗?移섎낫???묒븘 ?湲고빀?덈떎.",
        "rebalance_to_better_candidate": "???섏? ?꾨낫媛 ?섏? ?ы룊媛???대룞?⑸땲??",
    }
    if reason.startswith("min_hold_active:"):
        return "理쒖냼 蹂댁쑀 ?쒓컙 議곌굔???쒖꽦 ?곹깭?낅땲??"
    return table.get(reason, "誘명솗???ъ쑀")


def _regime_ko(regime: str) -> str:
    v = str(regime or "").upper()
    if v == "BULL":
        return "bull"
    if v == "BEAR":
        return "bear"
    if v:
        return v
    return "-"


_ERROR_GUIDE_TABLE: list[tuple[str, str, str]] = [
    ("engine_in_panic", "?붿쭊??PANIC ?곹깭?낅땲??, "?ъ???醫낅즺 ??/panic濡?蹂듦뎄?섍퀬 ?ㅼ떆 ?쒖옉?섏꽭??"),
    ("recovery_lock_active", "蹂듦뎄 ?쎌씠 ?쒖꽦 ?곹깭?낅땲??, "蹂듦뎄 ?湲??쒓컙???앸궃 ???ㅼ떆 ?쒕룄?섏꽭??"),
    ("ws_down_safe_mode", "?ъ슜???곗씠??WebSocket???딄꼈?듬땲??, "API ??沅뚰븳/?ㅽ듃?뚰겕 ?곹깭瑜??뺤씤?섍퀬 Binance WebSocket???ъ뿰寃고븯?몄슂."),
    ("multiple_open_positions_detected", "?щ낵???대? 2媛??댁긽 ?대┛ ?ъ??섏씠 ?덉뒿?덈떎", "湲곗〈 ?ъ??섏쓣 ?뺣━?????ㅼ떆 ?쒕룄?섏꽭??"),
    ("symbol_not_allowed", "?대떦 ?щ낵???댁쁺 ?щ낵 紐⑸줉???놁뒿?덈떎", "?ㅼ젙?먯꽌 enabled_symbols/?щ낵 ?ㅼ젙???섏젙?????ㅼ?以꾨윭瑜??ъ떆?묓븯?몄슂."),
    ("symbol_required", "?щ낵 媛믪씠 ?놁뒿?덈떎", "BTCUSDT泥섎읆 ?좏슚???щ낵???낅젰?섏꽭??"),
    ("quantity_below_min_qty", "二쇰Ц ?섎웾??嫄곕옒??理쒖냼移섎낫???묒뒿?덈떎", "?섎웾???섎━嫄곕굹 二쇰Ц 湲덉븸?????ш쾶 ?ㅼ젙?섏꽭??"),
    ("notional_below_min_notional", "二쇰Ц Notional??理쒖냼 湲덉븸蹂대떎 ?묒뒿?덈떎", "1??吏꾩엯 湲덉븸 ?먮뒗 per_trade_risk_pct瑜??섎젮 ?ㅼ떆 ?ㅽ뻾?섏꽭??"),
    ("min_qty", "?섎웾??理쒖냼 ?섎웾蹂대떎 ?묒뒿?덈떎", "?대떦 ?щ낵??理쒖냼 二쇰Ц ?⑥쐞濡??щ젮???ㅼ떆 ?쒕룄?섏꽭??"),
    ("hedge_mode_enabled", "怨꾩젙???ㅼ? 紐⑤뱶?낅땲??, "?좊Ъ 怨꾩젙??ONEWAY(?쇰컲) 紐⑤뱶濡??꾪솚?섏꽭??"),
    ("adding_to_position_not_allowed", "異붽? 吏꾩엯??李⑤떒?섏뿀?듬땲??, "?ъ???泥?궛/由ъ뀑 議곌굔??留욎쓣 ?뚭퉴吏 ?湲????ъ떆?꾪븯?몄슂."),
    ("single_asset_rule_unresolved", "?⑥씪 ?щ낵 洹쒖튃?먯꽌 ?ㅽ뻾??李⑤떒?섏뿀?듬땲??, "荑⑤떎?댁씠 ?앸굹嫄곕굹 ?ㅻⅨ ?щ낵 ?ъ??섏쓣 ?뺣━?????쒕룄?섏꽭??"),
    ("risk_guard_failed", "由ъ뒪??媛?쒖뿉??李⑤떒?섏뿀?듬땲??, "由ъ뒪???ㅼ젙(?쇱씪 ?먯떎?쒕룄, DD, 荑⑤떎?? ?몄텧?쒕룄)???먭??섏꽭??"),
    ("book_ticker_unavailable", "?꾩옱 媛寃??멸? 議고쉶媛 遺덉븞?뺥빀?덈떎", "?쇱떆?곸씤 ?쒖옣/?ㅽ듃?뚰겕 ?댁뒋?????덉쑝???좎떆 ???ъ떆?꾪븯?몄슂."),
    ("market_fallback_blocked_by_spread_guard", "?ㅽ봽?덈뱶媛 而ㅼ꽌 ?쒖옣媛 二쇰Ц ?泥닿? 李⑤떒?섏뿀?듬땲??, "spread_max_pct瑜??꾪솕?섍굅???쒖옣媛 ?泥?洹쒖튃??議곗젙?섏꽭??"),
    ("engine_not_running", "?붿쭊??RUNNING ?곹깭媛 ?꾨떃?덈떎", "?붿쭊???쒖옉?????ㅼ떆 ?쒕룄?섏꽭??"),
    ("binance_auth_error", "諛붿씠?몄뒪 ?몄쬆 ?ㅻ쪟", "API Key/Secret, IP ?묎렐?쒗븳, 沅뚰븳 ?ㅼ젙???뺤씤?섏꽭??"),
]


def _normalize_error_code(err: str) -> str:
    return " ".join(str(err).strip().split()).lower()


def _error_guidance(err: str) -> tuple[str, str] | None:
    text = _normalize_error_code(err)
    if not text:
        return None

    if text.startswith("engine_not_running:"):
        return (
            "engine_not_running",
            "?붿쭊??RUNNING ?곹깭媛 ?꾨떃?덈떎",
            "?붿쭊??癒쇱? ?쒖옉?????ㅼ떆 ?쒕룄?섏꽭??"
        )

    m = re.match(r"binance_http_(\d+)_code_(-?\d+)", text)
    if m:
        status = m.group(1)
        code = m.group(2)
        if status == "401":
            return (f"binance_http_{status}_code_{code}", "?몄쬆 ?ㅻ쪟 ?먮뒗 沅뚰븳 遺議?, "API Key/Secret 諛??좊Ъ 嫄곕옒 沅뚰븳???뺤씤?섏꽭??")
        if status == "403":
            return (f"binance_http_{status}_code_{code}", "?묎렐??李⑤떒?섏뿀?듬땲??, "API Key 沅뚰븳 諛?IP ?쒗븳(?붿씠?몃━?ㅽ듃)???먭??섏꽭??")
        if status == "429":
            return (f"binance_http_{status}_code_{code}", "Rate limit 珥덇낵", "?붿껌??以꾩씠怨??좎떆 ???ъ떆?꾪븯?몄슂.")
        if status and status.startswith("5"):
            return (f"binance_http_{status}_code_{code}", "嫄곕옒???쇱떆 ?ㅻ쪟", "?좎떆 ???ъ떆?꾪븯怨? 吏?띾릺硫?諛붿씠?몄뒪 ?곹깭 ?섏씠吏瑜??뺤씤?섏꽭??")
        return (f"binance_http_{status}_code_{code}", "嫄곕옒?뚭? ?붿껌??嫄곕??덉뒿?덈떎", "?щ낵/?ъ씠???섎웾/?덈쾭由ъ?瑜??뺤씤 ???ㅼ떆 ?쒕룄?섏꽭??")

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

    if kind in {"ENTER", "REBALANCE"}:
        side = str((detail or {}).get("side") or (detail or {}).get("direction") or "")
        qty = (detail or {}).get("qty")
        price = (detail or {}).get("price_ref")
        qty_f = _to_float(qty)
        price_f = _to_float(price)
        notional = (qty_f * price_f) if (qty_f is not None and price_f is not None) else None
        action = "ENTER" if kind == "ENTER" else "REBALANCE"
        if notional is not None:
            return (
                f"[EVENT] {action}: {symbol} {_side_to_ko(side)} | qty={_fmt_float(qty)} | price={_fmt_float(price, 4)} | notional=USD {_fmt_float(notional, 2)}"
            )
        return (
            f"[EVENT] {action}: {symbol} {_side_to_ko(side)} | qty={_fmt_float(qty)} | price={_fmt_float(price, 4)}"
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
                f"[EVENT] FILL {symbol} {_side_to_ko(side)} | qty={_fmt_float(qty)} | price={_fmt_float(price, 4)} | notional=USD {_fmt_float(notional, 2)} | realized={_fmt_float(realized, 4)} USDT"
            )
        return (
            f"[EVENT] FILL {symbol} {_side_to_ko(side)} | qty={_fmt_float(qty)} | price={_fmt_float(price, 4)} | realized={_fmt_float(realized, 4)} USDT"
        )

    if kind == "DAILY_REPORT":
        day = str(event.get("day") or (detail or {}).get("day") or "-")
        reported_at = str(event.get("reported_at") or (detail or {}).get("reported_at") or "-")
        engine_state = str(event.get("engine_state") or (detail or {}).get("engine_state") or "-")
        entries = _fmt_int((detail or {}).get("entries"))
        closes = _fmt_int((detail or {}).get("closes"))
        blocks = _fmt_int((detail or {}).get("blocks"))
        errors = _fmt_int((detail or {}).get("errors"))
        canceled = _fmt_int((detail or {}).get("canceled"))
        total_records = _fmt_int((detail or {}).get("total_records"))
        return (
            f"[DAILY REPORT] {day}\n"
            f"- time: {reported_at}\n"
            f"- state: {engine_state}\n"
            f"- entries: {entries} / closes: {closes}\n"
            f"- blocks: {blocks} / errors: {errors} / canceled: {canceled} (total {total_records})"
        )

    if kind == "ACCOUNT_UPDATE":
        positions_count = (detail or {}).get("positions_count")
        balances_count = (detail or {}).get("balances_count")
        return f"[EVENT] ACCOUNT_UPDATE positions={positions_count} balances={balances_count}"

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
        op = event.get("op")
        if isinstance(op, str) and op:
            return f"[EVENT] FAIL op={op} symbol={symbol}\n{_format_fail_line(symbol, err)}"
        return _format_fail_line(symbol, err)

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

    pos_line = "-"
    if pos:
        pos_line = f"{pos} (size {amt})"

    lines = [
        "[STATUS]",
        f"- state: {engine_state}",
        f"- position: {pos_line}",
        f"- pnl: upnl {upnl} USDT, day {daily}%, dd {dd}%",
        f"- regime: {regime}, candidate: {candidate}",
        f"- decision: {dec} -> {dec_ko}",
        f"- last_action: {last_action}",
    ]
    if str(last_error) != "-":
        err = str(last_error)
        lines.append(f"- last_error: {err}")
        guide = _error_guidance(err)
        if guide is not None:
            code, issue, action = guide
            lines.append(f"- 에러 코드: {code}")
            lines.append(f"- 원인: {issue}")
            lines.append(f"- 권장 대응: {action}")
    return "\n".join(lines)


def build_notifier(discord_webhook_url: str) -> Notifier:
    url = (discord_webhook_url or "").strip()
    if not url:
        logger.info("discord_webhook_disabled")
        return LoggingNotifier()
    return DiscordWebhookNotifier(url=url)

