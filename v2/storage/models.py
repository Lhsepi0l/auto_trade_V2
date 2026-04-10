from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class RuntimeStorage:
    sqlite_path: str

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS journal_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    reason TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """.strip()
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    client_id TEXT PRIMARY KEY,
                    exchange_id TEXT,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL,
                    price REAL,
                    event_time_ms INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """.strip()
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    client_id TEXT,
                    exchange_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    realized_pnl REAL,
                    fill_time_ms INTEGER,
                    created_at TEXT NOT NULL
                )
                """.strip()
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    position_amt REAL NOT NULL,
                    entry_price REAL,
                    unrealized_pnl REAL,
                    snapshot_time_ms INTEGER,
                    source_event_id TEXT,
                    created_at TEXT NOT NULL
                )
                """.strip()
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bracket_states (
                    symbol TEXT PRIMARY KEY,
                    tp_order_client_id TEXT,
                    sl_order_client_id TEXT,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """.strip()
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ops_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    paused INTEGER NOT NULL,
                    safe_mode INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """.strip()
            )
            conn.execute(
                """
                INSERT INTO ops_state(id, paused, safe_mode, updated_at)
                VALUES (1, 0, 0, ?)
                ON CONFLICT(id) DO NOTHING
                """.strip(),
                (_utcnow_iso(),),
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_risk_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    config_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """.strip()
            )
            conn.execute(
                """
                INSERT INTO runtime_risk_config(id, config_json, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """.strip(),
                ("{}", _utcnow_iso()),
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS submission_intents (
                    intent_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    order_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """.strip()
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_markers (
                    marker_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """.strip()
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operator_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    main_text TEXT NOT NULL,
                    sub_text TEXT,
                    event_time TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """.strip()
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS webpush_subscriptions (
                    endpoint TEXT PRIMARY KEY,
                    subscription_json TEXT NOT NULL,
                    device_id TEXT,
                    device_label TEXT,
                    user_agent TEXT,
                    platform TEXT,
                    standalone INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_success_at TEXT,
                    last_failure_at TEXT,
                    last_error TEXT
                )
                """.strip()
            )

    def append_journal_event(
        self,
        *,
        event_id: str,
        event_type: str,
        reason: str | None,
        payload_json: str,
        created_at: str | None = None,
    ) -> bool:
        stamp = created_at if created_at is not None else _utcnow_iso()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO journal_events(event_id, event_type, reason, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """.strip(),
                    (event_id, event_type, reason, payload_json, stamp),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def append_journal(self, *, event_type: str, payload_json: str) -> bool:
        return self.append_journal_event(
            event_id=f"legacy-{uuid.uuid4().hex}",
            event_type=event_type,
            reason=None,
            payload_json=payload_json,
        )

    def list_journal_events(
        self, *, limit: int | None = None, ascending: bool = True
    ) -> list[dict[str, Any]]:
        order = "ASC" if ascending else "DESC"
        sql = f"SELECT id, event_id, event_type, reason, payload_json, created_at FROM journal_events ORDER BY id {order}"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def upsert_order(
        self,
        *,
        client_id: str,
        exchange_id: str | None,
        symbol: str,
        status: str,
        order_type: str,
        side: str,
        qty: float | None,
        price: float | None,
        event_time_ms: int | None,
    ) -> None:
        now = _utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO orders(
                    client_id, exchange_id, symbol, status, order_type, side,
                    qty, price, event_time_ms, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    exchange_id=excluded.exchange_id,
                    symbol=excluded.symbol,
                    status=excluded.status,
                    order_type=excluded.order_type,
                    side=excluded.side,
                    qty=excluded.qty,
                    price=excluded.price,
                    event_time_ms=excluded.event_time_ms,
                    updated_at=excluded.updated_at
                """.strip(),
                (
                    client_id,
                    exchange_id,
                    symbol,
                    status,
                    order_type,
                    side,
                    qty,
                    price,
                    event_time_ms,
                    now,
                    now,
                ),
            )

    def mark_order_status(
        self, *, client_id: str, status: str, event_time_ms: int | None = None
    ) -> None:
        now = _utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE orders
                SET status=?, event_time_ms=COALESCE(?, event_time_ms), updated_at=?
                WHERE client_id=?
                """.strip(),
                (status, event_time_ms, now, client_id),
            )

    def list_orders(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM orders ORDER BY created_at ASC").fetchall()
        return [dict(row) for row in rows]

    def list_open_orders(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM orders
                WHERE status IN ('NEW','PARTIALLY_FILLED')
                ORDER BY updated_at ASC
                """.strip()
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_fill(
        self,
        *,
        fill_id: str,
        client_id: str | None,
        exchange_id: str | None,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        realized_pnl: float | None,
        fill_time_ms: int | None,
    ) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO fills(
                        fill_id, client_id, exchange_id, symbol, side,
                        qty, price, realized_pnl, fill_time_ms, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """.strip(),
                    (
                        fill_id,
                        client_id,
                        exchange_id,
                        symbol,
                        side,
                        qty,
                        price,
                        realized_pnl,
                        fill_time_ms,
                        _utcnow_iso(),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def recent_fills(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fills ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def append_positions_snapshot(
        self,
        *,
        positions: list[dict[str, Any]],
        snapshot_time_ms: int | None,
        source_event_id: str | None,
    ) -> None:
        now = _utcnow_iso()
        with self._connect() as conn:
            for row in positions:
                conn.execute(
                    """
                    INSERT INTO positions_snapshots(
                        symbol, position_amt, entry_price, unrealized_pnl,
                        snapshot_time_ms, source_event_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """.strip(),
                    (
                        str(row.get("symbol") or ""),
                        _to_float(row.get("position_amt"), default=0.0),
                        _to_float(row.get("entry_price"))
                        if row.get("entry_price") is not None
                        else None,
                        _to_float(row.get("unrealized_pnl"))
                        if row.get("unrealized_pnl") is not None
                        else None,
                        snapshot_time_ms,
                        source_event_id,
                        now,
                    ),
                )

    def latest_positions(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.*
                FROM positions_snapshots p
                INNER JOIN (
                    SELECT symbol, MAX(id) AS max_id
                    FROM positions_snapshots
                    GROUP BY symbol
                ) latest
                ON latest.max_id = p.id
                ORDER BY p.symbol ASC
                """.strip()
            ).fetchall()
        return [dict(row) for row in rows]

    def set_bracket_state(
        self,
        *,
        symbol: str,
        tp_order_client_id: str | None,
        sl_order_client_id: str | None,
        state: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bracket_states(symbol, tp_order_client_id, sl_order_client_id, state, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    tp_order_client_id=excluded.tp_order_client_id,
                    sl_order_client_id=excluded.sl_order_client_id,
                    state=excluded.state,
                    updated_at=excluded.updated_at
                """.strip(),
                (symbol, tp_order_client_id, sl_order_client_id, state, _utcnow_iso()),
            )

    def list_bracket_states(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM bracket_states ORDER BY symbol ASC").fetchall()
        return [dict(row) for row in rows]

    def set_ops_state(self, *, paused: bool | None = None, safe_mode: bool | None = None) -> None:
        current = self.get_ops_state()
        paused_v = current["paused"] if paused is None else bool(paused)
        safe_v = current["safe_mode"] if safe_mode is None else bool(safe_mode)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE ops_state
                SET paused=?, safe_mode=?, updated_at=?
                WHERE id=1
                """.strip(),
                (int(paused_v), int(safe_v), _utcnow_iso()),
            )

    def get_ops_state(self) -> dict[str, bool]:
        with self._connect() as conn:
            row = conn.execute("SELECT paused, safe_mode FROM ops_state WHERE id=1").fetchone()
        if row is None:
            return {"paused": False, "safe_mode": False}
        return {"paused": bool(row["paused"]), "safe_mode": bool(row["safe_mode"])}

    def save_runtime_risk_config(self, *, config: dict[str, Any]) -> None:
        payload = json.dumps(config, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_risk_config(id, config_json, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    config_json=excluded.config_json,
                    updated_at=excluded.updated_at
                """.strip(),
                (payload, _utcnow_iso()),
            )

    def load_runtime_risk_config(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT config_json FROM runtime_risk_config WHERE id=1").fetchone()
        if row is None:
            return {}
        raw = row["config_json"]
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def get_submission_intent(self, *, intent_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT intent_id, client_order_id, symbol, side, status, order_id, created_at, updated_at
                FROM submission_intents
                WHERE intent_id=?
                """.strip(),
                (intent_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_submission_intent(
        self,
        *,
        intent_id: str,
        client_order_id: str,
        symbol: str,
        side: str,
        status: str,
        order_id: str | None = None,
    ) -> None:
        now = _utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO submission_intents(
                    intent_id, client_order_id, symbol, side, status, order_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(intent_id) DO UPDATE SET
                    client_order_id=excluded.client_order_id,
                    symbol=excluded.symbol,
                    side=excluded.side,
                    status=excluded.status,
                    order_id=excluded.order_id,
                    updated_at=excluded.updated_at
                """.strip(),
                (
                    intent_id,
                    client_order_id,
                    symbol,
                    side,
                    status,
                    order_id,
                    now,
                    now,
                ),
            )

    def get_submission_intent_by_client_order_id(self, *, client_order_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT intent_id, client_order_id, symbol, side, status, order_id, created_at, updated_at
                FROM submission_intents
                WHERE client_order_id=?
                """.strip(),
                (client_order_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_submission_intents(self, *, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT intent_id, client_order_id, symbol, side, status, order_id, created_at, updated_at "
            "FROM submission_intents"
        )
        params: tuple[Any, ...] = ()
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql = f"{sql} WHERE status IN ({placeholders})"
            params = tuple(str(item) for item in statuses)
        sql = f"{sql} ORDER BY updated_at ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def mark_submission_intent_status(
        self,
        *,
        intent_id: str,
        status: str,
        order_id: str | None = None,
    ) -> None:
        now = _utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE submission_intents
                SET status=?, order_id=COALESCE(?, order_id), updated_at=?
                WHERE intent_id=?
                """.strip(),
                (status, order_id, now, intent_id),
            )

    def save_runtime_marker(self, *, marker_key: str, payload: dict[str, Any]) -> None:
        payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_markers(marker_key, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(marker_key) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """.strip(),
                (marker_key, payload_json, _utcnow_iso()),
            )

    def load_runtime_marker(self, *, marker_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM runtime_markers WHERE marker_key=?",
                (marker_key,),
            ).fetchone()
        if row is None:
            return None
        raw = row["payload_json"]
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def append_operator_event(
        self,
        *,
        event_type: str,
        category: str,
        title: str,
        main_text: str,
        sub_text: str | None,
        event_time: str,
        context: dict[str, Any],
    ) -> None:
        context_json = json.dumps(context, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operator_events(
                    event_type, category, title, main_text, sub_text, event_time, context_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """.strip(),
                (
                    event_type,
                    category,
                    title,
                    main_text,
                    sub_text,
                    event_time,
                    context_json,
                    _utcnow_iso(),
                ),
            )

    def list_operator_events(
        self,
        *,
        limit: int = 200,
        category: str | None = None,
        query: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        normalized_category = str(category or "").strip().lower()
        if normalized_category:
            clauses.append("category = ?")
            params.append(normalized_category)
        normalized_query = str(query or "").strip()
        if normalized_query:
            like = f"%{normalized_query}%"
            clauses.append(
                "(event_type LIKE ? OR title LIKE ? OR main_text LIKE ? OR COALESCE(sub_text, '') LIKE ?)"
            )
            params.extend([like, like, like, like])
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, event_type, category, title, main_text, sub_text, event_time, context_json, created_at
                FROM operator_events
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """.strip(),
                (*params, max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            raw_context = payload.get("context_json")
            if isinstance(raw_context, str) and raw_context.strip():
                try:
                    payload["context"] = json.loads(raw_context)
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload["context"] = {}
            else:
                payload["context"] = {}
            out.append(payload)
        return out

    def count_operator_events(
        self,
        *,
        category: str | None = None,
        query: str | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        normalized_category = str(category or "").strip().lower()
        if normalized_category:
            clauses.append("category = ?")
            params.append(normalized_category)
        normalized_query = str(query or "").strip()
        if normalized_query:
            like = f"%{normalized_query}%"
            clauses.append(
                "(event_type LIKE ? OR title LIKE ? OR main_text LIKE ? OR COALESCE(sub_text, '') LIKE ?)"
            )
            params.extend([like, like, like, like])
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM operator_events
                {where_sql}
                """.strip(),
                tuple(params),
            ).fetchone()
        if row is None:
            return 0
        try:
            return int(row["total"])
        except Exception:  # noqa: BLE001
            return 0

    def upsert_webpush_subscription(
        self,
        *,
        endpoint: str,
        subscription_json: str,
        device_id: str | None,
        device_label: str | None,
        user_agent: str | None,
        platform: str | None,
        standalone: bool,
    ) -> None:
        now = _utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO webpush_subscriptions(
                    endpoint,
                    subscription_json,
                    device_id,
                    device_label,
                    user_agent,
                    platform,
                    standalone,
                    active,
                    created_at,
                    updated_at,
                    last_success_at,
                    last_failure_at,
                    last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, NULL, NULL)
                ON CONFLICT(endpoint) DO UPDATE SET
                    subscription_json=excluded.subscription_json,
                    device_id=excluded.device_id,
                    device_label=excluded.device_label,
                    user_agent=excluded.user_agent,
                    platform=excluded.platform,
                    standalone=excluded.standalone,
                    active=1,
                    updated_at=excluded.updated_at,
                    last_error=NULL
                """.strip(),
                (
                    endpoint,
                    subscription_json,
                    device_id,
                    device_label,
                    user_agent,
                    platform,
                    int(bool(standalone)),
                    now,
                    now,
                ),
            )

    def list_webpush_subscriptions(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        sql = """
            SELECT
                endpoint,
                subscription_json,
                device_id,
                device_label,
                user_agent,
                platform,
                standalone,
                active,
                created_at,
                updated_at,
                last_success_at,
                last_failure_at,
                last_error
            FROM webpush_subscriptions
        """.strip()
        params: tuple[Any, ...] = ()
        if active_only:
            sql = f"{sql} WHERE active = 1"
        sql = f"{sql} ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_webpush_subscriptions(self, *, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) AS total FROM webpush_subscriptions"
        if active_only:
            sql = f"{sql} WHERE active = 1"
        with self._connect() as conn:
            row = conn.execute(sql).fetchone()
        if row is None:
            return 0
        return int(row["total"])

    def deactivate_webpush_subscription(self, *, endpoint: str, reason: str | None = None) -> bool:
        now = _utcnow_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE webpush_subscriptions
                SET active = 0,
                    updated_at = ?,
                    last_failure_at = ?,
                    last_error = COALESCE(?, last_error)
                WHERE endpoint = ?
                """.strip(),
                (now, now, reason, endpoint),
            )
        return int(cur.rowcount or 0) > 0

    def record_webpush_subscription_success(self, *, endpoint: str) -> None:
        now = _utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE webpush_subscriptions
                SET active = 1,
                    updated_at = ?,
                    last_success_at = ?,
                    last_error = NULL
                WHERE endpoint = ?
                """.strip(),
                (now, now, endpoint),
            )

    def record_webpush_subscription_failure(self, *, endpoint: str, error: str) -> None:
        now = _utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE webpush_subscriptions
                SET updated_at = ?,
                    last_failure_at = ?,
                    last_error = ?
                WHERE endpoint = ?
                """.strip(),
                (now, now, error, endpoint),
            )
