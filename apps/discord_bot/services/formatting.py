from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List


def _fmt_money(x: Any) -> str:
    try:
        return f"{float(x):.4f}"
    except Exception:
        return str(x)


def _truncate(s: str, *, limit: int = 1800) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


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
            pos_lines.append(f"- {sym}: amt={amt} entry={entry} pnl={pnl} lev={lev}")

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
        try:
            tick_sec = float(sched.get("tick_sec") or 1800.0)
            base_ts = sched.get("tick_finished_at") or sched.get("tick_started_at")
            if base_ts:
                next_ts = datetime.fromisoformat(str(base_ts)) + timedelta(seconds=max(tick_sec, 1.0))
                now = datetime.now(tz=timezone.utc)
                remaining = int((next_ts - now).total_seconds())
                if remaining < 0:
                    remaining = 0
                lines.append(f"다음 판단까지: {remaining // 60}분 {remaining % 60}초")
        except Exception:
            pass
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
