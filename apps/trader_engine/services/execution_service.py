from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from enum import Enum
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence

from apps.trader_engine.domain.enums import Direction, EngineState, ExecHint
from apps.trader_engine.exchange.binance_usdm import BinanceAuthError, BinanceHTTPError, BinanceUSDMClient
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.risk_service import RiskService
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.services.notifier_service import Notifier
from apps.trader_engine.services.oplog import OperationalLogger
from apps.trader_engine.services.snapshot_service import SnapshotService
from apps.trader_engine.services.sizing_service import SizingResult, SizingService
from apps.trader_engine.storage.repositories import OrderRecordRepo

logger = logging.getLogger(__name__)


Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class ExecutionRejected(Exception):
    message: str


@dataclass(frozen=True)
class ExecutionValidationError(Exception):
    message: str


def _dec(x: Any) -> Decimal:
    return Decimal(str(x))


def _floor_to_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return qty
    return (qty // step) * step


def _floor_to_tick(px: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return px
    return (px // tick) * tick


def _ceil_to_tick(px: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return px
    q = (px / tick).to_integral_value(rounding=ROUND_CEILING)
    return q * tick


def _direction_to_entry_side(direction: Direction) -> Side:
    return "BUY" if direction == Direction.LONG else "SELL"


def _direction_to_close_side(position_amt: float) -> Side:
    # positionAmt > 0 means long; close is SELL. positionAmt < 0 means short; close is BUY.
    return "SELL" if position_amt > 0 else "BUY"


def _is_filled(status: Any) -> bool:
    return str(status).upper() == "FILLED"


def _coerce_enum(raw: Any, enum_cls: type[Enum], *, err: str) -> Enum:
    if isinstance(raw, enum_cls):
        return raw
    if raw is None:
        raise ExecutionValidationError(err)
    # Pydantic can hand us Enum instances from the request model_dump(). Use .value if present.
    if hasattr(raw, "value"):
        raw = getattr(raw, "value")
    try:
        return enum_cls(str(raw).upper())
    except Exception as e:
        raise ExecutionValidationError(err) from e


class ExecutionService:
    """Order/close execution for Binance USDT-M Futures (One-way only).

    Safety goals:
    - Orders only when engine is RUNNING
    - Reject when engine is PANIC
    - Enforce single-asset position rule
    - No leverage auto-adjust (never calls set_leverage automatically)

    FINAL-2 tactics (entry):
    - LIMIT first
    - LIMIT timeout (cfg.exec_limit_timeout_sec; default 5s)
    - attempts (cfg.exec_limit_retries; default 2 attempts total)
    - if still not fully filled => MARKET fallback (unless spread guard blocks MARKET)
    - handle partial fills (re-submit remaining with same policy)
    """

    def __init__(
        self,
        *,
        client: BinanceUSDMClient,
        engine: EngineService,
        risk: RiskConfigService,
        pnl: Optional[PnLService] = None,
        policy: Optional[RiskService] = None,
        notifier: Optional[Notifier] = None,
        sizing: Optional[SizingService] = None,
        allowed_symbols: Sequence[str],
        split_parts: int = 3,
        dry_run: bool = True,
        dry_run_strict: bool = False,
        oplog: Optional[OperationalLogger] = None,
        snapshot: Optional[SnapshotService] = None,
        order_records: Optional[OrderRecordRepo] = None,
        exec_lock_timeout_sec: float = 5.0,
    ) -> None:
        self._client = client
        self._engine = engine
        self._risk = risk
        self._pnl = pnl
        self._policy = policy
        self._notifier = notifier
        self._sizing = sizing or SizingService(client=client)
        self._allowed_symbols = [s.upper() for s in allowed_symbols]
        self._split_parts = max(int(split_parts), 2)
        self._dry_run = bool(dry_run)
        self._dry_run_strict = bool(dry_run_strict)
        self._oplog = oplog
        self._snapshot = snapshot
        self._order_records = order_records
        self._cid_env = _sanitize_env_token(os.getenv("BOT_ENV", "PROD"))
        self._run_id = oplog.run_id if oplog else None
        self._exec_lock = asyncio.Lock()
        self._exec_lock_timeout_sec = max(float(exec_lock_timeout_sec), 0.01)

    async def _acquire_exec_lock(self) -> bool:
        try:
            await asyncio.wait_for(self._exec_lock.acquire(), timeout=self._exec_lock_timeout_sec)
            return True
        except asyncio.TimeoutError:
            return False

    def _emit(self, kind: str, payload: Mapping[str, Any]) -> None:
        if not self._notifier:
            return
        try:
            ev = {"kind": kind, **dict(payload)}
            self._notifier.notify(ev)
        except Exception:
            logger.exception("notifier_failed", extra={"kind": kind})

    @staticmethod
    def _new_intent_id(symbol: str) -> str:
        return f"intent-{symbol}-{int(time.time() * 1000)}"

    def _new_client_order_id(self, *, intent_id: str, attempt: int) -> str:
        return _make_client_order_id(env=self._cid_env, intent_id=intent_id, attempt=attempt)

    def _record_created(
        self,
        *,
        intent_id: Optional[str],
        cycle_id: Optional[str],
        symbol: str,
        side: str,
        order_type: str,
        reduce_only: bool,
        qty: Decimal,
        price: Optional[Decimal],
        time_in_force: Optional[str],
        client_order_id: str,
    ) -> None:
        if not self._order_records:
            return
        self._order_records.create_created(
            intent_id=intent_id,
            cycle_id=cycle_id,
            run_id=self._run_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            reduce_only=reduce_only,
            qty=float(qty),
            price=float(price) if price is not None else None,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
        )

    def _record_sent_or_ack(self, *, client_order_id: str, order: Mapping[str, Any], fallback_status: str = "SENT") -> None:
        if not self._order_records:
            return
        status = _map_exchange_status_to_record(order.get("status"), default=fallback_status)
        self._order_records.mark_sent_or_ack(
            client_order_id=client_order_id,
            exchange_order_id=_extract_exchange_order_id(order),
            status=status,
            last_error=None,
        )

    def _record_error(self, *, client_order_id: str, last_error: str) -> None:
        if not self._order_records:
            return
        self._order_records.mark_status(client_order_id=client_order_id, status="ERROR", last_error=last_error)

    def _query_order_by_client_order_id(self, *, symbol: str, client_order_id: str) -> Optional[Mapping[str, Any]]:
        try:
            return self._client.get_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
        except Exception:
            return None

    def _is_timeout_like_error(self, err: Exception) -> bool:
        txt = f"{type(err).__name__}:{err}".lower()
        return ("timeout" in txt) or ("timed out" in txt) or ("network_error" in txt)

    def _oplog_execution_from_order(
        self,
        *,
        intent_id: Optional[str],
        symbol: str,
        side: Optional[str],
        reason: Optional[str],
        order: Mapping[str, Any],
    ) -> None:
        if not self._oplog:
            return
        try:
            qty = order.get("executed_qty", order.get("orig_qty"))
            price = order.get("avg_price", order.get("price"))
            self._oplog.log_execution(
                intent_id=intent_id,
                symbol=symbol,
                side=side,
                qty=float(qty) if qty not in (None, "") else None,
                price=float(price) if price not in (None, "") else None,
                order_type=(str(order.get("type")) if order.get("type") is not None else None),
                client_order_id=(str(order.get("client_order_id")) if order.get("client_order_id") is not None else None),
                status=(str(order.get("status")) if order.get("status") is not None else None),
                reason=reason,
            )
        except Exception:
            logger.exception("oplog_execution_order_failed", extra={"symbol": symbol})

    def _capture_snapshot(
        self,
        *,
        reason: str,
        symbol: Optional[str] = None,
        intent_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
    ) -> None:
        if not self._snapshot:
            return
        try:
            self._snapshot.capture_snapshot(
                reason=reason,
                preferred_symbol=symbol,
                intent_id=intent_id,
                cycle_id=cycle_id,
            )
        except Exception:
            logger.exception("snapshot_capture_failed", extra={"reason": reason, "symbol": symbol})

    def _require_not_panic(self) -> None:
        st = self._engine.get_state().state
        if st == EngineState.PANIC:
            raise ExecutionRejected("engine_in_panic")

    def _require_running_for_enter(self) -> None:
        st = self._engine.get_state().state
        if hasattr(self._engine, "is_recovery_lock_active") and self._engine.is_recovery_lock_active():
            raise ExecutionRejected("recovery_lock_active")
        if hasattr(self._engine, "is_ws_safe_mode") and self._engine.is_ws_safe_mode():
            raise ExecutionRejected("ws_down_safe_mode")
        if st == EngineState.PANIC:
            raise ExecutionRejected("engine_in_panic")
        # COOLDOWN is evaluated by RiskService; allow the request through so the caller
        # receives a specific risk block reason rather than a generic engine_not_running.
        if st not in (EngineState.RUNNING, EngineState.COOLDOWN):
            raise ExecutionRejected(f"engine_not_running:{st.value}")

    def _require_one_way_mode(self) -> None:
        # This is a hard requirement. If hedge mode is on, refuse to trade.
        try:
            ok = self._client.get_position_mode_one_way()
        except BinanceAuthError as e:
            raise ExecutionRejected("binance_auth_error") from e
        except BinanceHTTPError as e:
            raise ExecutionRejected(f"binance_http_{e.status_code}_code_{e.code}") from e
        if not ok:
            raise ExecutionRejected("hedge_mode_enabled")

    def _validate_symbol(self, symbol: str) -> str:
        # Kept for backward-compat; use _normalize_symbol + _validate_symbol_for_entry.
        return self._validate_symbol_for_entry(symbol)

    def _normalize_symbol(self, symbol: str) -> str:
        sym = symbol.strip().upper()
        if not sym:
            raise ExecutionValidationError("symbol_required")
        return sym

    def _validate_symbol_for_entry(self, symbol: str) -> str:
        sym = self._normalize_symbol(symbol)
        if self._allowed_symbols and sym not in self._allowed_symbols:
            raise ExecutionValidationError("symbol_not_allowed")
        return sym

    def _book(self, symbol: str) -> Mapping[str, Any]:
        bt = self._client.get_book_ticker(symbol)
        return bt

    def _best_price_ref(self, *, symbol: str, side: Side) -> Decimal:
        bt = self._book(symbol)
        bid = _dec(bt.get("bidPrice", "0") or "0")
        ask = _dec(bt.get("askPrice", "0") or "0")
        if side == "BUY":
            return ask if ask > 0 else bid
        return bid if bid > 0 else ask

    def _round_qty(self, *, symbol: str, qty: Decimal, is_market: bool) -> Decimal:
        f = self._client.get_symbol_filters(symbol=symbol)
        step = _dec(f.get("step_size") or "0")
        min_qty = _dec(f.get("min_qty") or "0")
        q = _floor_to_step(qty, step) if step > 0 else qty
        if min_qty > 0 and q < min_qty:
            raise ExecutionValidationError("quantity_below_min_qty")
        return q

    def _round_price(self, *, symbol: str, px: Decimal) -> Decimal:
        f = self._client.get_symbol_filters(symbol=symbol)
        tick = _dec(f.get("tick_size") or "0")
        return _floor_to_tick(px, tick) if tick > 0 else px

    def _round_price_for_side(self, *, symbol: str, side: Side, px: Decimal) -> Decimal:
        f = self._client.get_symbol_filters(symbol=symbol)
        tick = _dec(f.get("tick_size") or "0")
        if tick <= 0:
            return px
        # For entry LIMITs, be slightly aggressive to improve fill probability:
        # BUY near ask => round up; SELL near bid => round down.
        return _ceil_to_tick(px, tick) if side == "BUY" else _floor_to_tick(px, tick)

    def _check_min_notional(self, *, symbol: str, qty: Decimal, price_ref: Decimal) -> None:
        f = self._client.get_symbol_filters(symbol=symbol)
        mn = f.get("min_notional")
        if mn is None:
            return
        min_notional = _dec(mn)
        if min_notional <= 0:
            return
        notional = qty * price_ref
        if notional < min_notional:
            raise ExecutionValidationError("notional_below_min_notional")

    def _get_open_positions(self) -> Dict[str, Dict[str, float]]:
        return self._client.get_open_positions_any()

    def _assert_single_asset_rule_or_raise(self, positions: Mapping[str, Any]) -> None:
        if len(positions) > 1:
            # This should never happen if the rule is respected. Treat as hard stop.
            raise ExecutionRejected("multiple_open_positions_detected")

    def _blocked_response(
        self,
        *,
        symbol: str,
        hint: ExecHint,
        reason: str,
        event_kind: str,
        intent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        out = {
            "symbol": symbol,
            "hint": hint.value,
            "orders": [],
            "blocked": True,
            "block_reason": reason,
            "intent_id": intent_id,
        }
        self._emit("BLOCK", {"symbol": symbol, "reason": reason, "message": f"ENTRY BLOCKED: {reason}"})
        self._emit(event_kind, {"symbol": symbol, "detail": out})
        if self._oplog:
            try:
                self._oplog.log_risk_block(
                    intent_id=intent_id,
                    symbol=symbol,
                    block_reason=reason,
                    details_json={"hint": hint.value, "event_kind": event_kind},
                )
            except Exception:
                logger.exception("oplog_risk_block_failed", extra={"symbol": symbol, "reason": reason})
        return out

    def _entry_block(
        self,
        *,
        symbol: str,
        hint: ExecHint,
        reason: str,
        event_kind: str,
        intent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        out = self._blocked_response(
            symbol=symbol,
            hint=hint,
            reason=reason,
            event_kind=event_kind,
            intent_id=intent_id,
        )
        if self._pnl:
            try:
                self._pnl.set_last_block_reason(reason)
            except Exception as e:  # noqa: BLE001
                logger.warning("pnl_set_last_block_reason_failed", extra={"reason": reason, "err": type(e).__name__}, exc_info=True)
        return out

    def _budget_guard(
        self,
        *,
        symbol: str,
        side: Side,
        exec_hint: ExecHint,
        cfg: Any,
        qty_in: Any,
        notional_usdt: Any,
        leverage: float,
        event_kind: str,
        intent_id: Optional[str] = None,
    ) -> tuple[Optional[Dict[str, Any]], Decimal, Decimal, SizingResult]:
        cap = self._sizing.compute_live(symbol=symbol, risk=cfg, leverage=leverage)
        if cap.blocked or cap.notional_usdt <= 0.0 or cap.qty <= 0.0:
            reason = str(cap.block_reason or "BUDGET_BLOCKED")
            if reason == "MARK_PRICE_UNAVAILABLE":
                reason = "MARKET_DATA_UNAVAILABLE"
            return self._blocked_response(symbol=symbol, hint=exec_hint, reason=reason, event_kind=event_kind, intent_id=intent_id), _dec("0"), _dec("0"), cap

        cap_notional = float(cap.notional_usdt)
        mark = float(cap.mark_price or 0.0)
        if mark <= 0.0:
            try:
                mark = float(self._best_price_ref(symbol=symbol, side=side))
            except Exception:
                mark = 0.0
        if mark <= 0.0:
            return self._blocked_response(symbol=symbol, hint=exec_hint, reason="MARKET_DATA_UNAVAILABLE", event_kind=event_kind, intent_id=intent_id), _dec("0"), _dec("0"), cap

        if qty_in is None and notional_usdt is None:
            requested_qty = _dec(cap.qty)
            requested_notional = _dec(cap.notional_usdt)
            return None, requested_qty, requested_notional, cap

        if qty_in is not None:
            requested_qty = _dec(qty_in)
            requested_notional = requested_qty * _dec(mark)
        else:
            requested_notional = _dec(notional_usdt)
            requested_qty = requested_notional / _dec(mark)

        if float(requested_notional) > (cap_notional + 1e-9):
            return self._blocked_response(
                symbol=symbol,
                hint=exec_hint,
                reason="ENTRY_EXCEEDS_BUDGET_CAP",
                event_kind=event_kind,
                intent_id=intent_id,
            ), _dec("0"), _dec("0"), cap

        return None, requested_qty, requested_notional, cap

    async def close_position(self, symbol: str, *, reason: str = "EXIT") -> Dict[str, Any]:
        locked = await self._acquire_exec_lock()
        if not locked:
            raise ExecutionRejected("EXECUTION_LOCK_BUSY")
        try:
            return self._close_position_unlocked(symbol, reason=reason)
        finally:
            self._exec_lock.release()

    def _close_position_unlocked(self, symbol: str, *, reason: str = "EXIT") -> Dict[str, Any]:
        self._require_not_panic()
        if self._dry_run and self._dry_run_strict:
            raise ExecutionRejected("dry_run_strict_close_blocked")
        # Closing must be allowed even if the symbol isn't in the bot's allowed list.
        sym = self._normalize_symbol(symbol)

        # Always cancel orders first for the symbol.
        try:
            canceled = self._client.cancel_all_open_orders(symbol=sym)
        except Exception as e:  # noqa: BLE001
            logger.warning("cancel_all_open_orders_failed", extra={"symbol": sym, "err": type(e).__name__})
            canceled = []
        if self._oplog and canceled:
            try:
                self._oplog.log_event(
                    "CANCEL_ALL",
                    {"symbol": sym, "action": "cancel_all", "reason": "close_position", "count": len(canceled)},
                )
            except Exception:
                logger.exception("oplog_cancel_all_failed", extra={"symbol": sym})

        positions = self._get_open_positions()
        pos = positions.get(sym)
        if not pos:
            return {"symbol": sym, "closed": False, "reason": "no_open_position", "canceled": len(canceled)}

        amt = float(pos.get("position_amt", 0.0) or 0.0)
        if abs(amt) <= 0:
            return {"symbol": sym, "closed": False, "reason": "no_open_position", "canceled": len(canceled)}

        side = _direction_to_close_side(amt)
        qty = _dec(abs(amt))
        qty = self._round_qty(symbol=sym, qty=qty, is_market=True)

        bal_before = None
        try:
            bal_before = self._client.get_account_balance_usdtm()
        except Exception:
            bal_before = None

        try:
            close_intent = self._new_intent_id(sym)
            close_cid = self._new_client_order_id(intent_id=close_intent, attempt=1)
            self._record_created(
                intent_id=close_intent,
                cycle_id=None,
                symbol=sym,
                side=side,
                order_type="MARKET",
                reduce_only=True,
                qty=qty,
                price=None,
                time_in_force=None,
                client_order_id=close_cid,
            )
            order = self._client.place_order_market(
                symbol=sym,
                side=side,
                quantity=float(qty),
                reduce_only=True,
                new_client_order_id=close_cid,
            )
            self._record_sent_or_ack(client_order_id=close_cid, order=order, fallback_status="ACK")
        except BinanceAuthError as e:
            raise ExecutionRejected("binance_auth_error") from e
        except BinanceHTTPError as e:
            raise ExecutionRejected(f"binance_http_{e.status_code}_code_{e.code}") from e

        # Best-effort realized PnL tracking: wallet balance delta around close.
        if self._pnl and bal_before and isinstance(bal_before, dict):
            try:
                time.sleep(0.2)
                bal_after = self._client.get_account_balance_usdtm()
                w0 = float(bal_before.get("wallet") or 0.0)
                w1 = float(bal_after.get("wallet") or 0.0)
                positions = self._client.get_open_positions_any()
                upnl = sum(float(r.get("unrealized_pnl") or 0.0) for r in positions.values())
                equity = w1 + upnl
                self._pnl.apply_realized_pnl_delta(realized_delta_usdt=(w1 - w0), equity_usdt=equity)
            except Exception:
                logger.exception("pnl_update_failed_on_close", extra={"symbol": sym})

        out = {"symbol": sym, "closed": True, "canceled": len(canceled), "order": _safe_order(order), "reason": reason}
        self._oplog_execution_from_order(
            intent_id=None,
            symbol=sym,
            side=side,
            reason=reason,
            order=out["order"],
        )
        kind = str(reason or "EXIT").upper()
        if kind not in {"EXIT", "TAKE_PROFIT", "STOP_LOSS", "REBALANCE", "WATCHDOG_SHOCK", "TRAILING_PCT", "TRAILING_ATR"}:
            kind = "EXIT"
        self._emit(kind, {"symbol": sym, "detail": out})
        self._capture_snapshot(reason=reason, symbol=sym)
        return out

    async def close_all_positions(self, *, reason: str = "EXIT") -> Dict[str, Any]:
        locked = await self._acquire_exec_lock()
        if not locked:
            raise ExecutionRejected("EXECUTION_LOCK_BUSY")
        try:
            return self._close_all_positions_unlocked(reason=reason)
        finally:
            self._exec_lock.release()

    def _close_all_positions_unlocked(self, *, reason: str = "EXIT") -> Dict[str, Any]:
        self._require_not_panic()
        if self._dry_run and self._dry_run_strict:
            raise ExecutionRejected("dry_run_strict_close_blocked")
        bal_before = None
        try:
            bal_before = self._client.get_account_balance_usdtm()
        except Exception:
            bal_before = None
        positions = self._get_open_positions()
        if not positions:
            return {"closed": False, "reason": "no_open_position"}
        # Always use the unified close-all path: cancels orders first then reduceOnly closes each position.
        out = self._panic_guarded_close_all(force=True)
        if self._pnl and bal_before and isinstance(bal_before, dict):
            try:
                time.sleep(0.2)
                bal_after = self._client.get_account_balance_usdtm()
                w0 = float(bal_before.get("wallet") or 0.0)
                w1 = float(bal_after.get("wallet") or 0.0)
                positions2 = self._client.get_open_positions_any()
                upnl = sum(float(r.get("unrealized_pnl") or 0.0) for r in positions2.values())
                equity = w1 + upnl
                self._pnl.apply_realized_pnl_delta(realized_delta_usdt=(w1 - w0), equity_usdt=equity)
            except Exception:
                logger.exception("pnl_update_failed_on_close_all")
        if len(positions) > 1:
            out["warning"] = "multiple_open_positions_detected"
        kind = str(reason or "EXIT").upper()
        if kind not in {"EXIT", "TAKE_PROFIT", "STOP_LOSS", "REBALANCE", "WATCHDOG_SHOCK", "TRAILING_PCT", "TRAILING_ATR"}:
            kind = "EXIT"
        self._emit(kind, {"symbol": "*", "detail": out})
        self._capture_snapshot(reason="CLOSE_ALL")
        return out

    async def enter_position(
        self,
        intent: Mapping[str, Any],
    ) -> Dict[str, Any]:
        locked = await self._acquire_exec_lock()
        if not locked:
            symbol = str(intent.get("symbol", "")).strip().upper() or "UNKNOWN"
            hint_raw = str(intent.get("exec_hint") or "LIMIT")
            try:
                hint = ExecHint(hint_raw.upper())
            except Exception:
                hint = ExecHint.LIMIT
            return self._blocked_response(
                symbol=symbol,
                hint=hint,
                reason="EXECUTION_LOCK_BUSY",
                event_kind="ENTER",
                intent_id=str(intent.get("intent_id") or self._new_intent_id(symbol)),
            )
        try:
            return self._enter_position_unlocked(intent)
        finally:
            self._exec_lock.release()

    def _enter_position_unlocked(
        self,
        intent: Mapping[str, Any],
    ) -> Dict[str, Any]:
        # Any entry path should resync time once (keeps signed requests stable).
        try:
            self._client.refresh_time_offset()
        except Exception as e:  # noqa: BLE001
            logger.warning("refresh_time_offset_failed", extra={"err": type(e).__name__}, exc_info=True)

        op = str(intent.get("op") or "ENTER").upper()
        event_kind = "REBALANCE" if op == "REBALANCE" else "ENTER"
        intent_id = str(intent.get("intent_id") or self._new_intent_id(str(intent.get("symbol") or "UNKNOWN").upper()))

        try:
            self._require_running_for_enter()
            self._require_one_way_mode()

            symbol = self._validate_symbol_for_entry(str(intent.get("symbol", "")))
            direction = _coerce_enum(intent.get("direction"), Direction, err="invalid_direction")  # type: ignore[assignment]
            exec_hint = _coerce_enum(intent.get("exec_hint"), ExecHint, err="invalid_exec_hint")  # type: ignore[assignment]

            # Size inputs
            qty_in = intent.get("qty")
            notional_usdt = intent.get("notional_usdt")

            # Optional leverage validation only (no auto-adjust).
            cfg = self._risk.get_config()
            lev = intent.get("leverage")
            lev_f = float(cfg.max_leverage)
            if lev is not None:
                try:
                    lev_f = float(lev)
                except Exception as e:
                    raise ExecutionValidationError("invalid_leverage") from e
                if lev_f > cfg.max_leverage:
                    raise ExecutionValidationError("leverage_above_max_leverage")

            side = _direction_to_entry_side(direction)
            if self._oplog:
                try:
                    self._oplog.log_event(
                        "ENTRY_INTENT",
                        {
                            "intent_id": intent_id,
                            "symbol": symbol,
                            "side": side,
                            "action": event_kind,
                            "reason": "received",
                        },
                    )
                except Exception:
                    logger.exception("oplog_entry_intent_failed", extra={"symbol": symbol})

            blocked_out, qty_calc, _notional_calc, cap = self._budget_guard(
                symbol=symbol,
                side=side,
                exec_hint=exec_hint,
                cfg=cfg,
                qty_in=qty_in,
                notional_usdt=notional_usdt,
                leverage=lev_f,
                event_kind=event_kind,
                intent_id=intent_id,
            )
            if blocked_out is not None:
                if str(blocked_out.get("block_reason") or "") == "MARK_PRICE_UNAVAILABLE":
                    return self._entry_block(
                        symbol=symbol,
                        hint=exec_hint,
                        reason="MARKET_DATA_UNAVAILABLE",
                        event_kind=event_kind,
                        intent_id=intent_id,
                    )
                if self._pnl:
                    try:
                        self._pnl.set_last_block_reason(str(blocked_out.get("block_reason") or "BUDGET_BLOCKED"))
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "pnl_set_last_block_reason_failed",
                            extra={"reason": str(blocked_out.get("block_reason") or "BUDGET_BLOCKED"), "err": type(e).__name__},
                            exc_info=True,
                        )
                return blocked_out

            # DRY_RUN: do not place NEW entry/rebalance orders. Return a simulated result + notify.
            if self._dry_run:
                # No side effects in dry_run mode: do not cancel orders and do not close positions.
                try:
                    price_ref = self._best_price_ref(symbol=symbol, side=side)
                except Exception:
                    return self._entry_block(
                        symbol=symbol,
                        hint=exec_hint,
                        reason="MARKET_DATA_UNAVAILABLE",
                        event_kind=event_kind,
                        intent_id=intent_id,
                    )
                if price_ref <= 0:
                    return self._entry_block(
                        symbol=symbol,
                        hint=exec_hint,
                        reason="MARKET_DATA_UNAVAILABLE",
                        event_kind=event_kind,
                        intent_id=intent_id,
                    )
                qty: Decimal = qty_calc

                is_market = exec_hint == ExecHint.MARKET
                qty = self._round_qty(symbol=symbol, qty=qty, is_market=is_market)
                self._check_min_notional(symbol=symbol, qty=qty, price_ref=price_ref)
                if qty <= 0:
                    raise ExecutionValidationError("quantity_invalid")

                if self._pnl:
                    try:
                        self._pnl.set_last_block_reason("dry_run_enabled")
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "pnl_set_last_block_reason_failed",
                            extra={"reason": "dry_run_enabled", "err": type(e).__name__},
                            exc_info=True,
                        )
                sim = {
                    "symbol": symbol,
                    "hint": exec_hint.value,
                    "orders": [],
                    "dry_run": True,
                    "intent_id": intent_id,
                    "side": side,
                    "qty": float(qty),
                    "price_ref": float(price_ref),
                    "notional_usdt_est": float(qty * price_ref),
                    "budget_cap_notional_usdt": float(cap.notional_usdt),
                }
                if self._oplog:
                    try:
                        self._oplog.log_execution(
                            intent_id=intent_id,
                            symbol=symbol,
                            side=side,
                            qty=float(qty),
                            price=float(price_ref),
                            order_type=str(exec_hint.value),
                            client_order_id=None,
                            status="DRY_RUN",
                            reason="dry_run_simulated_enter",
                        )
                    except Exception:
                        logger.exception("oplog_dry_run_execution_failed", extra={"symbol": symbol})
                logger.warning(
                    "dry_run_simulated_enter",
                    extra={"symbol": symbol, "hint": exec_hint.value, "qty": float(qty)},
                )
                self._emit(event_kind, {"symbol": symbol, "dry_run": True, "detail": sim})
                self._capture_snapshot(reason="DRY_RUN_ENTER", symbol=symbol, intent_id=intent_id)
                return sim

            # Sync: existing positions (across entire account) + enforce single-asset rule.
            try:
                positions = self._get_open_positions()
            except Exception as e:  # noqa: BLE001
                logger.warning("precheck_position_failed", extra={"symbol": symbol, "err": type(e).__name__})
                return self._entry_block(
                    symbol=symbol,
                    hint=exec_hint,
                    reason="PRECHECK_POSITION_FAILED",
                    event_kind=event_kind,
                    intent_id=intent_id,
                )
            if positions:
                # Enforce 1-asset rule by closing anything that isn't the target.
                open_syms = list(positions.keys())
                open_sym = open_syms[0]
                open_amt = float(positions[open_sym].get("position_amt", 0.0) or 0.0)
                if len(positions) > 1:
                    logger.warning("single_asset_rule_violation_detected", extra={"open_symbols": open_syms})
                    _ = self._close_all_positions_unlocked(reason="REBALANCE")
                    try:
                        positions = self._get_open_positions()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("precheck_position_failed", extra={"symbol": symbol, "err": type(e).__name__})
                        return self._entry_block(
                            symbol=symbol,
                            hint=exec_hint,
                            reason="PRECHECK_POSITION_FAILED",
                            event_kind=event_kind,
                            intent_id=intent_id,
                        )
                elif open_sym != symbol:
                    _ = self._close_all_positions_unlocked(reason="REBALANCE")
                    try:
                        positions = self._get_open_positions()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("precheck_position_failed", extra={"symbol": symbol, "err": type(e).__name__})
                        return self._entry_block(
                            symbol=symbol,
                            hint=exec_hint,
                            reason="PRECHECK_POSITION_FAILED",
                            event_kind=event_kind,
                            intent_id=intent_id,
                        )
                else:
                    # Same symbol; check direction. If opposite, close first.
                    if open_amt > 0 and direction == Direction.SHORT:
                        _ = self._close_position_unlocked(symbol, reason="REBALANCE")
                        try:
                            positions = self._get_open_positions()
                        except Exception as e:  # noqa: BLE001
                            logger.warning("precheck_position_failed", extra={"symbol": symbol, "err": type(e).__name__})
                            return self._entry_block(
                                symbol=symbol,
                                hint=exec_hint,
                                reason="PRECHECK_POSITION_FAILED",
                                event_kind=event_kind,
                                intent_id=intent_id,
                            )
                    elif open_amt < 0 and direction == Direction.LONG:
                        _ = self._close_position_unlocked(symbol, reason="REBALANCE")
                        try:
                            positions = self._get_open_positions()
                        except Exception as e:  # noqa: BLE001
                            logger.warning("precheck_position_failed", extra={"symbol": symbol, "err": type(e).__name__})
                            return self._entry_block(
                                symbol=symbol,
                                hint=exec_hint,
                                reason="PRECHECK_POSITION_FAILED",
                                event_kind=event_kind,
                                intent_id=intent_id,
                            )
                    else:
                        # Same symbol same direction: disallow for MVP.
                        raise ExecutionRejected("adding_to_position_not_allowed")

                if positions:
                    # Close-all/close should have cleared it; fail closed otherwise.
                    raise ExecutionRejected("single_asset_rule_unresolved")

            # Duplicate-entry prevention: if an open order already exists on the target symbol,
            # do not create a fresh entry order.
            try:
                existing_open = self._client.get_open_orders_usdtm([symbol])
                rows = existing_open.get(symbol) if isinstance(existing_open, Mapping) else None
                has_open = False
                if isinstance(rows, list):
                    for r in rows:
                        if not isinstance(r, Mapping):
                            continue
                        st0 = str(r.get("status") or "").upper()
                        if st0 in {"NEW", "PARTIALLY_FILLED"}:
                            has_open = True
                            break
                if has_open:
                    return self._entry_block(
                        symbol=symbol,
                        hint=exec_hint,
                        reason="open_entry_order_exists",
                        event_kind=event_kind,
                        intent_id=intent_id,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("precheck_open_orders_failed", extra={"symbol": symbol, "err": type(e).__name__})
                return self._entry_block(
                    symbol=symbol,
                    hint=exec_hint,
                    reason="PRECHECK_OPEN_ORDERS_FAILED",
                    event_kind=event_kind,
                    intent_id=intent_id,
                )

            # Determine reference price for sizing (post-close).
            blocked_live, qty_calc_live, _notional_calc_live, cap_live = self._budget_guard(
                symbol=symbol,
                side=side,
                exec_hint=exec_hint,
                cfg=cfg,
                qty_in=qty_in,
                notional_usdt=notional_usdt,
                leverage=lev_f,
                event_kind=event_kind,
                intent_id=intent_id,
            )
            if blocked_live is not None:
                if self._pnl:
                    try:
                        self._pnl.set_last_block_reason(str(blocked_live.get("block_reason") or "BUDGET_BLOCKED"))
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "pnl_set_last_block_reason_failed",
                            extra={"reason": str(blocked_live.get("block_reason") or "BUDGET_BLOCKED"), "err": type(e).__name__},
                            exc_info=True,
                        )
                return blocked_live

            try:
                price_ref = self._best_price_ref(symbol=symbol, side=side)
            except Exception:
                return self._entry_block(
                    symbol=symbol,
                    hint=exec_hint,
                    reason="MARKET_DATA_UNAVAILABLE",
                    event_kind=event_kind,
                    intent_id=intent_id,
                )
            if price_ref <= 0:
                return self._entry_block(
                    symbol=symbol,
                    hint=exec_hint,
                    reason="MARKET_DATA_UNAVAILABLE",
                    event_kind=event_kind,
                    intent_id=intent_id,
                )
            qty: Decimal = qty_calc_live

            is_market = exec_hint == ExecHint.MARKET
            qty = self._round_qty(symbol=symbol, qty=qty, is_market=is_market)
            self._check_min_notional(symbol=symbol, qty=qty, price_ref=price_ref)
            if qty <= 0:
                raise ExecutionValidationError("quantity_invalid")

            # ------------------------------------------------------------
            # RISK POLICY GUARD (hard block point before any real order)
            # This must run immediately before we submit orders to Binance.
            # If it returns BLOCK/PANIC, we must not place an order.
            # ------------------------------------------------------------
            if self._policy and self._pnl:
                try:
                    bal = self._client.get_account_balance_usdtm()
                    try:
                        pos = self._client.get_open_positions_any()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("precheck_position_failed", extra={"symbol": symbol, "err": type(e).__name__})
                        return self._entry_block(
                            symbol=symbol,
                            hint=exec_hint,
                            reason="PRECHECK_POSITION_FAILED",
                            event_kind=event_kind,
                            intent_id=intent_id,
                        )
                    wallet = float(bal.get("wallet") or 0.0)
                    upnl = sum(float(r.get("unrealized_pnl") or 0.0) for r in pos.values())
                    equity = wallet + upnl

                    # Update peak tracking before evaluation.
                    st = self._pnl.update_equity_peak(equity_usdt=equity)
                    metrics = self._pnl.compute_metrics(st=st, equity_usdt=equity)

                    try:
                        bt = self._book(symbol)
                    except Exception:
                        return self._entry_block(
                            symbol=symbol,
                            hint=exec_hint,
                            reason="MARKET_DATA_UNAVAILABLE",
                            event_kind=event_kind,
                            intent_id=intent_id,
                        )
                    bid = float(bt.get("bidPrice", 0) or 0.0)
                    ask = float(bt.get("askPrice", 0) or 0.0)

                    # Approximate exposure from open positions using current book mid.
                    total_exposure = 0.0
                    open_symbols = list(pos.keys())
                    for sym0, row0 in pos.items():
                        try:
                            bt0 = self._client.get_book_ticker(sym0)
                            bid0 = float(bt0.get("bidPrice", 0) or 0.0)
                            ask0 = float(bt0.get("askPrice", 0) or 0.0)
                            mid0 = (bid0 + ask0) / 2.0 if (bid0 and ask0) else 0.0
                            amt0 = float(row0.get("position_amt") or 0.0)
                            total_exposure += abs(amt0) * float(mid0 or 0.0)
                        except Exception:
                            return self._entry_block(
                                symbol=symbol,
                                hint=exec_hint,
                                reason="MARKET_DATA_UNAVAILABLE",
                                event_kind=event_kind,
                                intent_id=intent_id,
                            )

                    notional_est = float(qty * price_ref)
                    enriched_intent = dict(intent)
                    enriched_intent["symbol"] = symbol
                    enriched_intent["exec_hint"] = exec_hint
                    enriched_intent["notional_usdt_est"] = notional_est
                    enriched_intent["leverage"] = float(lev_f)

                    acc_state = {
                        "wallet_usdt": wallet,
                        "upnl_usdt": upnl,
                        "equity_usdt": equity,
                        "open_symbols": open_symbols,
                        "total_exposure_notional_usdt": total_exposure,
                    }
                    pnl_state = {
                        "day": st.day,
                        "daily_realized_pnl": st.daily_realized_pnl,
                        "equity_peak": st.equity_peak,
                        "lose_streak": st.lose_streak,
                        "cooldown_until": st.cooldown_until,
                        "daily_pnl_pct": metrics.daily_pnl_pct,
                        "drawdown_pct": metrics.drawdown_pct,
                    }
                    mkt_state = {"bid": bid, "ask": ask}

                    dec = self._policy.evaluate_pre_trade(
                        enriched_intent,
                        acc_state,
                        pnl_state,
                        mkt_state,
                    )
                    if dec.kind != "ALLOW":
                        reason = dec.reason or "risk_blocked"
                        self._pnl.set_last_block_reason(reason)
                        if reason == "cooldown_active" or reason == "lose_streak_cooldown":
                            self._emit(
                                "COOLDOWN",
                                {
                                    "symbol": symbol,
                                    "reason": reason,
                                    "until": dec.until.isoformat() if dec.until else None,
                                    "hours": float(cfg.cooldown_hours),
                                },
                            )
                        else:
                            self._emit("BLOCK", {"symbol": symbol, "reason": reason})
                        if dec.kind == "PANIC":
                            self._emit("PANIC", {"symbol": symbol, "reason": reason})
                            raise ExecutionRejected(f"risk_panic:{reason}")
                        raise ExecutionRejected(reason)

                    # Clear last block reason on success path.
                    self._pnl.set_last_block_reason(None)
                except ExecutionRejected:
                    raise
                except Exception as e:  # noqa: BLE001
                    # Guard failures should fail closed for safety.
                    logger.exception("risk_guard_failed", extra={"err": type(e).__name__})
                    if self._pnl:
                        try:
                            self._pnl.set_last_block_reason("risk_guard_failed")
                        except Exception as ie:  # noqa: BLE001
                            logger.warning("pnl_set_last_block_reason_failed", extra={"reason": "risk_guard_failed", "err": type(ie).__name__}, exc_info=True)
                    raise ExecutionRejected("risk_guard_failed") from e

            # Safety: clear any stale open orders for the target symbol before a new entry.
            try:
                canceled = self._client.cancel_all_open_orders(symbol=symbol)
                if self._oplog and canceled:
                    self._oplog.log_event(
                        "CANCEL_ALL",
                        {"intent_id": intent_id, "symbol": symbol, "action": "cancel_all", "reason": "pre_entry", "count": len(canceled)},
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("precheck_cancel_open_orders_failed", extra={"symbol": symbol, "err": type(e).__name__})
                return self._entry_block(
                    symbol=symbol,
                    hint=exec_hint,
                    reason="PRECHECK_OPEN_ORDERS_FAILED",
                    event_kind=event_kind,
                    intent_id=intent_id,
                )

            if exec_hint == ExecHint.MARKET:
                client_order_id = self._new_client_order_id(intent_id=intent_id, attempt=1)
                self._record_created(
                    intent_id=intent_id,
                    cycle_id=str(intent.get("cycle_id")) if intent.get("cycle_id") is not None else None,
                    symbol=symbol,
                    side=side,
                    order_type="MARKET",
                    reduce_only=False,
                    qty=qty,
                    price=price_ref,
                    time_in_force=None,
                    client_order_id=client_order_id,
                )
                try:
                    order = self._client.place_order_market(
                        symbol=symbol,
                        side=side,
                        quantity=float(qty),
                        reduce_only=False,
                        new_client_order_id=client_order_id,
                    )
                    self._record_sent_or_ack(client_order_id=client_order_id, order=order, fallback_status="ACK")
                except BinanceAuthError as e:
                    self._record_error(client_order_id=client_order_id, last_error="binance_auth_error")
                    raise ExecutionRejected("binance_auth_error") from e
                except BinanceHTTPError as e:
                    self._record_error(client_order_id=client_order_id, last_error=f"binance_http_{e.status_code}_code_{e.code}")
                    raise ExecutionRejected(f"binance_http_{e.status_code}_code_{e.code}") from e
                if self._pnl:
                    try:
                        from datetime import datetime, timezone

                        self._pnl.set_last_entry(symbol=symbol, at=datetime.now(tz=timezone.utc))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("pnl_set_last_entry_failed", extra={"symbol": symbol, "err": type(e).__name__}, exc_info=True)
                out = {"symbol": symbol, "hint": exec_hint.value, "intent_id": intent_id, "orders": [_safe_order(order)]}
                self._oplog_execution_from_order(
                    intent_id=intent_id,
                    symbol=symbol,
                    side=side,
                    reason=event_kind,
                    order=out["orders"][0],
                )
                self._emit(
                    event_kind,
                    {
                        "symbol": symbol,
                        "detail": {
                            **out,
                            "side": side,
                            "qty": float(qty),
                            "price_ref": float(price_ref),
                            "budget_cap_notional_usdt": float(cap_live.notional_usdt),
                        },
                    },
                )
                self._capture_snapshot(reason=event_kind, symbol=symbol, intent_id=intent_id)
                return out

            if exec_hint == ExecHint.LIMIT:
                try:
                    out = self._enter_limit_then_market(
                        symbol=symbol,
                        side=side,
                        qty=qty,
                        intent_id=intent_id,
                        cycle_id=(str(intent.get("cycle_id")) if intent.get("cycle_id") is not None else None),
                    )
                except ExecutionRejected as e:
                    if e.message == "book_ticker_unavailable":
                        return self._entry_block(
                            symbol=symbol,
                            hint=exec_hint,
                            reason="MARKET_DATA_UNAVAILABLE",
                            event_kind=event_kind,
                            intent_id=intent_id,
                        )
                    raise
                out["intent_id"] = intent_id
                for o in out.get("orders", []):
                    if isinstance(o, Mapping):
                        self._oplog_execution_from_order(
                            intent_id=intent_id,
                            symbol=symbol,
                            side=side,
                            reason=event_kind,
                            order=o,
                        )
                if self._pnl:
                    try:
                        from datetime import datetime, timezone

                        self._pnl.set_last_entry(symbol=symbol, at=datetime.now(tz=timezone.utc))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("pnl_set_last_entry_failed", extra={"symbol": symbol, "err": type(e).__name__}, exc_info=True)
                self._emit(
                    event_kind,
                    {
                        "symbol": symbol,
                        "detail": {
                            **out,
                            "side": side,
                            "qty": float(qty),
                            "price_ref": float(price_ref),
                            "budget_cap_notional_usdt": float(cap_live.notional_usdt),
                        },
                    },
                )
                self._capture_snapshot(reason=event_kind, symbol=symbol, intent_id=intent_id)
                return out

            if exec_hint == ExecHint.SPLIT:
                try:
                    out = self._enter_split(
                        symbol=symbol,
                        side=side,
                        qty=qty,
                        intent_id=intent_id,
                        cycle_id=(str(intent.get("cycle_id")) if intent.get("cycle_id") is not None else None),
                    )
                except ExecutionRejected as e:
                    if e.message == "book_ticker_unavailable":
                        return self._entry_block(
                            symbol=symbol,
                            hint=exec_hint,
                            reason="MARKET_DATA_UNAVAILABLE",
                            event_kind=event_kind,
                            intent_id=intent_id,
                        )
                    raise
                out["intent_id"] = intent_id
                for o in out.get("orders", []):
                    if isinstance(o, Mapping):
                        self._oplog_execution_from_order(
                            intent_id=intent_id,
                            symbol=symbol,
                            side=side,
                            reason=event_kind,
                            order=o,
                        )
                if self._pnl:
                    try:
                        from datetime import datetime, timezone

                        self._pnl.set_last_entry(symbol=symbol, at=datetime.now(tz=timezone.utc))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("pnl_set_last_entry_failed", extra={"symbol": symbol, "err": type(e).__name__}, exc_info=True)
                self._emit(
                    event_kind,
                    {
                        "symbol": symbol,
                        "detail": {
                            **out,
                            "side": side,
                            "qty": float(qty),
                            "price_ref": float(price_ref),
                            "budget_cap_notional_usdt": float(cap_live.notional_usdt),
                        },
                    },
                )
                self._capture_snapshot(reason=event_kind, symbol=symbol, intent_id=intent_id)
                return out

            raise ExecutionValidationError("unsupported_exec_hint")
        except ExecutionValidationError as e:
            self._emit("FAIL", {"op": op, "symbol": intent.get("symbol"), "error": e.message})
            raise
        except ExecutionRejected as e:
            self._emit("FAIL", {"op": op, "symbol": intent.get("symbol"), "error": e.message})
            raise
        except Exception as e:  # noqa: BLE001
            self._emit("FAIL", {"op": op, "symbol": intent.get("symbol"), "error": f"{type(e).__name__}: {e}"})
            raise

    async def rebalance(self, *, close_symbol: str, enter_intent: Mapping[str, Any]) -> Dict[str, Any]:
        locked = await self._acquire_exec_lock()
        if not locked:
            symbol = str(enter_intent.get("symbol", "")).strip().upper() or "UNKNOWN"
            return self._blocked_response(
                symbol=symbol,
                hint=ExecHint.LIMIT,
                reason="EXECUTION_LOCK_BUSY",
                event_kind="REBALANCE",
                intent_id=str(enter_intent.get("intent_id") or self._new_intent_id(symbol)),
            )
        try:
            close_sym = self._normalize_symbol(close_symbol)
            close_out = self._close_position_unlocked(close_sym, reason="REBALANCE")
            intent_payload = dict(enter_intent)
            intent_payload["op"] = "REBALANCE"
            enter_out = self._enter_position_unlocked(intent_payload)
            enter_out["rebalance_close"] = close_out
            return enter_out
        finally:
            self._exec_lock.release()

    async def panic(self) -> Dict[str, Any]:
        locked = await self._acquire_exec_lock()
        if not locked:
            raise ExecutionRejected("EXECUTION_LOCK_BUSY")
        try:
            return self._panic_unlocked()
        finally:
            self._exec_lock.release()

    def _panic_unlocked(self) -> Dict[str, Any]:
        if self._dry_run and self._dry_run_strict:
            raise ExecutionRejected("dry_run_strict_panic_blocked")
        # PANIC lock first.
        row = self._engine.panic()
        self._emit("PANIC", {"reason": "manual_panic"})
        panic_result = self._panic_guarded_close_all(force=True)
        self._emit(
            "PANIC_RESULT",
            {
                "ok": bool(panic_result.get("ok")),
                "canceled_orders_ok": bool(panic_result.get("canceled_orders_ok")),
                "close_ok": bool(panic_result.get("close_ok")),
                "errors": list(panic_result.get("errors") or []),
                "closed_symbol": panic_result.get("closed_symbol"),
                "closed_qty": panic_result.get("closed_qty"),
            },
        )
        logger.log(
            logging.INFO if bool(panic_result.get("ok")) else logging.WARNING,
            "panic_result",
            extra={"panic_result": panic_result},
        )
        # Best-effort cleanup. Do not raise; return structured result.
        info: Dict[str, Any] = {
            "engine_state": row.state.value,
            "updated_at": row.updated_at.isoformat(),
            "panic_result": panic_result,
        }
        self._capture_snapshot(reason="PANIC")
        return info

    def _panic_guarded_close(self, *, symbol: str, force: bool) -> Dict[str, Any]:
        try:
            if force:
                # bypass engine RUNNING check, but still block if PANIC is already set.
                return self._close_position_unlocked(symbol)
            return self._close_position_unlocked(symbol)
        except Exception as e:  # noqa: BLE001
            logger.exception("close_position_failed", extra={"symbol": symbol, "err": type(e).__name__})
            return {"ok": False, "symbol": symbol, "error": f"{type(e).__name__}: {e}"}

    def _panic_guarded_close_all(self, *, force: bool) -> Dict[str, Any]:
        # force=True is used for emergency cleanup paths (PANIC, forced symbol switch).
        errors: List[str] = []
        orders: List[Dict[str, Any]] = []
        cancels = 0
        canceled_orders_ok = True
        close_ok = True
        closed_symbol: Optional[str] = None
        closed_qty_total = 0.0
        try:
            if not force:
                self._require_not_panic()
            try:
                positions = self._get_open_positions()
            except Exception as e:  # noqa: BLE001
                return {
                    "ok": False,
                    "canceled_orders_ok": False,
                    "close_ok": False,
                    "errors": [f"positions:{type(e).__name__}:{e}"],
                    "closed_symbol": None,
                    "closed_qty": None,
                    "closed": False,
                    "canceled": 0,
                    "orders": [],
                }

            cancel_syms = set(self._allowed_symbols) | set(positions.keys())
            for sym in sorted(cancel_syms):
                try:
                    c = self._client.cancel_all_open_orders(symbol=sym)
                    cancels += len(c)
                    if self._oplog and c:
                        self._oplog.log_event(
                            "CANCEL_ALL",
                            {"symbol": sym, "action": "cancel_all", "reason": "panic_guarded_close_all", "count": len(c)},
                        )
                except Exception as e:  # noqa: BLE001
                    canceled_orders_ok = False
                    errors.append(f"cancel:{sym}:{type(e).__name__}:{e}")

            if positions:
                for sym, row in positions.items():
                    amt = float(row.get("position_amt", 0.0) or 0.0)
                    if abs(amt) <= 0:
                        continue
                    side = _direction_to_close_side(amt)
                    try:
                        qty = self._round_qty(symbol=sym, qty=_dec(abs(amt)), is_market=True)
                        panic_intent = self._new_intent_id(sym)
                        panic_cid = self._new_client_order_id(intent_id=panic_intent, attempt=1)
                        self._record_created(
                            intent_id=panic_intent,
                            cycle_id=None,
                            symbol=sym,
                            side=side,
                            order_type="MARKET",
                            reduce_only=True,
                            qty=qty,
                            price=None,
                            time_in_force=None,
                            client_order_id=panic_cid,
                        )
                        o = self._client.place_order_market(
                            symbol=sym,
                            side=side,
                            quantity=float(qty),
                            reduce_only=True,
                            new_client_order_id=panic_cid,
                        )
                        self._record_sent_or_ack(client_order_id=panic_cid, order=o, fallback_status="ACK")
                        so = _safe_order(o)
                        orders.append(so)
                        self._oplog_execution_from_order(
                            intent_id=None,
                            symbol=sym,
                            side=side,
                            reason="panic_guarded_close_all",
                            order=so,
                        )
                        closed_symbol = sym
                        try:
                            closed_qty_total += abs(float(so.get("executed_qty") or 0.0))
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                "panic_closed_qty_parse_failed",
                                extra={"symbol": sym, "executed_qty": so.get("executed_qty"), "err": type(e).__name__},
                                exc_info=True,
                            )
                    except Exception as e:  # noqa: BLE001
                        close_ok = False
                        errors.append(f"close:{sym}:{type(e).__name__}:{e}")
            ok = canceled_orders_ok and close_ok
            closed_flag = bool(orders)
            return {
                "ok": ok,
                "canceled_orders_ok": canceled_orders_ok,
                "close_ok": close_ok,
                "errors": errors,
                "closed_symbol": closed_symbol if len(orders) == 1 else None,
                "closed_qty": float(closed_qty_total) if closed_qty_total > 0 else None,
                "closed": closed_flag,
                "canceled": cancels,
                "orders": orders,
                "reason": "no_open_position" if not positions else None,
            }
        except Exception as e:  # noqa: BLE001
            logger.exception("panic_close_all_failed", extra={"err": type(e).__name__})
            errors.append(f"panic:{type(e).__name__}:{e}")
            return {
                "ok": False,
                "canceled_orders_ok": False,
                "close_ok": False,
                "errors": errors,
                "closed_symbol": None,
                "closed_qty": None,
                "closed": False,
                "canceled": cancels,
                "orders": orders,
            }

    def _market_fallback_allowed_now(self, *, symbol: str) -> bool:
        """Check whether MARKET is currently allowed by the spread guard config."""
        cfg = self._risk.get_config()
        bt = self._book(symbol)
        bid = float(bt.get("bidPrice", 0) or 0.0)
        ask = float(bt.get("askPrice", 0) or 0.0)
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return False
        mid = (ask + bid) / 2.0
        if mid <= 0.0:
            return False
        spread_ratio = (ask - bid) / mid
        max_ratio = float(cfg.spread_max_pct)
        if max_ratio > 0.1:
            max_ratio = max_ratio / 100.0
        if spread_ratio < max_ratio:
            return True
        return bool(cfg.allow_market_when_wide_spread)

    def _enter_limit_then_market(
        self,
        *,
        symbol: str,
        side: Side,
        qty: Decimal,
        intent_id: str,
        cycle_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        orders: List[Dict[str, Any]] = []
        cfg = self._risk.get_config()
        limit_timeout_sec = float(cfg.exec_limit_timeout_sec)
        # FINAL-2 semantics: cfg.exec_limit_retries == total LIMIT attempts (default 2 => 10s total).
        attempts = max(int(cfg.exec_limit_retries), 1)

        filled_total = _dec("0")
        remaining = qty
        last_err: Optional[str] = None
        used_market_fallback = False

        def _executed_qty_from(order: Mapping[str, Any]) -> Decimal:
            x = order.get("executedQty", order.get("executed_qty", 0))
            try:
                return _dec(x or 0)
            except Exception:
                return _dec("0")

        for attempt in range(1, attempts + 1):
            if remaining <= 0:
                break

            # Round remaining to a valid LIMIT quantity. If rounding makes it non-actionable, stop.
            try:
                remaining = self._round_qty(symbol=symbol, qty=remaining, is_market=False)
            except ExecutionValidationError:
                break
            if remaining <= 0:
                break

            price_ref = self._best_price_ref(symbol=symbol, side=side)
            px = self._round_price_for_side(symbol=symbol, side=side, px=price_ref)
            if px <= 0:
                raise ExecutionRejected("book_ticker_unavailable")

            try:
                client_order_id = self._new_client_order_id(intent_id=intent_id, attempt=attempt)
                self._record_created(
                    intent_id=intent_id,
                    cycle_id=cycle_id,
                    symbol=symbol,
                    side=side,
                    order_type="LIMIT",
                    reduce_only=False,
                    qty=remaining,
                    price=px,
                    time_in_force="GTC",
                    client_order_id=client_order_id,
                )
                try:
                    placed = self._client.place_order_limit(
                        symbol=symbol,
                        side=side,
                        quantity=float(remaining),
                        price=float(px),
                        post_only=False,
                        reduce_only=False,
                        new_client_order_id=client_order_id,
                    )
                    self._record_sent_or_ack(client_order_id=client_order_id, order=placed, fallback_status="ACK")
                except Exception as e:
                    # Timeout/network uncertainty: check by client_order_id before any resend.
                    if self._is_timeout_like_error(e):
                        found = self._query_order_by_client_order_id(symbol=symbol, client_order_id=client_order_id)
                        if found and str(found.get("status") or "").upper() not in {"", "CANCELED", "EXPIRED", "REJECTED"}:
                            placed = found
                            self._record_sent_or_ack(client_order_id=client_order_id, order=placed, fallback_status="SENT")
                        else:
                            self._record_error(client_order_id=client_order_id, last_error=f"{type(e).__name__}:{e}")
                            raise
                    else:
                        self._record_error(client_order_id=client_order_id, last_error=f"{type(e).__name__}:{e}")
                        raise
                orders.append(_safe_order(placed))
                oid = _extract_order_id(placed)
                if oid is None:
                    return {
                        "symbol": symbol,
                        "hint": ExecHint.LIMIT.value,
                        "orders": orders,
                        "filled_qty": float(filled_total),
                        "remaining_qty": float(remaining),
                        "market_fallback_used": used_market_fallback,
                    }

                deadline = time.monotonic() + limit_timeout_sec
                last_o: Optional[Mapping[str, Any]] = None
                filled_this_order = False
                while time.monotonic() < deadline:
                    o = self._client.get_order(symbol=symbol, order_id=oid)
                    last_o = o
                    self._record_sent_or_ack(client_order_id=client_order_id, order=o, fallback_status="SENT")
                    if _is_filled(o.get("status")):
                        orders.append(_safe_order(o))
                        filled_total += _executed_qty_from(o)
                        remaining = max(qty - filled_total, _dec("0"))
                        filled_this_order = True
                        break
                    time.sleep(max(0.01, min(0.2, deadline - time.monotonic())))

                if filled_this_order:
                    last_err = None
                    continue

                # Not fully filled within timeout -> cancel and capture partial fill if any.
                try:
                    _ = self._client.cancel_all_open_orders(symbol=symbol)
                except Exception as e:  # noqa: BLE001
                    logger.warning("limit_timeout_cancel_failed", extra={"symbol": symbol, "err": type(e).__name__}, exc_info=True)
                try:
                    o2 = self._client.get_order(symbol=symbol, order_id=oid)
                    orders.append(_safe_order(o2))
                    self._record_sent_or_ack(client_order_id=client_order_id, order=o2, fallback_status="SENT")
                    filled_total += _executed_qty_from(o2)
                except Exception:
                    if last_o is not None:
                        filled_total += _executed_qty_from(last_o)

                remaining = max(qty - filled_total, _dec("0"))
                last_err = "limit_timeout"
            except BinanceHTTPError as e:
                last_err = f"http_{e.status_code}_code_{e.code}"
            except BinanceAuthError:
                last_err = "binance_auth_error"
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"

        if remaining <= 0:
            return {
                "symbol": symbol,
                "hint": ExecHint.LIMIT.value,
                "orders": orders,
                "filled_qty": float(qty),
                "remaining_qty": 0.0,
                "market_fallback_used": used_market_fallback,
            }

        # LIMIT attempts exhausted; fallback to MARKET only if spread guard allows.
        if not self._market_fallback_allowed_now(symbol=symbol):
            raise ExecutionRejected("market_fallback_blocked_by_spread_guard")

        # Round remaining for market and enforce min notional.
        remaining_mkt = self._round_qty(symbol=symbol, qty=remaining, is_market=True)
        price_ref2 = self._best_price_ref(symbol=symbol, side=side)
        self._check_min_notional(symbol=symbol, qty=remaining_mkt, price_ref=price_ref2)

        try:
            market_attempt = attempts + 1
            market_cid = self._new_client_order_id(intent_id=intent_id, attempt=market_attempt)
            self._record_created(
                intent_id=intent_id,
                cycle_id=cycle_id,
                symbol=symbol,
                side=side,
                order_type="MARKET",
                reduce_only=False,
                qty=remaining_mkt,
                price=price_ref2,
                time_in_force=None,
                client_order_id=market_cid,
            )
            try:
                mkt = self._client.place_order_market(
                    symbol=symbol,
                    side=side,
                    quantity=float(remaining_mkt),
                    reduce_only=False,
                    new_client_order_id=market_cid,
                )
                self._record_sent_or_ack(client_order_id=market_cid, order=mkt, fallback_status="ACK")
            except Exception as e:
                if self._is_timeout_like_error(e):
                    found = self._query_order_by_client_order_id(symbol=symbol, client_order_id=market_cid)
                    if found and str(found.get("status") or "").upper() not in {"", "CANCELED", "EXPIRED", "REJECTED"}:
                        mkt = found
                        self._record_sent_or_ack(client_order_id=market_cid, order=mkt, fallback_status="SENT")
                    else:
                        self._record_error(client_order_id=market_cid, last_error=f"{type(e).__name__}:{e}")
                        raise
                else:
                    self._record_error(client_order_id=market_cid, last_error=f"{type(e).__name__}:{e}")
                    raise
        except BinanceAuthError as e:
            raise ExecutionRejected("binance_auth_error") from e
        except BinanceHTTPError as e:
            raise ExecutionRejected(f"binance_http_{e.status_code}_code_{e.code}") from e

        used_market_fallback = True
        orders.append(_safe_order(mkt))
        return {
            "symbol": symbol,
            "hint": ExecHint.LIMIT.value,
            "orders": orders,
            "filled_qty": float(qty),
            "remaining_qty": 0.0,
            "market_fallback_used": used_market_fallback,
            "last_limit_error": last_err,
        }

    def _enter_split(
        self,
        *,
        symbol: str,
        side: Side,
        qty: Decimal,
        intent_id: str,
        cycle_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Split qty into N parts and submit sequential LIMIT orders.
        parts = self._split_parts
        part_qty = qty / _dec(parts)
        # Round down each part; remainder is added to last part (rounded again).
        rounded_parts: List[Decimal] = []
        for _ in range(parts - 1):
            q = self._round_qty(symbol=symbol, qty=part_qty, is_market=False)
            rounded_parts.append(q)
        last = qty - sum(rounded_parts, start=_dec("0"))
        last = self._round_qty(symbol=symbol, qty=last, is_market=False)
        rounded_parts.append(last)

        orders: List[Dict[str, Any]] = []
        for q in rounded_parts:
            if q <= 0:
                continue
            res = self._enter_limit_then_market(
                symbol=symbol,
                side=side,
                qty=q,
                intent_id=intent_id,
                cycle_id=cycle_id,
            )
            orders.extend(res.get("orders", []))
        return {"symbol": symbol, "hint": ExecHint.SPLIT.value, "orders": orders}


def _extract_order_id(payload: Mapping[str, Any]) -> Optional[int]:
    if "orderId" in payload:
        try:
            return int(payload["orderId"])
        except Exception:
            return None
    if "order_id" in payload:
        try:
            return int(payload["order_id"])
        except Exception:
            return None
    return None


def _extract_exchange_order_id(order: Mapping[str, Any]) -> Optional[str]:
    oid = order.get("orderId", order.get("order_id"))
    if oid is None:
        return None
    return str(oid)


def _map_exchange_status_to_record(status: Any, *, default: str = "SENT") -> str:
    s = str(status or "").upper()
    mapping = {
        "NEW": "ACK",
        "PARTIALLY_FILLED": "PARTIAL",
        "FILLED": "FILLED",
        "CANCELED": "CANCELED",
        "EXPIRED": "EXPIRED",
        "REJECTED": "ERROR",
    }
    return mapping.get(s, default)


def _sanitize_env_token(v: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_-]+", "", str(v or "").upper())
    return token[:6] if token else "PROD"


def _make_client_order_id(*, env: str, intent_id: str, attempt: int) -> str:
    safe_intent = re.sub(r"[^A-Za-z0-9_-]+", "", str(intent_id or "intent"))
    base = f"BOT-{env}-{safe_intent}-{int(attempt)}"
    if len(base) <= 36:
        return base
    # Keep prefix+attempt readable, use hashed intent body for deterministic truncation.
    import hashlib

    digest = hashlib.sha1(safe_intent.encode("utf-8")).hexdigest()[:8]
    compact = f"BOT-{env}-{digest}-{int(attempt)}"
    return compact[:36]


def _safe_order(order: Mapping[str, Any]) -> Dict[str, Any]:
    # Keep a safe subset; never include signature/api keys.
    return {
        "symbol": order.get("symbol"),
        "order_id": order.get("orderId", order.get("order_id")),
        "client_order_id": order.get("clientOrderId", order.get("client_order_id")),
        "side": order.get("side"),
        "type": order.get("type"),
        "status": order.get("status"),
        "price": order.get("price"),
        "avg_price": order.get("avgPrice"),
        "orig_qty": order.get("origQty", order.get("orig_qty")),
        "executed_qty": order.get("executedQty", order.get("executed_qty")),
        "update_time": order.get("updateTime", order.get("time")),
    }
