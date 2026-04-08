from __future__ import annotations

from enum import Enum


class PositionLifecycleState(str, Enum):
    ENTRY_ARMED = "ENTRY_ARMED"
    RUNNER_ACTIVE = "RUNNER_ACTIVE"
    EXIT_PENDING = "EXIT_PENDING"
    EXITED = "EXITED"


class PositionLifecycleEvent(str, Enum):
    ENTRY_RECORDED = "entry_recorded"
    TP1_COMPLETED = "tp1_completed"
    EXIT_REQUESTED = "exit_requested"
    EXIT_CONFIRMED = "exit_confirmed"


_TRANSITIONS: dict[PositionLifecycleState | None, dict[PositionLifecycleEvent, PositionLifecycleState]] = {
    None: {
        PositionLifecycleEvent.ENTRY_RECORDED: PositionLifecycleState.ENTRY_ARMED,
    },
    PositionLifecycleState.ENTRY_ARMED: {
        PositionLifecycleEvent.TP1_COMPLETED: PositionLifecycleState.RUNNER_ACTIVE,
        PositionLifecycleEvent.EXIT_REQUESTED: PositionLifecycleState.EXIT_PENDING,
        PositionLifecycleEvent.EXIT_CONFIRMED: PositionLifecycleState.EXITED,
    },
    PositionLifecycleState.RUNNER_ACTIVE: {
        PositionLifecycleEvent.EXIT_REQUESTED: PositionLifecycleState.EXIT_PENDING,
        PositionLifecycleEvent.EXIT_CONFIRMED: PositionLifecycleState.EXITED,
    },
    PositionLifecycleState.EXIT_PENDING: {
        PositionLifecycleEvent.EXIT_CONFIRMED: PositionLifecycleState.EXITED,
    },
    PositionLifecycleState.EXITED: {},
}


def advance_position_lifecycle(
    current: str | PositionLifecycleState | None,
    event: str | PositionLifecycleEvent,
) -> str:
    if isinstance(current, PositionLifecycleState):
        current_state = current
    else:
        try:
            current_state = PositionLifecycleState(str(current)) if current is not None else None
        except ValueError:
            current_state = None
    next_event = event if isinstance(event, PositionLifecycleEvent) else PositionLifecycleEvent(str(event))
    next_state = _TRANSITIONS.get(current_state, {}).get(next_event)
    if next_state is None:
        if current_state is None:
            next_state = _TRANSITIONS[None][PositionLifecycleEvent.ENTRY_RECORDED]
        else:
            next_state = current_state
    return str(next_state.value)
