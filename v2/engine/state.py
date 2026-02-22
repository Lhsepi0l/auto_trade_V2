from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, cast

from v2.engine.journal import JournalWriter
from v2.exchange.types import ResyncSnapshot
from v2.storage import RuntimeStorage

EngineMode = Literal["shadow", "live"]
EngineStatus = Literal["STOPPED", "RUNNING", "PAUSED", "KILLED"]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class OperationalMode:
    paused: bool = False
    safe_mode: bool = False


@dataclass
class PositionState:
    symbol: str
    position_amt: float
    entry_price: float | None = None
    unrealized_pnl: float | None = None


@dataclass
class OrderState:
    client_id: str
    exchange_id: str | None
    symbol: str
    status: str
    order_type: str
    side: str
    qty: float | None = None
    price: float | None = None
    event_time_ms: int | None = None


@dataclass
class FillState:
    fill_id: str
    client_id: str | None
    exchange_id: str | None
    symbol: str
    side: str
    qty: float
    price: float
    realized_pnl: float | None = None
    fill_time_ms: int | None = None


@dataclass
class EngineRuntimeState:
    mode: EngineMode
    status: EngineStatus = "STOPPED"
    operational: OperationalMode = field(default_factory=OperationalMode)
    current_position: dict[str, PositionState] = field(default_factory=dict)
    open_orders: dict[str, OrderState] = field(default_factory=dict)
    last_fills: list[FillState] = field(default_factory=list)
    last_transition_at: str = field(default_factory=_utcnow_iso)
    last_reconcile_at: str | None = None


class EngineStateStore:
    def __init__(self, *, storage: RuntimeStorage, mode: EngineMode = "shadow") -> None:
        self._storage = storage
        self._storage.ensure_schema()
        self._journal = JournalWriter(storage)
        self._state = EngineRuntimeState(mode=mode)
        self._hydrate_from_storage()

    def _hydrate_from_storage(self) -> None:
        ops = self._storage.get_ops_state()
        self._state.operational = OperationalMode(
            paused=bool(ops["paused"]), safe_mode=bool(ops["safe_mode"])
        )

        open_orders: dict[str, OrderState] = {}
        for row in self._storage.list_open_orders():
            client_id = str(row.get("client_id") or "")
            if not client_id:
                continue
            open_orders[client_id] = OrderState(
                client_id=client_id,
                exchange_id=str(row.get("exchange_id"))
                if row.get("exchange_id") is not None
                else None,
                symbol=str(row.get("symbol") or ""),
                status=str(row.get("status") or ""),
                order_type=str(row.get("order_type") or ""),
                side=str(row.get("side") or ""),
                qty=_to_float(row.get("qty")) if row.get("qty") is not None else None,
                price=_to_float(row.get("price")) if row.get("price") is not None else None,
                event_time_ms=_to_int_or_none(row.get("event_time_ms")),
            )
        self._state.open_orders = open_orders

        positions: dict[str, PositionState] = {}
        for row in self._storage.latest_positions():
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            position_amt = _to_float(row.get("position_amt"))
            if abs(position_amt) <= 0:
                continue
            positions[symbol] = PositionState(
                symbol=symbol,
                position_amt=position_amt,
                entry_price=_to_float(row.get("entry_price"))
                if row.get("entry_price") is not None
                else None,
                unrealized_pnl=_to_float(row.get("unrealized_pnl"))
                if row.get("unrealized_pnl") is not None
                else None,
            )
        self._state.current_position = positions

        fills: list[FillState] = []
        for row in reversed(self._storage.recent_fills(limit=100)):
            fills.append(
                FillState(
                    fill_id=str(row.get("fill_id") or ""),
                    client_id=str(row.get("client_id"))
                    if row.get("client_id") is not None
                    else None,
                    exchange_id=str(row.get("exchange_id"))
                    if row.get("exchange_id") is not None
                    else None,
                    symbol=str(row.get("symbol") or ""),
                    side=str(row.get("side") or ""),
                    qty=_to_float(row.get("qty")),
                    price=_to_float(row.get("price")),
                    realized_pnl=_to_float(row.get("realized_pnl"))
                    if row.get("realized_pnl") is not None
                    else None,
                    fill_time_ms=_to_int_or_none(row.get("fill_time_ms")),
                )
            )
        self._state.last_fills = fills

    def get(self) -> EngineRuntimeState:
        return self._state

    def load_runtime_risk_config(self) -> dict[str, Any]:
        return self._storage.load_runtime_risk_config()

    def save_runtime_risk_config(self, *, config: dict[str, Any]) -> None:
        self._storage.save_runtime_risk_config(config=config)

    def set(
        self, *, mode: EngineMode | None = None, status: EngineStatus | None = None
    ) -> EngineRuntimeState:
        current = self._state
        self._state = EngineRuntimeState(
            mode=mode if mode is not None else current.mode,
            status=status if status is not None else current.status,
            operational=current.operational,
            current_position=current.current_position,
            open_orders=current.open_orders,
            last_fills=current.last_fills,
            last_transition_at=_utcnow_iso(),
            last_reconcile_at=current.last_reconcile_at,
        )
        return self._state

    def _event_hash(self, *, event_type: str, payload: dict[str, Any], reason: str | None) -> str:
        raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"{event_type}|{reason or ''}|{raw}".encode("utf-8")).hexdigest()
        return f"evt-{digest}"

    def _event_id_for_ws(self, event: dict[str, Any]) -> str:
        event_type = str(event.get("e") or "UNKNOWN")
        event_ms = int(event.get("E") or 0)
        if event_type == "ORDER_TRADE_UPDATE":
            raw_order = event.get("o")
            if not isinstance(raw_order, dict):
                return self._event_hash(event_type=f"ws.{event_type}", payload=event, reason=None)
            order = cast(dict[str, Any], raw_order)
            client_id = str(order.get("c") or "")
            exchange_id = str(order.get("i") or order.get("orderId") or "")
            trade_id = str(order.get("t") or "")
            execution_type = str(order.get("x") or "")
            return (
                f"ws-{event_type}-{event_ms}-{client_id}-{exchange_id}-{trade_id}-{execution_type}"
            )
        if event_type == "ACCOUNT_UPDATE":
            return f"ws-{event_type}-{event_ms}"
        return self._event_hash(event_type=f"ws.{event_type}", payload=event, reason=None)

    def startup_reconcile(
        self, *, snapshot: ResyncSnapshot, reason: str = "startup_reconcile"
    ) -> EngineRuntimeState:
        payload = {
            "open_orders": snapshot.open_orders,
            "positions": snapshot.positions,
            "balances": snapshot.balances,
        }
        event_id = self._event_hash(event_type="reconcile.startup", payload=payload, reason=reason)
        return self._apply_reconcile_payload(
            payload=payload, reason=reason, event_id=event_id, write_journal=True
        )

    def apply_reconciliation(
        self,
        *,
        open_orders: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        balances: list[dict[str, Any]],
        reason: str = "reconcile",
        event_id: str | None = None,
    ) -> EngineRuntimeState:
        payload = {
            "open_orders": open_orders,
            "positions": positions,
            "balances": balances,
        }
        effective_event_id = event_id or self._event_hash(
            event_type="reconcile", payload=payload, reason=reason
        )
        return self._apply_reconcile_payload(
            payload=payload,
            reason=reason,
            event_id=effective_event_id,
            write_journal=True,
        )

    def _apply_reconcile_payload(
        self,
        *,
        payload: dict[str, Any],
        reason: str,
        event_id: str,
        write_journal: bool,
        persist_storage: bool = True,
    ) -> EngineRuntimeState:
        if write_journal:
            inserted = self._journal.write(
                event_type="reconcile",
                payload=payload,
                reason=reason,
                event_id=event_id,
            )
            if not inserted:
                return self._state

        incoming_orders = payload.get("open_orders")
        open_orders_list = incoming_orders if isinstance(incoming_orders, list) else []
        next_open_orders: dict[str, OrderState] = {}
        for item in open_orders_list:
            if not isinstance(item, dict):
                continue
            client_id = str(
                item.get("clientOrderId") or item.get("client_id") or item.get("c") or ""
            )
            if not client_id:
                continue
            exchange_id = item.get("orderId") or item.get("exchange_id") or item.get("i")
            symbol = str(item.get("symbol") or item.get("s") or "")
            status = str(item.get("status") or item.get("X") or "NEW")
            order_type = str(item.get("type") or item.get("o") or "")
            side = str(item.get("side") or item.get("S") or "")
            qty_raw = item.get("origQty") if item.get("origQty") is not None else item.get("q")
            qty = _to_float(qty_raw) if qty_raw is not None else None
            price_raw = item.get("price") if item.get("price") is not None else item.get("p")
            price = _to_float(price_raw) if price_raw is not None else None
            event_time_ms = _to_int_or_none(item.get("updateTime") or item.get("E"))

            if persist_storage:
                self._storage.upsert_order(
                    client_id=client_id,
                    exchange_id=str(exchange_id) if exchange_id is not None else None,
                    symbol=symbol,
                    status=status,
                    order_type=order_type,
                    side=side,
                    qty=qty,
                    price=price,
                    event_time_ms=event_time_ms,
                )

            next_open_orders[client_id] = OrderState(
                client_id=client_id,
                exchange_id=str(exchange_id) if exchange_id is not None else None,
                symbol=symbol,
                status=status,
                order_type=order_type,
                side=side,
                qty=qty,
                price=price,
                event_time_ms=event_time_ms,
            )

        for stale_id in set(self._state.open_orders.keys()).difference(
            set(next_open_orders.keys())
        ):
            if persist_storage:
                self._storage.mark_order_status(client_id=stale_id, status="CANCELED")

        incoming_positions = payload.get("positions")
        positions_list = incoming_positions if isinstance(incoming_positions, list) else []
        next_positions: dict[str, PositionState] = {}
        position_rows: list[dict[str, Any]] = []
        for item in positions_list:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or item.get("s") or "").upper()
            if not symbol:
                continue
            position_amt = _to_float(
                item.get("positionAmt") if item.get("positionAmt") is not None else item.get("pa")
            )
            entry_price_raw = (
                item.get("entryPrice") if item.get("entryPrice") is not None else item.get("ep")
            )
            unrealized_raw = (
                item.get("unRealizedProfit")
                if item.get("unRealizedProfit") is not None
                else item.get("up")
            )
            entry_price = _to_float(entry_price_raw) if entry_price_raw is not None else None
            unrealized_pnl = _to_float(unrealized_raw) if unrealized_raw is not None else None
            if abs(position_amt) > 0:
                next_positions[symbol] = PositionState(
                    symbol=symbol,
                    position_amt=position_amt,
                    entry_price=entry_price,
                    unrealized_pnl=unrealized_pnl,
                )
            position_rows.append(
                {
                    "symbol": symbol,
                    "position_amt": position_amt,
                    "entry_price": entry_price,
                    "unrealized_pnl": unrealized_pnl,
                }
            )

        if persist_storage:
            self._storage.append_positions_snapshot(
                positions=position_rows,
                snapshot_time_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
                source_event_id=event_id,
            )

        self._state.open_orders = next_open_orders
        self._state.current_position = next_positions
        self._state.last_reconcile_at = _utcnow_iso()
        return self._state

    def apply_exchange_event(
        self, *, event: dict[str, Any], reason: str | None = None
    ) -> EngineRuntimeState:
        event_type = str(event.get("e") or "UNKNOWN")
        event_id = self._event_id_for_ws(event)
        inserted = self._journal.write(
            event_type=f"ws.{event_type}", payload=event, reason=reason, event_id=event_id
        )
        if not inserted:
            return self._state
        return self._apply_exchange_event_payload(
            event_type=event_type, payload=event, source_event_id=event_id
        )

    def _apply_exchange_event_payload(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        source_event_id: str,
        persist_storage: bool = True,
    ) -> EngineRuntimeState:
        if event_type == "ORDER_TRADE_UPDATE":
            order = payload.get("o")
            if not isinstance(order, dict):
                return self._state

            client_id = str(order.get("c") or order.get("clientOrderId") or "")
            if client_id:
                exchange_id = order.get("i") or order.get("orderId")
                symbol = str(order.get("s") or "")
                status = str(order.get("X") or "")
                order_type = str(order.get("o") or "")
                side = str(order.get("S") or "")
                qty_raw = order.get("q") if order.get("q") is not None else order.get("l")
                qty = _to_float(qty_raw) if qty_raw is not None else None
                price_raw = order.get("p") if order.get("p") is not None else order.get("L")
                price = _to_float(price_raw) if price_raw is not None else None
                event_time_ms = _to_int_or_none(payload.get("E"))

                if persist_storage:
                    self._storage.upsert_order(
                        client_id=client_id,
                        exchange_id=str(exchange_id) if exchange_id is not None else None,
                        symbol=symbol,
                        status=status,
                        order_type=order_type,
                        side=side,
                        qty=qty,
                        price=price,
                        event_time_ms=event_time_ms,
                    )

                if status in {"NEW", "PARTIALLY_FILLED"}:
                    self._state.open_orders[client_id] = OrderState(
                        client_id=client_id,
                        exchange_id=str(exchange_id) if exchange_id is not None else None,
                        symbol=symbol,
                        status=status,
                        order_type=order_type,
                        side=side,
                        qty=qty,
                        price=price,
                        event_time_ms=event_time_ms,
                    )
                else:
                    self._state.open_orders.pop(client_id, None)

            if str(order.get("x") or "") == "TRADE":
                trade_id = order.get("t")
                fill_time_ms = _to_int_or_none(
                    order.get("T") if order.get("T") is not None else payload.get("E")
                )
                fill_id = (
                    str(trade_id)
                    if trade_id is not None and str(trade_id) != ""
                    else f"{source_event_id}:{str(order.get('c') or '')}:{fill_time_ms or 0}"
                )
                inserted_fill = True
                if persist_storage:
                    inserted_fill = self._storage.insert_fill(
                        fill_id=fill_id,
                        client_id=str(order.get("c")) if order.get("c") is not None else None,
                        exchange_id=str(order.get("i")) if order.get("i") is not None else None,
                        symbol=str(order.get("s") or ""),
                        side=str(order.get("S") or ""),
                        qty=_to_float(order.get("l")),
                        price=_to_float(order.get("L")),
                        realized_pnl=_to_float(order.get("rp"))
                        if order.get("rp") is not None
                        else None,
                        fill_time_ms=fill_time_ms,
                    )
                if inserted_fill:
                    self._state.last_fills.append(
                        FillState(
                            fill_id=fill_id,
                            client_id=str(order.get("c")) if order.get("c") is not None else None,
                            exchange_id=str(order.get("i")) if order.get("i") is not None else None,
                            symbol=str(order.get("s") or ""),
                            side=str(order.get("S") or ""),
                            qty=_to_float(order.get("l")),
                            price=_to_float(order.get("L")),
                            realized_pnl=_to_float(order.get("rp"))
                            if order.get("rp") is not None
                            else None,
                            fill_time_ms=fill_time_ms,
                        )
                    )
                    if len(self._state.last_fills) > 100:
                        self._state.last_fills = self._state.last_fills[-100:]

            return self._state

        if event_type == "ACCOUNT_UPDATE":
            account = payload.get("a")
            if isinstance(account, dict):
                raw_positions_value = account.get("P")
                raw_positions: list[dict[str, Any]] = []
                if isinstance(raw_positions_value, list):
                    for raw_row in raw_positions_value:
                        if isinstance(raw_row, dict):
                            raw_positions.append(raw_row)
                rows: list[dict[str, Any]] = []
                next_positions = dict(self._state.current_position)
                for row in raw_positions:
                    symbol = str(row.get("s") or "").upper()
                    if not symbol:
                        continue
                    position_amt = _to_float(row.get("pa"))
                    entry_price = _to_float(row.get("ep")) if row.get("ep") is not None else None
                    unrealized_pnl = _to_float(row.get("up")) if row.get("up") is not None else None
                    if abs(position_amt) > 0:
                        next_positions[symbol] = PositionState(
                            symbol=symbol,
                            position_amt=position_amt,
                            entry_price=entry_price,
                            unrealized_pnl=unrealized_pnl,
                        )
                    else:
                        next_positions.pop(symbol, None)
                    rows.append(
                        {
                            "symbol": symbol,
                            "position_amt": position_amt,
                            "entry_price": entry_price,
                            "unrealized_pnl": unrealized_pnl,
                        }
                    )

                if persist_storage:
                    self._storage.append_positions_snapshot(
                        positions=rows,
                        snapshot_time_ms=_to_int_or_none(payload.get("E")),
                        source_event_id=source_event_id,
                    )
                self._state.current_position = next_positions
            return self._state

        return self._state

    def apply_ops_mode(
        self,
        *,
        paused: bool | None = None,
        safe_mode: bool | None = None,
        reason: str | None = None,
        event_id: str | None = None,
    ) -> EngineRuntimeState:
        current = self._state.operational
        payload = {
            "paused": current.paused if paused is None else bool(paused),
            "safe_mode": current.safe_mode if safe_mode is None else bool(safe_mode),
        }
        effective_event_id = event_id or self._event_hash(
            event_type="ops.UPDATE", payload=payload, reason=reason
        )
        inserted = self._journal.write(
            event_type="ops.UPDATE",
            payload=payload,
            reason=reason,
            event_id=effective_event_id,
        )
        if not inserted:
            return self._state
        self._storage.set_ops_state(paused=payload["paused"], safe_mode=payload["safe_mode"])
        self._state.operational = OperationalMode(
            paused=payload["paused"], safe_mode=payload["safe_mode"]
        )
        return self._state

    def replay_from_journal(self) -> EngineRuntimeState:
        replay_state = EngineRuntimeState(mode=self._state.mode, status=self._state.status)
        self._state = replay_state
        for row in self._storage.list_journal_events(ascending=True):
            event_type = str(row.get("event_type") or "")
            payload_raw = row.get("payload_json")
            try:
                payload = json.loads(str(payload_raw)) if payload_raw is not None else {}
            except (TypeError, ValueError):
                payload = {}
            event_id = str(row.get("event_id") or "")

            if event_type == "reconcile" and isinstance(payload, dict):
                self._apply_reconcile_payload(
                    payload=payload,
                    reason=str(row.get("reason") or "replay"),
                    event_id=event_id,
                    write_journal=False,
                    persist_storage=False,
                )
                continue

            if event_type.startswith("ws.") and isinstance(payload, dict):
                ws_type = event_type[3:]
                self._apply_exchange_event_payload(
                    event_type=ws_type,
                    payload=payload,
                    source_event_id=event_id,
                    persist_storage=False,
                )
                continue

            if event_type == "ops.UPDATE" and isinstance(payload, dict):
                paused = bool(payload.get("paused"))
                safe_mode = bool(payload.get("safe_mode"))
                self._state.operational = OperationalMode(paused=paused, safe_mode=safe_mode)
                continue

        return self._state
