from __future__ import annotations

from typing import Any

from v2.control.profile_policy import PRIVATE_REST_UNSAFE_ERRORS


def gate_snapshot(controller: Any, *, probe_private_rest: bool = False) -> dict[str, Any]:
    freshness = controller._freshness_snapshot()
    submission_recovery = controller._submission_recovery_snapshot()
    ops_state = controller.state_store.get().operational
    bracket_recovery = controller._boot_recovery.get("bracket_recovery")
    if probe_private_rest and controller.cfg.mode == "live":
        controller._fetch_live_usdt_balance()
    last_reconcile_at = freshness["last_reconcile_at"]
    user_ws_ok = controller.cfg.mode != "live" or (
        controller._user_stream_started
        and freshness["private_stream_seen"]
        and not freshness["user_ws_stale"]
    )
    reconcile_age_ok = (
        controller._startup_reconcile_ok is True
        and last_reconcile_at is not None
        and freshness["reconcile_age_sec"] is not None
        and freshness["reconcile_age_sec"] <= freshness["reconcile_max_age_sec"]
    )
    reconcile_live_sync_ok = (
        controller.cfg.mode == "live"
        and controller._startup_reconcile_ok is True
        and user_ws_ok
    )
    last_reconcile_ok = controller.cfg.mode != "live" or reconcile_age_ok or reconcile_live_sync_ok
    market_data_ok = controller.cfg.mode != "live" or (
        freshness["market_data_seen"] and not freshness["market_data_stale"]
    )
    bracket_recovery_ok = controller.cfg.mode != "live" or (
        isinstance(bracket_recovery, dict) and bool(bracket_recovery.get("ok"))
    )
    private_auth_ok = controller.cfg.mode != "live" or (
        controller.rest_client is not None
        and str(controller._last_balance_error or "") not in PRIVATE_REST_UNSAFE_ERRORS
    )
    ready = all(
        [
            bool(controller._runtime_lock_active or controller.cfg.mode != "live"),
            not controller._state_uncertain,
            not controller._recovery_required,
            bool(submission_recovery["ok"]),
            bracket_recovery_ok,
            last_reconcile_ok,
            user_ws_ok,
            market_data_ok,
            not bool(ops_state.paused),
            not bool(ops_state.safe_mode),
            private_auth_ok,
        ]
    )
    return {
        "ready": ready,
        "single_instance_ok": bool(controller._runtime_lock_active or controller.cfg.mode != "live"),
        "state_uncertain": bool(controller._state_uncertain),
        "recovery_required": bool(controller._recovery_required),
        "startup_reconcile_ok": controller._startup_reconcile_ok,
        "submission_recovery_ok": bool(submission_recovery["ok"]),
        "bracket_recovery_ok": bracket_recovery_ok,
        "last_reconcile_ok": last_reconcile_ok,
        "reconcile_age_ok": reconcile_age_ok,
        "reconcile_live_sync_ok": reconcile_live_sync_ok,
        "user_ws_ok": user_ws_ok,
        "market_data_ok": market_data_ok,
        "private_auth_ok": private_auth_ok,
        "paused": bool(ops_state.paused),
        "safe_mode": bool(ops_state.safe_mode),
        "freshness": freshness,
        "submission_recovery": submission_recovery,
        "bracket_recovery": bracket_recovery,
    }
