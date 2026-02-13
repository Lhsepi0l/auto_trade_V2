from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.storage.repositories import EngineStateRepo, RiskConfigRepo, StatusSnapshotRepo

import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Status:
    state: EngineState
    state_updated_at: datetime
    risk_config_present: bool
    ts: datetime


class StatusService:
    def __init__(
        self,
        *,
        engine_state_repo: EngineStateRepo,
        risk_config_repo: RiskConfigRepo,
        status_snapshot_repo: Optional[StatusSnapshotRepo] = None,
    ) -> None:
        self._engine_state_repo = engine_state_repo
        self._risk_config_repo = risk_config_repo
        self._status_snapshot_repo = status_snapshot_repo

    def get_status(self) -> Dict[str, Any]:
        state = self._engine_state_repo.get()
        rc = self._risk_config_repo.get()
        s = Status(
            state=state.state,
            state_updated_at=state.updated_at,
            risk_config_present=rc is not None,
            ts=datetime.now(tz=timezone.utc),
        )
        payload = asdict(s)
        if self._status_snapshot_repo:
            try:
                self._status_snapshot_repo.upsert_json(payload)
            except Exception:
                logger.exception("failed_to_persist_status_snapshot")
        return payload
