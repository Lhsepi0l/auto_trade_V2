from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import EngineStateRow, PnLState, RiskConfig
from apps.trader_engine.storage.db import Database


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    # ISO 8601 parser for the limited scope of this project.
    return datetime.fromisoformat(value)


class RiskConfigRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self) -> Optional[RiskConfig]:
        row = self._db.conn.execute("SELECT * FROM risk_config WHERE id=1").fetchone()
        if not row:
            return None
        keys = set(row.keys())
        payload: Dict[str, Any] = {}
        for k in RiskConfig.model_fields.keys():
            if k in keys:
                # If an existing DB row has newly-added columns, they'll be NULL.
                # Do not pass None into pydantic; let model defaults apply instead.
                v = row[k]
                if v is None:
                    continue
                payload[k] = v

        # Backward-compat: derive ratio-unit loss limits from legacy percent columns.
        if "daily_loss_limit_pct" not in payload:
            if "daily_loss_limit_pct" in keys and row["daily_loss_limit_pct"] is not None:
                payload["daily_loss_limit_pct"] = float(row["daily_loss_limit_pct"])
            elif "daily_loss_limit" in keys and row["daily_loss_limit"] is not None:
                payload["daily_loss_limit_pct"] = float(row["daily_loss_limit"]) / 100.0

        if "dd_limit_pct" not in payload:
            if "dd_limit_pct" in keys and row["dd_limit_pct"] is not None:
                payload["dd_limit_pct"] = float(row["dd_limit_pct"])
            elif "dd_limit" in keys and row["dd_limit"] is not None:
                payload["dd_limit_pct"] = float(row["dd_limit"]) / 100.0

        # Backward-compat for historical rows stored in 0..100 scale.
        if "max_exposure_pct" in payload and payload["max_exposure_pct"] is not None:
            _v = float(payload["max_exposure_pct"])
            if _v > 1.0:
                payload["max_exposure_pct"] = _v / 100.0

        return RiskConfig(**payload)

    def upsert(self, cfg: RiskConfig) -> None:
        # Keep legacy percent-unit columns in sync for older DBs/tools.
        daily_loss_limit_legacy = float(cfg.daily_loss_limit_pct) * 100.0
        dd_limit_legacy = float(cfg.dd_limit_pct) * 100.0
        universe_csv = ",".join([s.strip().upper() for s in (cfg.universe_symbols or []) if s.strip()])

        self._db.execute(
            """
            INSERT INTO risk_config(
                id,
                per_trade_risk_pct,
                max_exposure_pct,
                max_notional_pct,
                max_leverage,
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
                vol_shock_atr_mult_threshold,
                atr_mult_mean_window,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                per_trade_risk_pct=excluded.per_trade_risk_pct,
                max_exposure_pct=excluded.max_exposure_pct,
                max_notional_pct=excluded.max_notional_pct,
                max_leverage=excluded.max_leverage,
                daily_loss_limit=excluded.daily_loss_limit,
                dd_limit=excluded.dd_limit,
                daily_loss_limit_pct=excluded.daily_loss_limit_pct,
                dd_limit_pct=excluded.dd_limit_pct,
                lose_streak_n=excluded.lose_streak_n,
                cooldown_hours=excluded.cooldown_hours,
                notify_interval_sec=excluded.notify_interval_sec,
                min_hold_minutes=excluded.min_hold_minutes,
                score_conf_threshold=excluded.score_conf_threshold,
                score_gap_threshold=excluded.score_gap_threshold,
                exec_mode_default=excluded.exec_mode_default,
                exec_limit_timeout_sec=excluded.exec_limit_timeout_sec,
                exec_limit_retries=excluded.exec_limit_retries,
                spread_max_pct=excluded.spread_max_pct,
                allow_market_when_wide_spread=excluded.allow_market_when_wide_spread,
                capital_mode=excluded.capital_mode,
                capital_pct=excluded.capital_pct,
                capital_usdt=excluded.capital_usdt,
                margin_budget_usdt=excluded.margin_budget_usdt,
                margin_use_pct=excluded.margin_use_pct,
                max_position_notional_usdt=excluded.max_position_notional_usdt,
                fee_buffer_pct=excluded.fee_buffer_pct,
                universe_symbols=excluded.universe_symbols,
                enable_watchdog=excluded.enable_watchdog,
                watchdog_interval_sec=excluded.watchdog_interval_sec,
                shock_1m_pct=excluded.shock_1m_pct,
                shock_from_entry_pct=excluded.shock_from_entry_pct,
                trailing_enabled=excluded.trailing_enabled,
                trailing_mode=excluded.trailing_mode,
                trail_arm_pnl_pct=excluded.trail_arm_pnl_pct,
                trail_distance_pnl_pct=excluded.trail_distance_pnl_pct,
                trail_grace_minutes=excluded.trail_grace_minutes,
                atr_trail_timeframe=excluded.atr_trail_timeframe,
                atr_trail_k=excluded.atr_trail_k,
                atr_trail_min_pct=excluded.atr_trail_min_pct,
                atr_trail_max_pct=excluded.atr_trail_max_pct,
                tf_weight_4h=excluded.tf_weight_4h,
                tf_weight_1h=excluded.tf_weight_1h,
                tf_weight_30m=excluded.tf_weight_30m,
                vol_shock_atr_mult_threshold=excluded.vol_shock_atr_mult_threshold,
                atr_mult_mean_window=excluded.atr_mult_mean_window,
                updated_at=excluded.updated_at
            """.strip(),
            (
                cfg.per_trade_risk_pct,
                cfg.max_exposure_pct,
                cfg.max_notional_pct,
                cfg.max_leverage,
                daily_loss_limit_legacy,
                dd_limit_legacy,
                float(cfg.daily_loss_limit_pct),
                float(cfg.dd_limit_pct),
                cfg.lose_streak_n,
                cfg.cooldown_hours,
                cfg.notify_interval_sec,
                int(cfg.min_hold_minutes),
                float(cfg.score_conf_threshold),
                float(cfg.score_gap_threshold),
                str(cfg.exec_mode_default),
                float(cfg.exec_limit_timeout_sec),
                int(cfg.exec_limit_retries),
                float(cfg.spread_max_pct),
                int(bool(cfg.allow_market_when_wide_spread)),
                str(cfg.capital_mode.value),
                float(cfg.capital_pct),
                float(cfg.capital_usdt),
                float(cfg.margin_budget_usdt),
                float(cfg.margin_use_pct),
                float(cfg.max_position_notional_usdt) if cfg.max_position_notional_usdt is not None else None,
                float(cfg.fee_buffer_pct),
                universe_csv,
                int(bool(cfg.enable_watchdog)),
                int(cfg.watchdog_interval_sec),
                float(cfg.shock_1m_pct),
                float(cfg.shock_from_entry_pct),
                int(bool(cfg.trailing_enabled)),
                str(cfg.trailing_mode),
                float(cfg.trail_arm_pnl_pct),
                float(cfg.trail_distance_pnl_pct),
                int(cfg.trail_grace_minutes),
                str(cfg.atr_trail_timeframe),
                float(cfg.atr_trail_k),
                float(cfg.atr_trail_min_pct),
                float(cfg.atr_trail_max_pct),
                float(cfg.tf_weight_4h),
                float(cfg.tf_weight_1h),
                float(cfg.tf_weight_30m),
                float(cfg.vol_shock_atr_mult_threshold),
                int(cfg.atr_mult_mean_window),
                _utcnow_iso(),
            ),
        )


class EngineStateRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self) -> EngineStateRow:
        row = self._db.conn.execute("SELECT * FROM engine_state WHERE id=1").fetchone()
        if not row:
            # Default bootstrap state (persisted).
            state = EngineStateRow(state=EngineState.STOPPED, updated_at=datetime.now(tz=timezone.utc))
            self.upsert(state)
            return state
        keys = set(row.keys())
        return EngineStateRow(
            state=EngineState(row["state"]),
            updated_at=_parse_dt(row["updated_at"]),
            ws_connected=bool(row["ws_connected"]) if "ws_connected" in keys and row["ws_connected"] is not None else False,
            last_ws_event_time=(
                _parse_dt(row["last_ws_event_time"])
                if "last_ws_event_time" in keys and row["last_ws_event_time"]
                else None
            ),
        )

    def upsert(self, state: EngineStateRow) -> None:
        self._db.execute(
            """
            INSERT INTO engine_state(id, state, ws_connected, last_ws_event_time, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state=excluded.state,
                ws_connected=excluded.ws_connected,
                last_ws_event_time=excluded.last_ws_event_time,
                updated_at=excluded.updated_at
            """.strip(),
            (
                state.state.value,
                int(bool(state.ws_connected)),
                state.last_ws_event_time.isoformat() if state.last_ws_event_time else None,
                state.updated_at.isoformat(),
            ),
        )


class StatusSnapshotRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get_json(self) -> Optional[Dict[str, Any]]:
        row = self._db.conn.execute("SELECT json FROM status_snapshot WHERE id=1").fetchone()
        if not row:
            return None
        return json.loads(row["json"])

    def upsert_json(self, payload: Dict[str, Any]) -> None:
        self._db.execute(
            """
            INSERT INTO status_snapshot(id, json, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                json=excluded.json,
                updated_at=excluded.updated_at
            """.strip(),
            (json.dumps(payload, ensure_ascii=True, default=str), _utcnow_iso()),
        )


class PnLStateRepo:
    """Persist minimal PnL/risk-related state required for policy guards.

    Stored as a singleton row (id=1).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self) -> Optional[PnLState]:
        row = self._db.conn.execute("SELECT * FROM pnl_state WHERE id=1").fetchone()
        if not row:
            return None
        cooldown_until = row["cooldown_until"]
        keys = set(row.keys())
        last_entry_at = row["last_entry_at"] if "last_entry_at" in keys else None
        last_entry_symbol = row["last_entry_symbol"] if "last_entry_symbol" in keys else None
        last_fill_time = row["last_fill_time"] if "last_fill_time" in keys else None
        return PnLState(
            day=str(row["day"]),
            daily_realized_pnl=float(row["daily_realized_pnl"] or 0.0),
            equity_peak=float(row["equity_peak"] or 0.0),
            lose_streak=int(row["lose_streak"] or 0),
            cooldown_until=_parse_dt(cooldown_until) if cooldown_until else None,
            last_entry_symbol=str(last_entry_symbol) if last_entry_symbol is not None else None,
            last_entry_at=_parse_dt(last_entry_at) if last_entry_at else None,
            last_fill_symbol=str(row["last_fill_symbol"]) if "last_fill_symbol" in keys and row["last_fill_symbol"] else None,
            last_fill_side=str(row["last_fill_side"]) if "last_fill_side" in keys and row["last_fill_side"] else None,
            last_fill_qty=float(row["last_fill_qty"]) if "last_fill_qty" in keys and row["last_fill_qty"] is not None else None,
            last_fill_price=float(row["last_fill_price"]) if "last_fill_price" in keys and row["last_fill_price"] is not None else None,
            last_fill_realized_pnl=(
                float(row["last_fill_realized_pnl"])
                if "last_fill_realized_pnl" in keys and row["last_fill_realized_pnl"] is not None
                else None
            ),
            last_fill_time=_parse_dt(last_fill_time) if last_fill_time else None,
            last_block_reason=str(row["last_block_reason"]) if row["last_block_reason"] is not None else None,
            updated_at=_parse_dt(row["updated_at"]),
        )

    def upsert(self, st: PnLState) -> None:
        self._db.execute(
            """
            INSERT INTO pnl_state(
                id,
                day,
                daily_realized_pnl,
                equity_peak,
                lose_streak,
                cooldown_until,
                last_entry_symbol,
                last_entry_at,
                last_fill_symbol,
                last_fill_side,
                last_fill_qty,
                last_fill_price,
                last_fill_realized_pnl,
                last_fill_time,
                last_block_reason,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                day=excluded.day,
                daily_realized_pnl=excluded.daily_realized_pnl,
                equity_peak=excluded.equity_peak,
                lose_streak=excluded.lose_streak,
                cooldown_until=excluded.cooldown_until,
                last_entry_symbol=excluded.last_entry_symbol,
                last_entry_at=excluded.last_entry_at,
                last_fill_symbol=excluded.last_fill_symbol,
                last_fill_side=excluded.last_fill_side,
                last_fill_qty=excluded.last_fill_qty,
                last_fill_price=excluded.last_fill_price,
                last_fill_realized_pnl=excluded.last_fill_realized_pnl,
                last_fill_time=excluded.last_fill_time,
                last_block_reason=excluded.last_block_reason,
                updated_at=excluded.updated_at
            """.strip(),
            (
                st.day,
                float(st.daily_realized_pnl),
                float(st.equity_peak),
                int(st.lose_streak),
                st.cooldown_until.isoformat() if st.cooldown_until else None,
                st.last_entry_symbol,
                st.last_entry_at.isoformat() if st.last_entry_at else None,
                st.last_fill_symbol,
                st.last_fill_side,
                float(st.last_fill_qty) if st.last_fill_qty is not None else None,
                float(st.last_fill_price) if st.last_fill_price is not None else None,
                float(st.last_fill_realized_pnl) if st.last_fill_realized_pnl is not None else None,
                st.last_fill_time.isoformat() if st.last_fill_time else None,
                st.last_block_reason,
                st.updated_at.isoformat(),
            ),
        )


class OrderRecordRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create_created(
        self,
        *,
        intent_id: Optional[str],
        cycle_id: Optional[str],
        run_id: Optional[str],
        symbol: str,
        side: str,
        order_type: str,
        reduce_only: bool,
        qty: float,
        price: Optional[float],
        time_in_force: Optional[str],
        client_order_id: str,
    ) -> int:
        now = _utcnow_iso()
        cur = self._db.execute(
            """
            INSERT INTO order_records(
                ts_created, ts_updated, intent_id, cycle_id, run_id,
                symbol, side, order_type, reduce_only, qty, price, time_in_force,
                client_order_id, exchange_order_id, status, last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'CREATED', NULL)
            """.strip(),
            (
                now,
                now,
                intent_id,
                cycle_id,
                run_id,
                symbol,
                side,
                order_type,
                int(bool(reduce_only)),
                float(qty),
                float(price) if price is not None else None,
                time_in_force,
                client_order_id,
            ),
        )
        return int(cur.lastrowid)

    def mark_sent_or_ack(
        self,
        *,
        client_order_id: str,
        exchange_order_id: Optional[str],
        status: str,
        last_error: Optional[str] = None,
    ) -> None:
        self._db.execute(
            """
            UPDATE order_records
            SET ts_updated=?,
                exchange_order_id=COALESCE(?, exchange_order_id),
                status=?,
                last_error=?
            WHERE client_order_id=?
            """.strip(),
            (_utcnow_iso(), exchange_order_id, status, last_error, client_order_id),
        )

    def mark_status(
        self,
        *,
        client_order_id: str,
        status: str,
        exchange_order_id: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> None:
        self._db.execute(
            """
            UPDATE order_records
            SET ts_updated=?,
                status=?,
                exchange_order_id=COALESCE(?, exchange_order_id),
                last_error=?
            WHERE client_order_id=?
            """.strip(),
            (_utcnow_iso(), status, exchange_order_id, last_error, client_order_id),
        )

    def get_by_client_order_id(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        row = self._db.conn.execute(
            "SELECT * FROM order_records WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_pending_open(self) -> list[Dict[str, Any]]:
        rows = self._db.conn.execute(
            """
            SELECT * FROM order_records
            WHERE status IN ('CREATED','SENT','ACK','PARTIAL','OPEN')
            """.strip()
        ).fetchall()
        return [dict(r) for r in rows]
