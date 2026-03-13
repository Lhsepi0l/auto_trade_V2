from __future__ import annotations

from typing import Any


def capture_runtime_state(state_store: Any) -> Any:
    return state_store.get()


def capture_public_risk_config(controller: Any) -> dict[str, Any]:
    return controller._public_risk_config()


def capture_last_cycle_snapshot(
    last_cycle: dict[str, Any],
    *,
    coalesced: bool = False,
) -> dict[str, Any]:
    snapshot = dict(last_cycle)
    if coalesced:
        snapshot["coalesced"] = True
    return snapshot


def apply_tick_busy_cycle_state(
    last_cycle: dict[str, Any],
    *,
    finished_at: str,
) -> dict[str, Any]:
    last_cycle["tick_finished_at"] = finished_at
    last_cycle["last_action"] = "blocked"
    last_cycle["last_decision_reason"] = "tick_busy"
    last_cycle["last_error"] = "tick_busy"
    last_cycle["candidate"] = None
    last_cycle["last_candidate"] = None
    last_cycle["portfolio"] = None
    return capture_last_cycle_snapshot(last_cycle)
