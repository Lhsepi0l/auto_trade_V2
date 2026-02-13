from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, Mapping

from apps.trader_engine.exchange.binance_usdm import BinanceUSDMClient
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.oplog import OperationalLogger
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.storage.repositories import OrderRecordRepo

logger = logging.getLogger(__name__)


def _map_open_status(raw: str) -> str:
    s = str(raw or "").upper()
    if s == "PARTIALLY_FILLED":
        return "PARTIAL"
    return "ACK"


class ReconcileService:
    def __init__(
        self,
        *,
        client: BinanceUSDMClient,
        risk: RiskConfigService,
        engine: EngineService,
        order_records: OrderRecordRepo,
        oplog: OperationalLogger | None = None,
    ) -> None:
        self._client = client
        self._risk = risk
        self._engine = engine
        self._order_records = order_records
        self._oplog = oplog
        self._last_reconcile_ts = 0.0

    def startup_reconcile(self) -> bool:
        ok = False
        try:
            symbols = self._tracked_symbols()
            open_orders = self._client.get_open_orders_usdtm(symbols)
            positions = self._client.get_positions_usdtm(symbols)
            self._reconcile_open_orders(open_orders)
            pos_open = any(abs(float((r or {}).get("position_amt") or 0.0)) > 0.0 for r in (positions or {}).values())
            self._engine.set_recovery_lock(False)
            ok = True
            if self._oplog:
                self._oplog.log_event(
                    "STARTUP_RECONCILE_OK",
                    {"action": "startup_reconcile", "open_position": bool(pos_open), "symbols": symbols},
                )
            return True
        except Exception as e:  # noqa: BLE001
            self._engine.set_recovery_lock(True)
            logger.exception("startup_reconcile_failed")
            if self._oplog:
                try:
                    self._oplog.log_event("STARTUP_RECONCILE_FAIL", {"reason": f"{type(e).__name__}:{e}"})
                except Exception as log_err:  # noqa: BLE001
                    logger.warning("oplog_startup_reconcile_fail_event_failed", extra={"err": type(log_err).__name__}, exc_info=True)
            return False
        finally:
            self._last_reconcile_ts = time.time()
            if not ok:
                # keep lock when failed
                self._engine.set_recovery_lock(True)

    def maybe_periodic_reconcile(self, *, ws_bad: bool, min_interval_sec: int = 600) -> bool:
        if not ws_bad:
            return False
        now = time.time()
        if (now - self._last_reconcile_ts) < max(int(min_interval_sec), 30):
            return False
        return self.startup_reconcile()

    def _tracked_symbols(self) -> list[str]:
        cfg = self._risk.get_config()
        symbols = [str(s).upper() for s in (cfg.universe_symbols or []) if str(s).strip()]
        if not symbols:
            symbols = ["BTCUSDT", "ETHUSDT", "XAUUSDT"]
        return symbols

    def _reconcile_open_orders(self, open_orders: Mapping[str, Any]) -> None:
        exchange_open_by_cid: Dict[str, Mapping[str, Any]] = {}
        for row in self._iter_open_orders(open_orders):
            cid = str(row.get("clientOrderId") or row.get("client_order_id") or "").strip()
            if not cid:
                continue
            exchange_open_by_cid[cid] = row
            rec = self._order_records.get_by_client_order_id(cid)
            ex_status = _map_open_status(str(row.get("status") or "NEW"))
            ex_oid = row.get("orderId") or row.get("order_id")
            ex_oid_s = str(ex_oid) if ex_oid is not None else None
            if rec is None:
                # Record missing in DB; reconstruct minimal row as ACK.
                self._order_records.create_created(
                    intent_id=None,
                    cycle_id=None,
                    run_id=(self._oplog.run_id if self._oplog else None),
                    symbol=str(row.get("symbol") or ""),
                    side=str(row.get("side") or ""),
                    order_type=str(row.get("type") or ""),
                    reduce_only=bool(row.get("reduceOnly") or row.get("reduce_only")),
                    qty=float(row.get("origQty") or row.get("orig_qty") or 0.0),
                    price=(float(row.get("price")) if row.get("price") is not None else None),
                    time_in_force=(str(row.get("timeInForce")) if row.get("timeInForce") is not None else None),
                    client_order_id=cid,
                )
            self._order_records.mark_status(
                client_order_id=cid,
                status=ex_status,
                exchange_order_id=ex_oid_s,
                last_error=None,
            )

        pending = self._order_records.list_pending_open()
        for rec in pending:
            cid = str(rec.get("client_order_id") or "")
            if not cid:
                continue
            if cid in exchange_open_by_cid:
                continue
            last_error = str(rec.get("last_error") or "")
            to_status = "EXPIRED" if "expire" in last_error.lower() else "CANCELED"
            self._order_records.mark_status(client_order_id=cid, status=to_status, last_error=last_error or None)

    @staticmethod
    def _iter_open_orders(open_orders: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
        if not isinstance(open_orders, Mapping):
            return []
        out: list[Mapping[str, Any]] = []
        for v in open_orders.values():
            if isinstance(v, list):
                for row in v:
                    if isinstance(row, Mapping):
                        out.append(row)
        return out
