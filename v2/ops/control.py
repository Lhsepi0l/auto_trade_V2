from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from v2.engine.state import EngineStateStore


class OpsExchangeClient(Protocol):
    async def cancel_all_open_orders(self, *, symbol: str) -> dict[str, Any]: ...

    async def get_open_orders(self) -> list[dict[str, Any]]: ...

    async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]: ...

    async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]: ...

    async def get_positions(self) -> list[dict[str, Any]]: ...

    async def place_reduce_only_market_order(
        self,
        *,
        symbol: str,
        side: Literal["BUY", "SELL"],
        quantity: float,
        position_side: Literal["BOTH", "LONG", "SHORT"] = "BOTH",
    ) -> dict[str, Any]: ...


@dataclass
class FlattenResult:
    symbol: str
    paused: bool
    safe_mode: bool
    open_regular_orders: int
    open_algo_orders: int
    position_amt: float


@dataclass
class OpsController:
    state_store: EngineStateStore
    exchange: OpsExchangeClient | None = None

    def pause(self) -> None:
        self.state_store.apply_ops_mode(paused=True, reason="ops.pause")

    def resume(self) -> None:
        self.state_store.apply_ops_mode(paused=False, safe_mode=False, reason="ops.resume")

    def safe_mode(self) -> None:
        self.state_store.apply_ops_mode(paused=True, safe_mode=True, reason="ops.safe_mode")

    def can_open_new_entries(self) -> bool:
        ops = self.state_store.get().operational
        return (not ops.paused) and (not ops.safe_mode)

    async def flatten(
        self,
        *,
        symbol: str,
        retries: int = 10,
        retry_delay_sec: float = 0.2,
        latch_ops_mode: bool = True,
    ) -> FlattenResult:
        symbol_u = symbol.upper()

        if latch_ops_mode:
            self.state_store.apply_ops_mode(paused=True, safe_mode=True, reason=f"ops.flatten:{symbol_u}")

        if self.exchange is None:
            ops = self.state_store.get().operational
            return FlattenResult(
                symbol=symbol_u,
                paused=bool(ops.paused),
                safe_mode=bool(ops.safe_mode),
                open_regular_orders=0,
                open_algo_orders=0,
                position_amt=0.0,
            )

        await self.exchange.cancel_all_open_orders(symbol=symbol_u)

        open_algos = await self.exchange.get_open_algo_orders(symbol=symbol_u)
        for algo in open_algos:
            cancel_params = self._algo_cancel_params(symbol=symbol_u, row=algo)
            if cancel_params is None:
                continue
            await self.exchange.cancel_algo_order(params=cancel_params)

        position_amt = await self._position_amt(symbol=symbol_u)
        if abs(position_amt) > 0:
            side: Literal["BUY", "SELL"] = "SELL" if position_amt > 0 else "BUY"
            await self.exchange.place_reduce_only_market_order(
                symbol=symbol_u,
                side=side,
                quantity=abs(position_amt),
            )

        for _ in range(retries):
            open_regular = await self._open_regular_for_symbol(symbol=symbol_u)
            open_algo = await self.exchange.get_open_algo_orders(symbol=symbol_u)
            pos = await self._position_amt(symbol=symbol_u)
            if not open_regular and not open_algo and abs(pos) <= 0:
                self.state_store.apply_reconciliation(
                    open_orders=await self.exchange.get_open_orders(),
                    positions=await self.exchange.get_positions(),
                    balances=[],
                    reason=f"ops.flatten.verify:{symbol_u}",
                )
                ops = self.state_store.get().operational
                return FlattenResult(
                    symbol=symbol_u,
                    paused=bool(ops.paused),
                    safe_mode=bool(ops.safe_mode),
                    open_regular_orders=0,
                    open_algo_orders=0,
                    position_amt=0.0,
                )
            await asyncio.sleep(retry_delay_sec)

        open_regular = await self._open_regular_for_symbol(symbol=symbol_u)
        open_algo = await self.exchange.get_open_algo_orders(symbol=symbol_u)
        pos = await self._position_amt(symbol=symbol_u)
        raise RuntimeError(
            f"flatten verification failed symbol={symbol_u} open_regular={len(open_regular)} "
            f"open_algo={len(open_algo)} position_amt={pos}"
        )

    def _algo_cancel_params(self, *, symbol: str, row: dict[str, Any]) -> dict[str, Any] | None:
        client_algo_id = row.get("clientAlgoId")
        if client_algo_id is not None and str(client_algo_id).strip():
            return {"symbol": symbol, "clientAlgoId": str(client_algo_id)}
        algo_id = row.get("algoId")
        if algo_id is not None and str(algo_id).strip():
            return {"symbol": symbol, "algoId": str(algo_id)}
        return None

    async def _open_regular_for_symbol(self, *, symbol: str) -> list[dict[str, Any]]:
        rows = await self.exchange.get_open_orders() if self.exchange is not None else []
        return [r for r in rows if str(r.get("symbol") or "").upper() == symbol]

    async def _position_amt(self, *, symbol: str) -> float:
        rows = await self.exchange.get_positions() if self.exchange is not None else []
        total = 0.0
        for row in rows:
            if str(row.get("symbol") or "").upper() != symbol:
                continue
            raw = row.get("positionAmt") if row.get("positionAmt") is not None else row.get("pa")
            if raw is None:
                continue
            try:
                total += float(str(raw))
            except (TypeError, ValueError):
                continue
        return total
