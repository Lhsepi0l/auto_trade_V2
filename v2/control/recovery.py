from __future__ import annotations

from typing import Any

from v2.control.runtime_utils import (
    FutureTimeoutError,
    logger,
)
from v2.control.runtime_utils import (
    run_async_blocking as _run_async_blocking,
)
from v2.control.runtime_utils import (
    to_float as _to_float,
)
from v2.exchange.types import ResyncSnapshot


def recover_submission_intents_from_snapshot(
    controller: Any,
    *,
    snapshot: ResyncSnapshot,
    reason: str,
) -> dict[str, Any]:
    storage = controller.state_store.runtime_storage()
    rows = storage.list_submission_intents(statuses=["PENDING", "SUBMIT_ERROR", "REVIEW_REQUIRED"])
    open_orders_by_client: dict[str, dict[str, Any]] = {}
    for row in snapshot.open_orders:
        if not isinstance(row, dict):
            continue
        client_order_id = str(row.get("clientOrderId") or row.get("c") or "").strip()
        if client_order_id:
            open_orders_by_client[client_order_id] = row
    live_position_symbols = {
        str(item.get("symbol") or item.get("s") or "").strip().upper()
        for item in snapshot.positions
        if isinstance(item, dict)
        and abs(
            _to_float(
                item.get("positionAmt") if item.get("positionAmt") is not None else item.get("pa"),
                default=0.0,
            )
        )
        > 0.0
    }

    resolved_open = 0
    resolved_position = 0
    resolved_not_found = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        intent_id = str(row.get("intent_id") or "").strip()
        client_order_id = str(row.get("client_order_id") or "").strip()
        symbol = str(row.get("symbol") or "").strip().upper()
        if not intent_id or not client_order_id:
            continue
        open_row = open_orders_by_client.get(client_order_id)
        if open_row is not None:
            storage.mark_submission_intent_status(
                intent_id=intent_id,
                status="SUBMITTED",
                order_id=str(open_row.get("orderId") or ""),
            )
            resolved_open += 1
            continue
        if symbol and symbol in live_position_symbols:
            storage.mark_submission_intent_status(
                intent_id=intent_id,
                status="RECOVERED_POSITION_OPEN",
            )
            resolved_position += 1
            continue
        storage.mark_submission_intent_status(
            intent_id=intent_id,
            status="NOT_FOUND_AFTER_RECONCILE",
        )
        resolved_not_found += 1

    summary = {
        "reason": reason,
        "resolved_open_orders": resolved_open,
        "resolved_positions": resolved_position,
        "resolved_not_found": resolved_not_found,
        "pending_before": len(rows),
        "pending_after": controller._submission_recovery_snapshot()["pending_review_count"],
    }
    controller._boot_recovery["submission_recovery"] = summary
    controller._log_event("submission_recovery_sync", **summary)
    return summary


def perform_startup_reconcile(controller: Any, *, reason: str) -> None:
    if controller.cfg.mode != "live":
        controller._startup_reconcile_ok = None
        return
    controller._log_event("reconcile_start", reason=reason)
    try:
        snapshot = controller._fetch_exchange_snapshot()
        controller._apply_resync_snapshot(snapshot=snapshot, reason=reason)
    except FutureTimeoutError:
        controller._startup_reconcile_ok = False
        controller._set_state_uncertain(reason=f"{reason}:timeout", engage_safe_mode=True)
        controller._log_event("reconcile_failure", reason=f"{reason}:timeout")
        logger.warning("startup_reconcile_timed_out")
    except Exception as exc:  # noqa: BLE001
        controller._startup_reconcile_ok = False
        controller._set_state_uncertain(
            reason=f"{reason}:{type(exc).__name__}",
            engage_safe_mode=True,
        )
        controller._log_event("reconcile_failure", reason=f"{reason}:{type(exc).__name__}")
        logger.exception("startup_reconcile_failed")


def auto_reconcile_if_recovery_required(controller: Any, *, reason: str) -> bool:
    if not controller._recovery_required:
        return True
    if controller.cfg.mode != "live":
        controller._clear_recovery_required(reason=reason)
        return True
    controller._log_event("auto_reconcile_attempt", reason=reason)
    controller._perform_startup_reconcile(reason="manual_reconcile")
    if not controller._state_uncertain:
        controller._recover_brackets_on_boot(reason="manual_reconcile")
    return not controller._recovery_required


def recover_brackets_on_boot(controller: Any, *, reason: str = "startup_reconcile") -> None:
    if controller.cfg.mode != "live" or controller.rest_client is None:
        return
    controller._log_event("bracket_recovery_start", reason=reason)
    tracked_before = {
        str(row.get("symbol") or "").strip().upper(): dict(row)
        for row in controller._list_tracked_brackets()
        if isinstance(row, dict) and str(row.get("symbol") or "").strip()
    }
    try:
        result = _run_async_blocking(
            lambda: controller._bracket_service.recover(),
            timeout_sec=10.0,
        )
        if isinstance(result, list):
            for runtime in result:
                symbol = str(getattr(runtime, "symbol", "") or "").strip().upper()
                if not symbol:
                    continue
                previous = tracked_before.get(symbol)
                if previous is None:
                    continue
                if str(getattr(runtime, "state", "") or "").strip().upper() != "CLEANED":
                    continue
                inferred_exit = controller._infer_flat_bracket_exit(
                    symbol=symbol,
                    tp_id=str(previous.get("tp_order_client_id") or "").strip(),
                    sl_id=str(previous.get("sl_order_client_id") or "").strip(),
                )
                if inferred_exit is None:
                    continue
                outcome, _filled_id = inferred_exit
                controller._emit_bracket_exit_alert(symbol=symbol, outcome=outcome)
        controller._boot_recovery["bracket_recovery"] = {
            "reason": reason,
            "ok": True,
            "result": result if isinstance(result, dict) else {},
        }
        controller._clear_recovery_required(
            only_when_prefix="bracket_recovery_",
            reason=reason,
        )
        controller._log_event("bracket_recovery_success", reason=reason)
    except FutureTimeoutError:
        controller._boot_recovery["bracket_recovery"] = {
            "reason": reason,
            "ok": False,
            "error": "timeout",
        }
        controller._set_recovery_required(reason="bracket_recovery_timeout")
        controller._log_event("bracket_recovery_failure", reason=f"{reason}:timeout")
        logger.warning("bracket_recover_timed_out")
    except Exception:  # noqa: BLE001
        controller._boot_recovery["bracket_recovery"] = {
            "reason": reason,
            "ok": False,
            "error": "exception",
        }
        controller._set_recovery_required(reason="bracket_recovery_exception")
        controller._log_event("bracket_recovery_failure", reason=f"{reason}:exception")
        logger.exception("bracket_recover_failed")
