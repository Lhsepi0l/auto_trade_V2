from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from v2.control.live_balance_helpers import build_freshness_snapshot
from v2.control.operator_events import build_operator_event_payload
from v2.notify.runtime_events import (
    RuntimeNotificationContext,
    build_runtime_event_notification,
)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

logger = logging.getLogger(__name__)


def notification_context(controller: Any) -> RuntimeNotificationContext:
    return RuntimeNotificationContext(
        profile=controller.cfg.profile,
        mode=controller.cfg.mode,
        env=controller.cfg.env,
    )


def should_emit_operator_event(controller: Any, *, event: str, fields: dict[str, Any]) -> bool:
    if str(event or "").strip() != "cycle_result":
        return True
    if str(fields.get("trigger_source") or "").strip().lower() != "scheduler":
        return True

    interval_sec = max(
        1.0,
        float(_to_float(fields.get("notify_interval_sec"), default=30.0)),
    )
    now_mono = time.monotonic()
    last_mono = controller._last_scheduler_cycle_event_mono
    if last_mono is not None and (now_mono - last_mono) < interval_sec:
        return False
    controller._last_scheduler_cycle_event_mono = now_mono
    return True


def log_event(controller: Any, event: str, *, notify: bool = True, **fields: Any) -> None:
    ops_state = controller.state_store.get().operational
    logger.info(
        event,
        extra={
            "event": event,
            "mode": controller.cfg.mode,
            "profile": controller.cfg.profile,
            "state_uncertain": bool(controller._state_uncertain),
            "safe_mode": bool(ops_state.safe_mode),
            **fields,
        },
    )
    if not controller._should_emit_operator_event(event=event, fields=fields):
        return
    payload = build_operator_event_payload(event=event, fields=fields)
    if payload is not None:
        controller.state_store.runtime_storage().append_operator_event(
            event_type=str(payload["event_type"]),
            category=str(payload["category"]),
            title=str(payload["title"]),
            main_text=str(payload["main_text"]),
            sub_text=(
                str(payload["sub_text"])
                if payload.get("sub_text") is not None
                else None
            ),
            event_time=str(payload["event_time"]),
            context=dict(payload.get("context") or {}),
        )
    notification = build_runtime_event_notification(
        event=event,
        fields=fields,
        context=controller._notification_context(),
        provider=controller.notifier.resolved_provider(),
    )
    if notification is not None and notify and not controller._boot_notification_muted:
        _ = controller.notifier.send_notification(notification)
        dispatch_webpush_notification(controller, notification)


def dispatch_webpush_notification(controller: Any, notification: Any) -> None:
    if notification is None:
        return
    webpush_service = getattr(controller, "webpush_service", None)
    if webpush_service is None or not hasattr(webpush_service, "send"):
        return
    try:
        _ = webpush_service.send(notification)
    except Exception:  # noqa: BLE001
        logger.exception(
            "webpush_dispatch_failed event_type=%s",
            getattr(notification, "event_type", None),
        )


def freshness_snapshot(controller: Any) -> dict[str, Any]:
    return build_freshness_snapshot(controller)


def maybe_log_ready_transition(
    controller: Any,
    *,
    notify: bool = True,
    force: bool = False,
) -> None:
    gate = controller._gate_snapshot()
    ready = bool(gate["ready"])
    if controller._last_ready_state is None or controller._last_ready_state != ready or force:
        controller._last_ready_state = ready
        controller._log_event(
            "ready_transition",
            notify=notify,
            ready=ready,
            recovery_required=bool(controller._recovery_required),
            submission_recovery_ok=bool(gate["submission_recovery_ok"]),
            user_ws_stale=bool(gate["freshness"]["user_ws_stale"]),
            market_data_stale=bool(gate["freshness"]["market_data_stale"]),
        )


def update_stale_transitions(controller: Any) -> None:
    freshness = controller._freshness_snapshot()
    user_ws_stale = bool(freshness["user_ws_stale"])
    market_data_stale = bool(freshness["market_data_stale"])
    changed = False
    if controller._last_user_ws_stale is None or controller._last_user_ws_stale != user_ws_stale:
        changed = True
        controller._last_user_ws_stale = user_ws_stale
        controller._log_event(
            "stale_transition",
            stale_type="user_ws",
            stale=user_ws_stale,
            age_sec=freshness["user_ws_age_sec"],
        )
    if (
        controller._last_market_data_stale is None
        or controller._last_market_data_stale != market_data_stale
    ):
        changed = True
        controller._last_market_data_stale = market_data_stale
        controller._log_event(
            "stale_transition",
            stale_type="market_data",
            stale=market_data_stale,
            age_sec=freshness["market_data_age_sec"],
        )
    if changed:
        controller._maybe_log_ready_transition()


def maybe_probe_market_data(controller: Any) -> None:
    probe = getattr(controller.kernel, "probe_market_data", None)
    if not callable(probe):
        return
    try:
        _ = probe()
        controller._market_data_state["last_market_data_source_ok_at"] = _utcnow_iso()
        controller._market_data_state["last_market_data_source_error"] = None
        controller._market_data_state["last_market_data_source_fail_at"] = None
    except Exception as exc:  # noqa: BLE001
        controller._market_data_state["last_market_data_source_fail_at"] = _utcnow_iso()
        controller._market_data_state["last_market_data_source_error"] = type(exc).__name__
        controller._log_event(
            "market_data_probe_failed",
            reason=type(exc).__name__,
        )


def submission_recovery_snapshot(controller: Any) -> dict[str, Any]:
    rows = controller.state_store.runtime_storage().list_submission_intents(
        statuses=["REVIEW_REQUIRED"]
    )
    preview = [
        {
            "intent_id": str(row.get("intent_id") or ""),
            "client_order_id": str(row.get("client_order_id") or ""),
            "symbol": str(row.get("symbol") or ""),
            "status": str(row.get("status") or ""),
            "updated_at": row.get("updated_at"),
        }
        for row in rows[:10]
        if isinstance(row, dict)
    ]
    return {
        "pending_review_count": len(rows),
        "pending_review": preview,
        "ok": len(rows) == 0,
    }


def gate_snapshot(controller: Any, *, probe_private_rest: bool = False) -> dict[str, Any]:
    from v2.control.gates import gate_snapshot as build_gate_snapshot

    return build_gate_snapshot(controller, probe_private_rest=probe_private_rest)
