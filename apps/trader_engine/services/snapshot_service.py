from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from apps.trader_engine.services.oplog import OperationalLogger
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.storage.db import Database

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class SnapshotRow:
    ts: str
    symbol: str
    position_side: str
    qty: float
    entry_price: float
    mark_price: float
    unrealized_pnl_usdt: float
    unrealized_pnl_pct: float
    realized_pnl_usdt: float
    equity_usdt: float
    available_usdt: float


class SnapshotService:
    def __init__(
        self,
        *,
        db: Database,
        client: Any,
        pnl: PnLService,
        oplog: Optional[OperationalLogger] = None,
        balance_cache_ttl_sec: float = 5.0,
    ) -> None:
        self._db = db
        self._client = client
        self._pnl = pnl
        self._oplog = oplog
        self._balance_cache_ttl_sec = max(float(balance_cache_ttl_sec), 0.0)
        self._last_balance_ts = 0.0
        self._last_wallet = 0.0
        self._last_available = 0.0
        self._last_periodic_ts = 0.0

    @staticmethod
    def calc_unrealized(*, qty: float, entry_price: float, mark_price: float) -> tuple[str, float, float]:
        q = float(qty)
        ep = float(entry_price)
        mp = float(mark_price)
        side = "LONG" if q > 0 else "SHORT"
        upnl = (mp - ep) * q
        base = abs(ep * q)
        upnl_pct = (upnl / base * 100.0) if base > 0.0 else 0.0
        return side, float(upnl), float(upnl_pct)

    def _fetch_balance_cached(self) -> tuple[float, float]:
        now = time.monotonic()
        if (now - self._last_balance_ts) <= self._balance_cache_ttl_sec:
            return self._last_wallet, self._last_available
        try:
            bal = self._client.get_account_balance_usdtm()
            wallet = float((bal or {}).get("wallet") or 0.0)
            available = float((bal or {}).get("available") or 0.0)
            self._last_wallet = wallet
            self._last_available = available
            self._last_balance_ts = now
        except Exception as e:  # noqa: BLE001
            logger.warning("snapshot_balance_fetch_failed", extra={"err": type(e).__name__}, exc_info=True)
        return self._last_wallet, self._last_available

    def _pick_position(self, preferred_symbol: Optional[str] = None) -> tuple[Optional[str], Optional[Mapping[str, Any]]]:
        try:
            pos = self._client.get_open_positions_any()
        except Exception:
            return None, None
        if not isinstance(pos, Mapping) or not pos:
            return None, None
        if preferred_symbol:
            sym = str(preferred_symbol).upper()
            row = pos.get(sym)
            if isinstance(row, Mapping):
                return sym, row
        # one-asset rule expected; fallback to first non-zero row.
        for sym, row in pos.items():
            if not isinstance(row, Mapping):
                continue
            q = float(row.get("position_amt") or 0.0)
            if abs(q) > 0.0:
                return str(sym).upper(), row
        return None, None

    def capture_snapshot(
        self,
        *,
        reason: str,
        cycle_id: Optional[str] = None,
        intent_id: Optional[str] = None,
        preferred_symbol: Optional[str] = None,
    ) -> Optional[SnapshotRow]:
        symbol, row = self._pick_position(preferred_symbol=preferred_symbol)
        if not symbol or row is None:
            return None

        qty = float(row.get("position_amt") or 0.0)
        if abs(qty) <= 0.0:
            return None
        entry_price = float(row.get("entry_price") or row.get("entryPrice") or 0.0)
        if entry_price <= 0.0:
            return None
        try:
            mp = self._client.get_mark_price(symbol)
            mark_price = float((mp or {}).get("markPrice") or 0.0)
        except Exception:
            mark_price = 0.0
        if mark_price <= 0.0:
            return None

        side, upnl, upnl_pct = self.calc_unrealized(qty=qty, entry_price=entry_price, mark_price=mark_price)
        wallet, available = self._fetch_balance_cached()
        realized = float(self._pnl.get_or_bootstrap().daily_realized_pnl or 0.0)
        equity = float(wallet + upnl)

        ts = _utcnow_iso()
        self._db.execute(
            """
            INSERT INTO pnl_snapshots(
                ts, symbol, position_side, qty, entry_price, mark_price,
                unrealized_pnl_usdt, unrealized_pnl_pct, realized_pnl_usdt,
                equity_usdt, available_usdt
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """.strip(),
            (
                ts,
                symbol,
                side,
                qty,
                entry_price,
                mark_price,
                upnl,
                upnl_pct,
                realized,
                equity,
                available,
            ),
        )

        if self._oplog:
            try:
                self._oplog.log_event(
                    "PNL_SNAPSHOT",
                    {
                        "symbol": symbol,
                        "side": side,
                        "reason": reason,
                        "cycle_id": cycle_id,
                        "intent_id": intent_id,
                        "action": "snapshot",
                    },
                )
            except Exception:
                logger.exception("oplog_snapshot_event_failed", extra={"symbol": symbol, "reason": reason})

        return SnapshotRow(
            ts=ts,
            symbol=symbol,
            position_side=side,
            qty=qty,
            entry_price=entry_price,
            mark_price=mark_price,
            unrealized_pnl_usdt=upnl,
            unrealized_pnl_pct=upnl_pct,
            realized_pnl_usdt=realized,
            equity_usdt=equity,
            available_usdt=available,
        )

    def maybe_capture_periodic(self, *, cycle_id: Optional[str] = None, interval_sec: int = 1800) -> Optional[SnapshotRow]:
        now = time.monotonic()
        if (now - self._last_periodic_ts) < max(int(interval_sec), 1):
            return None
        row = self.capture_snapshot(reason="periodic", cycle_id=cycle_id)
        if row is not None:
            self._last_periodic_ts = now
        return row

    def get_last_snapshot_meta(self) -> tuple[Optional[str], Optional[float], Optional[float]]:
        try:
            row = self._db.query_one(
                """
                SELECT ts, unrealized_pnl_usdt, unrealized_pnl_pct
                FROM pnl_snapshots
                ORDER BY ts DESC
                LIMIT 1
                """.strip()
            )
        except Exception:
            return None, None, None
        if not row:
            return None, None, None
        return (
            str(row["ts"]) if row["ts"] is not None else None,
            float(row["unrealized_pnl_usdt"]) if row["unrealized_pnl_usdt"] is not None else None,
            float(row["unrealized_pnl_pct"]) if row["unrealized_pnl_pct"] is not None else None,
        )
