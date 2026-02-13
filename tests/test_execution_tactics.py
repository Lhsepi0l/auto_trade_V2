from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

import pytest

from apps.trader_engine.domain.enums import Direction, EngineState, ExecHint
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.execution_service import ExecutionRejected, ExecutionService


@dataclass
class _State:
    state: EngineState


class DummyEngine:
    def __init__(self, state: EngineState = EngineState.RUNNING) -> None:
        self._st = _State(state=state)

    def get_state(self) -> _State:
        return self._st


class DummyRisk:
    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> RiskConfig:
        return self._cfg


class FakeBinanceClient:
    def __init__(self) -> None:
        self._next_oid = 1
        self._orders: Dict[int, Dict[str, Any]] = {}
        self._book: Dict[str, Dict[str, Any]] = {}
        self._scenario: str = "fill"
        self._polls: Dict[int, int] = {}

    # --- misc ---
    def refresh_time_offset(self) -> int:
        return 0

    def get_position_mode_one_way(self) -> bool:
        return True

    def get_open_positions_any(self) -> Dict[str, Dict[str, float]]:
        return {}

    def get_open_orders_usdtm(self, symbols):  # type: ignore[no-untyped-def]
        out = {str(s).upper(): [] for s in symbols}
        for o in self._orders.values():
            sym = str(o.get("symbol") or "").upper()
            if sym not in out:
                continue
            if str(o.get("status") or "").upper() in {"NEW", "PARTIALLY_FILLED"}:
                out[sym].append(dict(o))
        return out

    def cancel_all_open_orders(self, *, symbol: str) -> List[Mapping[str, Any]]:
        # Mark all open orders for the symbol as CANCELED.
        for o in self._orders.values():
            if o.get("symbol") == symbol and o.get("status") in ("NEW", "PARTIALLY_FILLED"):
                o["status"] = "CANCELED"
        return []

    def get_book_ticker(self, symbol: str) -> Mapping[str, Any]:
        return self._book.get(symbol, {"bidPrice": "0", "askPrice": "0"})

    def get_symbol_filters(self, *, symbol: str) -> Mapping[str, Any]:
        return {
            "symbol": symbol,
            "step_size": 0.001,
            "min_qty": 0.001,
            "tick_size": 0.1,
            "min_notional": 1.0,
        }

    def get_exchange_info_cached(self) -> Mapping[str, Any]:
        return {"symbols": [{"symbol": "BTCUSDT"}]}

    def get_account_balance_usdtm(self) -> Mapping[str, Any]:
        return {"wallet": 1000.0, "available": 1000.0}

    def get_mark_price(self, symbol: str) -> Mapping[str, Any]:
        return {"symbol": symbol, "markPrice": "100"}

    # --- order endpoints ---
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
        oid = self._next_oid
        self._next_oid += 1
        o = {
            "symbol": symbol,
            "orderId": oid,
            "clientOrderId": str(new_client_order_id) if new_client_order_id else None,
            "side": side,
            "type": "LIMIT",
            "status": "NEW",
            "price": str(price),
            "origQty": str(quantity),
            "executedQty": "0",
        }
        self._orders[oid] = o
        self._polls[oid] = 0
        return o

    def get_order(self, *, symbol: str, order_id: int) -> Mapping[str, Any]:
        o = dict(self._orders[order_id])
        self._polls[order_id] = self._polls.get(order_id, 0) + 1

        if self._scenario == "fill":
            o["status"] = "FILLED"
            o["executedQty"] = o["origQty"]
        elif self._scenario == "partial_then_cancel":
            # For the first order only: partial, never filled.
            if order_id == 1:
                o["status"] = "PARTIALLY_FILLED"
                o["executedQty"] = "0.004"
            else:
                o["status"] = "FILLED"
                o["executedQty"] = o["origQty"]
        elif self._scenario == "never_fill":
            o["status"] = "NEW"
            o["executedQty"] = "0"
        else:
            raise AssertionError("unknown scenario")

        # Persist back
        self._orders[order_id] = dict(o)
        return o

    def place_order_market(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
        new_client_order_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        oid = self._next_oid
        self._next_oid += 1
        o = {
            "symbol": symbol,
            "orderId": oid,
            "clientOrderId": str(new_client_order_id) if new_client_order_id else None,
            "side": side,
            "type": "MARKET",
            "status": "FILLED",
            "price": "0",
            "origQty": str(quantity),
            "executedQty": str(quantity),
        }
        self._orders[oid] = o
        return o


def _mk_cfg(
    *,
    exec_limit_timeout_sec: float = 0.01,
    exec_limit_retries: int = 2,
    spread_max_pct: float = 0.0015,
    allow_market_when_wide_spread: bool = False,
) -> RiskConfig:
    return RiskConfig(
        per_trade_risk_pct=1,
        max_exposure_pct=0.2,
        max_notional_pct=50,
        max_leverage=5,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=60,
        exec_limit_timeout_sec=exec_limit_timeout_sec,
        exec_limit_retries=exec_limit_retries,
        spread_max_pct=spread_max_pct,
        allow_market_when_wide_spread=allow_market_when_wide_spread,
    )


def _mk_service(client: FakeBinanceClient, cfg: RiskConfig) -> ExecutionService:
    return ExecutionService(
        client=client,  # type: ignore[arg-type]
        engine=DummyEngine(),
        risk=DummyRisk(cfg),  # type: ignore[arg-type]
        pnl=None,
        policy=None,
        notifier=None,
        allowed_symbols=["BTCUSDT"],
        dry_run=False,
    )


@pytest.mark.asyncio
async def test_limit_fills_without_market_fallback() -> None:
    c = FakeBinanceClient()
    c._scenario = "fill"
    c._book["BTCUSDT"] = {"bidPrice": "99", "askPrice": "100"}
    s = _mk_service(c, _mk_cfg())
    out = await s.enter_position(
        {"symbol": "BTCUSDT", "direction": Direction.LONG, "exec_hint": ExecHint.LIMIT, "qty": 0.01}
    )
    assert out["symbol"] == "BTCUSDT"
    assert out["hint"] == "LIMIT"
    assert out.get("market_fallback_used") in (False, None)


@pytest.mark.asyncio
async def test_partial_fill_then_retry_limit_remaining() -> None:
    c = FakeBinanceClient()
    c._scenario = "partial_then_cancel"
    c._book["BTCUSDT"] = {"bidPrice": "99", "askPrice": "100"}
    s = _mk_service(c, _mk_cfg(exec_limit_timeout_sec=0.01, exec_limit_retries=2))
    out = await s.enter_position(
        {"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.01}
    )
    assert out["symbol"] == "BTCUSDT"
    assert out["hint"] == "LIMIT"
    assert out.get("market_fallback_used") is False


@pytest.mark.asyncio
async def test_limit_then_market_fallback_on_failure() -> None:
    c = FakeBinanceClient()
    c._scenario = "never_fill"
    c._book["BTCUSDT"] = {"bidPrice": "99", "askPrice": "100"}  # spread ok
    s = _mk_service(c, _mk_cfg(exec_limit_timeout_sec=0.01, exec_limit_retries=2, spread_max_pct=0.05))
    out = await s.enter_position(
        {"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.01}
    )
    assert out["symbol"] == "BTCUSDT"
    assert out["hint"] == "LIMIT"
    assert out.get("market_fallback_used") is True


@pytest.mark.asyncio
async def test_market_fallback_blocked_by_spread_guard() -> None:
    c = FakeBinanceClient()
    c._scenario = "never_fill"
    c._book["BTCUSDT"] = {"bidPrice": "90", "askPrice": "110"}  # very wide
    s = _mk_service(
        c,
        _mk_cfg(exec_limit_timeout_sec=0.01, exec_limit_retries=2, spread_max_pct=0.001, allow_market_when_wide_spread=False),
    )
    with pytest.raises(ExecutionRejected) as ei:
        _ = await s.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.01})
    assert "market_fallback_blocked_by_spread_guard" in ei.value.message


@pytest.mark.asyncio
async def test_dry_run_enter_has_no_side_effects() -> None:
    class SideEffectClient(FakeBinanceClient):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        def get_open_positions_any(self) -> Dict[str, Dict[str, float]]:
            # If execution tries to enforce single-asset rule in dry_run (close), we'd see side effects.
            return {"ETHUSDT": {"position_amt": 1.0, "entry_price": 0.0, "unrealized_pnl": 0.0, "leverage": 1.0}}

        def cancel_all_open_orders(self, *, symbol: str) -> List[Mapping[str, Any]]:
            raise AssertionError("dry_run should not cancel orders")

        def place_order_market(
            self,
            *,
            symbol: str,
            side: str,
            quantity: float,
            reduce_only: bool = False,
            new_client_order_id: Optional[str] = None,
        ) -> Mapping[str, Any]:
            raise AssertionError("dry_run should not place market orders")

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
            raise AssertionError("dry_run should not place limit orders")

    c = SideEffectClient()
    c._book["BTCUSDT"] = {"bidPrice": "99", "askPrice": "100"}
    cfg = _mk_cfg()
    s = ExecutionService(
        client=c,  # type: ignore[arg-type]
        engine=DummyEngine(),
        risk=DummyRisk(cfg),  # type: ignore[arg-type]
        pnl=None,
        policy=None,
        notifier=None,
        allowed_symbols=["BTCUSDT"],
        dry_run=True,
        dry_run_strict=False,
    )
    out = await s.enter_position({"symbol": "BTCUSDT", "direction": "LONG", "exec_hint": "LIMIT", "qty": 0.01})
    assert out["dry_run"] is True
