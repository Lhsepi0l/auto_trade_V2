from __future__ import annotations

from typing import Any

from v2.control.position_management_runtime import build_position_management_plan
from v2.control.runtime_utils import utcnow_iso
from v2.kernel.contracts import KernelCycleResult

POSITION_MANAGEMENT_MARKER_KEY = "position_management_state"


def load_position_management_state(controller: Any) -> dict[str, dict[str, Any]]:
    payload = controller.state_store.runtime_storage().load_runtime_marker(
        marker_key=POSITION_MANAGEMENT_MARKER_KEY
    )
    if not isinstance(payload, dict):
        return {}
    positions = payload.get("positions")
    if not isinstance(positions, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for symbol, row in positions.items():
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u or not isinstance(row, dict):
            continue
        normalized[symbol_u] = dict(row)
    return normalized


def save_position_management_state(controller: Any, state: dict[str, dict[str, Any]]) -> None:
    normalized = {
        str(symbol).strip().upper(): dict(payload)
        for symbol, payload in state.items()
        if str(symbol).strip() and isinstance(payload, dict)
    }
    controller.state_store.runtime_storage().save_runtime_marker(
        marker_key=POSITION_MANAGEMENT_MARKER_KEY,
        payload={"positions": normalized, "updated_at": utcnow_iso()},
    )


def clear_position_management_state(controller: Any, *, symbol: str) -> None:
    state = load_position_management_state(controller)
    state.pop(str(symbol).strip().upper(), None)
    save_position_management_state(controller, state)


def position_management_side(candidate_side: str | None) -> str:
    side = str(candidate_side or "").strip().upper()
    return "LONG" if side == "BUY" else "SHORT"


def record_position_management_plan(controller: Any, *, cycle: KernelCycleResult) -> None:
    plan = build_position_management_plan(cycle=cycle)
    if not isinstance(plan, dict):
        return
    symbol = str(plan.get("symbol") or "").strip().upper()
    if not symbol:
        return
    state = load_position_management_state(controller)
    state[symbol] = plan
    save_position_management_state(controller, state)
