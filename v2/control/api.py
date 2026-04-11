from __future__ import annotations

import logging
import threading
import time as _time_module
from collections.abc import Callable, Coroutine
from concurrent.futures import TimeoutError as _FutureTimeoutError
from datetime import datetime
from typing import Any, Literal

from v2.config.loader import EffectiveConfig
from v2.control.controller_brackets import (
    emit_bracket_exit_alert,
    has_recent_fill_for_client_id,
    infer_flat_bracket_exit,
    is_managed_bracket_algo_id,
    latest_recent_fill_for_client_id,
    latest_symbol_realized_pnl,
    latest_symbol_realized_pnl_from_income,
    list_tracked_brackets,
    maybe_trigger_symbol_sl_flatten,
    maybe_trigger_trailing_exit,
    poll_brackets_once,
    position_pnl_pct,
    repair_position_bracket_from_plan,
    resolve_symbol_realized_pnl,
    start_bracket_loop,
    trailing_distance_pct,
)
from v2.control.controller_events import (
    freshness_snapshot,
    gate_snapshot,
    log_event,
    maybe_log_ready_transition,
    maybe_probe_market_data,
    notification_context,
    should_emit_operator_event,
    submission_recovery_snapshot,
    update_stale_transitions,
)
from v2.control.controller_lifecycle import (
    active_worker_thread_locked,
    handle_user_stream_disconnect,
    handle_user_stream_event,
    handle_user_stream_private_ok,
    handle_user_stream_resync,
    join_worker_thread,
    loop_worker,
    panic_runtime,
    start_runtime,
    stop_runtime,
)
from v2.control.controller_lifecycle import (
    reconcile_now as lifecycle_reconcile_now,
)
from v2.control.controller_lifecycle import (
    set_scheduler_interval as lifecycle_set_scheduler_interval,
)
from v2.control.controller_lifecycle import (
    start_live_services as lifecycle_start_live_services,
)
from v2.control.controller_lifecycle import (
    stop_live_services as lifecycle_stop_live_services,
)
from v2.control.controller_lifecycle import (
    tick_scheduler_now as lifecycle_tick_scheduler_now,
)
from v2.control.controller_positions import (
    bars_held_for_management,
    clear_position_management_state,
    dynamic_weak_reduce_ratio,
    extract_latest_market_bar,
    inspect_position_signal,
    load_position_management_state,
    maybe_manage_open_position,
    place_brackets_for_cycle,
    position_management_side,
    record_position_management_plan,
    replace_management_bracket,
    resolve_bracket_config_for_cycle,
    runner_lock_targets,
    save_position_management_state,
)
from v2.control.controller_positions import (
    close_all as close_all_positions,
)
from v2.control.controller_positions import (
    close_position as close_position_single,
)
from v2.control.controller_readiness import (
    cached_live_balance,
    cached_or_fallback_balance,
    healthz_snapshot,
    live_readiness_snapshot,
    live_usdt_balance,
    readyz_snapshot,
    status_snapshot,
)
from v2.control.controller_readiness import (
    scheduler_response as build_controller_scheduler_response,
)
from v2.control.controller_reconcile import (
    apply_resync_snapshot,
    clear_recovery_required,
    clear_state_uncertain,
    fetch_exchange_snapshot,
    set_recovery_required,
    set_state_uncertain,
    should_clear_uncertainty_on_private_ok,
)
from v2.control.controller_risk import (
    clear_cooldown_state,
    get_risk_response,
    load_persisted_risk_config,
    persist_risk_config,
    preset_runtime_risk_profile,
    set_runtime_symbol_leverage,
    set_runtime_value,
)
from v2.control.controller_risk_runtime import (
    effective_budget_leverage,
    maybe_apply_auto_risk_circuit,
    record_recent_block,
    refresh_runtime_risk_context,
    runtime_budget_context,
    runtime_symbol_leverage_map,
    runtime_symbols,
    runtime_target_margin,
)
from v2.control.controller_runtime_config import (
    alpha_v2_profile_params,
    apply_scheduler_tick_change,
    freshness_defaults_for_scheduler_tick,
    initial_risk_config,
    migrate_legacy_strategy_runtime_defaults,
    migrate_persisted_scheduler_runtime_defaults,
    non_persistent_risk_keys,
    persistent_risk_config,
    strategy_runtime_defaults,
    strategy_runtime_snapshot,
    strip_persisted_strategy_runtime_overrides,
    sync_kernel_runtime_overrides,
)
from v2.control.controller_runtime_config import (
    profile_runtime_risk_overrides as controller_profile_runtime_risk_overrides,
)
from v2.control.controller_status import (
    emit_status_update,
    format_daily_report_message,
    send_daily_report,
    start_status_loop,
    status_pnl_summary,
    status_positions_source,
)
from v2.control.http_apps import create_control_http_app as _create_control_http_app
from v2.control.position_management_runtime import (
    plan_management_policy,
    plan_uses_runner_management,
)
from v2.control.presentation import (
    build_portfolio_slot_summary,
    build_status_summary,
    format_signed,
    position_side_label,
    translate_status_token,
)
from v2.control.profile_policy import (
    normalize_runtime_risk_config as _normalize_runtime_risk_config_impl,
)
from v2.control.profile_policy import (
    normalize_runtime_risk_key as _normalize_runtime_risk_key_impl,
)
from v2.control.profile_policy import (
    serialize_runtime_risk_config as _serialize_runtime_risk_config_impl,
)
from v2.control.runtime_utils import (
    age_seconds as _shared_age_seconds,
)
from v2.control.runtime_utils import (
    clamp as _shared_clamp,
)
from v2.control.runtime_utils import (
    normalize_pct as _shared_normalize_pct,
)
from v2.control.runtime_utils import (
    parse_iso_datetime as _shared_parse_iso_datetime,
)
from v2.control.runtime_utils import (
    run_async_blocking as _shared_run_async_blocking,
)
from v2.control.runtime_utils import (
    to_bool as _shared_to_bool,
)
from v2.control.runtime_utils import (
    to_float as _shared_to_float,
)
from v2.control.runtime_utils import (
    utcnow_iso as _shared_utcnow_iso,
)
from v2.core import EventBus, Scheduler
from v2.engine import EngineStateStore, OrderManager
from v2.exchange.types import ResyncSnapshot
from v2.kernel.contracts import KernelCycleResult
from v2.notify import Notifier
from v2.notify.runtime_events import (
    RuntimeNotificationContext,
)
from v2.ops import OpsController
from v2.strategies.ra_2026_alpha_v2 import RA2026AlphaV2Params
from v2.tpsl import BracketConfig, BracketPlanner, BracketService

logger = logging.getLogger(__name__)
FutureTimeoutError = _FutureTimeoutError
time = _time_module

_ALPHA_V2_RUNTIME_DEFAULTS = RA2026AlphaV2Params().__dict__.copy()
_ALPHA_V2_RUNTIME_PARAM_KEYS = (
    "enabled_alphas",
    "trend_adx_min_4h",
    "trend_adx_max_4h",
    "trend_adx_rising_lookback_4h",
    "trend_adx_rising_min_delta_4h",
    "breakout_buffer_bps",
    "expansion_buffer_bps",
    "expansion_body_ratio_min",
    "expansion_close_location_min",
    "expansion_width_expansion_min",
    "expansion_break_distance_atr_min",
    "expansion_breakout_efficiency_min",
    "expansion_breakout_stability_score_min",
    "expansion_breakout_stability_edge_score_min",
    "expansion_quality_score_min",
    "expansion_quality_score_v2_min",
    "min_volume_ratio_15m",
    "expansion_range_atr_min",
    "squeeze_percentile_threshold",
    "expected_move_cost_mult",
    "max_spread_bps",
    "min_expected_move_floor",
)
_LEGACY_CONTROLLER_SEEDED_STRATEGY_DEFAULTS = {
    "trend_adx_min_4h": 22.0,
    "breakout_buffer_bps": 8.0,
    "min_volume_ratio_15m": 1.2,
}
_RUNTIME_DERIVED_RISK_KEYS = (
    "daily_loss_used_pct",
    "dd_used_pct",
    "daily_realized_pnl",
    "daily_realized_pct",
    "lose_streak",
    "cooldown_until",
    "risk_day",
    "recent_blocks",
    "daily_lock",
    "dd_lock",
    "runtime_equity_now_usdt",
    "runtime_equity_peak_usdt",
    "last_auto_risk_reason",
    "last_auto_risk_at",
    "last_block_reason",
    "last_strategy_block_reason",
    "last_alpha_id",
    "last_entry_family",
    "last_regime",
    "last_alpha_blocks",
    "last_alpha_reject_focus",
    "last_alpha_reject_metrics",
    "overheat_state",
)
_POSITION_MANAGEMENT_MARKER_KEY = "position_management_state"

_utcnow_iso = _shared_utcnow_iso
_to_float = _shared_to_float
_to_bool = _shared_to_bool
_clamp = _shared_clamp
_normalize_pct = _shared_normalize_pct


def _parse_value(raw: str) -> Any:
    value = str(raw).strip()
    low = value.lower()
    if low in {"true", "false"}:
        return low == "true"
    if low in {"null", "none"}:
        return None
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        pass
    if "," in value:
        parts = [item.strip() for item in value.split(",") if item.strip()]
        if len(parts) > 0:
            return parts
    return value


def _normalize_runtime_risk_key(key: Any) -> str:
    return _normalize_runtime_risk_key_impl(key)


def _normalize_runtime_risk_config(payload: Any) -> dict[str, Any]:
    return _normalize_runtime_risk_config_impl(payload)


def _serialize_runtime_risk_config(payload: Any) -> dict[str, Any]:
    return _serialize_runtime_risk_config_impl(payload)


_parse_iso_datetime = _shared_parse_iso_datetime
_age_seconds = _shared_age_seconds


def _run_async_blocking(
    thunk: Callable[[], Coroutine[Any, Any, Any]], *, timeout_sec: float | None = None
) -> Any:
    return _shared_run_async_blocking(thunk, timeout_sec=timeout_sec)


class RuntimeController:
    def __init__(
        self,
        *,
        cfg: EffectiveConfig,
        state_store: EngineStateStore,
        ops: OpsController,
        kernel: Any,
        scheduler: Scheduler,
        order_manager: OrderManager,
        notifier: Notifier,
        rest_client: Any | None = None,
        webpush_service: Any | None = None,
        user_stream_manager: Any | None = None,
        market_data_state: dict[str, Any] | None = None,
        runtime_lock_active: bool = False,
        dirty_restart_detected: bool = False,
    ) -> None:
        self.cfg = cfg
        self.state_store = state_store
        self.ops = ops
        self.kernel = kernel
        self.scheduler = scheduler
        self.order_manager = order_manager
        self.notifier = notifier
        self.rest_client = rest_client
        self.webpush_service = webpush_service
        self.user_stream_manager = user_stream_manager
        self._market_data_state = market_data_state if market_data_state is not None else {}
        self._runtime_lock_active = bool(runtime_lock_active)
        self._dirty_restart_detected = bool(dirty_restart_detected)
        if (not self.notifier.enabled) and self.webpush_service is not None:
            self.notifier.enabled = True
        if (
            str(self.notifier.provider or "none").strip().lower() == "none"
            and self.webpush_service is not None
        ):
            self.notifier.provider = "webpush"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_stop = threading.Event()
        self._status_thread_stop = threading.Event()
        self._status_thread: threading.Thread | None = None
        self._bracket_thread_stop = threading.Event()
        self._bracket_thread: threading.Thread | None = None
        self._running = False
        self._last_cycle: dict[str, Any] = {
            "tick_started_at": None,
            "tick_finished_at": None,
            "last_action": "-",
            "last_decision_reason": "-",
            "last_error": None,
            "candidate": None,
            "last_candidate": None,
            "portfolio": None,
            "bracket": None,
        }
        self._report_stats = {
            "entries": 0,
            "closes": 0,
            "errors": 0,
            "canceled": 0,
            "blocks": 0,
            "total_records": 0,
        }
        self._last_status_notify_at: datetime | None = None
        self._risk = self._initial_risk_config()
        self._load_persisted_risk_config()
        self._last_balance_error: str | None = None
        self._last_balance_error_detail: str | None = None
        self._last_balance_available_usdt: float | None = None
        self._last_balance_wallet_usdt: float | None = None
        self._last_balance_fetched_mono: float | None = None
        self._sl_flatten_inflight_symbols: set[str] = set()
        self._sl_last_flatten_mono_by_symbol: dict[str, float] = {}
        self._trailing_state: dict[str, dict[str, Any]] = {}
        self._watchdog_state: dict[str, Any] = {}
        self._cycle_seq = 0
        self._cycle_done_seq = 0
        self._last_scheduler_cycle_event_mono: float | None = None
        self._last_alpha_drift_setup_key: str | None = None
        self._last_alpha_drift_confirm_key: str | None = None
        self._boot_notification_muted = True
        self._state_uncertain = False
        self._state_uncertain_reason: str | None = None
        self._startup_reconcile_ok: bool | None = None
        self._recovery_required = bool(dirty_restart_detected)
        self._recovery_reason = (
            "dirty_restart_recovery_required" if dirty_restart_detected else None
        )
        self._user_stream_started = False
        self._user_stream_started_at: str | None = None
        self._user_stream_last_event_at: str | None = None
        self._user_stream_last_disconnect_at: str | None = None
        self._user_stream_last_error: str | None = None
        self._last_private_stream_ok_at: str | None = None
        self._last_ready_state: bool | None = None
        self._last_user_ws_stale: bool | None = None
        self._last_market_data_stale: bool | None = None
        self._boot_recovery: dict[str, Any] = {
            "submission_recovery": None,
            "bracket_recovery": None,
        }
        self._last_report: dict[str, Any] = {
            "reported_at": None,
            "kind": "DAILY_REPORT",
            "day": None,
            "status": None,
            "notifier_enabled": bool(self.notifier.enabled),
            "notifier_sent": False,
            "notifier_error": None,
            "summary": None,
            "detail": {},
        }
        self._last_shutdown_marker = self.state_store.runtime_storage().load_runtime_marker(
            marker_key="shutdown_state"
        )
        self._bracket_service = BracketService(
            planner=BracketPlanner(
                cfg=BracketConfig(
                    take_profit_pct=float(self.cfg.behavior.tpsl.take_profit_pct),
                    stop_loss_pct=float(self.cfg.behavior.tpsl.stop_loss_pct),
                )
            ),
            storage=self.state_store.runtime_storage(),
            rest_client=self.rest_client,
            mode=self.cfg.mode,
        )
        self.state_store.set(mode=self.cfg.mode, status="STOPPED")
        self.scheduler.tick_seconds = max(
            1,
            int(
                _to_float(
                    self._risk.get("scheduler_tick_sec"),
                    default=float(self.scheduler.tick_seconds),
                )
            ),
        )
        self._perform_startup_reconcile(reason="startup_reconcile")
        self._recover_brackets_on_boot()
        self._start_bracket_loop()
        self._start_status_loop()
        self._refresh_runtime_risk_context()
        self._sync_kernel_runtime_overrides()
        self.state_store.runtime_storage().save_runtime_marker(
            marker_key="runtime_boot",
            payload={
                "profile": self.cfg.profile,
                "mode": self.cfg.mode,
                "env": self.cfg.env,
                "dirty_restart_detected": bool(self._dirty_restart_detected),
                "started_at": _utcnow_iso(),
            },
        )
        self._update_stale_transitions()
        self._boot_notification_muted = False
        self._maybe_log_ready_transition(notify=False)
        self._log_event(
            "controller_initialized",
            dirty_restart_detected=self._dirty_restart_detected,
            recovery_required=self._recovery_required,
        )

    def _set_state_uncertain(self, *, reason: str, engage_safe_mode: bool) -> None:
        set_state_uncertain(self, reason=reason, engage_safe_mode=engage_safe_mode)

    def _clear_state_uncertain(self) -> None:
        clear_state_uncertain(self)

    def _should_clear_uncertainty_on_private_ok(self) -> bool:
        return should_clear_uncertainty_on_private_ok(self)

    def _set_recovery_required(self, *, reason: str) -> None:
        set_recovery_required(self, reason=reason)

    def _clear_recovery_required(self, *, only_when_prefix: str | None = None, reason: str) -> None:
        clear_recovery_required(self, only_when_prefix=only_when_prefix, reason=reason)

    def _log_event(self, event: str, *, notify: bool = True, **fields: Any) -> None:
        log_event(self, event, notify=notify, **fields)

    def _should_emit_operator_event(self, *, event: str, fields: dict[str, Any]) -> bool:
        return should_emit_operator_event(self, event=event, fields=fields)

    def _notification_context(self) -> RuntimeNotificationContext:
        return notification_context(self)

    def _freshness_snapshot(self) -> dict[str, Any]:
        return freshness_snapshot(self)

    def _update_stale_transitions(self) -> None:
        update_stale_transitions(self)

    def _maybe_probe_market_data(self) -> None:
        maybe_probe_market_data(self)

    def _submission_recovery_snapshot(self) -> dict[str, Any]:
        return submission_recovery_snapshot(self)

    def _recover_submission_intents_from_snapshot(
        self,
        *,
        snapshot: ResyncSnapshot,
        reason: str,
    ) -> dict[str, Any]:
        from v2.control.recovery import recover_submission_intents_from_snapshot

        return recover_submission_intents_from_snapshot(self, snapshot=snapshot, reason=reason)

    def _gate_snapshot(self, *, probe_private_rest: bool = False) -> dict[str, Any]:
        return gate_snapshot(self, probe_private_rest=probe_private_rest)

    def _maybe_log_ready_transition(self, *, notify: bool = True, force: bool = False) -> None:
        maybe_log_ready_transition(self, notify=notify, force=force)

    def _fetch_exchange_snapshot(self) -> ResyncSnapshot:
        return fetch_exchange_snapshot(self)

    def _apply_resync_snapshot(self, *, snapshot: ResyncSnapshot, reason: str) -> None:
        apply_resync_snapshot(self, snapshot=snapshot, reason=reason)

    def _perform_startup_reconcile(self, *, reason: str) -> None:
        from v2.control.recovery import perform_startup_reconcile

        perform_startup_reconcile(self, reason=reason)

    def _auto_reconcile_if_recovery_required(self, *, reason: str) -> bool:
        from v2.control.recovery import auto_reconcile_if_recovery_required

        return auto_reconcile_if_recovery_required(self, reason=reason)

    def _recover_brackets_on_boot(self, *, reason: str = "startup_reconcile") -> None:
        from v2.control.recovery import recover_brackets_on_boot

        recover_brackets_on_boot(self, reason=reason)

    def _load_persisted_risk_config(self) -> None:
        load_persisted_risk_config(self)

    def _persist_risk_config(self) -> None:
        persist_risk_config(self)

    def _public_risk_config(self) -> dict[str, Any]:
        return _serialize_runtime_risk_config(self._risk)

    def _alpha_v2_profile_params(self) -> dict[str, Any]:
        return alpha_v2_profile_params(self)

    def _strategy_runtime_defaults(self) -> dict[str, Any]:
        return strategy_runtime_defaults(
            self,
            runtime_param_keys=_ALPHA_V2_RUNTIME_PARAM_KEYS,
            runtime_defaults=_ALPHA_V2_RUNTIME_DEFAULTS,
        )

    def _strategy_runtime_snapshot(self) -> dict[str, Any]:
        return strategy_runtime_snapshot(
            self,
            runtime_param_keys=_ALPHA_V2_RUNTIME_PARAM_KEYS,
            runtime_defaults=_ALPHA_V2_RUNTIME_DEFAULTS,
        )

    def _non_persistent_risk_keys(self) -> set[str]:
        return non_persistent_risk_keys(
            self,
            runtime_param_keys=_ALPHA_V2_RUNTIME_PARAM_KEYS,
            runtime_derived_risk_keys=_RUNTIME_DERIVED_RISK_KEYS,
        )

    def _persistent_risk_config(self) -> dict[str, Any]:
        return persistent_risk_config(
            self,
            runtime_param_keys=_ALPHA_V2_RUNTIME_PARAM_KEYS,
            runtime_derived_risk_keys=_RUNTIME_DERIVED_RISK_KEYS,
        )

    def _strip_persisted_strategy_runtime_overrides(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        return strip_persisted_strategy_runtime_overrides(
            self,
            payload,
            runtime_param_keys=_ALPHA_V2_RUNTIME_PARAM_KEYS,
            runtime_derived_risk_keys=_RUNTIME_DERIVED_RISK_KEYS,
        )

    def _migrate_legacy_strategy_runtime_defaults(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        return migrate_legacy_strategy_runtime_defaults(
            self,
            payload,
            runtime_param_keys=_ALPHA_V2_RUNTIME_PARAM_KEYS,
            runtime_defaults=_ALPHA_V2_RUNTIME_DEFAULTS,
            legacy_defaults=_LEGACY_CONTROLLER_SEEDED_STRATEGY_DEFAULTS,
        )

    def _migrate_persisted_scheduler_runtime_defaults(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        return migrate_persisted_scheduler_runtime_defaults(self, payload)

    def _sync_kernel_runtime_overrides(self) -> None:
        sync_kernel_runtime_overrides(
            self,
            runtime_param_keys=_ALPHA_V2_RUNTIME_PARAM_KEYS,
            runtime_defaults=_ALPHA_V2_RUNTIME_DEFAULTS,
        )

    def _effective_budget_leverage(
        self,
        *,
        symbols: list[str],
        max_leverage: float,
        symbol_leverage_map: dict[str, float],
    ) -> float:
        return effective_budget_leverage(
            symbols=symbols,
            max_leverage=max_leverage,
            symbol_leverage_map=symbol_leverage_map,
        )

    @staticmethod
    def _freshness_defaults_for_scheduler_tick(*, sched_sec: int) -> dict[str, float]:
        return freshness_defaults_for_scheduler_tick(sched_sec=sched_sec)

    def _apply_scheduler_tick_change(self, *, sec: int) -> None:
        apply_scheduler_tick_change(self, sec=sec)

    def _initial_risk_config(self) -> dict[str, Any]:
        return initial_risk_config(
            self,
            runtime_param_keys=_ALPHA_V2_RUNTIME_PARAM_KEYS,
            runtime_defaults=_ALPHA_V2_RUNTIME_DEFAULTS,
        )

    def _profile_runtime_risk_overrides(self, *, sched_sec: int) -> dict[str, Any]:
        return controller_profile_runtime_risk_overrides(self, sched_sec=sched_sec)

    def _live_readiness_snapshot(
        self,
        *,
        live_balance_source: str | None = None,
        private_error: str | None = None,
    ) -> dict[str, Any]:
        return live_readiness_snapshot(
            self,
            live_balance_source=live_balance_source,
            private_error=private_error,
        )

    def _runtime_symbols(self) -> list[str]:
        return runtime_symbols(self)

    def _runtime_symbol_leverage_map(self) -> dict[str, float]:
        return runtime_symbol_leverage_map(self)

    def _runtime_target_margin(self) -> float:
        return runtime_target_margin(self)

    def _runtime_budget_context(self) -> tuple[list[str], dict[str, float], float, float, float]:
        return runtime_budget_context(self)

    def _record_recent_block(self, reason: str) -> None:
        record_recent_block(self, reason)

    def _refresh_runtime_risk_context(self) -> None:
        refresh_runtime_risk_context(self)

    def _maybe_apply_auto_risk_circuit(self, cycle: KernelCycleResult) -> None:
        maybe_apply_auto_risk_circuit(self, cycle)

    def _status_snapshot(self) -> dict[str, Any]:
        return status_snapshot(self)

    def _healthz_snapshot(self) -> dict[str, Any]:
        return healthz_snapshot(self)

    def _readyz_snapshot(self) -> dict[str, Any]:
        return readyz_snapshot(self)

    def _cached_live_balance(
        self, *, max_age_sec: float
    ) -> tuple[float | None, float | None] | None:
        return cached_live_balance(self, max_age_sec=max_age_sec)

    def _cached_or_fallback_balance(
        self,
        *,
        preserve_private_error: bool = False,
    ) -> tuple[float | None, float | None, str]:
        return cached_or_fallback_balance(
            self,
            preserve_private_error=preserve_private_error,
        )

    def _fetch_live_usdt_balance(self) -> tuple[float | None, float | None, str]:
        return live_usdt_balance(self)

    def _load_position_management_state(self) -> dict[str, dict[str, Any]]:
        return load_position_management_state(self)

    def _save_position_management_state(self, state: dict[str, dict[str, Any]]) -> None:
        save_position_management_state(self, state)

    def _clear_position_management_state(self, *, symbol: str) -> None:
        clear_position_management_state(self, symbol=symbol)

    @staticmethod
    def _plan_management_policy(plan: dict[str, Any] | None) -> str:
        return plan_management_policy(plan)

    @classmethod
    def _plan_uses_runner_management(cls, plan: dict[str, Any] | None) -> bool:
        return plan_uses_runner_management(plan)

    @staticmethod
    def _position_management_side(candidate_side: str | None) -> str:
        return position_management_side(candidate_side)

    def _record_position_management_plan(self, *, cycle: KernelCycleResult) -> None:
        record_position_management_plan(self, cycle=cycle)

    @staticmethod
    def _dynamic_weak_reduce_ratio(
        plan: dict[str, Any],
        *,
        stage: int,
        signal_view: dict[str, Any] | None = None,
    ) -> float:
        return dynamic_weak_reduce_ratio(plan, stage=stage, signal_view=signal_view)

    @staticmethod
    def _runner_lock_targets(plan: dict[str, Any]) -> list[tuple[float, float, int]]:
        return runner_lock_targets(plan)

    @staticmethod
    def _extract_latest_market_bar(snapshot: dict[str, Any], *, symbol: str) -> dict[str, float] | None:
        return extract_latest_market_bar(snapshot, symbol=symbol)

    @staticmethod
    def _bars_held_for_management(plan: dict[str, Any], bar: dict[str, float] | None) -> int:
        return bars_held_for_management(plan, bar)

    def _maybe_manage_open_position(
        self,
        *,
        symbol: str,
        row: dict[str, Any],
        plan: dict[str, Any],
        market_bar: dict[str, float] | None,
    ) -> tuple[bool, dict[str, Any]]:
        return maybe_manage_open_position(
            self,
            symbol=symbol,
            row=row,
            plan=plan,
            market_bar=market_bar,
        )

    def _inspect_position_signal(self, *, symbol: str) -> dict[str, Any] | None:
        return inspect_position_signal(self, symbol=symbol)

    def _replace_management_bracket(
        self,
        *,
        symbol: str,
        entry_side: Literal["BUY", "SELL"],
        position_side: str,
        quantity: float,
        take_profit_price: float,
        stop_loss_price: float,
        reason: str,
    ) -> bool:
        return replace_management_bracket(
            self,
            symbol=symbol,
            entry_side=entry_side,
            position_side=position_side,
            quantity=quantity,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            reason=reason,
        )

    def _resolve_bracket_config_for_cycle(
        self,
        *,
        cycle: KernelCycleResult,
        entry_price: float,
    ) -> tuple[BracketConfig, float | None, dict[str, Any]]:
        return resolve_bracket_config_for_cycle(self, cycle=cycle, entry_price=entry_price)

    def _place_brackets_for_cycle(self, *, cycle: KernelCycleResult) -> None:
        place_brackets_for_cycle(self, cycle=cycle)

    def _fetch_live_positions(
        self,
    ) -> tuple[dict[str, float], dict[str, dict[str, Any]], bool, str | None]:
        from v2.control.cycle import fetch_live_positions

        return fetch_live_positions(self)

    def _confirm_live_position_row(self, *, symbol: str) -> tuple[dict[str, Any] | None, bool]:
        symbol_u = str(symbol or "").strip().upper()
        if not symbol_u:
            return None, False
        positions, position_rows, positions_ok, _position_error = self._fetch_live_positions()
        if not positions_ok:
            return None, False
        position_amt = _to_float(positions.get(symbol_u), default=0.0)
        if abs(position_amt) <= 0.0:
            return None, True
        row = position_rows.get(symbol_u)
        if isinstance(row, dict):
            confirmed = dict(row)
        else:
            confirmed = {}
        confirmed["symbol"] = symbol_u
        confirmed["positionAmt"] = position_amt
        confirmed["positionSide"] = str(confirmed.get("positionSide") or "BOTH").strip().upper() or "BOTH"
        return confirmed, True

    def _is_live_reentry_blocked(self) -> bool:
        from v2.control.cycle import is_live_reentry_blocked

        return is_live_reentry_blocked(self)

    def _maybe_trigger_symbol_sl_flatten(self, *, trigger_symbol: str) -> None:
        maybe_trigger_symbol_sl_flatten(self, trigger_symbol=trigger_symbol)

    @staticmethod
    def _position_pnl_pct(row: dict[str, Any]) -> float | None:
        return position_pnl_pct(row)

    def _trailing_distance_pct(self, *, row: dict[str, Any]) -> float:
        return trailing_distance_pct(self, row=row)

    def _maybe_trigger_trailing_exit(
        self,
        *,
        symbol: str,
        row: dict[str, Any],
        rest_client: Any,
    ) -> bool:
        return maybe_trigger_trailing_exit(
            self,
            symbol=symbol,
            row=row,
            rest_client=rest_client,
        )

    @staticmethod
    def _is_managed_bracket_algo_id(value: str | None) -> bool:
        return is_managed_bracket_algo_id(value)

    def _list_tracked_brackets(self) -> list[dict[str, Any]]:
        return list_tracked_brackets(self)

    def _has_recent_fill_for_client_id(
        self,
        *,
        symbol: str,
        client_id: str,
        lookback_sec: float = 900.0,
    ) -> bool:
        return has_recent_fill_for_client_id(
            self,
            symbol=symbol,
            client_id=client_id,
            lookback_sec=lookback_sec,
        )

    def _latest_recent_fill_for_client_id(
        self,
        *,
        symbol: str,
        client_id: str,
        lookback_sec: float = 900.0,
    ) -> dict[str, Any] | None:
        return latest_recent_fill_for_client_id(
            self,
            symbol=symbol,
            client_id=client_id,
            lookback_sec=lookback_sec,
        )

    def _infer_flat_bracket_exit(
        self,
        *,
        symbol: str,
        tp_id: str,
        sl_id: str,
    ) -> tuple[Literal["TP", "SL"], str | None] | None:
        return infer_flat_bracket_exit(
            self,
            symbol=symbol,
            tp_id=tp_id,
            sl_id=sl_id,
        )

    def _repair_position_bracket_from_plan(
        self,
        *,
        symbol: str,
        position_row: dict[str, Any],
        plan: dict[str, Any],
        reason: str,
    ) -> bool:
        return repair_position_bracket_from_plan(
            self,
            symbol=symbol,
            position_row=position_row,
            plan=plan,
            reason=reason,
        )

    def _latest_symbol_realized_pnl(
        self, *, symbol: str, lookback_sec: float = 900.0
    ) -> float | None:
        return latest_symbol_realized_pnl(self, symbol=symbol, lookback_sec=lookback_sec)

    def _latest_symbol_realized_pnl_from_income(
        self, *, symbol: str, lookback_sec: float = 900.0
    ) -> float | None:
        return latest_symbol_realized_pnl_from_income(
            self,
            symbol=symbol,
            lookback_sec=lookback_sec,
        )

    def _resolve_symbol_realized_pnl(self, *, symbol: str) -> float | None:
        return resolve_symbol_realized_pnl(self, symbol=symbol)

    def _emit_bracket_exit_alert(self, *, symbol: str, outcome: Literal["TP", "SL"]) -> None:
        emit_bracket_exit_alert(self, symbol=symbol, outcome=outcome)

    def _poll_brackets_once(self) -> None:
        poll_brackets_once(self)

    def _start_bracket_loop(self) -> None:
        start_bracket_loop(self)

    def _run_cycle_once_locked(self, *, trigger_source: str = "scheduler") -> dict[str, Any]:
        from v2.control.cycle import run_cycle_once_locked

        return run_cycle_once_locked(self, trigger_source=trigger_source)

    def _status_summary(self) -> str:
        return build_status_summary(self)

    def _portfolio_slot_summary(self) -> str:
        return build_portfolio_slot_summary(self._last_cycle.get("portfolio"))

    @staticmethod
    def _fmt_signed(value: float) -> str:
        return format_signed(value)

    @staticmethod
    def _position_side_label(position_amt: float) -> str:
        return position_side_label(position_amt)

    def _status_positions_source(self) -> list[tuple[str, float, float, float]]:
        return status_positions_source(self)

    def _status_pnl_summary(self) -> tuple[str, str]:
        return status_pnl_summary(self)

    @staticmethod
    def _translate_status_token(raw: str, labels: dict[str, str]) -> str:
        return translate_status_token(raw, labels)

    def _emit_status_update(self, *, force: bool = False) -> bool:
        return emit_status_update(self, force=force)

    def _start_status_loop(self) -> None:
        start_status_loop(self)

    def _loop_worker(self) -> None:
        loop_worker(self)

    def _active_worker_thread_locked(self) -> threading.Thread | None:
        return active_worker_thread_locked(self)

    def _join_worker_thread(self, thread: threading.Thread | None, *, timeout_sec: float = 2.0) -> None:
        join_worker_thread(self, thread, timeout_sec=timeout_sec)

    def start(self) -> dict[str, Any]:
        return start_runtime(self)

    def stop(self, *, emit_event: bool = True) -> dict[str, Any]:
        return stop_runtime(self, emit_event=emit_event)

    async def panic(self) -> dict[str, Any]:
        return await panic_runtime(self)

    def get_risk(self) -> dict[str, Any]:
        with self._lock:
            return get_risk_response(self)

    def set_value(self, *, key: str, value: str) -> dict[str, Any]:
        with self._lock:
            normalized_key = _normalize_runtime_risk_key(key)
            parsed = _parse_value(value)
            return set_runtime_value(
                self,
                normalized_key=normalized_key,
                parsed=parsed,
                requested_value=value,
            )

    def set_symbol_leverage(self, *, symbol: str, leverage: float) -> dict[str, Any]:
        with self._lock:
            return set_runtime_symbol_leverage(self, symbol=symbol, leverage=leverage)

    def get_scheduler(self) -> dict[str, Any]:
        return build_controller_scheduler_response(self)

    async def _handle_user_stream_event(self, event: dict[str, Any]) -> None:
        await handle_user_stream_event(self, event)

    async def _handle_user_stream_resync(self, snapshot: ResyncSnapshot) -> None:
        await handle_user_stream_resync(self, snapshot)

    async def _handle_user_stream_disconnect(self, reason: str) -> None:
        await handle_user_stream_disconnect(self, reason)

    async def _handle_user_stream_private_ok(self, source: str) -> None:
        await handle_user_stream_private_ok(self, source)

    async def start_live_services(self) -> None:
        await lifecycle_start_live_services(self)

    async def stop_live_services(self) -> None:
        await lifecycle_stop_live_services(self)

    async def reconcile_now(self) -> dict[str, Any]:
        return await lifecycle_reconcile_now(self)

    def set_scheduler_interval(self, tick_sec: float) -> dict[str, Any]:
        return lifecycle_set_scheduler_interval(self, tick_sec)

    def tick_scheduler_now(self) -> dict[str, Any]:
        return lifecycle_tick_scheduler_now(self)

    @staticmethod
    def _format_daily_report_message(payload: dict[str, Any]) -> str:
        return format_daily_report_message(payload)

    def send_daily_report(self) -> dict[str, Any]:
        return send_daily_report(self)

    def preset(self, name: str) -> dict[str, Any]:
        with self._lock:
            return preset_runtime_risk_profile(self, name)

    async def close_position(
        self,
        *,
        symbol: str,
        notify_reason: str = "forced_close",
    ) -> dict[str, Any]:
        return await close_position_single(self, symbol=symbol, notify_reason=notify_reason)

    async def close_all(self, *, notify_reason: str = "forced_close") -> dict[str, Any]:
        return await close_all_positions(self, notify_reason=notify_reason)

    def clear_cooldown(self) -> dict[str, Any]:
        with self._lock:
            return clear_cooldown_state(self)


def create_control_http_app(*, controller: RuntimeController, enable_operator_web: bool = False):
    return _create_control_http_app(
        controller=controller,
        enable_operator_web=enable_operator_web,
    )


def build_runtime_controller(
    *,
    cfg: EffectiveConfig,
    state_store: EngineStateStore,
    ops: OpsController,
    kernel: Any,
    scheduler: Scheduler,
    event_bus: EventBus,
    notifier: Notifier,
    rest_client: Any | None,
    webpush_service: Any | None = None,
    user_stream_manager: Any | None = None,
    market_data_state: dict[str, Any] | None = None,
    runtime_lock_active: bool = False,
    dirty_restart_detected: bool = False,
) -> RuntimeController:
    _ = event_bus
    return RuntimeController(
        cfg=cfg,
        state_store=state_store,
        ops=ops,
        kernel=kernel,
        scheduler=scheduler,
        order_manager=OrderManager(event_bus=event_bus),
        notifier=notifier,
        rest_client=rest_client,
        webpush_service=webpush_service,
        user_stream_manager=user_stream_manager,
        market_data_state=market_data_state,
        runtime_lock_active=runtime_lock_active,
        dirty_restart_detected=dirty_restart_detected,
    )
