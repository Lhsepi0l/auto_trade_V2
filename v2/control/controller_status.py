from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from v2.control.controller_events import dispatch_webpush_notification
from v2.control.presentation import build_status_pnl_summary
from v2.control.report_builders import build_daily_report_message, build_daily_report_payload
from v2.notify.runtime_events import build_report_notification

logger = logging.getLogger(__name__)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def status_positions_source(controller: Any) -> list[tuple[str, float, float, float]]:
    live_positions, live_rows, live_ok, _live_error = controller._fetch_live_positions()
    if live_ok:
        out_live: list[tuple[str, float, float, float]] = []
        for symbol, row in sorted(live_rows.items()):
            if not isinstance(row, dict):
                continue
            position_amt = _to_float(row.get("positionAmt"), default=0.0)
            if abs(position_amt) <= 0.0:
                continue
            entry_price = _to_float(row.get("entryPrice"), default=0.0)
            unrealized = _to_float(
                row.get("unRealizedProfit") or row.get("unrealizedProfit"),
                default=0.0,
            )
            out_live.append((symbol, position_amt, unrealized, entry_price))
        if out_live:
            return out_live
        if any(abs(_to_float(v, default=0.0)) > 0.0 for v in live_positions.values()):
            return [
                (symbol, _to_float(amount, default=0.0), 0.0, 0.0)
                for symbol, amount in sorted(live_positions.items())
                if abs(_to_float(amount, default=0.0)) > 0.0
            ]
        return []

    state = controller.state_store.get()
    out_state: list[tuple[str, float, float, float]] = []
    for symbol, row in sorted(state.current_position.items()):
        position_amt = _to_float(row.position_amt, default=0.0)
        if abs(position_amt) <= 0.0:
            continue
        pnl = _to_float(row.unrealized_pnl, default=0.0)
        entry_price = _to_float(row.entry_price, default=0.0)
        out_state.append((symbol, position_amt, pnl, entry_price))
    return out_state


def status_pnl_summary(controller: Any) -> tuple[str, str]:
    return build_status_pnl_summary(
        positions=status_positions_source(controller),
        fills=controller.state_store.get().last_fills,
    )


def emit_status_update(controller: Any, *, force: bool = False) -> bool:
    if not controller.notifier.supports_periodic_status():
        return False
    notify_interval = max(
        1,
        int(_to_float(controller._risk.get("notify_interval_sec"), default=30.0)),
    )
    now = datetime.now(timezone.utc)
    should_notify = force or (
        controller._last_status_notify_at is None
        or (now - controller._last_status_notify_at).total_seconds() >= float(notify_interval)
    )
    if not should_notify:
        return False
    try:
        controller.notifier.send(controller._status_summary())
        controller._last_status_notify_at = now
        return True
    except Exception:  # noqa: BLE001
        logger.exception("status_notify_failed")
        return False


def start_status_loop(controller: Any) -> None:
    if controller._status_thread is not None and controller._status_thread.is_alive():
        return

    def _worker() -> None:
        while not controller._status_thread_stop.is_set():
            interval = max(
                1,
                int(_to_float(controller._risk.get("notify_interval_sec"), default=30.0)),
            )
            if controller._status_thread_stop.wait(timeout=float(interval)):
                break
            emit_status_update(controller, force=False)

    controller._status_thread = threading.Thread(target=_worker, daemon=True)
    controller._status_thread.start()


def format_daily_report_message(payload: dict[str, Any]) -> str:
    return build_daily_report_message(payload)


def send_daily_report(controller: Any) -> dict[str, Any]:
    payload = build_daily_report_payload(
        day=datetime.now(timezone.utc).date().isoformat(),
        engine_state=controller.state_store.get().status,
        detail=dict(controller._report_stats),
        notifier_enabled=bool(controller.notifier.enabled),
        reported_at=datetime.now(timezone.utc).isoformat(),
    )
    message = format_daily_report_message(payload)
    notification = build_report_notification(
        payload=payload,
        context=controller._notification_context(),
    )
    result = controller.notifier.send_notification(notification)
    dispatch_webpush_notification(controller, notification)
    payload["notifier_sent"] = bool(result.sent)
    if result.error and result.error != "disabled":
        payload["notifier_error"] = result.error
    payload["summary"] = message
    controller._log_event(
        "report_sent",
        status="sent" if bool(payload["notifier_sent"]) else "not_sent",
        notifier_error=payload.get("notifier_error"),
        event_time=payload.get("reported_at"),
    )
    controller._last_report = {
        "reported_at": payload.get("reported_at"),
        "kind": payload.get("kind"),
        "day": payload.get("day"),
        "status": "success" if payload.get("notifier_error") in {None, "disabled"} else "failed",
        "notifier_enabled": bool(payload.get("notifier_enabled")),
        "notifier_sent": bool(payload.get("notifier_sent")),
        "notifier_error": payload.get("notifier_error"),
        "summary": message,
        "detail": dict(payload.get("detail") or {}),
    }
    return payload
