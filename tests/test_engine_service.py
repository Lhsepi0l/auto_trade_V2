from __future__ import annotations

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.storage.db import connect, migrate
from apps.trader_engine.storage.repositories import EngineStateRepo


def test_stop_is_idempotent_from_stopped(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    db = connect(str(db_path))
    migrate(db)

    repo = EngineStateRepo(db)
    svc = EngineService(engine_state_repo=repo)

    # Bootstrap state is STOPPED.
    got = svc.stop()
    assert got.state == EngineState.STOPPED


def test_stop_clears_panic(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    db = connect(str(db_path))
    migrate(db)

    repo = EngineStateRepo(db)
    svc = EngineService(engine_state_repo=repo)

    svc.panic()
    got = svc.stop()
    assert got.state == EngineState.STOPPED

