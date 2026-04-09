from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from typing import Any, Literal

from v2.common.async_bridge import run_async_blocking
from v2.control.position_management_runtime import handle_take_profit_rearm
from v2.notify.runtime_events import build_bracket_exit_notification

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _clamp(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def maybe_trigger_symbol_sl_flatten(controller: Any, *, trigger_symbol: str) -> None:
    symbol = str(trigger_symbol).strip().upper()
    if not symbol:
        return
    if symbol in controller._sl_flatten_inflight_symbols:
        return

    cooldown_sec = max(5.0, _to_float(controller._risk.get("sl_flatten_cooldown_sec"), default=60.0))
    now_mono = time.monotonic()
    last_mono = float(controller._sl_last_flatten_mono_by_symbol.get(symbol) or 0.0)
    if last_mono > 0.0 and (now_mono - last_mono) < cooldown_sec:
        return

    controller._sl_flatten_inflight_symbols.add(symbol)
    controller._sl_last_flatten_mono_by_symbol[symbol] = now_mono
    controller._watchdog_state["last_sl_flatten_symbol"] = symbol
    controller._watchdog_state["last_sl_flatten_triggered_at"] = _utcnow_iso()
    try:
        _ = run_async_blocking(
            lambda s=symbol: controller.close_position(
                symbol=s,
                notify_reason="stoploss_forced_close",
            ),
            timeout_sec=30.0,
        )
    except Exception:  # noqa: BLE001
        logger.exception("sl_symbol_flatten_failed symbol=%s", symbol)
    finally:
        controller._sl_flatten_inflight_symbols.discard(symbol)


def position_pnl_pct(row: dict[str, Any]) -> float | None:
    position_amt = _to_float(row.get("positionAmt"), default=0.0)
    if abs(position_amt) <= 0.0:
        return None
    entry_price = _to_float(row.get("entryPrice"), default=0.0)
    mark_price = _to_float(row.get("markPrice"), default=0.0)
    if entry_price <= 0.0 or mark_price <= 0.0:
        return None
    if position_amt > 0.0:
        return ((mark_price - entry_price) / entry_price) * 100.0
    return ((entry_price - mark_price) / entry_price) * 100.0


def trailing_distance_pct(controller: Any, *, row: dict[str, Any]) -> float:
    mode = str(controller._risk.get("trailing_mode") or "PCT").strip().upper()
    if mode == "ATR":
        min_pct = _to_float(controller._risk.get("atr_trail_min_pct"), default=0.6)
        max_pct = _to_float(controller._risk.get("atr_trail_max_pct"), default=1.8)
        return _clamp(min_pct, 0.0, max(min_pct, max_pct))
    return max(0.0, _to_float(controller._risk.get("trail_distance_pnl_pct"), default=0.8))


def maybe_trigger_trailing_exit(
    controller: Any,
    *,
    symbol: str,
    row: dict[str, Any],
    rest_client: Any,
) -> bool:
    if not _to_bool(controller._risk.get("trailing_enabled"), default=False):
        return False

    pnl_pct = position_pnl_pct(row)
    if pnl_pct is None:
        return False

    now = time.monotonic()
    state = controller._trailing_state.get(symbol) or {
        "first_seen_mono": now,
        "peak_pnl_pct": pnl_pct,
        "armed": False,
    }
    first_seen = _to_float(state.get("first_seen_mono"), default=now)
    peak = max(_to_float(state.get("peak_pnl_pct"), default=pnl_pct), pnl_pct)
    arm_pct = max(0.0, _to_float(controller._risk.get("trail_arm_pnl_pct"), default=1.2))
    grace_minutes = max(0, int(_to_float(controller._risk.get("trail_grace_minutes"), default=0.0)))
    distance_pct = trailing_distance_pct(controller, row=row)

    state["peak_pnl_pct"] = peak
    state["armed"] = bool(state.get("armed")) or peak >= arm_pct
    state["first_seen_mono"] = first_seen
    controller._trailing_state[symbol] = state

    controller._watchdog_state["last_trailing_symbol"] = symbol
    controller._watchdog_state["last_trailing_pnl_pct"] = round(float(pnl_pct), 4)
    controller._watchdog_state["last_trailing_peak_pct"] = round(float(peak), 4)
    controller._watchdog_state["last_trailing_distance_pct"] = round(float(distance_pct), 4)

    if grace_minutes > 0 and (now - first_seen) < float(grace_minutes * 60):
        return False
    if not bool(state.get("armed")):
        return False
    if pnl_pct > (peak - distance_pct):
        return False

    position_amt = _to_float(row.get("positionAmt"), default=0.0)
    if abs(position_amt) <= 0.0:
        return False
    exit_side: Literal["BUY", "SELL"] = "SELL" if position_amt > 0.0 else "BUY"
    position_side = str(row.get("positionSide") or "BOTH").strip().upper() or "BOTH"
    try:
        _ = run_async_blocking(
            lambda s=symbol, side=exit_side, qty=abs(position_amt), ps=position_side: (
                rest_client.close_position_market(
                    symbol=s,
                    side=side,
                    quantity=qty,
                    position_side=ps,
                )
            ),
            timeout_sec=8.0,
        )
        _ = run_async_blocking(
            lambda s=symbol: controller._bracket_service.cleanup_if_flat(symbol=s, position_amt=0.0),
            timeout_sec=8.0,
        )
    except Exception:  # noqa: BLE001
        logger.exception("trailing_exit_failed symbol=%s", symbol)
        return False

    controller._watchdog_state["last_trailing_triggered_symbol"] = symbol
    controller._watchdog_state["last_trailing_triggered_at"] = _utcnow_iso()
    controller._watchdog_state["last_trailing_triggered_pnl_pct"] = round(float(pnl_pct), 4)
    controller._trailing_state.pop(symbol, None)
    return True


def is_managed_bracket_algo_id(value: str | None) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("v2tp") or text.startswith("v2sl")


def list_tracked_brackets(controller: Any) -> list[dict[str, Any]]:
    rows = controller.state_store.runtime_storage().list_bracket_states()
    tracked: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        state = str(row.get("state") or "").strip().upper()
        if not symbol or state == "CLEANED":
            continue
        tracked.append(row)
    return tracked


def has_recent_fill_for_client_id(
    controller: Any,
    *,
    symbol: str,
    client_id: str,
    lookback_sec: float = 900.0,
) -> bool:
    symbol_u = symbol.upper()
    client_id_s = str(client_id or "").strip()
    if not client_id_s:
        return False
    lookback_ms = max(1, int(float(lookback_sec) * 1000.0))
    now_ms = int(time.time() * 1000)
    fills = controller.state_store.runtime_storage().recent_fills(limit=100)
    for row in fills:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or "").strip().upper()
        if row_symbol != symbol_u:
            continue
        row_client_id = str(row.get("client_id") or "").strip()
        if row_client_id != client_id_s:
            continue
        fill_ms = int(_to_float(row.get("fill_time_ms"), default=0.0))
        if fill_ms > 0 and now_ms - fill_ms > lookback_ms:
            continue
        return True
    return False


def latest_recent_fill_for_client_id(
    controller: Any,
    *,
    symbol: str,
    client_id: str,
    lookback_sec: float = 900.0,
) -> dict[str, Any] | None:
    symbol_u = symbol.upper()
    client_id_s = str(client_id or "").strip()
    if not client_id_s:
        return None
    lookback_ms = max(1, int(float(lookback_sec) * 1000.0))
    now_ms = int(time.time() * 1000)
    fills = controller.state_store.runtime_storage().recent_fills(limit=100)
    for row in fills:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or "").strip().upper()
        if row_symbol != symbol_u:
            continue
        row_client_id = str(row.get("client_id") or "").strip()
        if row_client_id != client_id_s:
            continue
        fill_ms = int(_to_float(row.get("fill_time_ms"), default=0.0))
        if fill_ms > 0 and now_ms - fill_ms > lookback_ms:
            continue
        return dict(row)
    return None


def infer_flat_bracket_exit(
    controller: Any,
    *,
    symbol: str,
    tp_id: str,
    sl_id: str,
) -> tuple[Literal["TP", "SL"], str | None] | None:
    tp_fill = controller._latest_recent_fill_for_client_id(symbol=symbol, client_id=tp_id)
    sl_fill = controller._latest_recent_fill_for_client_id(symbol=symbol, client_id=sl_id)

    if tp_fill is not None or sl_fill is not None:
        tp_fill_ms = int(_to_float((tp_fill or {}).get("fill_time_ms"), default=0.0))
        sl_fill_ms = int(_to_float((sl_fill or {}).get("fill_time_ms"), default=0.0))
        if tp_fill is not None and (sl_fill is None or tp_fill_ms >= sl_fill_ms):
            return "TP", tp_id or None
        if sl_fill is not None:
            return "SL", sl_id or None

    realized = controller._resolve_symbol_realized_pnl(symbol=symbol)
    if realized is None:
        return None
    return ("TP" if realized > 0.0 else "SL"), None


def repair_position_bracket_from_plan(
    controller: Any,
    *,
    symbol: str,
    position_row: dict[str, Any],
    plan: dict[str, Any],
    reason: str,
) -> bool:
    position_amt = _to_float(position_row.get("positionAmt"), default=0.0)
    if abs(position_amt) <= 0.0:
        return False
    quantity = abs(position_amt)
    take_profit_price = _to_float(plan.get("take_profit_price"), default=0.0)
    stop_loss_price = _to_float(plan.get("stop_price"), default=0.0)
    if quantity <= 0.0 or take_profit_price <= 0.0 or stop_loss_price <= 0.0:
        return False
    entry_side: Literal["BUY", "SELL"] = "BUY" if position_amt > 0.0 else "SELL"
    position_side = str(position_row.get("positionSide") or "BOTH").strip().upper() or "BOTH"
    return controller._replace_management_bracket(
        symbol=symbol,
        entry_side=entry_side,
        position_side=position_side,
        quantity=float(quantity),
        take_profit_price=float(take_profit_price),
        stop_loss_price=float(stop_loss_price),
        reason=reason,
    )


def latest_symbol_realized_pnl(controller: Any, *, symbol: str, lookback_sec: float = 900.0) -> float | None:
    symbol_u = symbol.upper()
    lookback_ms = max(1, int(float(lookback_sec) * 1000.0))
    now_ms = int(time.time() * 1000)
    for row in controller.state_store.runtime_storage().recent_fills(limit=100):
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").strip().upper() != symbol_u:
            continue
        realized_pnl = row.get("realized_pnl")
        if realized_pnl is None:
            continue
        fill_ms = int(_to_float(row.get("fill_time_ms"), default=0.0))
        if fill_ms is not None and now_ms - int(fill_ms) > lookback_ms:
            continue
        return _to_float(realized_pnl, default=0.0)

    fills = controller.state_store.get().last_fills
    for fill in reversed(fills):
        if str(fill.symbol or "").strip().upper() != symbol_u:
            continue
        if fill.realized_pnl is None:
            continue
        fill_ms = fill.fill_time_ms
        if fill_ms is not None and now_ms - int(fill_ms) > lookback_ms:
            continue
        return _to_float(fill.realized_pnl, default=0.0)
    return None


def latest_symbol_realized_pnl_from_income(
    controller: Any,
    *,
    symbol: str,
    lookback_sec: float = 900.0,
) -> float | None:
    rest_client = controller.rest_client
    if (
        controller.cfg.mode != "live"
        or rest_client is None
        or not hasattr(rest_client, "signed_request")
    ):
        return None

    symbol_u = symbol.upper()
    rest_client_any: Any = rest_client
    try:
        payload = run_async_blocking(
            lambda s=symbol_u: rest_client_any.signed_request(
                "GET",
                "/fapi/v1/income",
                params={"symbol": s, "incomeType": "REALIZED_PNL", "limit": 10},
            ),
            timeout_sec=8.0,
        )
    except FutureTimeoutError:
        logger.warning("income_history_fetch_timed_out symbol=%s", symbol_u)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("income_history_fetch_failed symbol=%s", symbol_u)
        return None

    if not isinstance(payload, list):
        return None

    lookback_ms = max(1, int(float(lookback_sec) * 1000.0))
    now_ms = int(time.time() * 1000)
    for row in payload:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or "").strip().upper()
        if row_symbol and row_symbol != symbol_u:
            continue
        income_raw = row.get("income")
        if income_raw is None:
            continue
        when_ms = int(_to_float(row.get("time"), default=0.0))
        if when_ms > 0 and now_ms - when_ms > lookback_ms:
            continue
        return _to_float(income_raw, default=0.0)
    return None


def resolve_symbol_realized_pnl(controller: Any, *, symbol: str) -> float | None:
    realized = controller._latest_symbol_realized_pnl(symbol=symbol)
    if realized is not None:
        return realized
    return controller._latest_symbol_realized_pnl_from_income(symbol=symbol)


def emit_bracket_exit_alert(controller: Any, *, symbol: str, outcome: Literal["TP", "SL"]) -> None:
    realized = controller._resolve_symbol_realized_pnl(symbol=symbol)
    normalized_realized = realized
    if realized is not None and abs(realized) < 0.00005:
        normalized_realized = 0.0
    try:
        _ = controller.notifier.send_notification(
            build_bracket_exit_notification(
                symbol=symbol,
                outcome=outcome,
                realized_pnl=normalized_realized,
                context=controller._notification_context(),
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception("bracket_exit_notify_failed symbol=%s outcome=%s", symbol, outcome)
    controller._log_event(
        "position_closed",
        symbol=symbol,
        reason="take_profit" if outcome == "TP" else "stop_loss",
        realized_pnl=normalized_realized,
        outcome=outcome,
        event_time=_utcnow_iso(),
    )


def poll_brackets_once(controller: Any) -> None:
    rest_client = controller.rest_client
    if controller.cfg.mode != "live" or rest_client is None:
        return
    rest_client_any: Any = rest_client

    tracked = controller._list_tracked_brackets()
    management_state = controller._load_position_management_state()
    if not tracked and not management_state:
        return

    positions, position_rows, positions_ok, _position_error = controller._fetch_live_positions()
    snapshot = None
    probe = getattr(controller.kernel, "probe_market_data", None)
    if callable(probe):
        try:
            snapshot = probe()
        except Exception:  # noqa: BLE001
            logger.exception("position_management_market_probe_failed")

    tracked_by_symbol = {
        str(row.get("symbol") or "").strip().upper(): row
        for row in tracked
        if str(row.get("symbol") or "").strip()
    }
    symbols = sorted(
        {
            *tracked_by_symbol.keys(),
            *management_state.keys(),
            *(str(symbol).strip().upper() for symbol in position_rows.keys()),
        }
    )
    next_management_state = dict(management_state)

    for symbol in symbols:
        if not symbol:
            continue
        row = tracked_by_symbol.get(symbol)
        tp_id = str((row or {}).get("tp_order_client_id") or "").strip()
        sl_id = str((row or {}).get("sl_order_client_id") or "").strip()

        open_orders: list[dict[str, Any]] = []
        if tp_id or sl_id:
            try:
                open_orders = run_async_blocking(
                    lambda s=symbol: rest_client_any.get_open_algo_orders(symbol=s),
                    timeout_sec=8.0,
                )
            except FutureTimeoutError:
                logger.warning("open_algo_orders_fetch_timed_out symbol=%s", symbol)
                continue
            except Exception:  # noqa: BLE001
                logger.exception("open_algo_orders_fetch_failed symbol=%s", symbol)
                continue

        open_ids: set[str] = set()
        if isinstance(open_orders, list):
            for item in open_orders:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("clientAlgoId") or item.get("clientOrderId") or "").strip()
                if cid:
                    open_ids.add(cid)
        if tp_id or sl_id:
            extra_managed_ids = {
                cid
                for cid in open_ids
                if cid not in {tp_id, sl_id} and controller._is_managed_bracket_algo_id(cid)
            }
            for cid in sorted(extra_managed_ids):
                try:
                    _ = run_async_blocking(
                        lambda s=symbol, current_id=cid: rest_client_any.cancel_algo_order(
                            params={"symbol": s, "clientAlgoId": current_id}
                        ),
                        timeout_sec=8.0,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "extra_bracket_algo_cancel_failed symbol=%s client_algo_id=%s",
                        symbol,
                        cid,
                    )

        position_amt = _to_float(positions.get(symbol), default=0.0) if positions_ok else 0.0
        position_row = position_rows.get(symbol)
        management_plan = next_management_state.get(symbol)
        position_is_open = positions_ok and abs(position_amt) > 0.0
        position_is_flat = positions_ok and not position_is_open

        if position_is_open and isinstance(position_row, dict) and isinstance(management_plan, dict):
            exited, updated_plan = controller._maybe_manage_open_position(
                symbol=symbol,
                row=position_row,
                plan=dict(management_plan),
                market_bar=(
                    controller._extract_latest_market_bar(snapshot, symbol=symbol)
                    if isinstance(snapshot, dict)
                    else None
                ),
            )
            if exited:
                next_management_state.pop(symbol, None)
                continue
            next_management_state[symbol] = dict(updated_plan)
            management_plan = next_management_state.get(symbol)

        if position_is_open and isinstance(position_row, dict):
            if controller._maybe_trigger_trailing_exit(
                symbol=symbol,
                row=position_row,
                rest_client=rest_client_any,
            ):
                next_management_state.pop(symbol, None)
                continue

        tp_open = tp_id in open_ids if tp_id else False
        sl_open = sl_id in open_ids if sl_id else False

        if tp_id and sl_id and tp_open != sl_open:
            outcome: Literal["TP", "SL"] = "SL" if tp_open else "TP"
            filled_id = sl_id if outcome == "SL" else tp_id
            fill_confirmed = controller._has_recent_fill_for_client_id(
                symbol=symbol,
                client_id=filled_id,
            )
            if position_is_flat or fill_confirmed:
                try:
                    _ = run_async_blocking(
                        lambda s=symbol, cid=filled_id: controller._bracket_service.on_leg_filled(
                            symbol=s,
                            filled_client_algo_id=cid,
                        ),
                        timeout_sec=8.0,
                    )
                    if outcome == "TP" and isinstance(management_plan, dict):
                        if handle_take_profit_rearm(
                            controller,
                            symbol=symbol,
                            filled_client_id=filled_id,
                            management_plan=dict(management_plan),
                        ):
                            next_management_state[symbol] = controller._load_position_management_state().get(
                                symbol,
                                dict(management_plan),
                            )
                            continue

                    next_management_state.pop(symbol, None)
                    controller._emit_bracket_exit_alert(symbol=symbol, outcome=outcome)
                    if outcome == "SL":
                        controller._maybe_trigger_symbol_sl_flatten(trigger_symbol=symbol)
                except Exception:  # noqa: BLE001
                    logger.exception("bracket_on_leg_filled_failed symbol=%s", symbol)
                continue

            if (
                position_is_open
                and isinstance(position_row, dict)
                and isinstance(management_plan, dict)
                and controller._repair_position_bracket_from_plan(
                    symbol=symbol,
                    position_row=position_row,
                    plan=management_plan,
                    reason="missing_bracket_leg_repair",
                )
            ):
                continue

            logger.warning(
                "bracket_leg_missing_without_exit_confirmation "
                "symbol=%s tp_open=%s sl_open=%s position_amt=%s",
                symbol,
                tp_open,
                sl_open,
                round(float(position_amt), 8),
            )
            continue

        if (
            position_is_open
            and isinstance(position_row, dict)
            and isinstance(management_plan, dict)
            and (not tp_id or not sl_id or not tp_open or not sl_open)
        ):
            if controller._repair_position_bracket_from_plan(
                symbol=symbol,
                position_row=position_row,
                plan=management_plan,
                reason="missing_bracket_repair",
            ):
                continue

        if position_is_flat:
            next_management_state.pop(symbol, None)
            if tp_id or sl_id:
                inferred_exit = controller._infer_flat_bracket_exit(
                    symbol=symbol,
                    tp_id=tp_id,
                    sl_id=sl_id,
                )
                if inferred_exit is not None:
                    outcome, filled_id = inferred_exit
                    try:
                        if filled_id:
                            _ = run_async_blocking(
                                lambda s=symbol, cid=filled_id: controller._bracket_service.on_leg_filled(
                                    symbol=s,
                                    filled_client_algo_id=cid,
                                ),
                                timeout_sec=8.0,
                            )
                        else:
                            _ = run_async_blocking(
                                lambda s=symbol: controller._bracket_service.cleanup_if_flat(
                                    symbol=s,
                                    position_amt=0.0,
                                ),
                                timeout_sec=8.0,
                            )
                        controller._emit_bracket_exit_alert(symbol=symbol, outcome=outcome)
                        if outcome == "SL":
                            controller._maybe_trigger_symbol_sl_flatten(trigger_symbol=symbol)
                    except Exception:  # noqa: BLE001
                        logger.exception("flat_bracket_exit_recovery_failed symbol=%s", symbol)
                    continue
                try:
                    _ = run_async_blocking(
                        lambda s=symbol: controller._bracket_service.cleanup_if_flat(
                            symbol=s,
                            position_amt=0.0,
                        ),
                        timeout_sec=8.0,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("bracket_cleanup_if_flat_failed symbol=%s", symbol)
            continue

    controller._save_position_management_state(next_management_state)


def start_bracket_loop(controller: Any) -> None:
    if controller.cfg.mode != "live" or controller.rest_client is None:
        return
    if controller._bracket_thread is not None and controller._bracket_thread.is_alive():
        return

    def _worker() -> None:
        while not controller._bracket_thread_stop.is_set():
            interval = max(
                5.0, _to_float(controller._risk.get("watchdog_interval_sec"), default=15.0)
            )
            if controller._bracket_thread_stop.wait(timeout=interval):
                break
            controller._poll_brackets_once()

    controller._bracket_thread = threading.Thread(target=_worker, daemon=True)
    controller._bracket_thread.start()
