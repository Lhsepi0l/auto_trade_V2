from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
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
from v2.storage import RuntimeStorage

logger = logging.getLogger(__name__)


def _normalize_pct(value: float | None, *, default: float = 0.0) -> float:
    if value is None:
        normalized_default = abs(float(default))
        if normalized_default > 1.0:
            normalized_default = normalized_default / 100.0
        return max(normalized_default, 0.0)
    normalized = abs(float(value))
    if normalized > 1.0:
        normalized = normalized / 100.0
    return max(normalized, 0.0)


def _clamp(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _parse_iso_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
class LiveRuntimeRiskGate:
    daily_loss_limit_pct: float = 0.02
    dd_limit_pct: float = 0.15
    drawdown_scale_start_pct: float = 0.12
    drawdown_scale_max_pct: float = 0.32
    drawdown_min_factor: float = 0.35

    def _drawdown_scale(
        self,
        dd_used_pct: float,
        *,
        start_pct: float | None = None,
        max_pct: float | None = None,
        min_factor: float | None = None,
    ) -> float:
        dd_used = max(float(dd_used_pct), 0.0)
        start = max(float(start_pct if start_pct is not None else self.drawdown_scale_start_pct), 0.0)
        end = max(float(max_pct if max_pct is not None else self.drawdown_scale_max_pct), start + 1e-9)
        floor = _clamp(
            float(min_factor if min_factor is not None else self.drawdown_min_factor),
            0.1,
            1.0,
        )
        if dd_used <= start:
            return 1.0
        if dd_used >= end:
            return floor
        ratio = (dd_used - start) / (end - start)
        return 1.0 - ((1.0 - floor) * ratio)

    def evaluate(self, *, candidate: Candidate, context: KernelContext) -> RiskDecision:
        now_ts = time.time()
        cooldown_until = float(context.cooldown_until or 0.0)
        if cooldown_until > now_ts:
            return RiskDecision(
                allow=False,
                reason="cooldown_active",
                max_notional=0.0,
                size_factor=0.0,
            )

        score_min = context.risk_score_min
        if score_min is not None and float(candidate.score) < float(score_min):
            return RiskDecision(
                allow=False,
                reason="confidence_below_threshold",
                max_notional=0.0,
                size_factor=0.0,
            )

        spread_max_pct = context.spread_max_pct
        if (
            spread_max_pct is not None
            and candidate.spread_pct is not None
            and float(candidate.spread_pct) > float(spread_max_pct)
        ):
            return RiskDecision(
                allow=False,
                reason="spread_block",
                max_notional=0.0,
                size_factor=0.0,
            )

        if (
            candidate.expected_move_frac is not None
            and candidate.required_move_frac is not None
            and float(candidate.expected_move_frac) < float(candidate.required_move_frac)
        ):
            return RiskDecision(
                allow=False,
                reason="edge_below_cost",
                max_notional=0.0,
                size_factor=0.0,
            )

        daily_limit = _normalize_pct(context.daily_loss_limit_pct, default=self.daily_loss_limit_pct)
        daily_used = max(float(context.daily_loss_used_pct or 0.0), 0.0)
        if daily_limit > 0.0 and daily_used >= daily_limit:
            return RiskDecision(
                allow=False,
                reason="daily_loss_limit",
                max_notional=0.0,
                size_factor=0.0,
            )

        dd_limit = _normalize_pct(context.dd_limit_pct, default=self.dd_limit_pct)
        dd_used = max(float(context.dd_used_pct or 0.0), 0.0)
        if dd_limit > 0.0 and dd_used >= dd_limit:
            return RiskDecision(
                allow=False,
                reason="drawdown_limit",
                max_notional=0.0,
                size_factor=0.0,
            )

        size_factor = 1.0
        size_factor = min(
            size_factor,
            self._drawdown_scale(
                dd_used,
                start_pct=context.dd_scale_start_pct,
                max_pct=context.dd_scale_max_pct,
                min_factor=context.dd_scale_min_factor,
            ),
        )

        if daily_limit > 0.0 and daily_used > 0.0:
            half_limit = daily_limit * 0.5
            if daily_used > half_limit:
                ratio = (daily_used - half_limit) / max(daily_limit - half_limit, 1e-9)
                size_factor = min(size_factor, 1.0 - (0.5 * _clamp(ratio, 0.0, 1.0)))

        lose_streak = max(int(context.lose_streak or 0), 0)
        if lose_streak >= 2:
            size_factor = min(size_factor, max(0.4, 1.0 - (0.15 * float(lose_streak - 1))))

        if size_factor < 1.0:
            return RiskDecision(
                allow=True,
                reason="risk_ok_scaled",
                max_notional=None,
                size_factor=max(size_factor, 0.1),
            )
        return RiskDecision(allow=True, reason="risk_ok", max_notional=None, size_factor=1.0)


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
        self._default_leverage = self._max_leverage

    def set_notional_config(self, *, fallback_notional: float, max_notional: float | None) -> None:
        fallback = float(fallback_notional)
        if fallback > 0:
            self._fallback_notional = fallback

        if max_notional is None:
            self._max_notional = None
            return

        max_value = float(max_notional)
        self._max_notional = max_value if max_value > 0 else None

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
        leverage = self._resolve_leverage(candidate.symbol)
        if candidate.entry_price is None or candidate.entry_price <= 0:
            return SizePlan(
                symbol=candidate.symbol,
                qty=0.0,
                leverage=leverage,
                notional=0.0,
                reason="invalid_entry_price",
            )

        if candidate.score <= 0:
            return SizePlan(
                symbol=candidate.symbol,
                qty=0.0,
                leverage=leverage,
                notional=0.0,
                reason="non_positive_signal_score",
            )

        configured_notional = float(self._fallback_notional) * float(leverage)
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
            leverage=leverage,
            notional=configured_notional,
            reason="size_ok",
        )


class RiskAwareSizer(DynamicNotionalSizer):
    def _candidate_stop_frac(self, candidate: Candidate) -> float | None:
        entry_price = float(candidate.entry_price or 0.0)
        if entry_price <= 0.0:
            return None
        if candidate.stop_distance_frac is not None and float(candidate.stop_distance_frac) > 0.0:
            return max(float(candidate.stop_distance_frac), 0.0)
        if candidate.stop_price_hint is not None and float(candidate.stop_price_hint) > 0.0:
            return max(abs(entry_price - float(candidate.stop_price_hint)) / entry_price, 0.0)
        if candidate.volatility_hint is not None and float(candidate.volatility_hint) > 0.0:
            return max(float(candidate.volatility_hint) / entry_price, 0.0)
        if candidate.required_move_frac is not None and float(candidate.required_move_frac) > 0.0:
            return max(float(candidate.required_move_frac), 0.0)
        return None

    def size(
        self,
        *,
        candidate: Candidate,
        risk: RiskDecision,
        context: KernelContext,
    ) -> SizePlan:
        entry_price = float(candidate.entry_price or 0.0)
        leverage = self._resolve_leverage(candidate.symbol)
        if candidate.max_effective_leverage is not None and candidate.max_effective_leverage > 0.0:
            leverage = min(leverage, float(candidate.max_effective_leverage))
        leverage = max(leverage, 1.0)

        if entry_price <= 0.0:
            return SizePlan(
                symbol=candidate.symbol,
                qty=0.0,
                leverage=leverage,
                notional=0.0,
                reason="invalid_entry_price",
            )

        if candidate.score <= 0:
            return SizePlan(
                symbol=candidate.symbol,
                qty=0.0,
                leverage=leverage,
                notional=0.0,
                reason="non_positive_signal_score",
            )

        capital_base = float(self._fallback_notional)
        configured_notional = capital_base * leverage
        if self._max_notional and self._max_notional > 0:
            configured_notional = min(configured_notional, float(self._max_notional))
        if risk.max_notional and risk.max_notional > 0:
            configured_notional = min(configured_notional, float(risk.max_notional))

        size_factor = float(risk.size_factor or 1.0)
        if size_factor > 0.0:
            configured_notional *= size_factor

        if context.dry_run:
            configured_notional = min(configured_notional, 1.0)

        if configured_notional <= 0.0:
            return SizePlan(
                symbol=candidate.symbol,
                qty=0.0,
                leverage=leverage,
                notional=0.0,
                reason="risk_budget_exhausted",
            )

        qty = configured_notional / entry_price
        return SizePlan(
            symbol=candidate.symbol,
            qty=qty,
            leverage=leverage,
            notional=configured_notional,
            reason="risk_aware_size_ok",
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
    def __init__(
        self,
        *,
        rest_client: Any,
        storage: RuntimeStorage | None = None,
        idempotency_window_sec: float = 30.0,
        idempotency_enabled: bool = True,
    ) -> None:
        self._rest = rest_client
        self._storage = storage
        self._idempotency_window_sec = max(float(idempotency_window_sec), 1.0)
        self._idempotency_enabled = bool(idempotency_enabled)
        self._symbol_rule_cache: dict[str, tuple[float, float, float | None]] = {}

    @staticmethod
    def _is_ambiguous_submit_error(exc: Exception) -> bool:
        if isinstance(exc, FutureTimeoutError):
            return True
        if isinstance(exc, BinanceRESTError):
            if exc.status_code >= 500:
                return True
            if exc.code in {-1001, -1006, -1007, -1016, -1021}:
                return True
            return False
        return isinstance(exc, (TimeoutError, ConnectionError, OSError, RuntimeError))

    def _query_order_by_client_order_id(
        self,
        *,
        symbol: str,
        client_order_id: str,
    ) -> dict[str, Any] | None:
        try:
            payload = self._run(
                lambda: self._rest.signed_request(
                    "GET",
                    "/fapi/v1/order",
                    params={"symbol": symbol, "origClientOrderId": client_order_id},
                )
            )
        except BinanceRESTError as exc:
            if exc.code == -2013:
                return None
            raise
        if isinstance(payload, dict) and payload:
            return payload
        return None

    def _recover_after_submit_error(
        self,
        *,
        intent_id: str,
        client_order_id: str,
        symbol: str,
        side: str,
        context: KernelContext,
        error_label: str,
    ) -> ExecutionResult:
        try:
            order = self._query_order_by_client_order_id(
                symbol=symbol,
                client_order_id=client_order_id,
            )
        except Exception as query_exc:  # noqa: BLE001
            if self._idempotency_enabled and self._storage is not None:
                self._storage.upsert_submission_intent(
                    intent_id=intent_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    status="REVIEW_REQUIRED",
                )
            logger.warning(
                "order_intent_review_required",
                extra={
                    "event": "order_intent_review_required",
                    "mode": context.mode,
                    "profile": context.profile,
                    "state_uncertain": False,
                    "safe_mode": False,
                    "intent_id": intent_id,
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": side,
                    "reason": f"{error_label}:query_{type(query_exc).__name__}",
                },
            )
            return ExecutionResult(
                ok=False,
                order_id=client_order_id,
                reason="live_order_review_required:query_failed",
            )

        if order is not None:
            order_id = str(order.get("orderId") or order.get("clientOrderId") or client_order_id)
            if self._idempotency_enabled and self._storage is not None:
                self._storage.upsert_submission_intent(
                    intent_id=intent_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    status="SUBMITTED",
                    order_id=order_id,
                )
            logger.info(
                "order_intent_recovered",
                extra={
                    "event": "order_intent_recovered",
                    "mode": context.mode,
                    "profile": context.profile,
                    "state_uncertain": False,
                    "safe_mode": False,
                    "intent_id": intent_id,
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": side,
                    "reason": error_label,
                },
            )
            return ExecutionResult(
                ok=True,
                order_id=order_id,
                reason="live_order_recovered_after_submit_error",
            )

        if self._idempotency_enabled and self._storage is not None:
            self._storage.upsert_submission_intent(
                intent_id=intent_id,
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                status="REVIEW_REQUIRED",
            )
        logger.warning(
            "order_intent_review_required",
            extra={
                "event": "order_intent_review_required",
                "mode": context.mode,
                "profile": context.profile,
                "state_uncertain": False,
                "safe_mode": False,
                "intent_id": intent_id,
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": side,
                "reason": f"{error_label}:not_found",
            },
        )
        return ExecutionResult(
            ok=False,
            order_id=client_order_id,
            reason="live_order_review_required:not_found_after_submit_error",
        )

    @staticmethod
    def _derive_intent_id(*, candidate: Candidate, size: SizePlan, context: KernelContext) -> str:
        payload = {
            "profile": context.profile,
            "symbol": str(candidate.symbol or "").upper(),
            "side": str(candidate.side).upper(),
            "tick": int(context.tick),
            "qty": round(float(size.qty), 8),
            "leverage": round(float(size.leverage), 4),
            "entry_price": round(float(candidate.entry_price or 0.0), 6),
            "alpha_id": str(candidate.alpha_id or ""),
            "entry_family": str(candidate.entry_family or ""),
            "regime_hint": str(candidate.regime_hint or ""),
        }
        raw = "|".join(f"{key}={payload[key]}" for key in sorted(payload))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return f"intent-{digest}"

    def _load_reusable_submission(self, *, intent_id: str) -> dict[str, Any] | None:
        if not self._idempotency_enabled or self._storage is None:
            return None
        row = self._storage.get_submission_intent(intent_id=intent_id)
        if row is None:
            return None
        updated_at = _parse_iso_timestamp(row.get("updated_at"))
        if updated_at is None:
            return None
        age = (datetime.now(timezone.utc) - updated_at).total_seconds()
        if age > self._idempotency_window_sec:
            return None
        return row

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

        intent_id = self._derive_intent_id(candidate=candidate, size=size, context=context)
        logger.info(
            "order_intent",
            extra={
                "event": "order_intent",
                "mode": context.mode,
                "profile": context.profile,
                "state_uncertain": False,
                "safe_mode": False,
                "intent_id": intent_id,
                "symbol": symbol,
                "side": side,
                "action": "submit_attempt",
            },
        )
        reused = self._load_reusable_submission(intent_id=intent_id)
        if reused is not None:
            status = str(reused.get("status") or "").strip() or "UNKNOWN"
            logger.info(
                "order_intent_deduped",
                extra={
                    "event": "order_intent_deduped",
                    "mode": context.mode,
                    "profile": context.profile,
                    "state_uncertain": False,
                    "safe_mode": False,
                    "intent_id": intent_id,
                    "client_order_id": str(reused.get("client_order_id") or ""),
                    "symbol": symbol,
                    "side": side,
                    "reason": status,
                },
            )
            return ExecutionResult(
                ok=status == "SUBMITTED",
                order_id=str(reused.get("order_id") or reused.get("client_order_id") or ""),
                reason=f"live_order_reused:{status}",
            )

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
                    affordable_margin = float(available_usdt) / 1.01
                    affordable_notional = max(affordable_margin * float(leverage_int), 0.0)
                    affordable_qty = affordable_notional / float(candidate.entry_price)
                    if step_size > 0:
                        affordable_qty = self._floor_to_step(affordable_qty, step_size)
                    qty = min(qty, max(affordable_qty, 0.0))
                    est_notional = qty * float(candidate.entry_price)
                    if min_notional is not None and est_notional < float(min_notional):
                        qty = 0.0
                    if min_qty > 0 and qty < min_qty:
                        qty = 0.0
                    if qty <= 0.0:
                        return ExecutionResult(
                            ok=False,
                            reason=(
                                "insufficient_available_margin:"
                                f"required={required_margin + fee_buffer:.6f},"
                                f"available={available_usdt:.6f}"
                            ),
                        )

            payload["quantity"] = f"{qty:.8f}"
            if self._idempotency_enabled and self._storage is not None:
                self._storage.upsert_submission_intent(
                    intent_id=intent_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    status="PENDING",
                )
            _ = self._run(lambda: self._rest.change_leverage(symbol=symbol, leverage=leverage_int))
        except BinanceRESTError as exc:
            if self._idempotency_enabled and self._storage is not None:
                self._storage.upsert_submission_intent(
                    intent_id=intent_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    status="SUBMIT_ERROR",
                )
            logger.warning(
                "order_intent_submit_failed",
                extra={
                    "event": "order_intent_submit_failed",
                    "mode": context.mode,
                    "profile": context.profile,
                    "state_uncertain": False,
                    "safe_mode": False,
                    "intent_id": intent_id,
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": side,
                    "reason": f"BinanceRESTError:{exc.code if exc.code is not None else 'none'}",
                },
            )
            code = exc.code if exc.code is not None else "none"
            return ExecutionResult(ok=False, reason=f"live_order_failed:BinanceRESTError:{code}")
        except Exception as exc:  # noqa: BLE001
            if self._idempotency_enabled and self._storage is not None:
                self._storage.upsert_submission_intent(
                    intent_id=intent_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    status="SUBMIT_ERROR",
                )
            logger.warning(
                "order_intent_submit_failed",
                extra={
                    "event": "order_intent_submit_failed",
                    "mode": context.mode,
                    "profile": context.profile,
                    "state_uncertain": False,
                    "safe_mode": False,
                    "intent_id": intent_id,
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": side,
                    "reason": type(exc).__name__,
                },
            )
            return ExecutionResult(ok=False, reason=f"live_order_failed:{type(exc).__name__}")

        try:
            resp = self._run(lambda: self._rest.place_order(params=payload))
        except Exception as exc:  # noqa: BLE001
            if self._is_ambiguous_submit_error(exc):
                return self._recover_after_submit_error(
                    intent_id=intent_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    context=context,
                    error_label=type(exc).__name__,
                )
            if self._idempotency_enabled and self._storage is not None:
                self._storage.upsert_submission_intent(
                    intent_id=intent_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    status="SUBMIT_ERROR",
                )
            logger.warning(
                "order_intent_submit_failed",
                extra={
                    "event": "order_intent_submit_failed",
                    "mode": context.mode,
                    "profile": context.profile,
                    "state_uncertain": False,
                    "safe_mode": False,
                    "intent_id": intent_id,
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": side,
                    "reason": (
                        f"BinanceRESTError:{exc.code if exc.code is not None else 'none'}"
                        if isinstance(exc, BinanceRESTError)
                        else type(exc).__name__
                    ),
                },
            )
            if isinstance(exc, BinanceRESTError):
                code = exc.code if exc.code is not None else "none"
                return ExecutionResult(ok=False, reason=f"live_order_failed:BinanceRESTError:{code}")
            return ExecutionResult(ok=False, reason=f"live_order_failed:{type(exc).__name__}")

        if not isinstance(resp, dict):
            if self._idempotency_enabled and self._storage is not None:
                self._storage.upsert_submission_intent(
                    intent_id=intent_id,
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    status="BAD_RESPONSE",
                )
            logger.warning(
                "order_intent_submit_failed",
                extra={
                    "event": "order_intent_submit_failed",
                    "mode": context.mode,
                    "profile": context.profile,
                    "state_uncertain": False,
                    "safe_mode": False,
                    "intent_id": intent_id,
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": side,
                    "reason": "BAD_RESPONSE",
                },
            )
            return ExecutionResult(ok=False, reason="live_order_bad_response")

        order_id = resp.get("orderId") or resp.get("clientOrderId") or client_order_id
        if self._idempotency_enabled and self._storage is not None:
            self._storage.upsert_submission_intent(
                intent_id=intent_id,
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                status="SUBMITTED",
                order_id=str(order_id),
            )
        logger.info(
            "order_intent_submitted",
            extra={
                "event": "order_intent_submitted",
                "mode": context.mode,
                "profile": context.profile,
                "state_uncertain": False,
                "safe_mode": False,
                "intent_id": intent_id,
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": side,
                "action": "submitted",
            },
        )
        return ExecutionResult(ok=True, order_id=str(order_id), reason="live_order_submitted")

    @staticmethod
    def _run(thunk):  # type: ignore[no-untyped-def]
        return run_async_blocking(thunk)
