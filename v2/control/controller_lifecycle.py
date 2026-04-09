from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from v2.control.mutating_core_helpers import (
    apply_tick_busy_cycle_state,
    capture_last_cycle_snapshot,
    capture_runtime_state,
)
from v2.control.mutating_responses import (
    build_panic_response,
    build_state_response,
    build_tick_scheduler_response,
)
from v2.control.position_management_runtime import maybe_handle_fill_event_fast_path
from v2.control.presentation import build_reconcile_response, build_scheduler_response
from v2.notify.runtime_events import build_position_close_notification


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def active_worker_thread_locked(controller: Any) -> threading.Thread | None:
    thread = controller._thread
    if thread is not None and not thread.is_alive():
        controller._thread = None
        return None
    return thread


def join_worker_thread(
    controller: Any,
    thread: threading.Thread | None,
    *,
    timeout_sec: float = 2.0,
) -> None:
    if thread is None or thread is threading.current_thread():
        return
    if thread.is_alive():
        thread.join(timeout=timeout_sec)
    with controller._lock:
        if controller._thread is thread and not thread.is_alive():
            controller._thread = None


def loop_worker(controller: Any) -> None:
    while not controller._thread_stop.is_set():
        with controller._lock:
            if not controller._running:
                break
            controller._run_cycle_once_locked(trigger_source="scheduler")
        controller._thread_stop.wait(timeout=max(0.2, float(controller.scheduler.tick_seconds)))


def start_runtime(controller: Any) -> dict[str, Any]:
    thread_to_start: threading.Thread | None = None
    resumed_running_gate = False
    with controller._lock:
        controller._auto_reconcile_if_recovery_required(reason="operator_start")
        active_thread = active_worker_thread_locked(controller)
        if controller._running and active_thread is not None:
            ops_state = controller.state_store.get().operational
            if bool(ops_state.paused) or bool(ops_state.safe_mode):
                controller.ops.resume()
                resumed_running_gate = True
                if controller._risk.get("last_block_reason") in {"ops_paused", "safe_mode"}:
                    controller._risk["last_block_reason"] = None
            state = capture_runtime_state(controller.state_store)
            result = build_state_response(runtime_state=state)
            controller._emit_status_update(force=resumed_running_gate)
            return result
        controller.state_store.set(status="RUNNING")
        controller.ops.resume()
        if controller._risk.get("last_block_reason") in {"ops_paused", "safe_mode"}:
            controller._risk["last_block_reason"] = None
        controller._running = True
        controller._thread_stop.clear()
        thread_to_start = threading.Thread(target=controller._loop_worker, daemon=True)
        controller._thread = thread_to_start
        controller._log_event("runtime_start", running=True)
        state = capture_runtime_state(controller.state_store)
        result = build_state_response(runtime_state=state)
    assert thread_to_start is not None
    thread_to_start.start()
    return result


def stop_runtime(controller: Any, *, emit_event: bool = True) -> dict[str, Any]:
    thread_to_join: threading.Thread | None = None
    with controller._lock:
        controller._running = False
        controller._thread_stop.set()
        controller.ops.pause()
        controller.state_store.set(status="PAUSED")
        thread_to_join = controller._thread
        if emit_event:
            controller._log_event("runtime_stop", running=False)
        state = capture_runtime_state(controller.state_store)
        result = build_state_response(runtime_state=state)
    join_worker_thread(controller, thread_to_join)
    controller._emit_status_update(force=True)
    return result


async def panic_runtime(controller: Any) -> dict[str, Any]:
    stop_runtime(controller, emit_event=False)
    controller.ops.safe_mode()
    controller.state_store.set(status="KILLED")
    controller._log_event("panic_triggered", action="panic")
    controller._emit_status_update(force=True)
    result = await controller.ops.flatten(symbol=controller.cfg.behavior.exchange.default_symbol)
    controller._log_event(
        "flatten_requested",
        action="panic_flatten",
        symbol=result.symbol,
    )
    _ = controller.notifier.send_notification(
        build_position_close_notification(
            symbol=result.symbol,
            reason="panic_close",
            context=controller._notification_context(),
        )
    )
    state = capture_runtime_state(controller.state_store)
    controller._report_stats["closes"] += 1
    return build_panic_response(
        runtime_state=state,
        flatten_result=result,
    )


async def handle_user_stream_event(controller: Any, event: dict[str, Any]) -> None:
    controller.state_store.apply_exchange_event(
        event=event,
        reason="user_stream_event",
    )
    _ = maybe_handle_fill_event_fast_path(controller, event=event)
    controller._user_stream_last_event_at = _utcnow_iso()
    controller._last_private_stream_ok_at = controller._user_stream_last_event_at
    controller._update_stale_transitions()


async def handle_user_stream_resync(controller: Any, snapshot: Any) -> None:
    try:
        controller._apply_resync_snapshot(
            snapshot=snapshot,
            reason="user_stream_resync",
        )
        controller._last_private_stream_ok_at = _utcnow_iso()
        controller._user_stream_last_error = None
        controller._log_event("user_stream_resync", ok=True)
    except Exception as exc:  # noqa: BLE001
        controller._user_stream_last_error = f"resync_failed:{type(exc).__name__}"
        controller._set_state_uncertain(reason=controller._user_stream_last_error, engage_safe_mode=True)
        controller._log_event("user_stream_resync", ok=False, reason=controller._user_stream_last_error)
        raise


async def handle_user_stream_disconnect(controller: Any, reason: str) -> None:
    controller._user_stream_last_disconnect_at = _utcnow_iso()
    controller._user_stream_last_error = str(reason or "user_stream_disconnected")
    controller._set_state_uncertain(reason=controller._user_stream_last_error, engage_safe_mode=False)
    controller._log_event("user_stream_disconnect", reason=controller._user_stream_last_error)


async def handle_user_stream_private_ok(controller: Any, source: str) -> None:
    controller._last_private_stream_ok_at = _utcnow_iso()
    controller._update_stale_transitions()
    freshness = controller._freshness_snapshot()
    if controller._should_clear_uncertainty_on_private_ok() and not bool(freshness["user_ws_stale"]):
        controller._user_stream_last_error = None
        controller._clear_state_uncertain()
    if str(source or "") != "ws_alive":
        controller._log_event("user_stream_private_ok", source=source)


async def start_live_services(controller: Any) -> None:
    if controller.cfg.mode != "live" or controller.user_stream_manager is None or controller._user_stream_started:
        return
    controller._user_stream_started = True
    controller._user_stream_started_at = _utcnow_iso()
    controller.user_stream_manager.start(
        on_event=controller._handle_user_stream_event,
        on_resync=controller._handle_user_stream_resync,
        on_disconnect=controller._handle_user_stream_disconnect,
        on_private_ok=controller._handle_user_stream_private_ok,
    )
    controller._maybe_probe_market_data()
    controller._update_stale_transitions()


async def stop_live_services(controller: Any) -> None:
    if controller.user_stream_manager is not None and controller._user_stream_started:
        await controller.user_stream_manager.stop()
        controller._user_stream_started = False
    controller.state_store.runtime_storage().save_runtime_marker(
        marker_key="shutdown_state",
        payload={
            "profile": controller.cfg.profile,
            "mode": controller.cfg.mode,
            "env": controller.cfg.env,
            "clean_shutdown": True,
            "shutdown_at": _utcnow_iso(),
            "engine_state": controller.state_store.get().status,
            "last_reconcile_at": controller.state_store.get().last_reconcile_at,
            "readyz": controller._readyz_snapshot(),
        },
    )
    controller._log_event("runtime_shutdown", clean_shutdown=True)


async def reconcile_now(controller: Any) -> dict[str, Any]:
    with controller._lock:
        controller._perform_startup_reconcile(reason="manual_reconcile")
        if not controller._state_uncertain:
            controller._recover_brackets_on_boot(reason="manual_reconcile")
        state = capture_runtime_state(controller.state_store)
        return build_reconcile_response(
            state_uncertain=bool(controller._state_uncertain),
            state_uncertain_reason=controller._state_uncertain_reason,
            startup_reconcile_ok=bool(controller._startup_reconcile_ok),
            last_reconcile_at=state.last_reconcile_at,
        )


def set_scheduler_interval(controller: Any, tick_sec: float) -> dict[str, Any]:
    with controller._lock:
        sec = max(1, int(tick_sec))
        controller._apply_scheduler_tick_change(sec=sec)
        controller._persist_risk_config()
        return build_scheduler_response(
            tick_sec=float(controller.scheduler.tick_seconds),
            running=bool(controller._running),
        )


def tick_scheduler_now(controller: Any) -> dict[str, Any]:
    if controller._lock.acquire(blocking=False):
        try:
            controller._auto_reconcile_if_recovery_required(reason="operator_tick")
            return controller._run_cycle_once_locked(trigger_source="manual_tick")
        finally:
            controller._lock.release()

    if controller._running:
        deadline = time.monotonic() + 7.0
        target_seq = max(1, int(controller._cycle_seq))
        while time.monotonic() < deadline:
            if int(controller._cycle_done_seq) >= target_seq:
                snapshot = capture_last_cycle_snapshot(controller._last_cycle, coalesced=True)
                return build_tick_scheduler_response(
                    ok=True,
                    tick_sec=float(controller.scheduler.tick_seconds),
                    snapshot=snapshot,
                    error=None,
                )
            if controller._lock.acquire(timeout=0.1):
                try:
                    return controller._run_cycle_once_locked(trigger_source="manual_tick")
                finally:
                    controller._lock.release()
            time.sleep(0.1)

        snapshot = apply_tick_busy_cycle_state(controller._last_cycle, finished_at=_utcnow_iso())
        return build_tick_scheduler_response(
            ok=False,
            tick_sec=float(controller.scheduler.tick_seconds),
            snapshot=snapshot,
            error="tick_busy",
        )

    if controller._lock.acquire(timeout=6.0):
        try:
            return controller._run_cycle_once_locked(trigger_source="manual_tick")
        finally:
            controller._lock.release()
    snapshot = apply_tick_busy_cycle_state(controller._last_cycle, finished_at=_utcnow_iso())
    return build_tick_scheduler_response(
        ok=False,
        tick_sec=float(controller.scheduler.tick_seconds),
        snapshot=snapshot,
        error="tick_busy",
    )
