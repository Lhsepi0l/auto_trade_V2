from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol, cast

from v2.exchange.client_order_id import generate_client_order_id
from v2.storage import RuntimeStorage

BracketMethod = Literal["percent", "atr"]
PositionSide = Literal["LONG", "SHORT"]
EntrySide = Literal["BUY", "SELL"]
HedgePositionSide = Literal["BOTH", "LONG", "SHORT"]
BracketState = Literal["CREATED", "PLACED", "ACTIVE", "TP_HIT", "SL_HIT", "CLEANED"]


class AlgoRESTClient(Protocol):
    async def place_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]: ...

    async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]: ...

    async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]: ...


def _round_price(value: float, ndigits: int = 8) -> float:
    q = Decimal(10) ** -ndigits
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _as_bracket_state(value: str | None) -> BracketState:
    if value in {"CREATED", "PLACED", "ACTIVE", "TP_HIT", "SL_HIT", "CLEANED"}:
        return cast(BracketState, value)
    return "CREATED"


def _extract_client_algo_id(row: dict[str, Any]) -> str | None:
    for key in ("clientAlgoId", "clientOrderId"):
        raw = row.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            return text
    return None


@dataclass
class BracketConfig:
    method: BracketMethod = "percent"
    take_profit_pct: float = 0.02
    stop_loss_pct: float = 0.01
    tp_atr: float = 2.0
    sl_atr: float = 1.0
    working_type: Literal["MARK_PRICE", "CONTRACT_PRICE"] = "MARK_PRICE"
    price_protect: bool = True


@dataclass
class PlannedBracket:
    symbol: str
    entry_side: EntrySide
    position_side: HedgePositionSide
    quantity: float
    take_profit_price: float
    stop_loss_price: float
    tp_client_algo_id: str
    sl_client_algo_id: str


@dataclass
class BracketRuntime:
    symbol: str
    tp_order_client_id: str | None
    sl_order_client_id: str | None
    state: BracketState


@dataclass
class BracketPlanner:
    cfg: BracketConfig

    def levels(
        self,
        *,
        entry_price: float,
        side: PositionSide = "LONG",
        atr: float | None = None,
    ) -> dict[str, float]:
        side_u = str(side).upper()
        if side_u not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        if self.cfg.method == "atr":
            if atr is None or atr <= 0:
                raise ValueError("ATR method requires a positive atr value")
            if side_u == "LONG":
                stop_loss = entry_price - (atr * self.cfg.sl_atr)
                take_profit = entry_price + (atr * self.cfg.tp_atr)
            else:
                stop_loss = entry_price + (atr * self.cfg.sl_atr)
                take_profit = entry_price - (atr * self.cfg.tp_atr)
        else:
            if side_u == "LONG":
                stop_loss = entry_price * (1.0 - self.cfg.stop_loss_pct)
                take_profit = entry_price * (1.0 + self.cfg.take_profit_pct)
            else:
                stop_loss = entry_price * (1.0 + self.cfg.stop_loss_pct)
                take_profit = entry_price * (1.0 - self.cfg.take_profit_pct)
        return {"take_profit": float(take_profit), "stop_loss": float(stop_loss)}


class BracketService:
    _ALLOWED: dict[BracketState, set[BracketState]] = {
        "CREATED": {"PLACED", "CLEANED"},
        "PLACED": {"ACTIVE", "CLEANED"},
        "ACTIVE": {"TP_HIT", "SL_HIT", "CLEANED"},
        "TP_HIT": {"CLEANED"},
        "SL_HIT": {"CLEANED"},
        "CLEANED": set(),
    }

    def __init__(
        self,
        *,
        planner: BracketPlanner,
        storage: RuntimeStorage,
        rest_client: AlgoRESTClient | None,
        mode: Literal["shadow", "live"],
    ) -> None:
        self._planner = planner
        self._storage = storage
        self._rest = rest_client
        self._mode = mode

    def _print_shadow_payloads(
        self,
        *,
        symbol: str,
        tp_payload: dict[str, Any],
        sl_payload: dict[str, Any],
    ) -> None:
        payload = {
            "event": "tpsl.shadow.place",
            "symbol": symbol,
            "tp_payload": tp_payload,
            "sl_payload": sl_payload,
        }
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))

    def _get_runtime(self, *, symbol: str) -> BracketRuntime | None:
        symbol_u = symbol.upper()
        for row in self._storage.list_bracket_states():
            if str(row.get("symbol") or "").upper() != symbol_u:
                continue
            return BracketRuntime(
                symbol=symbol_u,
                tp_order_client_id=str(row.get("tp_order_client_id")) if row.get("tp_order_client_id") is not None else None,
                sl_order_client_id=str(row.get("sl_order_client_id")) if row.get("sl_order_client_id") is not None else None,
                state=_as_bracket_state(str(row.get("state") or "CREATED")),
            )
        return None

    def _persist(self, *, runtime: BracketRuntime) -> None:
        self._storage.set_bracket_state(
            symbol=runtime.symbol,
            tp_order_client_id=runtime.tp_order_client_id,
            sl_order_client_id=runtime.sl_order_client_id,
            state=runtime.state,
        )

    def _transition(self, *, symbol: str, to_state: BracketState) -> BracketRuntime:
        current = self._get_runtime(symbol=symbol)
        if current is None:
            raise ValueError(f"bracket missing for symbol={symbol}")
        if to_state != current.state and to_state not in self._ALLOWED[current.state]:
            raise ValueError(f"invalid bracket transition {current.state}->{to_state} for symbol={symbol}")
        current.state = to_state
        self._persist(runtime=current)
        return current

    def plan(
        self,
        *,
        symbol: str,
        entry_side: EntrySide,
        position_side: HedgePositionSide = "BOTH",
        entry_price: float,
        quantity: float,
        atr: float | None = None,
    ) -> PlannedBracket:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        entry_side_u = str(entry_side).upper()
        if entry_side_u not in {"BUY", "SELL"}:
            raise ValueError("entry_side must be BUY or SELL")
        side: PositionSide = "LONG" if entry_side_u == "BUY" else "SHORT"
        levels = self._planner.levels(entry_price=entry_price, side=side, atr=atr)
        uid = uuid.uuid4().hex[:8]
        return PlannedBracket(
            symbol=symbol.upper(),
            entry_side=cast(EntrySide, entry_side_u),
            position_side=position_side,
            quantity=float(quantity),
            take_profit_price=_round_price(float(levels["take_profit"])),
            stop_loss_price=_round_price(float(levels["stop_loss"])),
            tp_client_algo_id=generate_client_order_id(prefix=f"v2tp{uid}", max_length=31),
            sl_client_algo_id=generate_client_order_id(prefix=f"v2sl{uid}", max_length=31),
        )

    async def create_and_place(
        self,
        *,
        symbol: str,
        entry_side: EntrySide,
        position_side: HedgePositionSide = "BOTH",
        entry_price: float,
        quantity: float,
        atr: float | None = None,
    ) -> dict[str, Any]:
        planned = self.plan(
            symbol=symbol,
            entry_side=entry_side,
            position_side=position_side,
            entry_price=entry_price,
            quantity=quantity,
            atr=atr,
        )
        self._persist(
            runtime=BracketRuntime(
                symbol=planned.symbol,
                tp_order_client_id=planned.tp_client_algo_id,
                sl_order_client_id=planned.sl_client_algo_id,
                state="CREATED",
            )
        )

        exit_side: EntrySide = "SELL" if planned.entry_side == "BUY" else "BUY"
        tp_payload = {
            "algoType": "CONDITIONAL",
            "symbol": planned.symbol,
            "side": exit_side,
            "type": "TAKE_PROFIT_MARKET",
            "positionSide": planned.position_side,
            "quantity": str(planned.quantity),
            "triggerPrice": str(planned.take_profit_price),
            "workingType": self._planner.cfg.working_type,
            "priceProtect": "TRUE" if self._planner.cfg.price_protect else "FALSE",
            "reduceOnly": "true",
            "clientAlgoId": planned.tp_client_algo_id,
        }
        sl_payload = {
            "algoType": "CONDITIONAL",
            "symbol": planned.symbol,
            "side": exit_side,
            "type": "STOP_MARKET",
            "positionSide": planned.position_side,
            "quantity": str(planned.quantity),
            "triggerPrice": str(planned.stop_loss_price),
            "workingType": self._planner.cfg.working_type,
            "priceProtect": "TRUE" if self._planner.cfg.price_protect else "FALSE",
            "reduceOnly": "true",
            "clientAlgoId": planned.sl_client_algo_id,
        }

        self._transition(symbol=planned.symbol, to_state="PLACED")
        tp_resp: dict[str, Any] = {}
        sl_resp: dict[str, Any] = {}
        if self._mode == "live":
            if self._rest is None:
                raise ValueError("live mode requires rest_client")
            tp_resp = await self._rest.place_algo_order(params=tp_payload)
            sl_resp = await self._rest.place_algo_order(params=sl_payload)
        else:
            self._print_shadow_payloads(
                symbol=planned.symbol,
                tp_payload=tp_payload,
                sl_payload=sl_payload,
            )
        self._transition(symbol=planned.symbol, to_state="ACTIVE")

        return {
            "planned": asdict(planned),
            "tp_payload": tp_payload,
            "sl_payload": sl_payload,
            "tp_response": tp_resp,
            "sl_response": sl_resp,
        }

    async def on_leg_filled(self, *, symbol: str, filled_client_algo_id: str) -> BracketRuntime:
        runtime = self._get_runtime(symbol=symbol)
        if runtime is None:
            raise ValueError(f"bracket missing for symbol={symbol}")
        if runtime.state != "ACTIVE":
            return runtime

        filled = filled_client_algo_id.strip()
        counterpart: str | None = None
        if filled == (runtime.tp_order_client_id or ""):
            self._transition(symbol=symbol, to_state="TP_HIT")
            counterpart = runtime.sl_order_client_id
        elif filled == (runtime.sl_order_client_id or ""):
            self._transition(symbol=symbol, to_state="SL_HIT")
            counterpart = runtime.tp_order_client_id
        else:
            return runtime

        if self._mode == "live" and self._rest is not None and counterpart:
            await self._rest.cancel_algo_order(params={"symbol": symbol.upper(), "clientAlgoId": counterpart})

        cleaned = BracketRuntime(symbol=symbol.upper(), tp_order_client_id=None, sl_order_client_id=None, state="CLEANED")
        self._persist(runtime=cleaned)
        return cleaned

    async def cleanup_if_flat(self, *, symbol: str, position_amt: float) -> BracketRuntime | None:
        if abs(position_amt) > 0:
            return None
        runtime = self._get_runtime(symbol=symbol)
        if runtime is None:
            return None

        if self._mode == "live" and self._rest is not None:
            symbol_u = symbol.upper()
            open_orders = await self._rest.get_open_algo_orders(symbol=symbol_u)
            cancel_targets: set[str] = set()
            for row in open_orders:
                cid = _extract_client_algo_id(row)
                if cid is not None:
                    cancel_targets.add(cid)
            if runtime.tp_order_client_id:
                cancel_targets.add(runtime.tp_order_client_id)
            if runtime.sl_order_client_id:
                cancel_targets.add(runtime.sl_order_client_id)
            for cid in sorted(cancel_targets):
                await self._rest.cancel_algo_order(params={"symbol": symbol_u, "clientAlgoId": cid})

        cleaned = BracketRuntime(symbol=symbol.upper(), tp_order_client_id=None, sl_order_client_id=None, state="CLEANED")
        self._persist(runtime=cleaned)
        return cleaned

    async def recover(self, *, symbol: str | None = None) -> list[BracketRuntime]:
        rows = self._storage.list_bracket_states()
        if symbol is not None:
            sym = symbol.upper()
            rows = [row for row in rows if str(row.get("symbol") or "").upper() == sym]

        if self._mode == "shadow" or self._rest is None:
            return [
                BracketRuntime(
                    symbol=str(row.get("symbol") or "").upper(),
                    tp_order_client_id=str(row.get("tp_order_client_id")) if row.get("tp_order_client_id") is not None else None,
                    sl_order_client_id=str(row.get("sl_order_client_id")) if row.get("sl_order_client_id") is not None else None,
                    state=_as_bracket_state(str(row.get("state") or "CREATED")),
                )
                for row in rows
            ]

        open_orders = await self._rest.get_open_algo_orders(symbol=symbol.upper() if symbol else None)
        open_ids: set[str] = set()
        for row in open_orders:
            cid = _extract_client_algo_id(row)
            if cid is not None:
                open_ids.add(cid)

        recovered: list[BracketRuntime] = []
        for row in rows:
            runtime = BracketRuntime(
                symbol=str(row.get("symbol") or "").upper(),
                tp_order_client_id=str(row.get("tp_order_client_id")) if row.get("tp_order_client_id") is not None else None,
                sl_order_client_id=str(row.get("sl_order_client_id")) if row.get("sl_order_client_id") is not None else None,
                state=_as_bracket_state(str(row.get("state") or "CREATED")),
            )
            if runtime.state != "CLEANED":
                tp_open = runtime.tp_order_client_id is not None and runtime.tp_order_client_id in open_ids
                sl_open = runtime.sl_order_client_id is not None and runtime.sl_order_client_id in open_ids
                if tp_open or sl_open:
                    runtime.state = "ACTIVE"
                    self._persist(runtime=runtime)
                else:
                    runtime.state = "CLEANED"
                    runtime.tp_order_client_id = None
                    runtime.sl_order_client_id = None
                    self._persist(runtime=runtime)
            recovered.append(runtime)
        return recovered

    async def reconcile_open_algo_orders(self, *, symbol: str | None = None) -> list[BracketRuntime]:
        return await self.recover(symbol=symbol)


class BracketReconcilePoller:
    def __init__(
        self,
        *,
        service: BracketService,
        symbol: str | None = None,
        interval_seconds: float = 15.0,
    ) -> None:
        self._service = service
        self._symbol = symbol
        self._interval_seconds = max(interval_seconds, 0.5)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> list[BracketRuntime]:
        return await self._service.reconcile_open_algo_orders(symbol=self._symbol)

    async def run_forever(self) -> None:
        while not self._stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        await self._task
