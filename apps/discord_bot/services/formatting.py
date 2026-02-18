from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from apps.trader_engine.services.notifier_service import _error_guidance


def _fmt_money(x: Any, *, digits: int = 4) -> str:
    try:
        return f"{float(x):.{digits}f}"
    except Exception:
        return str(x)


def _fmt_pct(x: Any) -> str:
    try:
        v = float(x)
        return f"{v:.2f}%"
    except Exception:
        return str(x)


def _fmt_time(ts: Any) -> str:
    if not ts:
        return "-"
    if isinstance(ts, str):
        return ts
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


def _truncate(s: str, *, limit: int = 1900) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _parse_ts(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            return None
    return None


def _next_tick_eta(payload: Dict[str, Any]) -> str:
    sched = _as_dict(payload.get("scheduler"))
    tick_sec = float(sched.get("tick_sec") or 1800.0)
    base_ts = sched.get("tick_finished_at") or sched.get("tick_started_at")
    if base_ts is None:
        return "확인 필요"
    base = _parse_ts(base_ts)
    if base is None:
        return "확인 필요"
    next_tick = base + timedelta(seconds=max(tick_sec, 1.0))
    remain = int((next_tick - datetime.now(timezone.utc)).total_seconds())
    if remain < 0:
        remain = 0
    return f"{remain // 60}분 {remain % 60}초 후"


def _regime_to_kor(raw_regime: Any) -> tuple[str, str]:
    code = str(raw_regime or "").strip().upper()
    if not code or code in {"NONE", "UNKNOWN"}:
        return "미판단", "-"
    if code in {"BEAR", "DOWN"}:
        return "하락", "BEAR"
    if code in {"BULL", "UP"}:
        return "상승", "BULL"
    if code in {"NEUTRAL", "FLAT", "SIDEWAYS"}:
        return "횡보", code
    return "기타", code


def _reason_to_kor(raw_reason: Any) -> str:
    reason = str(raw_reason or "").strip()
    if not reason:
        return "-"
    reason_map: Dict[str, str] = {
        "no_candidate": "진입 후보가 없어 판단을 건너뜁니다.",
        "vol_shock_no_entry": "변동성 급등 구간이라 신규 진입이 보류됩니다.",
        "confidence_below_threshold": "신뢰도 점수가 기준치 아래여서 진입을 보류합니다.",
        "short_not_allowed_regime": "현재 구간에서는 숏 진입이 제한됩니다.",
        "enter_candidate": "진입 후보를 확인해 주문 준비로 진행합니다.",
        "vol_shock_close": "변동성 급등으로 청산 판단이 보류되었습니다.",
        "profit_hold": "익절 조건 미달로 포지션을 유지합니다.",
        "same_symbol": "현재 보유 심볼과 후보 심볼이 같아 중복 진입을 생략합니다.",
        "gap_below_threshold": "점수 차이가 기준치 이하라 판단을 생략합니다.",
        "rebalance_to_better_candidate": "더 유리한 후보로 리밸런싱 예정입니다.",
        "close_symbol_missing": "종료 심볼 정보를 찾을 수 없습니다.",
        "enter_symbol_missing": "진입 심볼 정보를 찾을 수 없습니다.",
        "price_unavailable": "가격 데이터를 확인할 수 없습니다.",
        "cooldown_active": "쿨다운 상태여서 판단이 보류됩니다.",
        "daily_loss_limit_reached": "일일 손실 한도에 걸려 진입이 중단되었습니다.",
        "dd_limit_reached": "DD 한도에 걸려 진입이 중단되었습니다.",
        "lose_streak_cooldown": "연패 중이라 일시 중단됩니다.",
        "equity_unavailable": "자산(Equity) 데이터가 유효하지 않습니다.",
        "leverage_above_max_leverage": "레버리지가 허용 범위를 초과합니다.",
        "single_asset_rule_violation": "단일 자산 룰 충돌로 주문이 차단됩니다.",
        "exposure_above_max_exposure": "총 노출 한도 초과로 주문이 차단됩니다.",
        "notional_above_max_notional": "주문 기준금액이 최대 허용 한도를 초과했습니다.",
        "notional_unavailable": "명목 금액 계산값이 없습니다.",
        "per_trade_risk_exceeded": "1회 위험 한도를 초과했습니다.",
        "book_unavailable_market_disabled": "호가창 데이터가 유효하지 않습니다.",
    }

    for key in reason_map:
        if reason.startswith(key):
            return reason_map[key]
    return reason


def _position_side(amount: Any, side_hint: Any = None) -> str:
    if side_hint is not None:
        s = str(side_hint).strip().upper()
        if s in {"LONG", "BUY", "롱"}:
            return "롱"
        if s in {"SHORT", "SELL", "숏"}:
            return "숏"
    try:
        v = float(amount)
    except Exception:
        return "-"
    if v > 0:
        return "롱"
    if v < 0:
        return "숏"
    return "-"


def _first_position(positions: Any) -> tuple[str, str, float, str]:
    if not isinstance(positions, dict):
        return "-", "-", 0.0, "-"

    fallback_symbol = "-"
    fallback_unrealized = 0.0
    fallback_side = "-"
    for sym, row in positions.items():
        if not isinstance(row, dict):
            continue
        if fallback_symbol == "-":
            fallback_symbol = str(sym)
            fallback_side = _position_side(row.get("position_amt"), row.get("position_side"))
            try:
                fallback_unrealized = float(row.get("unrealized_pnl") or 0.0)
            except Exception:
                fallback_unrealized = 0.0

        try:
            amt = float(row.get("position_amt") or 0.0)
        except Exception:
            continue
        if abs(amt) > 1e-12:
            try:
                unrealized = float(row.get("unrealized_pnl") or 0.0)
            except Exception:
                unrealized = 0.0
            return str(sym), _fmt_money(abs(amt)), unrealized, _position_side(amt, row.get("position_side"))

    if fallback_symbol == "-":
        return "-", "-", 0.0, "-"
    return fallback_symbol, _fmt_money(0.0), fallback_unrealized, fallback_side


def _collect_unrealized(positions: Any, pnl: Dict[str, Any]) -> float:
    total = 0.0
    if isinstance(positions, dict):
        for row in positions.values():
            if isinstance(row, dict):
                try:
                    total += float(row.get("unrealized_pnl") or 0.0)
                except Exception:
                    continue
    if total != 0:
        return total

    if isinstance(pnl, dict):
        try:
            return float(pnl.get("last_unrealized_pnl_usdt") or 0.0)
        except Exception:
            pass
    return 0.0


def format_status_payload(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return _truncate(f"status payload type error: {type(payload).__name__}")

    engine = _as_dict(payload.get("engine_state"))
    binance = _as_dict(payload.get("binance"))
    pnl = _as_dict(payload.get("pnl"))
    sched = _as_dict(payload.get("scheduler"))
    risk = _as_dict(payload.get("risk_config"))
    summary = _as_dict(payload.get("config_summary"))
    capital = _as_dict(payload.get("capital_snapshot"))
    watchdog = _as_dict(payload.get("watchdog"))

    state = str(engine.get("state", "UNKNOWN"))
    enabled_symbols = [str(x) for x in (binance.get("enabled_symbols") or []) if str(x).strip()]

    positions = binance.get("positions")
    pos_symbol, pos_qty, pos_unrealized, pos_side = _first_position(positions)
    unrealized_sum = _collect_unrealized(positions, pnl)
    if unrealized_sum == 0 and pos_unrealized != 0:
        unrealized_sum = pos_unrealized

    daily_pnl = pnl.get("daily_pnl_pct") if isinstance(pnl, dict) else None
    if daily_pnl is None and isinstance(pnl, dict):
        daily_pnl = pnl.get("daily_realized_pnl")
    dd = pnl.get("drawdown_pct") if isinstance(pnl, dict) else None

    cand = _as_dict(sched.get("candidate") or sched.get("last_candidate"))
    candidate_symbol = str(cand.get("symbol") or "-")
    regime_raw = sched.get("regime_4h") or cand.get("regime_4h") or sched.get("last_regime")
    regime_kor, regime_code = _regime_to_kor(regime_raw)

    if candidate_symbol == "-":
        uni = risk.get("universe_symbols")
        if isinstance(uni, list) and uni:
            candidate_symbol = str(uni[0])

    decision_raw = sched.get("last_decision_reason")
    decision_code = str(decision_raw or "-").strip()
    decision_human = _reason_to_kor(decision_raw)

    last_action = str(sched.get("last_action") or "-")
    last_error = payload.get("last_error")

    lines: List[str] = []
    lines.append("[상태 알림]")
    lines.append(f"엔진 상태: {state}")
    if pos_symbol == "-":
        lines.append("현재 포지션: -")
    else:
        side_label = f" [{pos_side}]" if pos_side != "-" else ""
        lines.append(f"현재 포지션: {pos_symbol}{side_label} (수량 {pos_qty})")

    lines.append(
        "손익 요약: "
        f"미실현손익(uPnL) {_fmt_money(unrealized_sum)} USDT, "
        f"일일손익 {_fmt_pct(daily_pnl) if daily_pnl is not None else '-'}, "
        f"DD {_fmt_pct(dd) if dd is not None else '-'}"
    )
    lines.append(f"시장 판단: 레짐 {regime_kor}({regime_code}), 후보 심볼 {candidate_symbol}")

    if decision_code == "-":
        lines.append("이번 결정: -")
    else:
        lines.append(f"이번 결정: {decision_code} -> {decision_human}")

    lines.append(f"최근 액션: {last_action}")

    if enabled_symbols:
        lines.append(f"운영 심볼: {', '.join(enabled_symbols)}")

    if capital:
        budget = capital.get("budget_usdt")
        notional = capital.get("notional_usdt")
        blocked = capital.get("blocked")
        if budget is not None:
            lines.append(f"증거금: {_fmt_money(budget)} USDT")
        if notional is not None:
            lines.append(f"예상 주문금액: {_fmt_money(notional)} USDT")
        if blocked is not None:
            lines.append(f"예산 차단: {'예' if bool(blocked) else '아니오'}")

    lines.append(f"다음 판단: {_next_tick_eta(payload)}")

    ls = pnl.get("lose_streak") if isinstance(pnl, dict) else None
    cooldown_until = pnl.get("cooldown_until") if isinstance(pnl, dict) else None
    if ls is not None:
        lines.append(f"연패: {ls}")
    if cooldown_until:
        lines.append(f"쿨다운 해제 시각: {_fmt_time(cooldown_until)}")

    if watchdog.get("last_blocked_symbol"):
        lines.append(f"차단 심볼: {watchdog.get('last_blocked_symbol')}")

    if last_error:
        err_text = str(last_error)
        lines.append(f"오류: {err_text}")
        guide = _error_guidance(err_text)
        if guide is not None:
            code, issue, action = guide
            lines.append(f"권장 대응: {code} - {issue}")
            lines.append(f"대응: {action}")

    return _truncate("\n".join(lines))
