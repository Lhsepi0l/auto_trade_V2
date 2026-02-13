from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Optional

from apps.trader_engine.storage.db import Database

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _to_json(obj: Mapping[str, Any]) -> str:
    return json.dumps(dict(obj), ensure_ascii=True, default=str)


@dataclass
class OperationalLogger:
    db: Database
    run_id: str
    component: str = "engine"

    @classmethod
    def create(cls, *, db: Database, component: str = "engine") -> "OperationalLogger":
        return cls(db=db, run_id=f"run-{uuid.uuid4().hex[:12]}", component=component)

    def _with_run_id(self, payload: Optional[Mapping[str, Any]]) -> MutableMapping[str, Any]:
        out: MutableMapping[str, Any] = dict(payload or {})
        out.setdefault("run_id", self.run_id)
        return out

    def log_event(self, event_type: str, payload_dict: Optional[Mapping[str, Any]] = None) -> None:
        ts = _utcnow_iso()
        payload = self._with_run_id(payload_dict)
        self.db.execute(
            "INSERT INTO op_events(ts, event_type, json) VALUES (?, ?, ?)",
            (ts, str(event_type), _to_json(payload)),
        )
        logger.info(
            "op_event",
            extra={
                "component": self.component,
                "event": str(event_type),
                "run_id": self.run_id,
                "symbol": payload.get("symbol"),
                "side": payload.get("side"),
                "action": payload.get("action"),
                "reason": payload.get("reason"),
                "cycle_id": payload.get("cycle_id"),
                "intent_id": payload.get("intent_id"),
                "client_order_id": payload.get("client_order_id"),
            },
        )

    def log_decision(
        self,
        *,
        cycle_id: str,
        symbol: str,
        direction: str,
        confidence: Optional[float],
        regime_4h: Optional[str],
        scores_json: Mapping[str, Any],
        reason: Optional[str],
    ) -> None:
        ts = _utcnow_iso()
        self.db.execute(
            """
            INSERT INTO decisions(ts, cycle_id, symbol, direction, confidence, regime_4h, scores_json, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """.strip(),
            (
                ts,
                str(cycle_id),
                str(symbol),
                str(direction),
                float(confidence) if confidence is not None else None,
                str(regime_4h) if regime_4h is not None else None,
                _to_json(scores_json),
                str(reason) if reason is not None else None,
            ),
        )
        self.log_event(
            "DECISION",
            {
                "cycle_id": cycle_id,
                "symbol": symbol,
                "action": direction,
                "reason": reason,
                "confidence": confidence,
                "regime_4h": regime_4h,
            },
        )

    def log_execution(
        self,
        *,
        intent_id: Optional[str],
        symbol: str,
        side: Optional[str],
        qty: Optional[float],
        price: Optional[float],
        order_type: Optional[str],
        client_order_id: Optional[str],
        status: Optional[str],
        reason: Optional[str],
    ) -> None:
        ts = _utcnow_iso()
        self.db.execute(
            """
            INSERT INTO executions(
                ts, intent_id, symbol, side, qty, price, order_type, client_order_id, status, reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """.strip(),
            (
                ts,
                str(intent_id) if intent_id else None,
                str(symbol),
                str(side) if side else None,
                float(qty) if qty is not None else None,
                float(price) if price is not None else None,
                str(order_type) if order_type else None,
                str(client_order_id) if client_order_id else None,
                str(status) if status else None,
                str(reason) if reason else None,
            ),
        )
        self.log_event(
            "EXECUTION",
            {
                "intent_id": intent_id,
                "symbol": symbol,
                "side": side,
                "action": order_type,
                "client_order_id": client_order_id,
                "reason": reason,
                "status": status,
            },
        )

    def log_risk_block(
        self,
        *,
        intent_id: Optional[str],
        symbol: str,
        block_reason: str,
        details_json: Optional[Mapping[str, Any]] = None,
    ) -> None:
        ts = _utcnow_iso()
        details = self._with_run_id(details_json)
        self.db.execute(
            """
            INSERT INTO risk_blocks(ts, intent_id, symbol, block_reason, details_json)
            VALUES (?, ?, ?, ?, ?)
            """.strip(),
            (
                ts,
                str(intent_id) if intent_id else None,
                str(symbol),
                str(block_reason),
                _to_json(details),
            ),
        )
        self.log_event(
            "RISK_BLOCK",
            {
                "intent_id": intent_id,
                "symbol": symbol,
                "reason": block_reason,
            },
        )

    def log_snapshot(self, payload_dict: Optional[Mapping[str, Any]] = None) -> None:
        self.log_event("SNAPSHOT", payload_dict or {})
