from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from v2.clean_room.contracts import (
    Candidate,
    ExecutionResult,
    KernelContext,
    RiskDecision,
    SizePlan,
)
from v2.common.async_bridge import run_async_blocking
from v2.exchange import BinanceRESTError
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
        self._symbol_rule_cache: dict[str, tuple[float, float, float | None]] = {}

    @staticmethod
    def _floor_to_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        v = Decimal(str(value))
        s = Decimal(str(step))
        return float((v / s).to_integral_value(rounding=ROUND_DOWN) * s)

    @staticmethod
    def _ceil_to_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        v = Decimal(str(value))
        s = Decimal(str(step))
        return float((v / s).to_integral_value(rounding=ROUND_UP) * s)

    def _resolve_symbol_rules(self, symbol: str) -> tuple[float, float, float | None]:
        cached = self._symbol_rule_cache.get(symbol)
        if cached is not None:
            return cached

        payload = self._run(
            lambda: self._rest.public_request(
                "GET",
                "/fapi/v1/exchangeInfo",
                params={"symbol": symbol},
            )
        )
        if not isinstance(payload, dict):
            raise RuntimeError("exchange_info_invalid")

        symbols_raw = payload.get("symbols")
        if not isinstance(symbols_raw, list) or not symbols_raw:
            raise RuntimeError("exchange_info_missing_symbol")
        symbol_row = symbols_raw[0]
        if not isinstance(symbol_row, dict):
            raise RuntimeError("exchange_info_symbol_invalid")

        filters_raw = symbol_row.get("filters")
        if not isinstance(filters_raw, list):
            raise RuntimeError("exchange_info_filters_missing")

        step_size = 0.0
        min_qty = 0.0
        min_notional: float | None = None

        for row in filters_raw:
            if not isinstance(row, dict):
                continue
            ft = str(row.get("filterType") or "").upper()
            if ft in {"LOT_SIZE", "MARKET_LOT_SIZE"}:
                step_size = max(step_size, float(row.get("stepSize") or 0.0))
                min_qty = max(min_qty, float(row.get("minQty") or 0.0))
            elif ft in {"MIN_NOTIONAL", "NOTIONAL"}:
                value = row.get("notional")
                if value is None:
                    value = row.get("minNotional")
                if value is not None:
                    parsed = float(value)
                    min_notional = parsed if min_notional is None else max(min_notional, parsed)

        rules = (step_size, min_qty, min_notional)
        self._symbol_rule_cache[symbol] = rules
        return rules

    def _fetch_available_usdt(self) -> float | None:
        payload = self._run(lambda: self._rest.get_balances())
        if not isinstance(payload, list):
            return None

        target: dict[str, Any] | None = None
        for item in payload:
            if not isinstance(item, dict):
                continue
            asset = str(item.get("asset") or item.get("coin") or "").upper()
            if asset == "USDT":
                target = item
                break

        if target is None:
            return None

        raw = (
            target.get("availableBalance")
            or target.get("withdrawAvailable")
            or target.get("balance")
        )
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

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
            step_size, min_qty, min_notional = self._resolve_symbol_rules(symbol)

            qty = float(size.qty)
            if step_size > 0:
                qty = self._floor_to_step(qty, step_size)
            if min_notional is not None and candidate.entry_price and candidate.entry_price > 0:
                required_qty = float(min_notional) / float(candidate.entry_price)
                if step_size > 0:
                    required_qty = self._ceil_to_step(required_qty, step_size)
                qty = max(qty, required_qty)
            if min_qty > 0 and qty < min_qty:
                return ExecutionResult(ok=False, reason="quantity_below_min_qty")

            if candidate.entry_price is not None and candidate.entry_price > 0 and leverage_int > 0:
                est_notional = qty * float(candidate.entry_price)
                required_margin = est_notional / float(leverage_int)
                fee_buffer = required_margin * 0.01
                available_usdt = self._fetch_available_usdt()
                if available_usdt is not None and (required_margin + fee_buffer) > available_usdt:
                    return ExecutionResult(
                        ok=False,
                        reason=(
                            "insufficient_available_margin:"
                            f"required={required_margin + fee_buffer:.6f},"
                            f"available={available_usdt:.6f}"
                        ),
                    )

            payload["quantity"] = f"{qty:.8f}"
            _ = self._run(lambda: self._rest.change_leverage(symbol=symbol, leverage=leverage_int))
            resp = self._run(lambda: self._rest.place_order(params=payload))
        except BinanceRESTError as exc:
            code = exc.code if exc.code is not None else "none"
            return ExecutionResult(ok=False, reason=f"live_order_failed:BinanceRESTError:{code}")
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(ok=False, reason=f"live_order_failed:{type(exc).__name__}")

        if not isinstance(resp, dict):
            return ExecutionResult(ok=False, reason="live_order_bad_response")

        order_id = resp.get("orderId") or resp.get("clientOrderId") or client_order_id
        return ExecutionResult(ok=True, order_id=str(order_id), reason="live_order_submitted")

    @staticmethod
    def _run(thunk):  # type: ignore[no-untyped-def]
        return run_async_blocking(thunk)
