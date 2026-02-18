from __future__ import annotations

import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


SCHEMA_MIGRATIONS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS risk_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        per_trade_risk_pct REAL NOT NULL,
        max_exposure_pct REAL NOT NULL,
        max_notional_pct REAL NOT NULL,
        max_leverage REAL NOT NULL,
        symbol_leverage_map TEXT,
        -- Legacy percent-unit fields kept for backward compatibility (e.g. -2 for -2%)
        daily_loss_limit REAL NOT NULL,
        dd_limit REAL NOT NULL,
        -- Preferred ratio-unit fields (e.g. -0.02 for -2%)
        daily_loss_limit_pct REAL,
        dd_limit_pct REAL,
        lose_streak_n INTEGER NOT NULL,
        cooldown_hours REAL NOT NULL,
        notify_interval_sec INTEGER NOT NULL,
        min_hold_minutes INTEGER,
        score_conf_threshold REAL,
        score_gap_threshold REAL,
        exec_mode_default TEXT,
        exec_limit_timeout_sec REAL,
        exec_limit_retries INTEGER,
        spread_max_pct REAL,
        allow_market_when_wide_spread INTEGER,
        universe_symbols TEXT,
        enable_watchdog INTEGER,
        watchdog_interval_sec INTEGER,
        shock_1m_pct REAL,
        shock_from_entry_pct REAL,
        trailing_enabled INTEGER,
        trailing_mode TEXT,
        trail_arm_pnl_pct REAL,
        trail_distance_pnl_pct REAL,
        trail_grace_minutes INTEGER,
        atr_trail_timeframe TEXT,
        atr_trail_k REAL,
        atr_trail_min_pct REAL,
        atr_trail_max_pct REAL,
        tf_weight_4h REAL,
        tf_weight_1h REAL,
        tf_weight_30m REAL,
        tf_weight_10m REAL,
        tf_weight_15m REAL,
        score_tf_15m_enabled INTEGER,
        vol_shock_atr_mult_threshold REAL,
        atr_mult_mean_window INTEGER,
        updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS engine_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        state TEXT NOT NULL,
        ws_connected INTEGER,
        last_ws_event_time TEXT,
        updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS status_snapshot (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS pnl_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        day TEXT NOT NULL,
        daily_realized_pnl REAL NOT NULL,
        equity_peak REAL NOT NULL,
        lose_streak INTEGER NOT NULL,
        cooldown_until TEXT,
        last_entry_symbol TEXT,
        last_entry_at TEXT,
        last_fill_symbol TEXT,
        last_fill_side TEXT,
        last_fill_qty REAL,
        last_fill_price REAL,
        last_fill_realized_pnl REAL,
        last_fill_time TEXT,
        last_block_reason TEXT,
        updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS risk_config_new (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        per_trade_risk_pct REAL NOT NULL,
        max_exposure_pct REAL,
        max_notional_pct REAL NOT NULL,
        max_leverage REAL NOT NULL,
        symbol_leverage_map TEXT,
        -- Legacy percent-unit fields kept for backward compatibility (e.g. -2 for -2%)
        daily_loss_limit REAL NOT NULL,
        dd_limit REAL NOT NULL,
        -- Preferred ratio-unit fields (e.g. -0.02 for -2%)
        daily_loss_limit_pct REAL,
        dd_limit_pct REAL,
        lose_streak_n INTEGER NOT NULL,
        cooldown_hours REAL NOT NULL,
        notify_interval_sec INTEGER NOT NULL,
        min_hold_minutes INTEGER,
        score_conf_threshold REAL,
        score_gap_threshold REAL,
        exec_mode_default TEXT,
        exec_limit_timeout_sec REAL,
        exec_limit_retries INTEGER,
        spread_max_pct REAL,
        allow_market_when_wide_spread INTEGER,
        capital_mode TEXT,
        capital_pct REAL,
        capital_usdt REAL,
        margin_budget_usdt REAL,
        margin_use_pct REAL,
        max_position_notional_usdt REAL,
        fee_buffer_pct REAL,
        universe_symbols TEXT,
        enable_watchdog INTEGER,
        watchdog_interval_sec INTEGER,
        shock_1m_pct REAL,
        shock_from_entry_pct REAL,
        trailing_enabled INTEGER,
        trailing_mode TEXT,
        trail_arm_pnl_pct REAL,
        trail_distance_pnl_pct REAL,
        trail_grace_minutes INTEGER,
        atr_trail_timeframe TEXT,
        atr_trail_k REAL,
        atr_trail_min_pct REAL,
        atr_trail_max_pct REAL,
        tf_weight_4h REAL,
        tf_weight_1h REAL,
        tf_weight_30m REAL,
        tf_weight_10m REAL,
        tf_weight_15m REAL,
        score_tf_15m_enabled INTEGER,
        vol_shock_atr_mult_threshold REAL,
        atr_mult_mean_window INTEGER,
        updated_at TEXT NOT NULL
    );

    INSERT INTO risk_config_new(
        id,
        per_trade_risk_pct,
        max_exposure_pct,
        max_notional_pct,
        max_leverage,
        symbol_leverage_map,
        daily_loss_limit,
        dd_limit,
        daily_loss_limit_pct,
        dd_limit_pct,
        lose_streak_n,
        cooldown_hours,
        notify_interval_sec,
        min_hold_minutes,
        score_conf_threshold,
        score_gap_threshold,
        exec_mode_default,
        exec_limit_timeout_sec,
        exec_limit_retries,
        spread_max_pct,
        allow_market_when_wide_spread,
        capital_mode,
        capital_pct,
        capital_usdt,
        margin_budget_usdt,
        margin_use_pct,
        max_position_notional_usdt,
        fee_buffer_pct,
        universe_symbols,
        enable_watchdog,
        watchdog_interval_sec,
        shock_1m_pct,
        shock_from_entry_pct,
        trailing_enabled,
        trailing_mode,
        trail_arm_pnl_pct,
        trail_distance_pnl_pct,
        trail_grace_minutes,
        atr_trail_timeframe,
        atr_trail_k,
        atr_trail_min_pct,
        atr_trail_max_pct,
        tf_weight_4h,
        tf_weight_1h,
        tf_weight_30m,
        tf_weight_10m,
        tf_weight_15m,
        score_tf_15m_enabled,
        vol_shock_atr_mult_threshold,
        atr_mult_mean_window,
        updated_at
    )
        SELECT
        id,
        per_trade_risk_pct,
        CASE
            WHEN max_exposure_pct IS NULL THEN NULL
            WHEN max_exposure_pct > 1.0 THEN (max_exposure_pct / 100.0)
            ELSE max_exposure_pct
        END AS max_exposure_pct,
        max_notional_pct,
        max_leverage,
        NULL AS symbol_leverage_map,
        daily_loss_limit,
        dd_limit,
        daily_loss_limit_pct,
        dd_limit_pct,
        lose_streak_n,
        cooldown_hours,
        notify_interval_sec,
        min_hold_minutes,
        score_conf_threshold,
        score_gap_threshold,
        exec_mode_default,
        exec_limit_timeout_sec,
        exec_limit_retries,
        spread_max_pct,
        allow_market_when_wide_spread,
        'PCT_AVAILABLE' AS capital_mode,
        0.20 AS capital_pct,
        100.0 AS capital_usdt,
        100.0 AS margin_budget_usdt,
        0.90 AS margin_use_pct,
        NULL AS max_position_notional_usdt,
        0.002 AS fee_buffer_pct,
        universe_symbols,
        enable_watchdog,
        watchdog_interval_sec,
        shock_1m_pct,
        shock_from_entry_pct,
        1 AS trailing_enabled,
        'PCT' AS trailing_mode,
        1.2 AS trail_arm_pnl_pct,
        0.8 AS trail_distance_pnl_pct,
        30 AS trail_grace_minutes,
        '1h' AS atr_trail_timeframe,
        2.0 AS atr_trail_k,
        0.6 AS atr_trail_min_pct,
        1.8 AS atr_trail_max_pct,
        tf_weight_4h,
        tf_weight_1h,
        tf_weight_30m,
        COALESCE(tf_weight_10m, 0.25) AS tf_weight_10m,
        COALESCE(tf_weight_15m, 0.0) AS tf_weight_15m,
        COALESCE(score_tf_15m_enabled, 0) AS score_tf_15m_enabled,
        vol_shock_atr_mult_threshold,
        atr_mult_mean_window,
        updated_at
    FROM risk_config
    WHERE EXISTS (SELECT 1 FROM risk_config);

    DROP TABLE risk_config;
    ALTER TABLE risk_config_new RENAME TO risk_config;
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS op_events (
        ts TEXT NOT NULL,
        event_type TEXT NOT NULL,
        json TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS decisions (
        ts TEXT NOT NULL,
        cycle_id TEXT,
        symbol TEXT,
        direction TEXT,
        confidence REAL,
        regime_4h TEXT,
        scores_json TEXT,
        reason TEXT
    );

    CREATE TABLE IF NOT EXISTS executions (
        ts TEXT NOT NULL,
        intent_id TEXT,
        symbol TEXT,
        side TEXT,
        qty REAL,
        price REAL,
        order_type TEXT,
        client_order_id TEXT,
        status TEXT,
        reason TEXT
    );

    CREATE TABLE IF NOT EXISTS risk_blocks (
        ts TEXT NOT NULL,
        intent_id TEXT,
        symbol TEXT,
        block_reason TEXT NOT NULL,
        details_json TEXT
    );
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS pnl_snapshots (
        ts TEXT NOT NULL,
        symbol TEXT NOT NULL,
        position_side TEXT NOT NULL,
        qty REAL NOT NULL,
        entry_price REAL NOT NULL,
        mark_price REAL NOT NULL,
        unrealized_pnl_usdt REAL NOT NULL,
        unrealized_pnl_pct REAL NOT NULL,
        realized_pnl_usdt REAL NOT NULL,
        equity_usdt REAL NOT NULL,
        available_usdt REAL NOT NULL
    );
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS order_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_created TEXT NOT NULL,
        ts_updated TEXT NOT NULL,
        intent_id TEXT,
        cycle_id TEXT,
        run_id TEXT,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        order_type TEXT NOT NULL,
        reduce_only INTEGER NOT NULL,
        qty REAL NOT NULL,
        price REAL,
        time_in_force TEXT,
        client_order_id TEXT NOT NULL UNIQUE,
        exchange_order_id TEXT,
        status TEXT NOT NULL,
        last_error TEXT
    );
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS trailing_state (
        symbol TEXT PRIMARY KEY,
        position_side TEXT NOT NULL,
        entry_price REAL NOT NULL,
        entry_ts REAL NOT NULL,
        peak_pnl_pct REAL NOT NULL,
        armed INTEGER NOT NULL,
        close_state TEXT NOT NULL,
        last_close_attempt_ts REAL,
        attempt_count INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    );
    """.strip(),
]

db_lock = threading.RLock()


@dataclass
class Database:
    conn: sqlite3.Connection
    lock: threading.RLock

    def execute(self, sql: str, params: Iterable[object] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def executescript(self, sql: str) -> None:
        with self.lock:
            self.conn.executescript(sql)
            self.conn.commit()

    def query_one(self, sql: str, params: Iterable[object] = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def query_all(self, sql: str, params: Iterable[object] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()


def connect(db_path: str) -> Database:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        check_same_thread=False,
        isolation_level=None,  # autocommit mode; we still guard with a lock
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return Database(conn=conn, lock=db_lock)


def get_schema_version(db: Database) -> int:
    try:
        row = db.query_one("SELECT MAX(version) AS v FROM schema_migrations")
        if not row or row["v"] is None:
            return 0
        return int(row["v"])
    except sqlite3.OperationalError:
        return 0


def migrate(db: Database) -> None:
    # Simple linear migrations: apply SCHEMA_MIGRATIONS entries in order.
    # Each entry index is its version number.
    db.executescript(SCHEMA_MIGRATIONS[0])
    current = get_schema_version(db)

    for version in range(current + 1, len(SCHEMA_MIGRATIONS)):
        db.executescript(SCHEMA_MIGRATIONS[version])
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
            (version,),
        )

    # Best-effort forward-compatible schema tweaks for existing DBs.
    _ensure_columns(db)
    _backfill_derived_columns(db)
    _ensure_operational_tables(db)
    _ensure_snapshot_table(db)
    _ensure_order_records_table(db)
    _ensure_trailing_state_table(db)


def _table_columns(db: Database, table: str) -> set[str]:
    try:
        rows = db.query_all(f"PRAGMA table_info({table})")
        return {str(r["name"]) for r in rows if r and r["name"]}
    except Exception:
        return set()


def _ensure_columns(db: Database) -> None:
    # SQLite lacks "ADD COLUMN IF NOT EXISTS", so we check first.
    risk_cols = _table_columns(db, "risk_config")
    if risk_cols:
        adds: list[tuple[str, str]] = [
            ("daily_loss_limit_pct", "REAL"),
            ("dd_limit_pct", "REAL"),
            ("min_hold_minutes", "INTEGER"),
            ("score_conf_threshold", "REAL"),
            ("score_gap_threshold", "REAL"),
            ("exec_mode_default", "TEXT"),
            ("exec_limit_timeout_sec", "REAL"),
            ("exec_limit_retries", "INTEGER"),
            ("spread_max_pct", "REAL"),
            ("allow_market_when_wide_spread", "INTEGER"),
            ("capital_mode", "TEXT"),
            ("capital_pct", "REAL"),
            ("capital_usdt", "REAL"),
            ("margin_budget_usdt", "REAL"),
            ("margin_use_pct", "REAL"),
            ("max_position_notional_usdt", "REAL"),
            ("fee_buffer_pct", "REAL"),
            ("universe_symbols", "TEXT"),
            ("enable_watchdog", "INTEGER"),
            ("watchdog_interval_sec", "INTEGER"),
            ("shock_1m_pct", "REAL"),
            ("shock_from_entry_pct", "REAL"),
            ("trailing_enabled", "INTEGER"),
            ("trailing_mode", "TEXT"),
            ("trail_arm_pnl_pct", "REAL"),
            ("trail_distance_pnl_pct", "REAL"),
            ("trail_grace_minutes", "INTEGER"),
            ("atr_trail_timeframe", "TEXT"),
            ("atr_trail_k", "REAL"),
            ("atr_trail_min_pct", "REAL"),
            ("atr_trail_max_pct", "REAL"),
            ("tf_weight_4h", "REAL"),
            ("tf_weight_1h", "REAL"),
            ("tf_weight_30m", "REAL"),
            ("tf_weight_10m", "REAL"),
            ("tf_weight_15m", "REAL"),
            ("score_tf_15m_enabled", "INTEGER"),
            ("vol_shock_atr_mult_threshold", "REAL"),
            ("atr_mult_mean_window", "INTEGER"),
            ("symbol_leverage_map", "TEXT"),
        ]
        for name, typ in adds:
            if name in risk_cols:
                continue
            try:
                db.execute(f"ALTER TABLE risk_config ADD COLUMN {name} {typ}")
            except Exception as e:  # noqa: BLE001
                logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)

    pnl_cols = _table_columns(db, "pnl_state")
    if pnl_cols:
        adds2: list[tuple[str, str]] = [
            ("last_entry_symbol", "TEXT"),
            ("last_entry_at", "TEXT"),
            ("last_fill_symbol", "TEXT"),
            ("last_fill_side", "TEXT"),
            ("last_fill_qty", "REAL"),
            ("last_fill_price", "REAL"),
            ("last_fill_realized_pnl", "REAL"),
            ("last_fill_time", "TEXT"),
        ]
        for name, typ in adds2:
            if name in pnl_cols:
                continue
            try:
                db.execute(f"ALTER TABLE pnl_state ADD COLUMN {name} {typ}")
            except Exception as e:  # noqa: BLE001
                logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)

    engine_cols = _table_columns(db, "engine_state")
    if engine_cols:
        adds3: list[tuple[str, str]] = [
            ("ws_connected", "INTEGER"),
            ("last_ws_event_time", "TEXT"),
        ]
        for name, typ in adds3:
            if name in engine_cols:
                continue
            try:
                db.execute(f"ALTER TABLE engine_state ADD COLUMN {name} {typ}")
            except Exception as e:  # noqa: BLE001
                logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)

    order_cols = _table_columns(db, "order_records")
    if order_cols:
        adds4: list[tuple[str, str]] = [
            ("realized_pnl", "REAL"),
        ]
        for name, typ in adds4:
            if name in order_cols:
                continue
            try:
                db.execute(f"ALTER TABLE order_records ADD COLUMN {name} {typ}")
            except Exception as e:  # noqa: BLE001
                logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)


def _backfill_derived_columns(db: Database) -> None:
    # Backfill ratio-unit loss limits from legacy percent-unit columns when present.
    cols = _table_columns(db, "risk_config")
    if not cols:
        return
    if "daily_loss_limit_pct" in cols and "daily_loss_limit" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET daily_loss_limit_pct = (daily_loss_limit / 100.0)
                WHERE id=1 AND (daily_loss_limit_pct IS NULL)
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "dd_limit_pct" in cols and "dd_limit" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET dd_limit_pct = (dd_limit / 100.0)
                WHERE id=1 AND (dd_limit_pct IS NULL)
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "capital_mode" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET capital_mode = 'PCT_AVAILABLE'
                WHERE id=1 AND (capital_mode IS NULL OR capital_mode = '')
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "capital_pct" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET capital_pct = 0.20
                WHERE id=1 AND capital_pct IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "capital_usdt" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET capital_usdt = 100.0
                WHERE id=1 AND capital_usdt IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "margin_budget_usdt" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET margin_budget_usdt = 100.0
                WHERE id=1 AND margin_budget_usdt IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "margin_use_pct" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET margin_use_pct = 0.90
                WHERE id=1 AND margin_use_pct IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "fee_buffer_pct" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET fee_buffer_pct = 0.002
                WHERE id=1 AND fee_buffer_pct IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "trailing_enabled" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET trailing_enabled = 1
                WHERE id=1 AND trailing_enabled IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "trailing_mode" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET trailing_mode = 'PCT'
                WHERE id=1 AND (trailing_mode IS NULL OR trailing_mode = '')
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "trail_arm_pnl_pct" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET trail_arm_pnl_pct = 1.2
                WHERE id=1 AND trail_arm_pnl_pct IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "trail_distance_pnl_pct" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET trail_distance_pnl_pct = 0.8
                WHERE id=1 AND trail_distance_pnl_pct IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "trail_grace_minutes" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET trail_grace_minutes = 30
                WHERE id=1 AND trail_grace_minutes IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "atr_trail_timeframe" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET atr_trail_timeframe = '1h'
                WHERE id=1 AND (atr_trail_timeframe IS NULL OR atr_trail_timeframe = '')
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "atr_trail_k" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET atr_trail_k = 2.0
                WHERE id=1 AND atr_trail_k IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "atr_trail_min_pct" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET atr_trail_min_pct = 0.6
                WHERE id=1 AND atr_trail_min_pct IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "atr_trail_max_pct" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET atr_trail_max_pct = 1.8
                WHERE id=1 AND atr_trail_max_pct IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "tf_weight_10m" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET tf_weight_10m = 0.25
                WHERE id=1 AND (tf_weight_10m IS NULL OR tf_weight_10m < 0)
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "tf_weight_15m" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET tf_weight_15m = 0.0
                WHERE id=1 AND (tf_weight_15m IS NULL OR tf_weight_15m < 0)
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)
    if "score_tf_15m_enabled" in cols:
        try:
            db.execute(
                """
                UPDATE risk_config
                SET score_tf_15m_enabled = 0
                WHERE id=1 AND score_tf_15m_enabled IS NULL
                """.strip()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)


def _ensure_operational_tables(db: Database) -> None:
    try:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS op_events (
                ts TEXT NOT NULL,
                event_type TEXT NOT NULL,
                json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                ts TEXT NOT NULL,
                cycle_id TEXT,
                symbol TEXT,
                direction TEXT,
                confidence REAL,
                regime_4h TEXT,
                scores_json TEXT,
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS executions (
                ts TEXT NOT NULL,
                intent_id TEXT,
                symbol TEXT,
                side TEXT,
                qty REAL,
                price REAL,
                order_type TEXT,
                client_order_id TEXT,
                status TEXT,
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS risk_blocks (
                ts TEXT NOT NULL,
                intent_id TEXT,
                symbol TEXT,
                block_reason TEXT NOT NULL,
                details_json TEXT
            );
            """.strip()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)


def _ensure_snapshot_table(db: Database) -> None:
    try:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS pnl_snapshots (
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                position_side TEXT NOT NULL,
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                mark_price REAL NOT NULL,
                unrealized_pnl_usdt REAL NOT NULL,
                unrealized_pnl_pct REAL NOT NULL,
                realized_pnl_usdt REAL NOT NULL,
                equity_usdt REAL NOT NULL,
                available_usdt REAL NOT NULL
            );
            """.strip()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)


def _ensure_order_records_table(db: Database) -> None:
    try:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS order_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_created TEXT NOT NULL,
                ts_updated TEXT NOT NULL,
                intent_id TEXT,
                cycle_id TEXT,
                run_id TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                reduce_only INTEGER NOT NULL,
                qty REAL NOT NULL,
                price REAL,
                time_in_force TEXT,
                client_order_id TEXT NOT NULL UNIQUE,
                exchange_order_id TEXT,
                status TEXT NOT NULL,
                realized_pnl REAL,
                last_error TEXT
            );
            """.strip()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)


def _ensure_trailing_state_table(db: Database) -> None:
    try:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS trailing_state (
                symbol TEXT PRIMARY KEY,
                position_side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                entry_ts REAL NOT NULL,
                peak_pnl_pct REAL NOT NULL,
                armed INTEGER NOT NULL,
                close_state TEXT NOT NULL,
                last_close_attempt_ts REAL,
                attempt_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            """.strip()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("db_noncritical_step_failed", extra={"err": type(e).__name__}, exc_info=True)


def close(db: Database) -> None:
    with db.lock:
        db.conn.close()

