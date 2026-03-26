from __future__ import annotations

from typing import Any

from v2.clean_room.contracts import KernelCycleResult
from v2.control import api as api_module

FutureTimeoutError = api_module.FutureTimeoutError
_run_async_blocking = api_module._run_async_blocking
_to_bool = api_module._to_bool
_to_float = api_module._to_float
_utcnow_iso = api_module._utcnow_iso
logger = api_module.logger


def _summarize_alpha_reject_focus(alpha_blocks: dict[str, Any]) -> str | None:
    ordered = [
        (str(alpha_id).strip(), str(reason).strip())
        for alpha_id, reason in alpha_blocks.items()
        if str(alpha_id).strip() and str(reason).strip()
    ]
    if not ordered:
        return None
    return ", ".join(f"{alpha_id}:{reason}" for alpha_id, reason in ordered[:3])


def fetch_live_positions(
    controller: Any,
) -> tuple[dict[str, float], dict[str, dict[str, Any]], bool, str | None]:
    rest_client = controller.rest_client
    if rest_client is None or not hasattr(rest_client, "get_positions"):
        return {}, {}, False, "live_positions_rest_unavailable"
    rest_client_any: Any = rest_client
    try:
        payload = _run_async_blocking(lambda: rest_client_any.get_positions(), timeout_sec=8.0)
    except FutureTimeoutError:
        logger.warning("live_positions_fetch_timed_out")
        return {}, {}, False, "live_positions_fetch_timeout"
    except Exception:  # noqa: BLE001
        logger.exception("live_positions_fetch_failed")
        return {}, {}, False, "live_positions_fetch_failed"
    if not isinstance(payload, list):
        return {}, {}, False, "live_positions_payload_invalid"
    out: dict[str, float] = {}
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        position_amt = _to_float(row.get("positionAmt"), default=0.0)
        out[symbol] = position_amt
        rows_by_symbol[symbol] = dict(row)
    return out, rows_by_symbol, True, None


def is_live_reentry_blocked(controller: Any) -> bool:
    allow_reentry = _to_bool(
        controller._risk.get("allow_reentry"),
        default=bool(controller.cfg.behavior.engine.allow_reentry),
    )
    if allow_reentry:
        return False
    if controller.cfg.mode != "live":
        return False
    if not controller._running:
        return False

    symbols_raw = controller._risk.get("universe_symbols")
    if isinstance(symbols_raw, list):
        symbols = [str(sym).strip().upper() for sym in symbols_raw if str(sym).strip()]
    else:
        symbols = [controller.cfg.behavior.exchange.default_symbol]
    if len(symbols) > 1:
        return False

    positions, _rows, ok, error_reason = controller._fetch_live_positions()
    if not ok:
        controller._set_state_uncertain(
            reason=str(error_reason or "live_positions_fetch_failed"),
            engage_safe_mode=True,
        )
        return True
    return any(
        abs(_to_float(position_amt, default=0.0)) > 0.0 for position_amt in positions.values()
    )


def run_cycle_once_locked(
    controller: Any,
    *,
    trigger_source: str = "scheduler",
) -> dict[str, Any]:
    controller._cycle_seq += 1
    cycle_seq = controller._cycle_seq
    controller._last_cycle["tick_started_at"] = _utcnow_iso()
    controller._last_cycle["last_error"] = None
    controller._last_cycle["bracket"] = None
    try:
        controller._refresh_runtime_risk_context()
        controller._sync_kernel_runtime_overrides()
        if hasattr(controller.kernel, "set_tick"):
            controller.kernel.set_tick(cycle_seq)
        controller._update_stale_transitions()
        freshness = controller._freshness_snapshot()
        submission_recovery = controller._submission_recovery_snapshot()
        if bool(freshness["market_data_stale"]):
            controller._maybe_probe_market_data()
            controller._update_stale_transitions()
            freshness = controller._freshness_snapshot()
            submission_recovery = controller._submission_recovery_snapshot()
        if controller._recovery_required:
            cycle = KernelCycleResult(
                state="blocked",
                reason="recovery_required",
                candidate=None,
            )
        elif not bool(submission_recovery["ok"]):
            cycle = KernelCycleResult(
                state="blocked",
                reason="submit_recovery_required",
                candidate=None,
            )
        elif controller._state_uncertain:
            cycle = KernelCycleResult(
                state="blocked",
                reason="state_uncertain",
                candidate=None,
            )
        elif bool(freshness["user_ws_stale"]):
            cycle = KernelCycleResult(
                state="blocked",
                reason="user_ws_stale",
                candidate=None,
            )
        elif bool(freshness["market_data_stale"]):
            cycle = KernelCycleResult(
                state="blocked",
                reason="market_data_stale",
                candidate=None,
            )
        else:
            reentry_blocked = controller._is_live_reentry_blocked()
            if controller._state_uncertain:
                cycle = KernelCycleResult(
                    state="blocked",
                    reason="state_uncertain",
                    candidate=None,
                )
            elif reentry_blocked:
                cycle = KernelCycleResult(
                    state="blocked",
                    reason="position_open",
                    candidate=None,
                )
            else:
                cycle = controller.kernel.run_once()
        portfolio_cycle = None
        portfolio_reader = getattr(controller.kernel, "last_portfolio_cycle", None)
        if callable(portfolio_reader):
            try:
                portfolio_cycle = portfolio_reader()
            except Exception:  # noqa: BLE001
                logger.exception("portfolio_cycle_read_failed")
        controller.scheduler.run_once()
        portfolio_results = (
            portfolio_cycle.results
            if portfolio_cycle is not None and isinstance(portfolio_cycle.results, list)
            else []
        )
        actionable_cycles = [
            item
            for item in portfolio_results
            if isinstance(item, KernelCycleResult) and item.state in {"executed", "dry_run"}
        ]
        if not actionable_cycles and cycle.state in {"executed", "dry_run"}:
            actionable_cycles = [cycle]

        if (
            controller.ops.can_open_new_entries()
            and controller._running
            and not controller._thread_stop.is_set()
        ):
            for actionable in actionable_cycles:
                submit_symbol = (
                    actionable.candidate.symbol
                    if actionable.candidate is not None
                    and str(actionable.candidate.symbol).strip()
                    else controller.cfg.behavior.exchange.default_symbol
                )
                controller.order_manager.submit({"symbol": submit_symbol, "mode": controller.cfg.mode})

        for actionable in actionable_cycles:
            controller._record_position_management_plan(cycle=actionable)
            if actionable.candidate is not None and actionable.size is not None:
                controller._log_event(
                    "position_entry_opened",
                    symbol=str(actionable.candidate.symbol or "").strip().upper(),
                    side=str(actionable.candidate.side or "").strip().upper(),
                    alpha_id=getattr(actionable.candidate, "alpha_id", None),
                    entry_family=getattr(actionable.candidate, "entry_family", None),
                    entry_price=getattr(actionable.candidate, "entry_price", None),
                    qty=getattr(actionable.size, "qty", None),
                    leverage=getattr(actionable.size, "leverage", None),
                    notional=getattr(actionable.size, "notional", None),
                    action=actionable.state,
                    order_id=(
                        getattr(actionable.execution, "order_id", None)
                        if actionable.execution is not None
                        else None
                    ),
                    event_time=controller._last_cycle["tick_finished_at"],
                )
            controller._place_brackets_for_cycle(cycle=actionable)

        controller._last_cycle["tick_finished_at"] = _utcnow_iso()
        controller._last_cycle["last_action"] = cycle.state
        controller._last_cycle["last_decision_reason"] = cycle.reason
        controller._last_cycle["candidate"] = (
            {
                "symbol": cycle.candidate.symbol,
                "side": cycle.candidate.side,
                "score": cycle.candidate.score,
                "source": getattr(cycle.candidate, "source", None),
                "alpha_id": getattr(cycle.candidate, "alpha_id", None),
                "entry_family": getattr(cycle.candidate, "entry_family", None),
                "regime_hint": getattr(cycle.candidate, "regime_hint", None),
                "regime_strength": getattr(cycle.candidate, "regime_strength", None),
                "volatility_hint": getattr(cycle.candidate, "volatility_hint", None),
            }
            if cycle.candidate is not None
            else None
        )
        controller._last_cycle["last_candidate"] = controller._last_cycle["candidate"]
        controller._last_cycle["portfolio"] = (
            {
                "slots_used": int(
                    _to_float(
                        getattr(portfolio_cycle, "open_position_count", 0),
                        default=0.0,
                    )
                )
                + len(actionable_cycles),
                "slots_total": int(
                    _to_float(getattr(portfolio_cycle, "max_open_positions", 0), default=0.0)
                ),
                "selected_candidates": [
                    {
                        "symbol": item.symbol,
                        "side": item.side,
                        "score": item.score,
                        "portfolio_score": getattr(item, "portfolio_score", None),
                        "bucket": getattr(item, "portfolio_bucket", None),
                        "alpha_id": getattr(item, "alpha_id", None),
                    }
                    for item in getattr(portfolio_cycle, "selected_candidates", [])
                    if item is not None
                ],
                "blocked_reasons": dict(getattr(portfolio_cycle, "blocked_reasons", {}) or {}),
            }
            if portfolio_cycle is not None
            else None
        )
        controller._risk["last_alpha_id"] = (
            getattr(cycle.candidate, "alpha_id", None) if cycle.candidate is not None else None
        )
        controller._risk["last_entry_family"] = (
            getattr(cycle.candidate, "entry_family", None)
            if cycle.candidate is not None
            else None
        )
        controller._risk["last_regime"] = (
            getattr(cycle.candidate, "regime_hint", None) if cycle.candidate is not None else None
        )
        reject_context_reader = getattr(controller.kernel, "get_last_no_candidate_context", None)
        reject_context = reject_context_reader() if callable(reject_context_reader) else None
        if cycle.state in {"blocked", "risk_rejected", "no_candidate"}:
            controller._risk["last_strategy_block_reason"] = cycle.reason
        elif cycle.state in {"executed", "dry_run"}:
            controller._risk["last_strategy_block_reason"] = None
        if cycle.state == "no_candidate" and isinstance(reject_context, dict):
            symbol_context = None
            for symbol in controller._risk.get("universe_symbols") or []:
                if symbol in reject_context and isinstance(reject_context[symbol], dict):
                    symbol_context = reject_context[symbol]
                    break
            if symbol_context is None and reject_context:
                symbol_context = next(
                    (value for value in reject_context.values() if isinstance(value, dict)),
                    None,
                )
            alpha_blocks = (
                dict(symbol_context.get("alpha_blocks") or {})
                if isinstance(symbol_context, dict)
                else {}
            )
            alpha_reject_metrics = (
                dict(symbol_context.get("alpha_reject_metrics") or {})
                if isinstance(symbol_context, dict)
                else {}
            )
            controller._risk["last_alpha_blocks"] = alpha_blocks
            controller._risk["last_alpha_reject_metrics"] = alpha_reject_metrics
            controller._risk["last_alpha_reject_focus"] = _summarize_alpha_reject_focus(alpha_blocks)
        else:
            controller._risk["last_alpha_blocks"] = {}
            controller._risk["last_alpha_reject_metrics"] = {}
            controller._risk["last_alpha_reject_focus"] = None
        overheat_reason = cycle.reason if str(cycle.reason).startswith("overheat_") else None
        controller._risk["overheat_state"] = {
            "blocked": overheat_reason is not None,
            "reason": overheat_reason,
        }
        cycle_error = cycle.reason if cycle.state == "execution_failed" else None
        existing_error = str(controller._last_cycle.get("last_error") or "").strip()
        controller._last_cycle["last_error"] = existing_error or cycle_error
        if cycle.state == "execution_failed" and "REVIEW_REQUIRED" in str(cycle.reason).upper():
            controller._set_state_uncertain(
                reason="submit_recovery_required",
                engage_safe_mode=True,
            )

        controller._report_stats["total_records"] += 1
        if cycle.state in {"executed", "dry_run"}:
            controller._report_stats["entries"] += 1
        if cycle.state == "execution_failed":
            controller._report_stats["errors"] += 1
        if cycle.state in {"blocked", "risk_rejected"}:
            controller._report_stats["blocks"] += 1
            controller._record_recent_block(cycle.reason)
        controller._maybe_apply_auto_risk_circuit(cycle)
        controller._persist_risk_config()
        controller._log_event(
            "cycle_result",
            action=cycle.state,
            reason=cycle.reason,
            candidate_symbol=(
                getattr(cycle.candidate, "symbol", None) if cycle.candidate is not None else None
            ),
            candidate_side=(
                getattr(cycle.candidate, "side", None) if cycle.candidate is not None else None
            ),
            notify_interval_sec=int(
                _to_float(controller._risk.get("notify_interval_sec"), default=30.0)
            ),
            trigger_source=trigger_source,
            event_time=controller._last_cycle["tick_finished_at"],
        )
        ok = True
        error_message = None
        controller._cycle_done_seq = cycle_seq
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).strip()
        if detail:
            error_message = f"cycle_failed:{type(exc).__name__}:{detail}"
        else:
            error_message = f"cycle_failed:{type(exc).__name__}"
        logger.exception("runtime_cycle_failed")
        controller._last_cycle["tick_finished_at"] = _utcnow_iso()
        controller._last_cycle["last_action"] = "error"
        controller._last_cycle["last_decision_reason"] = error_message
        controller._last_cycle["candidate"] = None
        controller._last_cycle["last_candidate"] = None
        controller._last_cycle["portfolio"] = None
        controller._last_cycle["last_error"] = error_message
        controller._last_cycle["bracket"] = None
        controller._report_stats["total_records"] += 1
        controller._report_stats["errors"] += 1
        controller._log_event(
            "cycle_result",
            action="error",
            reason=error_message,
            notify_interval_sec=int(
                _to_float(controller._risk.get("notify_interval_sec"), default=30.0)
            ),
            trigger_source=trigger_source,
            event_time=controller._last_cycle["tick_finished_at"],
        )
        ok = False
        controller._cycle_done_seq = cycle_seq

    controller._emit_status_update()

    out: dict[str, Any] = {
        "ok": ok,
        "tick_sec": float(controller.scheduler.tick_seconds),
        "snapshot": dict(controller._last_cycle),
    }
    if error_message is not None:
        out["error"] = error_message
    return out
