from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from apps.trader_engine.services.market_data_service import Candle


@dataclass
class FakeBinanceRest:
    enabled_symbols: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "XAUUSDT"])
    wallet: float = 10000.0
    available: float = 10000.0
    order_id_seq: itertools.count = field(default_factory=lambda: itertools.count(1))

    def __post_init__(self) -> None:
        self.open_orders: List[Dict[str, Any]] = []
        self.positions: Dict[str, Dict[str, float]] = {}
        self.fills: List[Dict[str, Any]] = []
        self.listen_key: Optional[str] = None
        self.listen_keepalive_calls: int = 0
        self.limit_fill_mode: str = "fill_immediate"  # fill_immediate | never_fill | partial_then_fill
        self._order_poll_count: Dict[int, int] = {}
        self._book: Dict[str, Dict[str, float]] = {
            "BTCUSDT": {"bid": 100.0, "ask": 100.2, "mark": 100.1},
            "ETHUSDT": {"bid": 50.0, "ask": 50.1, "mark": 50.05},
            "XAUUSDT": {"bid": 20.0, "ask": 20.05, "mark": 20.02},
        }
        self._klines: Dict[tuple[str, str], List[List[Any]]] = {}
        self._seed_klines()

    def _seed_klines(self) -> None:
        now = int(time.time() * 1000)
        for sym in self.enabled_symbols:
            base = self._book.get(sym, {"mark": 100.0})["mark"]
            for itv in ("30m", "1h", "4h"):
                rows: List[List[Any]] = []
                p = base
                for i in range(320):
                    t = now - (320 - i) * 60_000
                    o = p
                    h = p * 1.001
                    l = p * 0.999
                    c = p * (1.0003 if (i % 3 == 0) else 0.9998)
                    v = 10.0
                    rows.append([t, str(o), str(h), str(l), str(c), str(v), t + 59_000, "0", "0", "0", "0", "0"])
                    p = c
                self._klines[(sym, itv)] = rows

    # --- time/symbol metadata ---
    def get_server_time_ms(self) -> int:
        return int(time.time() * 1000)

    def get_server_time(self) -> Mapping[str, Any]:
        return {"serverTime": self.get_server_time_ms()}

    def refresh_time_offset(self) -> int:
        return 0

    @property
    def time_sync(self):  # noqa: ANN201
        class _TS:
            @staticmethod
            def measure(*, server_time_ms: int):
                class _R:
                    offset_ms = 0
                    measured_at_ms = server_time_ms

                return _R()

        return _TS()

    def validate_symbols(self, allowed_list: Sequence[str]):
        enabled = [s for s in allowed_list if s in self.enabled_symbols]
        disabled = [{"symbol": s, "reason": "not_found"} for s in allowed_list if s not in enabled]
        return enabled, disabled

    def get_exchange_info(self) -> Mapping[str, Any]:
        return {
            "symbols": [
                {
                    "symbol": s,
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
                for s in self.enabled_symbols
            ]
        }

    def get_exchange_info_cached(self) -> Mapping[str, Any]:
        return self.get_exchange_info()

    def get_symbol_filters(self, *, symbol: str) -> Mapping[str, Any]:
        return {"symbol": symbol, "step_size": 0.001, "min_qty": 0.001, "tick_size": 0.1, "min_notional": 5.0}

    # --- market data ---
    def set_book(self, symbol: str, *, bid: float, ask: float, mark: Optional[float] = None) -> None:
        self._book[symbol] = {"bid": bid, "ask": ask, "mark": mark if mark is not None else (bid + ask) / 2.0}

    def get_book_ticker(self, symbol: str) -> Mapping[str, Any]:
        b = self._book[symbol]
        return {"symbol": symbol, "bidPrice": str(b["bid"]), "askPrice": str(b["ask"])}

    def get_mark_price(self, symbol: str) -> Mapping[str, Any]:
        b = self._book[symbol]
        return {"symbol": symbol, "markPrice": str(b["mark"])}

    def get_klines(self, *, symbol: str, interval: str, limit: int = 200):
        return list(self._klines.get((symbol, interval), []))[-int(limit) :]

    # --- account state ---
    def get_account_balance_usdtm(self) -> Dict[str, float]:
        return {"wallet": float(self.wallet), "available": float(self.available)}

    def get_positions_usdtm(self, symbols: Sequence[str]) -> Dict[str, Dict[str, float]]:
        return {s: v for s, v in self.positions.items() if s in symbols}

    def get_open_positions_any(self) -> Dict[str, Dict[str, float]]:
        return {k: dict(v) for k, v in self.positions.items() if abs(float(v.get("position_amt", 0.0))) > 0}

    def get_open_orders_usdtm(self, symbols: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {s: [] for s in symbols}
        for o in self.open_orders:
            sym = str(o.get("symbol"))
            if sym in out and o.get("status") in {"NEW", "PARTIALLY_FILLED"}:
                out[sym].append(dict(o))
        return out

    # --- order execution ---
    def get_position_mode_one_way(self) -> bool:
        return True

    def place_order_market(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
        new_client_order_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        oid = next(self.order_id_seq)
        px = float(self._book[symbol]["mark"])
        qty = float(quantity)
        executed = qty
        self._apply_fill(symbol=symbol, side=side, qty=executed, price=px, reduce_only=reduce_only)
        o = {
            "symbol": symbol,
            "orderId": oid,
            "clientOrderId": str(new_client_order_id) if new_client_order_id else None,
            "side": side,
            "type": "MARKET",
            "status": "FILLED",
            "price": str(px),
            "avgPrice": str(px),
            "origQty": str(qty),
            "executedQty": str(executed),
            "updateTime": int(time.time() * 1000),
        }
        return o

    def place_order_limit(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        post_only: bool = False,
        reduce_only: bool = False,
        new_client_order_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        oid = next(self.order_id_seq)
        o = {
            "symbol": symbol,
            "orderId": oid,
            "clientOrderId": str(new_client_order_id) if new_client_order_id else None,
            "side": side,
            "type": "LIMIT",
            "status": "NEW",
            "price": str(price),
            "avgPrice": "0",
            "origQty": str(quantity),
            "executedQty": "0",
            "reduceOnly": bool(reduce_only),
            "updateTime": int(time.time() * 1000),
        }
        self.open_orders.append(o)
        self._order_poll_count[oid] = 0
        if self.limit_fill_mode == "fill_immediate":
            self._mark_filled(o, executed=float(quantity))
        return dict(o)

    def get_order(self, *, symbol: str, order_id: int) -> Mapping[str, Any]:
        for o in self.open_orders:
            if int(o["orderId"]) == int(order_id):
                self._order_poll_count[order_id] = self._order_poll_count.get(order_id, 0) + 1
                polls = self._order_poll_count[order_id]
                if self.limit_fill_mode == "partial_then_fill":
                    if polls == 1 and o["status"] == "NEW":
                        orig = float(o["origQty"])
                        self._mark_partial(o, executed=max(orig * 0.5, 0.001))
                    elif polls >= 2 and o["status"] in {"NEW", "PARTIALLY_FILLED"}:
                        self._mark_filled(o, executed=float(o["origQty"]))
                return dict(o)
        return {"symbol": symbol, "orderId": order_id, "status": "CANCELED"}

    def get_order_by_client_order_id(self, *, symbol: str, client_order_id: str) -> Mapping[str, Any]:
        for o in self.open_orders:
            if str(o.get("symbol")) == str(symbol) and str(o.get("clientOrderId") or "") == str(client_order_id):
                return dict(o)
        for o in self.fills:
            if str(o.get("symbol")) == str(symbol) and str(o.get("clientOrderId") or "") == str(client_order_id):
                return dict(o)
        return {"symbol": symbol, "clientOrderId": client_order_id, "status": "CANCELED"}

    def cancel_all_open_orders(self, *, symbol: str):
        canceled = []
        for o in self.open_orders:
            if o["symbol"] == symbol and o["status"] in {"NEW", "PARTIALLY_FILLED"}:
                o["status"] = "CANCELED"
                o["updateTime"] = int(time.time() * 1000)
                canceled.append(dict(o))
        return canceled

    def _mark_partial(self, order: Dict[str, Any], *, executed: float) -> None:
        orig = float(order["origQty"])
        ex = min(max(float(executed), 0.0), orig)
        order["status"] = "PARTIALLY_FILLED"
        order["executedQty"] = str(ex)
        order["avgPrice"] = order["price"]
        order["updateTime"] = int(time.time() * 1000)
        self._apply_fill(
            symbol=str(order["symbol"]),
            side=str(order["side"]),
            qty=ex,
            price=float(order["price"]),
            reduce_only=bool(order.get("reduceOnly", False)),
        )

    def _mark_filled(self, order: Dict[str, Any], *, executed: float) -> None:
        orig = float(order["origQty"])
        ex = min(max(float(executed), 0.0), orig)
        order["status"] = "FILLED"
        order["executedQty"] = str(ex)
        order["avgPrice"] = order["price"]
        order["updateTime"] = int(time.time() * 1000)
        self._apply_fill(
            symbol=str(order["symbol"]),
            side=str(order["side"]),
            qty=ex,
            price=float(order["price"]),
            reduce_only=bool(order.get("reduceOnly", False)),
        )

    def _apply_fill(self, *, symbol: str, side: str, qty: float, price: float, reduce_only: bool) -> None:
        pos = self.positions.get(symbol, {"position_amt": 0.0, "entry_price": 0.0, "unrealized_pnl": 0.0, "leverage": 1.0})
        amt = float(pos.get("position_amt", 0.0))
        signed = qty if side.upper() == "BUY" else -qty
        if reduce_only:
            # reduce position towards 0
            if amt > 0:
                amt = max(0.0, amt - qty)
            elif amt < 0:
                amt = min(0.0, amt + qty)
            if abs(amt) <= 0:
                self.positions.pop(symbol, None)
            else:
                pos["position_amt"] = amt
                self.positions[symbol] = pos
        else:
            new_amt = amt + signed
            if abs(new_amt) > 0:
                pos["position_amt"] = new_amt
                pos["entry_price"] = price
                self.positions[symbol] = pos
        self.fills.append({"symbol": symbol, "side": side, "qty": qty, "price": price, "reduce_only": reduce_only, "ts": int(time.time() * 1000)})

    # --- user stream listenKey ---
    def start_user_stream(self) -> str:
        self.listen_key = self.listen_key or "test-listen-key"
        return self.listen_key

    def keepalive_user_stream(self, *, listen_key: str) -> None:
        if listen_key != self.listen_key:
            raise RuntimeError("invalid_listen_key")
        self.listen_keepalive_calls += 1

    def close_user_stream(self, *, listen_key: str) -> None:
        if listen_key == self.listen_key:
            self.listen_key = None

    # --- compatibility ---
    def close(self) -> None:
        return None


def fake_candle_series(symbol: str, interval: str, count: int = 260, base: float = 100.0) -> List[Candle]:
    now = int(time.time() * 1000)
    out: List[Candle] = []
    p = float(base)
    for i in range(count):
        t = now - (count - i) * 60_000
        o = p
        h = p * 1.001
        l = p * 0.999
        c = p * (1.0005 if i % 2 == 0 else 0.9995)
        out.append(Candle(open_time_ms=t, open=o, high=h, low=l, close=c, volume=10.0, close_time_ms=t + 59_000))
        p = c
    return out
