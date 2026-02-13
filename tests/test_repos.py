from __future__ import annotations

from datetime import datetime, timezone

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import EngineStateRow, PnLState, RiskConfig
from apps.trader_engine.storage.db import connect, migrate
from apps.trader_engine.storage.repositories import EngineStateRepo, PnLStateRepo, RiskConfigRepo


def test_risk_config_upsert_and_get(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    db = connect(str(db_path))
    migrate(db)

    repo = RiskConfigRepo(db)
    assert repo.get() is None

    cfg = RiskConfig(
        per_trade_risk_pct=1.0,
        max_exposure_pct=0.2,
        max_notional_pct=50.0,
        max_leverage=3,
        daily_loss_limit_pct=-0.05,
        dd_limit_pct=-0.10,
        lose_streak_n=3,
        cooldown_hours=2.0,
        notify_interval_sec=60,
    )
    repo.upsert(cfg)

    got = repo.get()
    assert got is not None
    assert got.per_trade_risk_pct == 1.0
    assert got.notify_interval_sec == 60


def test_engine_state_upsert_and_get(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    db = connect(str(db_path))
    migrate(db)

    repo = EngineStateRepo(db)
    initial = repo.get()
    assert initial.state == EngineState.STOPPED

    row = EngineStateRow(state=EngineState.RUNNING, updated_at=datetime.now(tz=timezone.utc))
    repo.upsert(row)

    got = repo.get()
    assert got.state == EngineState.RUNNING


def test_pnl_state_upsert_and_get(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    db = connect(str(db_path))
    migrate(db)

    repo = PnLStateRepo(db)
    assert repo.get() is None

    row = PnLState(
        day="2026-02-09",
        daily_realized_pnl=-12.5,
        equity_peak=1000.0,
        lose_streak=2,
        cooldown_until=None,
        last_block_reason="cooldown_active",
        updated_at=datetime.now(tz=timezone.utc),
    )
    repo.upsert(row)
    got = repo.get()
    assert got is not None
    assert got.day == "2026-02-09"
    assert got.lose_streak == 2
    assert got.last_block_reason == "cooldown_active"


def test_risk_config_universe_symbols_alias_normalized(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    db = connect(str(db_path))
    migrate(db)

    repo = RiskConfigRepo(db)
    cfg = RiskConfig(
        per_trade_risk_pct=1.0,
        max_exposure_pct=0.2,
        max_notional_pct=50.0,
        max_leverage=3,
        daily_loss_limit_pct=-0.05,
        dd_limit_pct=-0.10,
        lose_streak_n=3,
        cooldown_hours=2.0,
        notify_interval_sec=60,
        universe_symbols=["BTCUSDT", "ETHUSDT", "XAUTUSDT"],
    )
    repo.upsert(cfg)

    got = repo.get()
    assert got is not None
    assert got.universe_symbols == ["BTCUSDT", "ETHUSDT", "XAUUSDT"]
