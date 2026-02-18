from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from apps.trader_engine.services.notifier_service import _error_guidance


def _fmt_money(x: Any) -> str:
    try:
        return f"{float(x):.4f}"
    except Exception:
        return str(x)


def _truncate(s: str, *, limit: int = 1800) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _fmt_time(ts: Any) -> str:
    if not ts:
        return "-"
    if isinstance(ts, str):
        return ts
    try:
        if isinstance(ts, datetime):
            return ts.isoformat()
    except Exception:
        pass
    return str(ts)


def _fmt_pct(x: Any) -> str:
    try:
        v = float(x)
        return f"{v:.2f}%"
    except Exception:
        return str(x)


def _remaining_sec(sched: Dict[str, Any] | None) -> str:
    if not sched:
        return "-"
    try:
        tick_sec = float(sched.get("tick_sec") or 1800.0)
        base_ts = sched.get("tick_finished_at") or sched.get("tick_started_at")
        if not base_ts:
            return "-"
        dt = datetime.fromisoformat(str(base_ts))
        next_ts = dt + timedelta(seconds=max(tick_sec, 1.0))
        now = datetime.now(tz=timezone.utc)
        remaining = int((next_ts - now).total_seconds())
        if remaining < 0:
            remaining = 0
        return f"{remaining // 60}분 {remaining % 60}초"
    except Exception:
        return "-"


def _format_leverage_lines(enabled_symbols: list[str], sched: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    lev_map = sched.get("symbol_leverage") or {}
    target = sched.get("leverage_sync_target")
    updated_at = sched.get("leverage_sync_updated_at")
    err = sched.get("leverage_sync_error")

    items: List[str] = []
    for sym in enabled_symbols:
        current = lev_map.get(sym)
        if isinstance(current, (int, float)):
            if target is None:
                items.append(f"{sym}:{_fmt_money(current)}")
            else:
                c = _fmt_money(current)
                t = _fmt_money(target)
                items.append(f"{sym}:{c}/{t}")
        else:
            items.append(f"{sym}:?-/{_fmt_money(target)}" if target is not None else f"{sym}:?")
    if items:
        lines.append("심볼 레버리지(현재/목표): " + ", ".join(items))
    if updated_at:
        lines.append(f"레버리지 마지막 동기화: {_fmt_time(updated_at)}")
    if err:
        lines.append(f"레버리지 동기화 상태: 실패 ({err})")
    elif target is not None:
        lines.append("레버리지 동기화 상태: OK")
    return lines


def format_status_payload(payload: Dict[str, Any]) -> str:
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
    enabled = [str(x) for x in (binance.get("enabled_symbols") or [])]
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
            pos_lines.append(f"- {sym}: 수량={amt}, 진입가={entry}, 손익={pnl}, 레버리지={lev}")

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
                spread_wide.append(f"- {sym}: spread_pct={_fmt_pct(row.get('spread_pct'))}")

    # 엔진 위험/자금/최근 에러
    dd = pnl.get("drawdown_pct")
    dp = pnl.get("daily_pnl_pct")
    if dp is None and isinstance(pnl, dict):
        dp = pnl.get("daily_realized_pnl", 0)
    ls = pnl.get("lose_streak")
    cd = pnl.get("cooldown_until")

    lines: List[str] = []
    lines.append(f"엔진 상태: {state}")
    lines.append(f"모의모드(DRY_RUN): {dry_run} (strict={dry_run_strict})")
    lines.append(f"활성 심볼: {', '.join(enabled) if enabled else '(없음)'}")
    if disabled:
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
        lines.append("확장된 스프레드:")
        lines.extend(spread_wide[:5])

    if isinstance(pnl, dict) and pnl:
        lines.append(f"손익 요약: 미실현손익 { _fmt_money(pnl.get('last_unrealized_pnl_usdt') or 0.0)} USDT, 일일손익 {_fmt_pct(dp if dp is not None else 0.0)}, DD {_fmt_pct(dd if dd is not None else 0.0)}")
        if ls is not None:
            lines.append(f"연패: {ls}")
        if cd:
            lines.append(f"쿨다운 해제 시각: {_fmt_time(cd)}")

    if isinstance(sched, dict) and sched:
        cand = sched.get("candidate") or {}
        ai = sched.get("ai_signal") or {}
        la = sched.get("last_action")
        le = sched.get("last_error")

        if isinstance(cand, dict) and cand.get("symbol"):
            lines.append(
                f"후보: {cand.get('symbol')} {cand.get('direction')} "
                f"강도={_fmt_money(cand.get('strength'))} 변동성={cand.get('vol_tag')}"
            )
        if isinstance(ai, dict) and ai.get("target_asset"):
            lines.append(
                f"AI: {ai.get('target_asset')} {ai.get('direction')} "
                f"신뢰도={_fmt_money(ai.get('confidence'))} 힌트={ai.get('exec_hint')} 태그={ai.get('risk_tag')}"
            )

        lines.extend(_format_leverage_lines(enabled, sched))
        lines.append(f"다음 판단까지: {_remaining_sec(sched)}")

        if la:
            lines.append(f"최근 액션: {la}")
        if le:
            lines.append(f"최근 오류: {le}")

    if isinstance(summary, dict) and summary:
        lines.append(
            f"설정: symbols={','.join(summary.get('universe_symbols') or [])} "
            f"max_lev={summary.get('max_leverage')} "
            f"dl={summary.get('daily_loss_limit_pct')} dd={summary.get('dd_limit_pct')} "
            f"spread={summary.get('spread_max_pct')}"
        )
    elif isinstance(risk, dict):
        lines.append(
            f"리스크: per_trade={risk.get('per_trade_risk_pct')}% "
            f"max_lev={risk.get('max_leverage')} notify={risk.get('notify_interval_sec')}s"
        )

    if last_error:
        lines.append(f"오류: {last_error}")
        guide = _error_guidance(str(last_error))
        if guide is not None:
            code, issue, action = guide
            lines.append(f"- error_code: {code}")
            lines.append(f"- error_issue: {issue}")
            lines.append(f"- recommended_action: {action}")

    return _truncate("\n".join(lines))
