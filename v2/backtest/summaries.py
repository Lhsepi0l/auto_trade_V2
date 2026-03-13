from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from typing import Any

from v2.backtest.analytics import _calc_trade_event_drawdown_pct


def _format_utc_iso(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _add_months_utc(dt: datetime, months: int) -> datetime:
    total_month = (int(dt.month) - 1) + int(months)
    year = int(dt.year) + (total_month // 12)
    month = (total_month % 12) + 1
    day = min(int(dt.day), calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _build_half_year_window_summaries(
    *,
    symbol_reports: list[dict[str, Any]],
    start_ms: int,
    end_ms: int,
    total_initial_capital: float,
) -> list[dict[str, Any]]:
    if int(start_ms) >= int(end_ms):
        return []

    start_dt = datetime.fromtimestamp(int(start_ms) / 1000.0, tz=timezone.utc).replace(
        microsecond=0
    )
    end_dt = datetime.fromtimestamp(int(end_ms) / 1000.0, tz=timezone.utc).replace(microsecond=0)
    windows: list[tuple[int, int, str]] = []
    cursor = start_dt
    while cursor < end_dt:
        next_dt = _add_months_utc(cursor, 6)
        if next_dt <= cursor:
            break
        if next_dt > end_dt:
            next_dt = end_dt
        window_start_ms = int(cursor.timestamp() * 1000)
        window_end_ms = int(next_dt.timestamp() * 1000)
        windows.append(
            (
                window_start_ms,
                window_end_ms,
                f"{cursor.date().isoformat()} ~ {(next_dt - timedelta(milliseconds=1)).date().isoformat()}",
            )
        )
        cursor = next_dt

    payload: list[dict[str, Any]] = []
    for window_start_ms, window_end_ms, label in windows:
        events: list[dict[str, Any]] = []
        for item in symbol_reports:
            trade_events = item.get("trade_events")
            if not isinstance(trade_events, list):
                continue
            for trade in trade_events:
                if not isinstance(trade, dict):
                    continue
                try:
                    exit_time_ms = int(trade.get("exit_time_ms") or 0)
                except (TypeError, ValueError):
                    continue
                if int(window_start_ms) <= exit_time_ms < int(window_end_ms):
                    events.append(dict(trade))

        gross_profit = sum(max(0.0, float(item.get("pnl") or 0.0)) for item in events)
        gross_loss = abs(sum(min(0.0, float(item.get("pnl") or 0.0)) for item in events))
        net_profit = sum(float(item.get("pnl") or 0.0) for item in events)
        total_fees = sum(
            max(
                float(item.get("entry_fee") or 0.0) + float(item.get("exit_fee") or 0.0),
                0.0,
            )
            for item in events
        )
        gross_trade_pnl = sum(float(item.get("gross_pnl") or 0.0) for item in events)
        profit_factor = None if gross_loss <= 0.0 else round(gross_profit / gross_loss, 6)
        payload.append(
            {
                "label": label,
                "start_ms": int(window_start_ms),
                "end_ms": int(window_end_ms),
                "start_utc": _format_utc_iso(window_start_ms),
                "end_utc": _format_utc_iso(window_end_ms),
                "trades": len(events),
                "net_profit": round(net_profit, 6),
                "profit_factor": profit_factor,
                "fee_to_trade_gross_pct": (
                    round((total_fees / gross_trade_pnl) * 100.0, 6)
                    if gross_trade_pnl > 0.0
                    else None
                ),
                "max_drawdown_pct": round(
                    _calc_trade_event_drawdown_pct(
                        trade_events=events,
                        initial_equity=float(total_initial_capital),
                    ),
                    6,
                ),
            }
        )
    return payload
