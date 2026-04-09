from __future__ import annotations

from typing import Any

from v2.common.async_bridge import run_async_blocking
from v2.exchange.types import ResyncSnapshot


def set_state_uncertain(controller: Any, *, reason: str, engage_safe_mode: bool) -> None:
    next_reason = str(reason or "state_uncertain")
    changed = (not controller._state_uncertain) or controller._state_uncertain_reason != next_reason
    controller._state_uncertain = True
    controller._state_uncertain_reason = next_reason
    if engage_safe_mode:
        controller.ops.safe_mode()
    if changed:
        controller._log_event(
            "uncertainty_transition",
            state_uncertain=True,
            reason=next_reason,
            engage_safe_mode=bool(engage_safe_mode),
        )
        controller._maybe_log_ready_transition()


def clear_state_uncertain(controller: Any) -> None:
    changed = controller._state_uncertain
    controller._state_uncertain = False
    controller._state_uncertain_reason = None
    if changed:
        controller._log_event("uncertainty_transition", state_uncertain=False)
        controller._maybe_log_ready_transition()


def should_clear_uncertainty_on_private_ok(controller: Any) -> bool:
    if not controller._state_uncertain:
        return False
    reason = str(controller._state_uncertain_reason or "").strip()
    last_error = str(controller._user_stream_last_error or "").strip()
    if not reason or not last_error or reason != last_error:
        return False
    return (
        reason == "user_stream_disconnected"
        or reason == "listen_key_expired"
        or reason.startswith("user_stream_error:")
        or reason.startswith("socket_")
    )


def set_recovery_required(controller: Any, *, reason: str) -> None:
    next_reason = str(reason or "recovery_required")
    changed = (not controller._recovery_required) or controller._recovery_reason != next_reason
    controller._recovery_required = True
    controller._recovery_reason = next_reason
    if changed:
        controller._log_event("recovery_transition", recovery_required=True, reason=next_reason)
        controller._maybe_log_ready_transition()


def clear_recovery_required(
    controller: Any,
    *,
    only_when_prefix: str | None = None,
    reason: str,
) -> None:
    current_reason = str(controller._recovery_reason or "")
    if only_when_prefix is not None and not current_reason.startswith(only_when_prefix):
        return
    changed = controller._recovery_required or bool(controller._recovery_reason)
    controller._recovery_required = False
    controller._recovery_reason = None
    if changed:
        controller._log_event("recovery_transition", recovery_required=False, reason=reason)
        controller._maybe_log_ready_transition()


def fetch_exchange_snapshot(controller: Any) -> ResyncSnapshot:
    if controller.rest_client is None:
        raise RuntimeError("rest_client_missing")

    def _call_or_default(name: str) -> list[dict[str, Any]]:
        method = getattr(controller.rest_client, name, None)
        if not callable(method):
            return []
        value = run_async_blocking(lambda: method(), timeout_sec=15.0)
        return value if isinstance(value, list) else []

    open_orders = _call_or_default("get_open_orders")
    positions = _call_or_default("get_positions")
    balances = _call_or_default("get_balances")
    return ResyncSnapshot(
        open_orders=open_orders,
        positions=positions,
        balances=balances,
    )


def apply_resync_snapshot(controller: Any, *, snapshot: ResyncSnapshot, reason: str) -> None:
    controller.state_store.startup_reconcile(snapshot=snapshot, reason=reason)
    controller._recover_submission_intents_from_snapshot(snapshot=snapshot, reason=reason)
    controller._startup_reconcile_ok = True
    controller._clear_state_uncertain()
    if reason == "manual_reconcile" and controller._recovery_required:
        controller._clear_recovery_required(reason=reason)
    controller._log_event("reconcile_success", reason=reason)
    controller._update_stale_transitions()
