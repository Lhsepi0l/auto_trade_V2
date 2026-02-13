from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import EngineStateRow
from apps.trader_engine.storage.repositories import EngineStateRepo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineConflict(Exception):
    message: str


class EngineService:
    """Engine state machine for control-plane actions (no trading logic here)."""

    def __init__(self, *, engine_state_repo: EngineStateRepo) -> None:
        self._engine_state_repo = engine_state_repo
        self._recovery_lock_active = True
        self._ws_safe_mode = False
        # Bootstraps STOPPED row if missing (repo persists it).
        _ = self._engine_state_repo.get()

    def set_recovery_lock(self, active: bool) -> None:
        self._recovery_lock_active = bool(active)

    def is_recovery_lock_active(self) -> bool:
        return bool(self._recovery_lock_active)

    def set_ws_safe_mode(self, active: bool) -> None:
        self._ws_safe_mode = bool(active)

    def is_ws_safe_mode(self) -> bool:
        return bool(self._ws_safe_mode)

    def get_state(self) -> EngineStateRow:
        return self._engine_state_repo.get()

    def set_state(self, state: EngineState) -> EngineStateRow:
        cur = self._engine_state_repo.get()
        row = EngineStateRow(
            state=state,
            updated_at=datetime.now(tz=timezone.utc),
            ws_connected=cur.ws_connected,
            last_ws_event_time=cur.last_ws_event_time,
        )
        self._engine_state_repo.upsert(row)
        logger.info("engine_state_set", extra={"state": state.value})
        return row

    def set_ws_status(self, *, connected: bool, last_event_time: datetime | None = None) -> EngineStateRow:
        cur = self._engine_state_repo.get()
        row = EngineStateRow(
            state=cur.state,
            updated_at=datetime.now(tz=timezone.utc),
            ws_connected=bool(connected),
            last_ws_event_time=last_event_time if last_event_time is not None else cur.last_ws_event_time,
        )
        self._engine_state_repo.upsert(row)
        return row

    def start(self) -> EngineStateRow:
        cur = self.get_state()
        if cur.state == EngineState.PANIC:
            raise EngineConflict("engine_in_panic")
        if cur.state == EngineState.RUNNING:
            # Treat start as idempotent for safer repeated control commands.
            return cur
        if cur.state != EngineState.STOPPED:
            raise EngineConflict(f"cannot_start_from_{cur.state.value}")
        return self.set_state(EngineState.RUNNING)

    def stop(self) -> EngineStateRow:
        cur = self.get_state()
        # Stop is treated as a safe, idempotent action:
        # - RUNNING/COOLDOWN/PANIC can always be forced to STOPPED
        # - STOPPED stays STOPPED
        if cur.state == EngineState.STOPPED:
            return cur
        if cur.state in (EngineState.RUNNING, EngineState.COOLDOWN, EngineState.PANIC):
            return self.set_state(EngineState.STOPPED)
        raise EngineConflict(f"cannot_stop_from_{cur.state.value}")

    def panic(self) -> EngineStateRow:
        return self.set_state(EngineState.PANIC)
