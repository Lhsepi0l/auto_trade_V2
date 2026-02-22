from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from v2.clean_room.contracts import (
    Candidate,
    ExecutionResult,
    KernelContext,
    RiskDecision,
    SizePlan,
)
from v2.exchange.client_order_id import generate_client_order_id


@dataclass(frozen=True)
class NoopCandidateSelector:
    def select(self, *, context: KernelContext) -> Candidate | None:
        _ = context
        return None


@dataclass(frozen=True)
class AlwaysAllowedRiskGate:
    reason: str = "default_allow"

    def evaluate(self, *, candidate: Candidate, context: KernelContext) -> RiskDecision:
        _ = candidate
        _ = context
        return RiskDecision(allow=True, reason=self.reason, max_notional=None)


@dataclass(frozen=True)
class FixedNotionalSizer:
    fallback_notional: float = 10.0
    leverage: float = 1.0
    max_notional: float | None = None

    def size(
        self,
        *,
        candidate: Candidate,
        risk: RiskDecision,
        context: KernelContext,
    ) -> SizePlan:
        _ = risk
        if candidate.entry_price is None or candidate.entry_price <= 0:
            return SizePlan(
                symbol=candidate.symbol,
                qty=0.0,
                leverage=self.leverage,
                notional=0.0,
                reason="invalid_entry_price",
            )

        if candidate.score <= 0:
            return SizePlan(
                symbol=candidate.symbol,
                qty=0.0,
                leverage=self.leverage,
                notional=0.0,
                reason="non_positive_signal_score",
            )

        configured_notional = self.fallback_notional
        if self.max_notional and self.max_notional > 0:
            configured_notional = min(configured_notional, self.max_notional)
        if risk.max_notional and risk.max_notional > 0:
            configured_notional = min(configured_notional, risk.max_notional)
        if context.dry_run:
            configured_notional = min(configured_notional, 1.0)

        qty = configured_notional / candidate.entry_price
        return SizePlan(
            symbol=candidate.symbol,
            qty=qty,
            leverage=self.leverage,
            notional=configured_notional,
            reason="size_ok",
        )


class DynamicNotionalSizer:
    def __init__(
        self,
        *,
        fallback_notional: float = 10.0,
        default_leverage: float = 1.0,
        max_notional: float | None = None,
    ) -> None:
        self._fallback_notional = float(fallback_notional)
        self._default_leverage = float(default_leverage)
        self._max_notional = max_notional
        self._max_leverage = float(default_leverage)
        self._symbol_leverage_map: dict[str, float] = {}

    def set_leverage_config(
        self, *, symbol_leverage_map: dict[str, float], max_leverage: float
    ) -> None:
        self._symbol_leverage_map = {
            str(sym).upper(): float(lev)
            for sym, lev in symbol_leverage_map.items()
            if str(sym).strip() and float(lev) > 0
        }
        self._max_leverage = max(1.0, float(max_leverage))

    def _resolve_leverage(self, symbol: str) -> float:
        symbol_u = str(symbol).upper()
        selected = self._symbol_leverage_map.get(symbol_u, self._default_leverage)
        selected = max(1.0, float(selected))
        return min(selected, self._max_leverage)

    def size(
        self,
        *,
        candidate: Candidate,
        risk: RiskDecision,
        context: KernelContext,
    ) -> SizePlan:
        if candidate.entry_price is None or candidate.entry_price <= 0:
            return SizePlan(
                symbol=candidate.symbol,
                qty=0.0,
                leverage=self._resolve_leverage(candidate.symbol),
                notional=0.0,
                reason="invalid_entry_price",
            )

        if candidate.score <= 0:
            return SizePlan(
                symbol=candidate.symbol,
                qty=0.0,
                leverage=self._resolve_leverage(candidate.symbol),
                notional=0.0,
                reason="non_positive_signal_score",
            )

        configured_notional = self._fallback_notional
        if self._max_notional and self._max_notional > 0:
            configured_notional = min(configured_notional, self._max_notional)
        if risk.max_notional and risk.max_notional > 0:
            configured_notional = min(configured_notional, risk.max_notional)
        if context.dry_run:
            configured_notional = min(configured_notional, 1.0)

        qty = configured_notional / candidate.entry_price
        return SizePlan(
            symbol=candidate.symbol,
            qty=qty,
            leverage=self._resolve_leverage(candidate.symbol),
            notional=configured_notional,
            reason="size_ok",
        )


class ReplaySafeExecutionService:
    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._seq = 0

    def execute(
        self,
        *,
        candidate: Candidate,
        size: SizePlan,
        context: KernelContext,
    ) -> ExecutionResult:
        _ = context
        if not self._enabled or size.qty <= 0 or candidate.entry_price is None:
            return ExecutionResult(ok=False, reason="execution_disabled_or_invalid")
        self._seq += 1
        return ExecutionResult(
            ok=True,
            order_id=f"shadow-{candidate.symbol.lower()}-{self._seq}",
            reason="planned_execution",
        )


class BinanceLiveExecutionService:
    def __init__(self, *, rest_client: Any) -> None:
        self._rest = rest_client

    def execute(
        self,
        *,
        candidate: Candidate,
        size: SizePlan,
        context: KernelContext,
    ) -> ExecutionResult:
        if context.dry_run:
            return ExecutionResult(ok=False, reason="dry_run_mode")
        if self._rest is None:
            return ExecutionResult(ok=False, reason="rest_client_missing")
        if size.qty <= 0:
            return ExecutionResult(ok=False, reason="invalid_qty")

        side = str(candidate.side).upper()
        if side not in {"BUY", "SELL"}:
            return ExecutionResult(ok=False, reason="invalid_side")

        symbol = str(candidate.symbol or "").upper()
        if not symbol:
            return ExecutionResult(ok=False, reason="invalid_symbol")

        client_order_id = generate_client_order_id(prefix="v2live", max_length=31)
        payload = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": f"{float(size.qty):.8f}",
            "newClientOrderId": client_order_id,
        }

        try:
            leverage_int = int(max(1, min(125, round(float(size.leverage)))))
            _ = self._run(lambda: self._rest.change_leverage(symbol=symbol, leverage=leverage_int))
            resp = self._run(lambda: self._rest.place_order(params=payload))
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(ok=False, reason=f"live_order_failed:{type(exc).__name__}")

        if not isinstance(resp, dict):
            return ExecutionResult(ok=False, reason="live_order_bad_response")

        order_id = resp.get("orderId") or resp.get("clientOrderId") or client_order_id
        return ExecutionResult(ok=True, order_id=str(order_id), reason="live_order_submitted")

    @staticmethod
    def _run(thunk):  # type: ignore[no-untyped-def]
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(thunk())

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, thunk())
            return future.result()
