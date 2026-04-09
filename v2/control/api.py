from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from typing import Any, Literal, cast

from v2.common.async_bridge import run_async_blocking
from v2.config.loader import EffectiveConfig
from v2.control.http_apps import create_control_http_app as _create_control_http_app
from v2.control.live_balance_helpers import (
    build_freshness_snapshot,
    fetch_live_usdt_balance,
    get_cached_live_balance,
    get_cached_or_fallback_balance,
)
from v2.control.mutating_core_helpers import (
    apply_tick_busy_cycle_state,
    capture_last_cycle_snapshot,
    capture_public_risk_config,
    capture_runtime_state,
)
from v2.control.mutating_responses import (
    build_clear_cooldown_response,
    build_panic_response,
    build_set_value_response,
    build_state_response,
    build_tick_scheduler_response,
    build_trade_close_all_response,
    build_trade_close_response,
)
from v2.control.operator_events import build_operator_event_payload
from v2.control.position_management_runtime import (
    build_position_management_plan,
    handle_take_profit_rearm,
    mark_exit_requested,
    mark_runner_activated,
    maybe_handle_fill_event_fast_path,
    plan_management_policy,
    plan_uses_runner_management,
)
from v2.control.presentation import (
    build_portfolio_slot_summary,
    build_reconcile_response,
    build_risk_response,
    build_scheduler_response,
    build_status_pnl_summary,
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
from v2.control.report_builders import build_daily_report_message, build_daily_report_payload
from v2.control.status_payloads import (
    build_healthz_snapshot,
    build_readyz_snapshot,
    build_status_snapshot,
)
from v2.core import EventBus, Scheduler
from v2.engine import EngineStateStore, OrderManager
from v2.exchange.types import ResyncSnapshot
from v2.kernel.contracts import KernelCycleResult
from v2.notify import Notifier
from v2.notify.runtime_events import (
    RuntimeNotificationContext,
    build_bracket_exit_notification,
    build_position_close_notification,
    build_report_notification,
    build_runtime_event_notification,
)
from v2.ops import OpsController
from v2.strategies.ra_2026_alpha_v2 import RA2026AlphaV2Params
from v2.tpsl import BracketConfig, BracketPlanner, BracketService

logger = logging.getLogger(__name__)

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


def _normalize_pct(value: Any, default: float = 0.0) -> float:
    parsed = abs(_to_float(value, default=default))
    if parsed > 1.0:
        parsed = parsed / 100.0
    return max(parsed, 0.0)


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


def _parse_iso_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(raw: Any) -> float | None:
    parsed = _parse_iso_datetime(raw)
    if parsed is None:
        return None
    return max((datetime.now(timezone.utc) - parsed).total_seconds(), 0.0)


def _run_async_blocking(
    thunk: Callable[[], Coroutine[Any, Any, Any]], *, timeout_sec: float | None = None
) -> Any:
    return run_async_blocking(thunk, timeout_sec=timeout_sec)


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
        if (not self.notifier.enabled) and str(self.notifier.webhook_url or "").strip():
            self.notifier.enabled = True
        if (not self.notifier.enabled) and str(self.notifier.ntfy_topic or "").strip():
            self.notifier.enabled = True
        if (
            str(self.notifier.provider or "none").strip().lower() == "none"
            and str(self.notifier.ntfy_topic or "").strip()
        ):
            self.notifier.provider = "ntfy"
        elif (
            str(self.notifier.provider or "none").strip().lower() == "none"
            and str(self.notifier.webhook_url or "").strip()
        ):
            self.notifier.provider = "discord"
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
        next_reason = str(reason or "state_uncertain")
        changed = (not self._state_uncertain) or self._state_uncertain_reason != next_reason
        self._state_uncertain = True
        self._state_uncertain_reason = next_reason
        if engage_safe_mode:
            self.ops.safe_mode()
        if changed:
            self._log_event(
                "uncertainty_transition",
                state_uncertain=True,
                reason=next_reason,
                engage_safe_mode=bool(engage_safe_mode),
            )
            self._maybe_log_ready_transition()

    def _clear_state_uncertain(self) -> None:
        changed = self._state_uncertain
        self._state_uncertain = False
        self._state_uncertain_reason = None
        if changed:
            self._log_event("uncertainty_transition", state_uncertain=False)
            self._maybe_log_ready_transition()

    def _should_clear_uncertainty_on_private_ok(self) -> bool:
        if not self._state_uncertain:
            return False
        reason = str(self._state_uncertain_reason or "").strip()
        last_error = str(self._user_stream_last_error or "").strip()
        if not reason or not last_error or reason != last_error:
            return False
        return (
            reason == "user_stream_disconnected"
            or reason == "listen_key_expired"
            or reason.startswith("user_stream_error:")
            or reason.startswith("socket_")
        )

    def _set_recovery_required(self, *, reason: str) -> None:
        next_reason = str(reason or "recovery_required")
        changed = (not self._recovery_required) or self._recovery_reason != next_reason
        self._recovery_required = True
        self._recovery_reason = next_reason
        if changed:
            self._log_event("recovery_transition", recovery_required=True, reason=next_reason)
            self._maybe_log_ready_transition()

    def _clear_recovery_required(self, *, only_when_prefix: str | None = None, reason: str) -> None:
        current_reason = str(self._recovery_reason or "")
        if only_when_prefix is not None and not current_reason.startswith(only_when_prefix):
            return
        changed = self._recovery_required or bool(self._recovery_reason)
        self._recovery_required = False
        self._recovery_reason = None
        if changed:
            self._log_event("recovery_transition", recovery_required=False, reason=reason)
            self._maybe_log_ready_transition()

    def _log_event(self, event: str, *, notify: bool = True, **fields: Any) -> None:
        ops_state = self.state_store.get().operational
        logger.info(
            event,
            extra={
                "event": event,
                "mode": self.cfg.mode,
                "profile": self.cfg.profile,
                "state_uncertain": bool(self._state_uncertain),
                "safe_mode": bool(ops_state.safe_mode),
                **fields,
            },
        )
        if not self._should_emit_operator_event(event=event, fields=fields):
            return
        payload = build_operator_event_payload(event=event, fields=fields)
        if payload is not None:
            self.state_store.runtime_storage().append_operator_event(
                event_type=str(payload["event_type"]),
                category=str(payload["category"]),
                title=str(payload["title"]),
                main_text=str(payload["main_text"]),
                sub_text=(
                    str(payload["sub_text"])
                    if payload.get("sub_text") is not None
                    else None
                ),
                event_time=str(payload["event_time"]),
                context=dict(payload.get("context") or {}),
            )
        notification = build_runtime_event_notification(
            event=event,
            fields=fields,
            context=self._notification_context(),
            provider=self.notifier.resolved_provider(),
        )
        if notification is not None and notify and not self._boot_notification_muted:
            _ = self.notifier.send_notification(notification)

    def _should_emit_operator_event(self, *, event: str, fields: dict[str, Any]) -> bool:
        if str(event or "").strip() != "cycle_result":
            return True
        if str(fields.get("trigger_source") or "").strip().lower() != "scheduler":
            return True

        interval_sec = max(
            1.0,
            float(_to_float(fields.get("notify_interval_sec"), default=30.0)),
        )
        now_mono = time.monotonic()
        last_mono = self._last_scheduler_cycle_event_mono
        if last_mono is not None and (now_mono - last_mono) < interval_sec:
            return False
        self._last_scheduler_cycle_event_mono = now_mono
        return True

    def _notification_context(self) -> RuntimeNotificationContext:
        return RuntimeNotificationContext(
            profile=self.cfg.profile,
            mode=self.cfg.mode,
            env=self.cfg.env,
        )

    def _freshness_snapshot(self) -> dict[str, Any]:
        return build_freshness_snapshot(self)

    def _update_stale_transitions(self) -> None:
        freshness = self._freshness_snapshot()
        user_ws_stale = bool(freshness["user_ws_stale"])
        market_data_stale = bool(freshness["market_data_stale"])
        changed = False
        if self._last_user_ws_stale is None or self._last_user_ws_stale != user_ws_stale:
            changed = True
            self._last_user_ws_stale = user_ws_stale
            self._log_event(
                "stale_transition",
                stale_type="user_ws",
                stale=user_ws_stale,
                age_sec=freshness["user_ws_age_sec"],
            )
        if self._last_market_data_stale is None or self._last_market_data_stale != market_data_stale:
            changed = True
            self._last_market_data_stale = market_data_stale
            self._log_event(
                "stale_transition",
                stale_type="market_data",
                stale=market_data_stale,
                age_sec=freshness["market_data_age_sec"],
            )
        if changed:
            self._maybe_log_ready_transition()

    def _maybe_probe_market_data(self) -> None:
        probe = getattr(self.kernel, "probe_market_data", None)
        if not callable(probe):
            return
        try:
            _ = probe()
            self._market_data_state["last_market_data_source_ok_at"] = _utcnow_iso()
            self._market_data_state["last_market_data_source_error"] = None
            self._market_data_state["last_market_data_source_fail_at"] = None
        except Exception as exc:  # noqa: BLE001
            self._market_data_state["last_market_data_source_fail_at"] = _utcnow_iso()
            self._market_data_state["last_market_data_source_error"] = type(exc).__name__
            self._log_event(
                "market_data_probe_failed",
                reason=type(exc).__name__,
            )

    def _submission_recovery_snapshot(self) -> dict[str, Any]:
        rows = self.state_store.runtime_storage().list_submission_intents(
            statuses=["REVIEW_REQUIRED"]
        )
        preview = [
            {
                "intent_id": str(row.get("intent_id") or ""),
                "client_order_id": str(row.get("client_order_id") or ""),
                "symbol": str(row.get("symbol") or ""),
                "status": str(row.get("status") or ""),
                "updated_at": row.get("updated_at"),
            }
            for row in rows[:10]
            if isinstance(row, dict)
        ]
        return {
            "pending_review_count": len(rows),
            "pending_review": preview,
            "ok": len(rows) == 0,
        }

    def _recover_submission_intents_from_snapshot(
        self,
        *,
        snapshot: ResyncSnapshot,
        reason: str,
    ) -> dict[str, Any]:
        from v2.control.recovery import recover_submission_intents_from_snapshot

        return recover_submission_intents_from_snapshot(self, snapshot=snapshot, reason=reason)

    def _gate_snapshot(self, *, probe_private_rest: bool = False) -> dict[str, Any]:
        from v2.control.gates import gate_snapshot

        return gate_snapshot(self, probe_private_rest=probe_private_rest)

    def _maybe_log_ready_transition(self, *, notify: bool = True, force: bool = False) -> None:
        gate = self._gate_snapshot()
        ready = bool(gate["ready"])
        if self._last_ready_state is None or self._last_ready_state != ready or force:
            self._last_ready_state = ready
            self._log_event(
                "ready_transition",
                notify=notify,
                ready=ready,
                recovery_required=bool(self._recovery_required),
                submission_recovery_ok=bool(gate["submission_recovery_ok"]),
                user_ws_stale=bool(gate["freshness"]["user_ws_stale"]),
                market_data_stale=bool(gate["freshness"]["market_data_stale"]),
            )

    def _fetch_exchange_snapshot(self) -> ResyncSnapshot:
        if self.rest_client is None:
            raise RuntimeError("rest_client_missing")

        def _call_or_default(name: str) -> list[dict[str, Any]]:
            method = getattr(self.rest_client, name, None)
            if not callable(method):
                return []
            value = _run_async_blocking(lambda: method(), timeout_sec=15.0)
            return value if isinstance(value, list) else []

        open_orders = _call_or_default("get_open_orders")
        positions = _call_or_default("get_positions")
        balances = _call_or_default("get_balances")
        return ResyncSnapshot(
            open_orders=open_orders,
            positions=positions,
            balances=balances,
        )

    def _apply_resync_snapshot(self, *, snapshot: ResyncSnapshot, reason: str) -> None:
        self.state_store.startup_reconcile(snapshot=snapshot, reason=reason)
        self._recover_submission_intents_from_snapshot(snapshot=snapshot, reason=reason)
        self._startup_reconcile_ok = True
        self._clear_state_uncertain()
        if reason == "manual_reconcile" and self._recovery_required:
            self._clear_recovery_required(reason=reason)
        self._log_event("reconcile_success", reason=reason)
        self._update_stale_transitions()

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
        try:
            persisted = self.state_store.load_runtime_risk_config()
        except Exception:  # noqa: BLE001
            logger.exception("runtime_risk_config_load_failed")
            return
        normalized = _normalize_runtime_risk_config(persisted)
        if not normalized:
            return
        normalized, stripped_changed = self._strip_persisted_strategy_runtime_overrides(
            normalized
        )
        normalized, changed = self._migrate_legacy_strategy_runtime_defaults(normalized)
        normalized, scheduler_changed = self._migrate_persisted_scheduler_runtime_defaults(
            normalized
        )
        for key, value in normalized.items():
            self._risk[key] = value
        if changed or stripped_changed or scheduler_changed:
            self._persist_risk_config()

    def _persist_risk_config(self) -> None:
        try:
            self.state_store.save_runtime_risk_config(
                config=self._persistent_risk_config()
            )
        except Exception:  # noqa: BLE001
            logger.exception("runtime_risk_config_save_failed")

    def _public_risk_config(self) -> dict[str, Any]:
        return _serialize_runtime_risk_config(self._risk)

    def _alpha_v2_profile_params(self) -> dict[str, Any]:
        for entry in self.cfg.behavior.strategies:
            if not bool(getattr(entry, "enabled", False)):
                continue
            if str(getattr(entry, "name", "")).strip() != "ra_2026_alpha_v2":
                continue
            params = getattr(entry, "params", None)
            if isinstance(params, dict):
                return dict(params)
            return {}
        return {}

    def _strategy_runtime_defaults(self) -> dict[str, Any]:
        strategy_params = self._alpha_v2_profile_params()
        if not strategy_params:
            return {}
        defaults: dict[str, Any] = {}
        for key in _ALPHA_V2_RUNTIME_PARAM_KEYS:
            base = copy.deepcopy(_ALPHA_V2_RUNTIME_DEFAULTS[key])
            defaults[key] = copy.deepcopy(strategy_params.get(key, base))
        return defaults

    def _strategy_runtime_snapshot(self) -> dict[str, Any]:
        defaults = self._strategy_runtime_defaults()
        if not defaults:
            return {}
        snapshot: dict[str, Any] = {}
        for key, default in defaults.items():
            snapshot[key] = copy.deepcopy(self._risk.get(key, default))
        return snapshot

    def _non_persistent_risk_keys(self) -> set[str]:
        keys = set(_ALPHA_V2_RUNTIME_PARAM_KEYS)
        keys.update(_RUNTIME_DERIVED_RISK_KEYS)
        sched_sec = int(_to_float(self._risk.get("scheduler_tick_sec"), default=float(self.scheduler.tick_seconds)))
        keys.update(self._profile_runtime_risk_overrides(sched_sec=sched_sec).keys())
        return keys

    def _persistent_risk_config(self) -> dict[str, Any]:
        payload = _normalize_runtime_risk_config(self._risk)
        for key in self._non_persistent_risk_keys():
            payload.pop(key, None)
        return payload

    def _strip_persisted_strategy_runtime_overrides(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        stripped = dict(payload)
        changed = False
        for key in self._non_persistent_risk_keys():
            if key not in stripped:
                continue
            stripped.pop(key, None)
            changed = True
        return stripped, changed

    def _migrate_legacy_strategy_runtime_defaults(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        defaults = self._strategy_runtime_defaults()
        if not defaults:
            return dict(payload), False
        migrated = dict(payload)
        changed = False
        for key, legacy_default in _LEGACY_CONTROLLER_SEEDED_STRATEGY_DEFAULTS.items():
            if key not in migrated or key not in defaults:
                continue
            current = migrated.get(key)
            profile_default = defaults[key]
            if current == legacy_default and profile_default != legacy_default:
                migrated[key] = copy.deepcopy(profile_default)
                changed = True
        return migrated, changed

    def _migrate_persisted_scheduler_runtime_defaults(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        migrated = dict(payload)
        if "scheduler_tick_sec" not in migrated:
            return migrated, False

        current_tick = max(
            1,
            int(
                _to_float(
                    self._risk.get("scheduler_tick_sec"),
                    default=float(self.scheduler.tick_seconds),
                )
            ),
        )
        persisted_tick = max(
            1,
            int(_to_float(migrated.get("scheduler_tick_sec"), default=float(current_tick))),
        )
        current_defaults = self._freshness_defaults_for_scheduler_tick(sched_sec=current_tick)
        next_defaults = self._freshness_defaults_for_scheduler_tick(sched_sec=persisted_tick)

        changed = False
        for key in ("user_ws_stale_sec", "market_data_stale_sec", "watchdog_interval_sec"):
            if key in migrated:
                current_value = _to_float(migrated.get(key), default=current_defaults[key])
                if abs(current_value - current_defaults[key]) >= 1e-9:
                    continue
            migrated[key] = next_defaults[key]
            changed = True
        return migrated, changed

    def _sync_kernel_runtime_overrides(self) -> None:
        symbols = self._runtime_symbols()
        mapping = self._runtime_symbol_leverage_map()
        target_margin = self._runtime_target_margin()
        max_leverage = max(1.0, _to_float(self._risk.get("max_leverage"), default=1.0))

        # Keep the kernel fallback on unlevered budget. The sizer applies the
        # final leverage so candidate caps and runtime leverage overrides cannot
        # inflate effective margin usage through double-counting.
        fallback_notional = float(target_margin)
        if fallback_notional <= 0.0:
            fallback_notional = 10.0

        max_position_notional = _to_float(
            self._risk.get("max_position_notional_usdt"),
            default=0.0,
        )
        capped_notional: float | None = (
            float(max_position_notional) if max_position_notional > 0 else None
        )
        if hasattr(self.kernel, "set_universe_symbols"):
            self.kernel.set_universe_symbols(symbols)  # type: ignore[attr-defined]
        if hasattr(self.kernel, "set_symbol_leverage_map"):
            self.kernel.set_symbol_leverage_map(  # type: ignore[attr-defined]
                mapping,
                max_leverage=max_leverage,
            )
        if hasattr(self.kernel, "set_notional_config"):
            self.kernel.set_notional_config(  # type: ignore[attr-defined]
                fallback_notional=fallback_notional,
                max_notional=capped_notional,
            )
        strategy_runtime = self._strategy_runtime_snapshot()
        if hasattr(self.kernel, "set_strategy_runtime_params") and strategy_runtime:
            self.kernel.set_strategy_runtime_params(  # type: ignore[attr-defined]
                **strategy_runtime,
            )
        if hasattr(self.kernel, "set_runtime_context"):
            self.kernel.set_runtime_context(  # type: ignore[attr-defined]
                daily_loss_limit_pct=self._risk.get("daily_loss_limit_pct"),
                dd_limit_pct=self._risk.get("dd_limit_pct"),
                daily_loss_used_pct=float(self._risk.get("daily_loss_used_pct") or 0.0),
                dd_used_pct=float(self._risk.get("dd_used_pct") or 0.0),
                lose_streak=int(self._risk.get("lose_streak") or 0),
                cooldown_until=self._risk.get("cooldown_until"),
                risk_score_min=self._risk.get("risk_score_min"),
                spread_max_pct=self._risk.get("spread_max_pct"),
                dd_scale_start_pct=self._risk.get("dd_scale_start_pct"),
                dd_scale_max_pct=self._risk.get("dd_scale_max_pct"),
                dd_scale_min_factor=self._risk.get("dd_scale_min_factor"),
                recent_blocks=dict(self._risk.get("recent_blocks") or {}),
            )

    def _effective_budget_leverage(
        self,
        *,
        symbols: list[str],
        max_leverage: float,
        symbol_leverage_map: dict[str, float],
    ) -> float:
        base = max(1.0, float(max_leverage))
        if not symbols:
            return base

        resolved: list[float] = []
        for sym in symbols:
            candidate = symbol_leverage_map.get(sym, base)
            lev = max(1.0, _to_float(candidate, default=base))
            resolved.append(min(lev, base))

        if not resolved:
            return base
        return max(1.0, min(resolved))

    @staticmethod
    def _freshness_defaults_for_scheduler_tick(*, sched_sec: int) -> dict[str, float]:
        sec = max(1, int(sched_sec))
        return {
            "user_ws_stale_sec": max(float(sec) * 4.0, 60.0),
            "market_data_stale_sec": max(float(sec) * 2.0, 30.0),
            "watchdog_interval_sec": float(sec),
        }

    def _apply_scheduler_tick_change(self, *, sec: int) -> None:
        normalized = max(1, int(sec))
        previous = max(
            1,
            int(_to_float(self._risk.get("scheduler_tick_sec"), default=float(self.scheduler.tick_seconds))),
        )
        prev_defaults = self._freshness_defaults_for_scheduler_tick(sched_sec=previous)
        next_defaults = self._freshness_defaults_for_scheduler_tick(sched_sec=normalized)

        self.scheduler.tick_seconds = normalized
        self._risk["scheduler_tick_sec"] = normalized

        for key in ("user_ws_stale_sec", "market_data_stale_sec", "watchdog_interval_sec"):
            current_value = _to_float(self._risk.get(key), default=prev_defaults[key])
            if abs(current_value - prev_defaults[key]) < 1e-9:
                self._risk[key] = next_defaults[key]

    def _initial_risk_config(self) -> dict[str, Any]:
        risk_cfg = self.cfg.behavior.risk
        tpsl_cfg = self.cfg.behavior.tpsl
        sched_sec = int(self.cfg.behavior.scheduler.tick_seconds)
        freshness_defaults = self._freshness_defaults_for_scheduler_tick(sched_sec=sched_sec)
        day_key = datetime.now(timezone.utc).date().isoformat()
        config = {
            "risk_per_trade_pct": 10.0,
            "max_exposure_pct": float(risk_cfg.max_exposure_pct),
            "max_notional_pct": 1000.0,
            "max_leverage": float(risk_cfg.max_leverage),
            "daily_loss_limit_pct": float(risk_cfg.daily_loss_limit_pct),
            "dd_limit_pct": float(risk_cfg.dd_limit_pct),
            "daily_loss_used_pct": 0.0,
            "dd_used_pct": 0.0,
            "daily_realized_pnl": 0.0,
            "daily_realized_pct": 0.0,
            "lose_streak": 0,
            "cooldown_until": None,
            "risk_day": day_key,
            "recent_blocks": {},
            "daily_lock": False,
            "dd_lock": False,
            "runtime_equity_now_usdt": 0.0,
            "runtime_equity_peak_usdt": 0.0,
            "last_auto_risk_reason": None,
            "last_auto_risk_at": None,
            "auto_risk_enabled": True,
            "auto_pause_on_risk": True,
            "auto_safe_mode_on_risk": True,
            "auto_flatten_on_risk": True,
            "risk_score_min": None,
            "dd_scale_start_pct": 0.12,
            "dd_scale_max_pct": 0.32,
            "dd_scale_min_factor": 0.35,
            "lose_streak_n": 3,
            "cooldown_hours": 2,
            "min_hold_minutes": 0,
            "trend_enter_adx_4h": 22.0,
            "trend_exit_adx_4h": 18.0,
            "regime_hold_bars_4h": 2,
            "breakout_buffer_bps": 8.0,
            "breakout_bar_size_atr_max": 1.6,
            "min_volume_ratio_15m": 1.2,
            "range_enabled": False,
            "overheat_funding_abs": 0.0008,
            "overheat_long_short_ratio_cap": 1.8,
            "overheat_long_short_ratio_floor": 0.56,
            "exec_mode_default": "MARKET",
            "exec_limit_timeout_sec": 3.0,
            "exec_limit_retries": 2,
            "scheduler_tick_sec": sched_sec,
            "notify_interval_sec": sched_sec,
            "spread_max_pct": 0.5,
            "allow_market_when_wide_spread": False,
            "user_ws_stale_sec": freshness_defaults["user_ws_stale_sec"],
            "market_data_stale_sec": freshness_defaults["market_data_stale_sec"],
            "reconcile_max_age_sec": 300.0,
            "capital_mode": "MARGIN_BUDGET_USDT",
            "capital_pct": 1.0,
            "capital_usdt": 100.0,
            "margin_budget_usdt": 100.0,
            "margin_use_pct": 1.0,
            "max_position_notional_usdt": None,
            "fee_buffer_pct": 0.001,
            "universe_symbols": [self.cfg.behavior.exchange.default_symbol],
            "enable_watchdog": False,
            "watchdog_interval_sec": freshness_defaults["watchdog_interval_sec"],
            "shock_1m_pct": 0.0,
            "shock_from_entry_pct": 0.0,
            "trailing_enabled": bool(tpsl_cfg.trailing_enabled),
            "trailing_mode": "PCT",
            "trail_arm_pnl_pct": float(tpsl_cfg.take_profit_pct * 100.0),
            "trail_distance_pnl_pct": float(tpsl_cfg.stop_loss_pct * 100.0),
            "trail_grace_minutes": 0,
            "atr_trail_timeframe": "1h",
            "atr_trail_k": 2.0,
            "atr_trail_min_pct": 0.6,
            "atr_trail_max_pct": 1.8,
            "tpsl_policy": "adaptive_regime",
            "tpsl_method": "percent",
            "tpsl_base_take_profit_pct": float(tpsl_cfg.take_profit_pct),
            "tpsl_base_stop_loss_pct": float(tpsl_cfg.stop_loss_pct),
            "tpsl_regime_mult_bull": 1.15,
            "tpsl_regime_mult_bear": 1.15,
            "tpsl_regime_mult_sideways": 0.9,
            "tpsl_regime_mult_unknown": 1.0,
            "tpsl_volatility_norm_enabled": False,
            "tpsl_atr_pct_ref": 0.01,
            "tpsl_vol_mult_min": 0.85,
            "tpsl_vol_mult_max": 1.2,
            "tpsl_tp_min_pct": 0.0025,
            "tpsl_tp_max_pct": 0.06,
            "tpsl_sl_min_pct": 0.0025,
            "tpsl_sl_max_pct": 0.03,
            "tpsl_rr_min": 0.8,
            "tpsl_rr_max": 3.0,
            "tpsl_tp_atr": 2.0,
            "tpsl_sl_atr": 1.0,
            "vol_shock_atr_mult_threshold": 0.0,
            "atr_mult_mean_window": 0,
            "symbol_leverage_map": {},
            "last_strategy_block_reason": None,
            "last_alpha_id": None,
            "last_entry_family": None,
            "last_regime": None,
            "last_alpha_blocks": {},
            "last_alpha_reject_focus": None,
            "last_alpha_reject_metrics": {},
            "overheat_state": {"blocked": False, "reason": None},
        }
        config.update(self._strategy_runtime_defaults())
        config.update(self._profile_runtime_risk_overrides(sched_sec=sched_sec))
        return config

    def _profile_runtime_risk_overrides(self, *, sched_sec: int) -> dict[str, Any]:
        from v2.control.profile_policy import profile_runtime_risk_overrides

        return profile_runtime_risk_overrides(self, sched_sec=sched_sec)

    def _live_readiness_snapshot(
        self,
        *,
        live_balance_source: str | None = None,
        private_error: str | None = None,
    ) -> dict[str, Any]:
        from v2.control.profile_policy import build_live_readiness_snapshot

        return build_live_readiness_snapshot(
            self,
            live_balance_source=live_balance_source,
            private_error=private_error,
        )

    def _runtime_symbols(self) -> list[str]:
        symbols_raw = self._risk.get("universe_symbols")
        if isinstance(symbols_raw, list):
            symbols = [str(sym).strip().upper() for sym in symbols_raw if str(sym).strip()]
        else:
            symbols = [self.cfg.behavior.exchange.default_symbol]
        if not symbols:
            symbols = [self.cfg.behavior.exchange.default_symbol]
        return symbols

    def _runtime_symbol_leverage_map(self) -> dict[str, float]:
        mapping_raw = self._risk.get("symbol_leverage_map")
        mapping: dict[str, float] = {}
        if isinstance(mapping_raw, dict):
            for sym, lev in mapping_raw.items():
                sym_u = str(sym).strip().upper()
                if not sym_u:
                    continue
                lev_f = _to_float(lev, default=0.0)
                if lev_f > 0:
                    mapping[sym_u] = lev_f
        return mapping

    def _runtime_target_margin(self) -> float:
        capital_mode = str(self._risk.get("capital_mode") or "").upper()
        margin_use_pct = max(0.0, _to_float(self._risk.get("margin_use_pct"), default=1.0))
        margin_budget = _to_float(self._risk.get("margin_budget_usdt"), default=100.0)
        fixed_budget = _to_float(self._risk.get("capital_usdt"), default=100.0)
        base_margin = fixed_budget if capital_mode == "FIXED_USDT" else margin_budget
        target_margin = float(base_margin) * float(margin_use_pct)
        if target_margin <= 0:
            target_margin = 10.0
        return target_margin

    def _runtime_budget_context(self) -> tuple[list[str], dict[str, float], float, float, float]:
        symbols = self._runtime_symbols()
        mapping = self._runtime_symbol_leverage_map()
        target_margin = self._runtime_target_margin()
        max_leverage = max(1.0, _to_float(self._risk.get("max_leverage"), default=1.0))
        leverage = self._effective_budget_leverage(
            symbols=symbols,
            max_leverage=max_leverage,
            symbol_leverage_map=mapping,
        )
        expected_notional = float(target_margin) * float(leverage)
        max_position_notional = _to_float(
            self._risk.get("max_position_notional_usdt"),
            default=0.0,
        )
        if max_position_notional > 0:
            expected_notional = min(expected_notional, float(max_position_notional))
        effective_margin = expected_notional / float(leverage) if leverage > 0 else target_margin
        return symbols, mapping, effective_margin, leverage, expected_notional

    def _record_recent_block(self, reason: str) -> None:
        raw_reason = str(reason or "unknown").strip() or "unknown"
        normalized = raw_reason.split(":", 1)[0]
        blocks_raw = self._risk.get("recent_blocks")
        blocks = dict(blocks_raw) if isinstance(blocks_raw, dict) else {}
        blocks[normalized] = int(_to_float(blocks.get(normalized), default=0.0)) + 1
        ranked = sorted(blocks.items(), key=lambda item: (-int(item[1]), item[0]))[:10]
        self._risk["recent_blocks"] = {key: count for key, count in ranked}
        self._risk["last_block_reason"] = raw_reason

    def _refresh_runtime_risk_context(self) -> None:
        state = self.state_store.get()
        today = datetime.now(timezone.utc).date().isoformat()
        _symbols, _mapping, effective_margin, _leverage, _expected_notional = self._runtime_budget_context()
        capital_base = max(float(effective_margin), 1e-9)
        live_equity_basis_ok = self.cfg.mode != "live"
        cached_balance = self._cached_live_balance(max_age_sec=300.0)
        if cached_balance is not None:
            _available, wallet = cached_balance
            if wallet is not None and float(wallet) > 0.0:
                capital_base = max(float(wallet), capital_base)
                live_equity_basis_ok = True
        elif self.cfg.mode == "live":
            _available, wallet, source = self._fetch_live_usdt_balance()
            if source in {"exchange", "exchange_cached"} and wallet is not None and float(wallet) > 0.0:
                capital_base = max(float(wallet), capital_base)
                live_equity_basis_ok = True

        daily_realized = 0.0
        lose_streak = 0
        last_loss_time_ms: int | None = None

        for fill in state.last_fills:
            pnl = fill.realized_pnl if fill.realized_pnl is not None else 0.0
            fill_time_ms = int(fill.fill_time_ms or 0)
            if fill_time_ms > 0:
                fill_day = datetime.fromtimestamp(fill_time_ms / 1000.0, tz=timezone.utc).date().isoformat()
                if fill_day == today:
                    daily_realized += float(pnl)

        for fill in reversed(state.last_fills):
            pnl = fill.realized_pnl
            if pnl is None:
                continue
            if float(pnl) < 0.0:
                lose_streak += 1
                if last_loss_time_ms is None and fill.fill_time_ms is not None:
                    last_loss_time_ms = int(fill.fill_time_ms)
                continue
            break

        unrealized = 0.0
        for position in state.current_position.values():
            unrealized += float(position.unrealized_pnl or 0.0)

        equity_now = capital_base + unrealized
        equity_peak = max(
            _to_float(self._risk.get("runtime_equity_peak_usdt"), default=0.0),
            float(equity_now),
        )
        if equity_peak <= 0.0:
            equity_peak = capital_base

        daily_realized_pct = float(daily_realized) / capital_base if capital_base > 0.0 else 0.0
        daily_loss_used_pct = max(0.0, -daily_realized_pct)
        if self.cfg.mode == "live" and not live_equity_basis_ok:
            dd_used_pct = 0.0
        else:
            dd_used_pct = max(0.0, (equity_peak - equity_now) / equity_peak) if equity_peak > 0.0 else 0.0

        daily_limit = _normalize_pct(self._risk.get("daily_loss_limit_pct"), default=0.02)
        dd_limit = _normalize_pct(self._risk.get("dd_limit_pct"), default=0.15)
        daily_lock = daily_limit > 0.0 and daily_loss_used_pct >= daily_limit
        dd_lock = dd_limit > 0.0 and dd_used_pct >= dd_limit

        cooldown_until_raw = self._risk.get("cooldown_until")
        cooldown_until = float(cooldown_until_raw) if cooldown_until_raw is not None else 0.0
        lose_streak_trigger = max(int(_to_float(self._risk.get("lose_streak_n"), default=0.0)), 0)
        cooldown_hours = max(0.0, _to_float(self._risk.get("cooldown_hours"), default=0.0))
        if (
            lose_streak_trigger > 0
            and lose_streak >= lose_streak_trigger
            and last_loss_time_ms is not None
            and cooldown_hours > 0.0
        ):
            cooldown_until = max(
                cooldown_until,
                (float(last_loss_time_ms) / 1000.0) + (cooldown_hours * 3600.0),
            )
        if cooldown_until <= time.time():
            cooldown_until_value: float | None = None
        else:
            cooldown_until_value = cooldown_until

        self._risk["risk_day"] = today
        self._risk["daily_realized_pnl"] = float(daily_realized)
        self._risk["daily_realized_pct"] = float(daily_realized_pct)
        self._risk["daily_loss_used_pct"] = float(daily_loss_used_pct)
        self._risk["dd_used_pct"] = float(dd_used_pct)
        self._risk["lose_streak"] = int(lose_streak)
        self._risk["cooldown_until"] = cooldown_until_value
        self._risk["daily_lock"] = bool(daily_lock)
        self._risk["dd_lock"] = bool(dd_lock)
        self._risk["runtime_equity_now_usdt"] = float(equity_now)
        self._risk["runtime_equity_peak_usdt"] = float(equity_peak)
        if not isinstance(self._risk.get("recent_blocks"), dict):
            self._risk["recent_blocks"] = {}

    def _maybe_apply_auto_risk_circuit(self, cycle: KernelCycleResult) -> None:
        if not _to_bool(self._risk.get("auto_risk_enabled"), default=True):
            return
        if cycle.reason not in {"daily_loss_limit", "drawdown_limit"}:
            return

        self._risk["last_auto_risk_reason"] = cycle.reason
        self._risk["last_auto_risk_at"] = _utcnow_iso()
        self._log_event("risk_trip", reason=cycle.reason)
        if _to_bool(self._risk.get("auto_safe_mode_on_risk"), default=True):
            self.ops.safe_mode()
        elif _to_bool(self._risk.get("auto_pause_on_risk"), default=True):
            self.ops.pause()

        if self.cfg.mode != "live" or not _to_bool(self._risk.get("auto_flatten_on_risk"), default=True):
            return

        symbols = set(self._runtime_symbols())
        for symbol in self.state_store.get().current_position.keys():
            symbols.add(str(symbol).strip().upper())

        for symbol in sorted(sym for sym in symbols if sym):
            try:
                _ = _run_async_blocking(
                    lambda s=symbol: self.close_position(
                        symbol=s,
                        notify_reason="auto_risk_close",
                    ),
                    timeout_sec=30.0,
                )
            except Exception:  # noqa: BLE001
                logger.exception("auto_risk_flatten_failed symbol=%s reason=%s", symbol, cycle.reason)

    def _status_snapshot(self) -> dict[str, Any]:
        return build_status_snapshot(self)

    def _healthz_snapshot(self) -> dict[str, Any]:
        return build_healthz_snapshot(self)

    def _readyz_snapshot(self) -> dict[str, Any]:
        return build_readyz_snapshot(self)

    def _cached_live_balance(
        self, *, max_age_sec: float
    ) -> tuple[float | None, float | None] | None:
        return get_cached_live_balance(self, max_age_sec=max_age_sec)

    def _cached_or_fallback_balance(
        self,
        *,
        preserve_private_error: bool = False,
    ) -> tuple[float | None, float | None, str]:
        return get_cached_or_fallback_balance(
            self,
            preserve_private_error=preserve_private_error,
        )

    def _fetch_live_usdt_balance(self) -> tuple[float | None, float | None, str]:
        return fetch_live_usdt_balance(self)

    def _load_position_management_state(self) -> dict[str, dict[str, Any]]:
        payload = self.state_store.runtime_storage().load_runtime_marker(
            marker_key=_POSITION_MANAGEMENT_MARKER_KEY
        )
        if not isinstance(payload, dict):
            return {}
        positions = payload.get("positions")
        if not isinstance(positions, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for symbol, row in positions.items():
            symbol_u = str(symbol or "").strip().upper()
            if not symbol_u or not isinstance(row, dict):
                continue
            normalized[symbol_u] = dict(row)
        return normalized

    def _save_position_management_state(self, state: dict[str, dict[str, Any]]) -> None:
        normalized = {
            str(symbol).strip().upper(): dict(payload)
            for symbol, payload in state.items()
            if str(symbol).strip() and isinstance(payload, dict)
        }
        self.state_store.runtime_storage().save_runtime_marker(
            marker_key=_POSITION_MANAGEMENT_MARKER_KEY,
            payload={"positions": normalized, "updated_at": _utcnow_iso()},
        )

    def _clear_position_management_state(self, *, symbol: str) -> None:
        state = self._load_position_management_state()
        state.pop(str(symbol).strip().upper(), None)
        self._save_position_management_state(state)

    @staticmethod
    def _plan_management_policy(plan: dict[str, Any] | None) -> str:
        return plan_management_policy(plan)

    @classmethod
    def _plan_uses_runner_management(cls, plan: dict[str, Any] | None) -> bool:
        return plan_uses_runner_management(plan)

    @staticmethod
    def _position_management_side(candidate_side: str | None) -> str:
        side = str(candidate_side or "").strip().upper()
        return "LONG" if side == "BUY" else "SHORT"

    def _record_position_management_plan(self, *, cycle: KernelCycleResult) -> None:
        plan = build_position_management_plan(cycle=cycle)
        if not isinstance(plan, dict):
            return
        symbol = str(plan.get("symbol") or "").strip().upper()
        if not symbol:
            return
        state = self._load_position_management_state()
        state[symbol] = plan
        self._save_position_management_state(state)

    @staticmethod
    def _dynamic_weak_reduce_ratio(
        plan: dict[str, Any],
        *,
        stage: int,
        signal_view: dict[str, Any] | None = None,
    ) -> float:
        alpha_id = str(plan.get("alpha_id") or "").strip().lower()
        regime = str(
            (signal_view or {}).get("regime")
            or plan.get("entry_regime")
            or ""
        ).strip().upper()
        stage_one = 0.25
        stage_two = 0.50
        if alpha_id == "alpha_expansion":
            stage_one = 0.20
            stage_two = 0.35
        elif alpha_id == "alpha_breakout":
            stage_one = 0.30
            stage_two = 0.50
        elif alpha_id == "alpha_pullback":
            stage_one = 0.25
            stage_two = 0.45

        if regime in {"UNKNOWN", "SIDEWAYS", "NONE"}:
            stage_one *= 1.1
            stage_two *= 1.1

        ratio = stage_one if stage <= 1 else stage_two
        return _clamp(float(ratio), 0.15, 0.60)

    @staticmethod
    def _runner_lock_targets(plan: dict[str, Any]) -> list[tuple[float, float, int]]:
        volatility_frac = max(
            _to_float(plan.get("volatility_frac"), default=0.0),
            0.0,
        )
        if volatility_frac >= 0.015:
            return [(1.6, 0.35, 1), (2.2, 0.75, 2)]
        if 0.0 < volatility_frac <= 0.006:
            return [(1.4, 0.65, 1), (1.9, 1.25, 2)]
        return [(1.5, 0.50, 1), (2.0, 1.00, 2)]

    @staticmethod
    def _extract_latest_market_bar(snapshot: dict[str, Any], *, symbol: str) -> dict[str, float] | None:
        symbols_payload = snapshot.get("symbols")
        market = None
        if isinstance(symbols_payload, dict):
            market = symbols_payload.get(symbol)
        if not isinstance(market, dict):
            market = snapshot.get("market")
        if not isinstance(market, dict):
            return None
        candles = market.get("15m")
        if not isinstance(candles, list) or not candles:
            return None
        row = candles[-1]
        if isinstance(row, dict):
            open_time = _to_float(row.get("open_time") or row.get("openTime") or row.get("t"), default=0.0)
            close_time = _to_float(row.get("close_time") or row.get("closeTime") or row.get("T"), default=0.0)
            open_v = _to_float(row.get("open"), default=0.0)
            high_v = _to_float(row.get("high"), default=0.0)
            low_v = _to_float(row.get("low"), default=0.0)
            close_v = _to_float(row.get("close"), default=0.0)
        elif isinstance(row, (list, tuple)) and len(row) >= 7:
            open_time = _to_float(row[0], default=0.0)
            open_v = _to_float(row[1], default=0.0)
            high_v = _to_float(row[2], default=0.0)
            low_v = _to_float(row[3], default=0.0)
            close_v = _to_float(row[4], default=0.0)
            close_time = _to_float(row[6], default=0.0)
        else:
            return None
        if close_v <= 0.0:
            return None
        return {
            "open_time_ms": float(open_time),
            "close_time_ms": float(close_time),
            "open": float(open_v),
            "high": float(high_v),
            "low": float(low_v),
            "close": float(close_v),
        }

    @staticmethod
    def _bars_held_for_management(plan: dict[str, Any], bar: dict[str, float] | None) -> int:
        entry_time_ms = int(_to_float(plan.get("entry_time_ms"), default=0.0))
        if entry_time_ms <= 0:
            return 0
        current_time_ms = (
            int(_to_float((bar or {}).get("close_time_ms"), default=0.0))
            if isinstance(bar, dict)
            else 0
        )
        if current_time_ms <= 0:
            current_time_ms = int(time.time() * 1000)
        return max((current_time_ms - entry_time_ms) // (15 * 60 * 1000), 0)

    def _maybe_manage_open_position(
        self,
        *,
        symbol: str,
        row: dict[str, Any],
        plan: dict[str, Any],
        market_bar: dict[str, float] | None,
    ) -> tuple[bool, dict[str, Any]]:
        mark_price = _to_float(row.get("markPrice"), default=0.0)
        if mark_price <= 0.0 and isinstance(market_bar, dict):
            mark_price = _to_float(market_bar.get("close"), default=0.0)
        entry_price = _to_float(plan.get("entry_price"), default=0.0)
        risk_per_unit = _to_float(plan.get("risk_per_unit"), default=0.0)
        side = str(plan.get("side") or "").strip().upper()
        if mark_price <= 0.0 or entry_price <= 0.0 or risk_per_unit <= 0.0 or side not in {"LONG", "SHORT"}:
            return False, plan

        favorable_price = mark_price
        if isinstance(market_bar, dict):
            if side == "LONG":
                favorable_price = max(
                    favorable_price,
                    _to_float(market_bar.get("high"), default=favorable_price),
                )
            else:
                favorable_price = min(
                    favorable_price,
                    _to_float(market_bar.get("low"), default=favorable_price),
                )

        previous_best = _to_float(plan.get("max_favorable_price"), default=entry_price)
        if side == "LONG":
            best_price = max(previous_best, favorable_price)
            max_favorable_r = max((best_price - entry_price) / risk_per_unit, 0.0)
            current_r = (mark_price - entry_price) / risk_per_unit
        else:
            best_price = min(previous_best, favorable_price)
            max_favorable_r = max((entry_price - best_price) / risk_per_unit, 0.0)
            current_r = (entry_price - mark_price) / risk_per_unit

        plan["max_favorable_price"] = float(best_price)
        plan["max_favorable_r"] = float(max_favorable_r)
        held_bars = self._bars_held_for_management(plan, market_bar)
        plan["held_bars"] = int(held_bars)
        plan["current_r"] = float(current_r)
        plan["last_evaluated_at"] = _utcnow_iso()
        plan.setdefault("weak_reduce_stage", 0)
        plan.setdefault("runner_lock_stage", 0)
        plan.setdefault("entry_score", 0.0)

        position_amt = abs(_to_float(row.get("positionAmt"), default=0.0))
        position_side = str(row.get("positionSide") or "BOTH").strip().upper() or "BOTH"
        entry_side: Literal["BUY", "SELL"] = "BUY" if side == "LONG" else "SELL"
        exit_side: Literal["BUY", "SELL"] = "SELL" if side == "LONG" else "BUY"

        signal_view = self._inspect_position_signal(symbol=symbol)
        if signal_view is not None:
            signal_state = str(signal_view.get("state") or "").strip().lower()
            signal_reason = str(signal_view.get("reason") or "").strip().lower()
            current_signal_side = str(signal_view.get("side") or "").strip().upper()
            current_score = _to_float(signal_view.get("score"), default=0.0)
            current_regime_strength = _to_float(signal_view.get("regime_strength"), default=0.0)
            current_bias_strength = _to_float(signal_view.get("bias_strength"), default=0.0)
            entry_score = max(_to_float(plan.get("entry_score"), default=0.0), 0.0)
            if current_signal_side and current_signal_side not in {side} and signal_state == "candidate":
                plan = mark_exit_requested(plan)
                self._clear_position_management_state(symbol=symbol)
                _ = _run_async_blocking(
                    lambda s=symbol: self.close_position(
                        symbol=s,
                        notify_reason="signal_flip_close",
                    ),
                    timeout_sec=30.0,
                )
                self._log_event(
                    "position_management_exit",
                    symbol=symbol,
                    reason="signal_flip_close",
                    held_bars=int(held_bars),
                    max_favorable_r=round(float(max_favorable_r), 4),
                    signal_side=current_signal_side,
                )
                return True, plan

            if signal_state != "candidate" and signal_reason in {"regime_missing", "bias_missing"}:
                plan = mark_exit_requested(plan)
                self._clear_position_management_state(symbol=symbol)
                _ = _run_async_blocking(
                    lambda s=symbol: self.close_position(
                        symbol=s,
                        notify_reason="regime_bias_lost_close",
                    ),
                    timeout_sec=30.0,
                )
                self._log_event(
                    "position_management_exit",
                    symbol=symbol,
                    reason="regime_bias_lost_close",
                    held_bars=int(held_bars),
                    max_favorable_r=round(float(max_favorable_r), 4),
                    signal_reason=signal_reason,
                )
                return True, plan

            weak_reduce_stage = int(_to_float(plan.get("weak_reduce_stage"), default=0.0))
            score_weak = (
                current_score > 0.0
                and entry_score > 0.0
                and current_score < max(entry_score * 0.7, 0.52)
            )
            structure_weak = (
                current_regime_strength > 0.0
                and current_regime_strength < max(_to_float(plan.get("entry_regime_strength"), default=0.0) * 0.7, 0.45)
            ) or (
                current_bias_strength > 0.0
                and current_bias_strength < max(_to_float(plan.get("entry_bias_strength"), default=0.0) * 0.7, 0.45)
            )
            blocked_weak = signal_state != "candidate" and signal_reason in {
                "volume_missing",
                "trigger_missing",
                "quality_score_v2_missing",
                "quality_score_missing",
                "short_overextension_risk",
                "breakout_efficiency_missing",
                "breakout_stability_missing",
                "breakout_stability_edge_missing",
            }
            if position_amt > 0.0 and weak_reduce_stage == 0 and (score_weak or structure_weak or blocked_weak):
                reduce_qty = max(
                    position_amt
                    * self._dynamic_weak_reduce_ratio(
                        plan,
                        stage=1,
                        signal_view=signal_view,
                    ),
                    0.0,
                )
                if reduce_qty > 0.0 and self.rest_client is not None and hasattr(self.rest_client, "place_reduce_only_market_order"):
                    rest_client_any: Any = self.rest_client
                    _ = _run_async_blocking(
                        lambda s=symbol, side_out=exit_side, qty=reduce_qty, ps=position_side: (
                            rest_client_any.place_reduce_only_market_order(
                                symbol=s,
                                side=side_out,
                                quantity=qty,
                                position_side=cast(Literal["BOTH", "LONG", "SHORT"], ps),
                            )
                        ),
                        timeout_sec=15.0,
                    )
                    plan["weak_reduce_stage"] = 1
                    plan["breakeven_protection_armed"] = True
                    remaining_qty = max(position_amt - reduce_qty, 0.0)
                    if remaining_qty > 0.0 and _to_float(plan.get("take_profit_price"), default=0.0) > 0.0:
                        self._replace_management_bracket(
                            symbol=symbol,
                            entry_side=entry_side,
                            position_side=position_side,
                            quantity=remaining_qty,
                            take_profit_price=_to_float(plan.get("take_profit_price"), default=0.0),
                            stop_loss_price=float(entry_price),
                            reason="weakness_reduce_reprice",
                        )
                        plan["stop_price"] = float(entry_price)
                    plan = mark_runner_activated(plan)
                    self._log_event(
                        "position_reduced",
                        symbol=symbol,
                        reason="signal_weakness_reduce",
                        stage=1,
                        reduced_qty=round(float(reduce_qty), 8),
                        remaining_qty=round(float(remaining_qty), 8),
                        current_r=round(float(current_r), 4),
                        event_time=_utcnow_iso(),
                    )
                    return False, plan

            severe_score_weak = (
                current_score > 0.0
                and entry_score > 0.0
                and current_score < max(entry_score * 0.55, 0.45)
            )
            severe_structure_weak = (
                current_regime_strength > 0.0
                and current_regime_strength
                < max(_to_float(plan.get("entry_regime_strength"), default=0.0) * 0.55, 0.35)
            ) or (
                current_bias_strength > 0.0
                and current_bias_strength
                < max(_to_float(plan.get("entry_bias_strength"), default=0.0) * 0.55, 0.35)
            )
            if position_amt > 0.0 and weak_reduce_stage == 1 and (
                severe_score_weak or severe_structure_weak or blocked_weak
            ):
                reduce_qty = max(
                    position_amt
                    * self._dynamic_weak_reduce_ratio(
                        plan,
                        stage=2,
                        signal_view=signal_view,
                    ),
                    0.0,
                )
                if reduce_qty > 0.0 and self.rest_client is not None and hasattr(self.rest_client, "place_reduce_only_market_order"):
                    rest_client_any: Any = self.rest_client
                    _ = _run_async_blocking(
                        lambda s=symbol, side_out=exit_side, qty=reduce_qty, ps=position_side: (
                            rest_client_any.place_reduce_only_market_order(
                                symbol=s,
                                side=side_out,
                                quantity=qty,
                                position_side=cast(Literal["BOTH", "LONG", "SHORT"], ps),
                            )
                        ),
                        timeout_sec=15.0,
                    )
                    plan["weak_reduce_stage"] = 2
                    remaining_qty = max(position_amt - reduce_qty, 0.0)
                    if remaining_qty > 0.0 and _to_float(plan.get("take_profit_price"), default=0.0) > 0.0:
                        self._replace_management_bracket(
                            symbol=symbol,
                            entry_side=entry_side,
                            position_side=position_side,
                            quantity=remaining_qty,
                            take_profit_price=_to_float(plan.get("take_profit_price"), default=0.0),
                            stop_loss_price=_to_float(plan.get("stop_price"), default=entry_price),
                            reason="weakness_reduce_reprice",
                        )
                    plan = mark_runner_activated(plan)
                    self._log_event(
                        "position_reduced",
                        symbol=symbol,
                        reason="signal_weakness_reduce",
                        stage=2,
                        reduced_qty=round(float(reduce_qty), 8),
                        remaining_qty=round(float(remaining_qty), 8),
                        current_r=round(float(current_r), 4),
                        event_time=_utcnow_iso(),
                    )
                    return False, plan

        runner_lock_stage = int(_to_float(plan.get("runner_lock_stage"), default=0.0))
        if bool(plan.get("partial_reduce_done")):
            lock_target_r = 0.0
            target_stage = 0
            for trigger_r, target_r, stage in self._runner_lock_targets(plan):
                if max_favorable_r >= trigger_r and runner_lock_stage < stage:
                    lock_target_r = target_r
                    target_stage = stage
            if target_stage > 0:
                plan["runner_lock_stage"] = target_stage
            if lock_target_r > 0.0 and position_amt > 0.0:
                locked_stop = (
                    entry_price + (risk_per_unit * lock_target_r)
                    if side == "LONG"
                    else entry_price - (risk_per_unit * lock_target_r)
                )
                if (side == "LONG" and locked_stop > _to_float(plan.get("stop_price"), default=0.0)) or (
                    side == "SHORT" and (
                        _to_float(plan.get("stop_price"), default=0.0) <= 0.0
                        or locked_stop < _to_float(plan.get("stop_price"), default=0.0)
                    )
                ):
                    self._replace_management_bracket(
                        symbol=symbol,
                        entry_side=entry_side,
                        position_side=position_side,
                        quantity=position_amt,
                        take_profit_price=_to_float(plan.get("take_profit_price"), default=0.0),
                        stop_loss_price=locked_stop,
                        reason="runner_lock_reprice",
                    )
                    plan["stop_price"] = float(locked_stop)
                    self._log_event(
                        "position_management_update",
                        symbol=symbol,
                        reason="runner_lock_reprice",
                        held_bars=int(held_bars),
                        max_favorable_r=round(float(max_favorable_r), 4),
                        locked_r=lock_target_r,
                        lock_stage=int(target_stage),
                        volatility_frac=round(_to_float(plan.get("volatility_frac"), default=0.0), 6),
                    )

        tp_partial_ratio = min(max(_to_float(plan.get("tp_partial_ratio"), default=0.0), 0.0), 1.0)
        tp_partial_at_r = _to_float(plan.get("tp_partial_at_r"), default=0.0)
        if (
            position_amt > 0.0
            and tp_partial_ratio > 0.0
            and tp_partial_at_r > 0.0
            and not bool(plan.get("partial_reduce_done"))
            and current_r >= tp_partial_at_r
            and self.rest_client is not None
            and hasattr(self.rest_client, "place_reduce_only_market_order")
        ):
            reduce_qty = max(position_amt * tp_partial_ratio, 0.0)
            if reduce_qty > 0.0:
                rest_client_any: Any = self.rest_client
                _ = _run_async_blocking(
                    lambda s=symbol, side_out=exit_side, qty=reduce_qty, ps=position_side: (
                        rest_client_any.place_reduce_only_market_order(
                            symbol=s,
                            side=side_out,
                            quantity=qty,
                            position_side=cast(Literal["BOTH", "LONG", "SHORT"], ps),
                        )
                    ),
                    timeout_sec=15.0,
                )
                plan["partial_reduce_done"] = True
                plan["partial_reduce_qty"] = float(reduce_qty)
                plan["partial_reduce_at_r_done"] = float(current_r)
                plan["breakeven_protection_armed"] = True

                remaining_qty = max(position_amt - reduce_qty, 0.0)
                if remaining_qty > 0.0:
                    reward_r = max(_to_float(plan.get("reward_risk_reference_r"), default=0.0), 0.0)
                    if reward_r > 0.0:
                        take_profit_price = (
                            entry_price + (risk_per_unit * reward_r)
                            if side == "LONG"
                            else entry_price - (risk_per_unit * reward_r)
                        )
                    else:
                        take_profit_price = _to_float(plan.get("take_profit_price"), default=0.0)
                    stop_price = float(entry_price)
                    self._replace_management_bracket(
                        symbol=symbol,
                        entry_side=entry_side,
                        position_side=position_side,
                        quantity=remaining_qty,
                        take_profit_price=take_profit_price,
                        stop_loss_price=stop_price,
                        reason="partial_reduce_reprice",
                    )
                    plan["stop_price"] = float(stop_price)
                    if take_profit_price > 0.0:
                        plan["take_profit_price"] = float(take_profit_price)
                plan = mark_runner_activated(plan)

                self._log_event(
                    "position_reduced",
                    symbol=symbol,
                    reason="partial_reduce_executed",
                    side=exit_side,
                    reduced_qty=round(float(reduce_qty), 8),
                    remaining_qty=round(float(remaining_qty), 8),
                    current_r=round(float(current_r), 4),
                    event_time=_utcnow_iso(),
                )
                self._log_event(
                    "position_management_update",
                    symbol=symbol,
                    reason="partial_reduce_executed",
                    held_bars=int(held_bars),
                    max_favorable_r=round(float(max_favorable_r), 4),
                    partial_reduce_qty=round(float(reduce_qty), 8),
                    partial_reduce_at_r=round(float(current_r), 4),
                )
                return False, plan

        extend_trigger_r = _to_float(plan.get("progress_extend_trigger_r"), default=0.0)
        extend_bars = int(_to_float(plan.get("progress_extend_bars"), default=0.0))
        if (
            extend_trigger_r > 0.0
            and extend_bars > 0
            and not bool(plan.get("progress_extension_applied"))
            and max_favorable_r >= extend_trigger_r
        ):
            current_time_stop = int(_to_float(plan.get("current_time_stop_bars"), default=0.0))
            plan["current_time_stop_bars"] = current_time_stop + extend_bars
            plan["progress_extension_applied"] = True
            self._log_event(
                "position_management_update",
                symbol=symbol,
                reason="progress_extension_applied",
                held_bars=int(held_bars),
                max_favorable_r=round(float(max_favorable_r), 4),
                current_time_stop_bars=int(plan["current_time_stop_bars"]),
            )

        if not bool(plan.get("selective_extension_activated")):
            proof_bars = int(_to_float(plan.get("selective_extension_proof_bars"), default=0.0))
            if (
                proof_bars > 0
                and held_bars <= proof_bars
                and max_favorable_r >= _to_float(plan.get("selective_extension_min_mfe_r"), default=0.0)
                and _to_float(plan.get("entry_regime_strength"), default=0.0)
                >= _to_float(plan.get("selective_extension_min_regime_strength"), default=0.0)
                and _to_float(plan.get("entry_bias_strength"), default=0.0)
                >= _to_float(plan.get("selective_extension_min_bias_strength"), default=0.0)
                and _to_float(plan.get("entry_quality_score_v2"), default=0.0)
                >= _to_float(plan.get("selective_extension_min_quality_score_v2"), default=0.0)
            ):
                selective_time_stop = int(
                    _to_float(plan.get("selective_extension_time_stop_bars"), default=0.0)
                )
                if selective_time_stop > int(_to_float(plan.get("current_time_stop_bars"), default=0.0)):
                    plan["current_time_stop_bars"] = selective_time_stop
                plan["selective_extension_activated"] = True
                extension_tp_r = _to_float(plan.get("selective_extension_take_profit_r"), default=0.0)
                if position_amt > 0.0 and extension_tp_r > 0.0:
                    take_profit_price = (
                        entry_price + (risk_per_unit * extension_tp_r)
                        if side == "LONG"
                        else entry_price - (risk_per_unit * extension_tp_r)
                    )
                    stop_price = float(entry_price) if bool(plan.get("breakeven_protection_armed")) else _to_float(
                        plan.get("stop_price"),
                        default=0.0,
                    )
                    if stop_price > 0.0:
                        self._replace_management_bracket(
                            symbol=symbol,
                            entry_side=entry_side,
                            position_side=position_side,
                            quantity=position_amt,
                            take_profit_price=take_profit_price,
                            stop_loss_price=stop_price,
                            reason="selective_extension_reprice",
                        )
                        plan["take_profit_price"] = float(take_profit_price)
                        plan["stop_price"] = float(stop_price)
                self._log_event(
                    "position_management_update",
                    symbol=symbol,
                    reason="selective_extension_activated",
                    held_bars=int(held_bars),
                    max_favorable_r=round(float(max_favorable_r), 4),
                    current_time_stop_bars=int(_to_float(plan.get("current_time_stop_bars"), default=0.0)),
                )

        breakeven_trigger_r = _to_float(
            plan.get("selective_extension_move_stop_to_be_at_r"),
            default=0.0,
        )
        if breakeven_trigger_r <= 0.0:
            breakeven_trigger_r = _to_float(plan.get("move_stop_to_be_at_r"), default=0.0)
        if breakeven_trigger_r > 0.0 and max_favorable_r >= breakeven_trigger_r:
            if not bool(plan.get("breakeven_protection_armed")):
                plan["breakeven_protection_armed"] = True
                if position_amt > 0.0 and _to_float(plan.get("take_profit_price"), default=0.0) > 0.0:
                    self._replace_management_bracket(
                        symbol=symbol,
                        entry_side=entry_side,
                        position_side=position_side,
                        quantity=position_amt,
                        take_profit_price=_to_float(plan.get("take_profit_price"), default=0.0),
                        stop_loss_price=float(entry_price),
                        reason="breakeven_reprice",
                    )
                    plan["stop_price"] = float(entry_price)
                self._log_event(
                    "position_management_update",
                    symbol=symbol,
                    reason="breakeven_protection_armed",
                    held_bars=int(held_bars),
                    max_favorable_r=round(float(max_favorable_r), 4),
                )

        if bool(plan.get("breakeven_protection_armed")) and current_r <= 0.0:
            plan = mark_exit_requested(plan)
            self._clear_position_management_state(symbol=symbol)
            _ = _run_async_blocking(
                lambda s=symbol: self.close_position(
                    symbol=s,
                    notify_reason="management_breakeven_close",
                ),
                timeout_sec=30.0,
            )
            self._log_event(
                "position_management_exit",
                symbol=symbol,
                reason="management_breakeven_close",
                held_bars=int(held_bars),
                max_favorable_r=round(float(max_favorable_r), 4),
            )
            return True, plan

        progress_check_bars = int(_to_float(plan.get("progress_check_bars"), default=0.0))
        progress_min_mfe_r = _to_float(plan.get("progress_min_mfe_r"), default=0.0)
        if progress_check_bars > 0 and progress_min_mfe_r > 0.0 and held_bars >= progress_check_bars:
            if max_favorable_r < progress_min_mfe_r:
                plan = mark_exit_requested(plan)
                self._clear_position_management_state(symbol=symbol)
                _ = _run_async_blocking(
                    lambda s=symbol: self.close_position(
                        symbol=s,
                        notify_reason="progress_failed_close",
                    ),
                    timeout_sec=30.0,
                )
                self._log_event(
                    "position_management_exit",
                    symbol=symbol,
                    reason="progress_failed_close",
                    held_bars=int(held_bars),
                    max_favorable_r=round(float(max_favorable_r), 4),
                    progress_min_mfe_r=round(float(progress_min_mfe_r), 4),
                )
                return True, plan

        time_stop_bars = int(_to_float(plan.get("current_time_stop_bars"), default=0.0))
        if time_stop_bars > 0 and held_bars >= time_stop_bars:
            plan = mark_exit_requested(plan)
            self._clear_position_management_state(symbol=symbol)
            _ = _run_async_blocking(
                lambda s=symbol: self.close_position(
                    symbol=s,
                    notify_reason="time_stop_close",
                ),
                timeout_sec=30.0,
            )
            self._log_event(
                "position_management_exit",
                symbol=symbol,
                reason="time_stop_close",
                held_bars=int(held_bars),
                max_favorable_r=round(float(max_favorable_r), 4),
                time_stop_bars=int(time_stop_bars),
            )
            return True, plan

        return False, plan

    def _inspect_position_signal(self, *, symbol: str) -> dict[str, Any] | None:
        probe = getattr(self.kernel, "probe_market_data", None)
        snapshot = None
        if callable(probe):
            try:
                snapshot = probe()
            except Exception:  # noqa: BLE001
                logger.exception("position_signal_probe_failed symbol=%s", symbol)
                return None
        inspector = getattr(self.kernel, "inspect_symbol_decision", None)
        if not callable(inspector):
            return None
        try:
            decision = inspector(symbol=symbol, snapshot=snapshot)
        except Exception:  # noqa: BLE001
            logger.exception("position_signal_inspect_failed symbol=%s", symbol)
            return None
        if not isinstance(decision, dict):
            return None
        intent = str(decision.get("intent") or "").strip().upper()
        state = "candidate" if intent in {"LONG", "SHORT"} else "blocked"
        side = "LONG" if intent == "LONG" else "SHORT" if intent == "SHORT" else "NONE"
        return {
            "state": state,
            "side": side,
            "reason": str(decision.get("reason") or "").strip(),
            "score": _to_float(decision.get("score"), default=0.0),
            "regime_strength": _to_float(decision.get("regime_strength"), default=0.0),
            "bias_strength": _to_float(decision.get("bias_strength"), default=0.0),
            "regime": str(decision.get("regime") or "").strip().upper() or None,
            "alpha_id": str(decision.get("alpha_id") or "").strip() or None,
        }

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
        if self.cfg.mode != "live" or self.rest_client is None or quantity <= 0.0:
            return False
        if take_profit_price <= 0.0 or stop_loss_price <= 0.0:
            return False
        runtime_bracket_service = BracketService(
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
        try:
            _ = _run_async_blocking(
                lambda: runtime_bracket_service.replace_with_prices(
                    symbol=symbol,
                    entry_side=entry_side,
                    position_side=cast(Literal["BOTH", "LONG", "SHORT"], position_side),
                    quantity=float(quantity),
                    take_profit_price=float(take_profit_price),
                    stop_loss_price=float(stop_loss_price),
                ),
                timeout_sec=15.0,
            )
            self._log_event(
                "position_management_update",
                symbol=symbol,
                reason=reason,
                take_profit_price=round(float(take_profit_price), 6),
                stop_loss_price=round(float(stop_loss_price), 6),
                quantity=round(float(quantity), 8),
            )
            return True
        except Exception:  # noqa: BLE001
            logger.exception("position_management_bracket_replace_failed symbol=%s reason=%s", symbol, reason)
            return False

    def _resolve_bracket_config_for_cycle(
        self,
        *,
        cycle: KernelCycleResult,
        entry_price: float,
    ) -> tuple[BracketConfig, float | None, dict[str, Any]]:
        base_tp = max(
            0.0,
            _to_float(
                self._risk.get("tpsl_base_take_profit_pct"),
                default=float(self.cfg.behavior.tpsl.take_profit_pct),
            ),
        )
        base_sl = max(
            0.0,
            _to_float(
                self._risk.get("tpsl_base_stop_loss_pct"),
                default=float(self.cfg.behavior.tpsl.stop_loss_pct),
            ),
        )
        policy = str(self._risk.get("tpsl_policy") or "adaptive_regime").strip().lower()
        method_raw = str(self._risk.get("tpsl_method") or "percent").strip().lower()
        method: Literal["percent", "atr"] = "atr" if method_raw == "atr" else "percent"

        regime = str(getattr(cycle.candidate, "regime_hint", "") or "").strip().upper()
        bull_mult = _to_float(self._risk.get("tpsl_regime_mult_bull"), default=1.15)
        bear_mult = _to_float(self._risk.get("tpsl_regime_mult_bear"), default=1.15)
        sideways_mult = _to_float(self._risk.get("tpsl_regime_mult_sideways"), default=0.9)
        unknown_mult = _to_float(self._risk.get("tpsl_regime_mult_unknown"), default=1.0)
        regime_mult = unknown_mult
        if policy == "adaptive_regime":
            if regime == "BULL":
                regime_mult = bull_mult
            elif regime == "BEAR":
                regime_mult = bear_mult
            elif regime == "SIDEWAYS":
                regime_mult = sideways_mult

        tp_pct = base_tp * regime_mult
        sl_pct = base_sl * regime_mult

        volatility_mult = 1.0
        atr_hint = _to_float(getattr(cycle.candidate, "volatility_hint", 0.0), default=0.0)
        if _to_bool(self._risk.get("tpsl_volatility_norm_enabled"), default=False):
            if atr_hint > 0.0 and entry_price > 0.0:
                atr_pct = atr_hint / max(entry_price, 1e-9)
                atr_pct_ref = max(
                    1e-6,
                    _to_float(self._risk.get("tpsl_atr_pct_ref"), default=0.01),
                )
                raw_mult = (atr_pct / atr_pct_ref) ** 0.5
                vol_min = max(0.1, _to_float(self._risk.get("tpsl_vol_mult_min"), default=0.85))
                vol_max = max(vol_min, _to_float(self._risk.get("tpsl_vol_mult_max"), default=1.2))
                volatility_mult = _clamp(raw_mult, vol_min, vol_max)
                tp_pct *= volatility_mult
                sl_pct *= volatility_mult

        tp_min = max(0.0, _to_float(self._risk.get("tpsl_tp_min_pct"), default=0.0025))
        tp_max = max(tp_min, _to_float(self._risk.get("tpsl_tp_max_pct"), default=0.06))
        sl_min = max(0.0, _to_float(self._risk.get("tpsl_sl_min_pct"), default=0.0025))
        sl_max = max(sl_min, _to_float(self._risk.get("tpsl_sl_max_pct"), default=0.03))
        tp_pct = _clamp(tp_pct, tp_min, tp_max)
        sl_pct = _clamp(sl_pct, sl_min, sl_max)

        rr_min = max(0.1, _to_float(self._risk.get("tpsl_rr_min"), default=0.8))
        rr_max = max(rr_min, _to_float(self._risk.get("tpsl_rr_max"), default=3.0))
        if sl_pct > 0.0:
            rr_now = tp_pct / sl_pct
            if rr_now < rr_min:
                tp_pct = _clamp(sl_pct * rr_min, tp_min, tp_max)
            elif rr_now > rr_max:
                tp_pct = _clamp(sl_pct * rr_max, tp_min, tp_max)

        atr_for_bracket: float | None = None
        if method == "atr":
            if atr_hint > 0.0:
                atr_for_bracket = atr_hint
            else:
                method = "percent"

        cfg = BracketConfig(
            method=method,
            take_profit_pct=tp_pct,
            stop_loss_pct=sl_pct,
            tp_atr=max(0.1, _to_float(self._risk.get("tpsl_tp_atr"), default=2.0)),
            sl_atr=max(0.1, _to_float(self._risk.get("tpsl_sl_atr"), default=1.0)),
            working_type="MARK_PRICE",
            price_protect=True,
        )
        meta = {
            "policy": policy,
            "regime": regime or "UNKNOWN",
            "regime_mult": regime_mult,
            "volatility_mult": volatility_mult,
            "method": cfg.method,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
        }
        return cfg, atr_for_bracket, meta

    def _place_brackets_for_cycle(self, *, cycle: KernelCycleResult) -> None:
        self._last_cycle["bracket"] = None
        if cycle.state != "executed":
            return
        if cycle.candidate is None or cycle.size is None:
            return

        symbol = str(cycle.candidate.symbol or "").strip().upper()
        side = str(cycle.candidate.side or "").strip().upper()
        qty = _to_float(cycle.size.qty, default=0.0)
        entry_price = _to_float(cycle.candidate.entry_price, default=0.0)
        if not symbol or side not in {"BUY", "SELL"} or qty <= 0.0 or entry_price <= 0.0:
            self._last_cycle["bracket"] = {
                "state": "skipped",
                "reason": "invalid_bracket_inputs",
            }
            return

        bracket_cfg, atr_for_bracket, bracket_meta = self._resolve_bracket_config_for_cycle(
            cycle=cycle,
            entry_price=entry_price,
        )
        tracked_runtime = next(
            (
                row
                for row in self._list_tracked_brackets()
                if str(row.get("symbol") or "").strip().upper() == symbol
                and str(row.get("state") or "").strip().upper() in {"CREATED", "PLACED", "ACTIVE"}
            ),
            None,
        )
        if tracked_runtime is not None:
            tracked_ids = {
                str(tracked_runtime.get("tp_order_client_id") or "").strip(),
                str(tracked_runtime.get("sl_order_client_id") or "").strip(),
            }
            tracked_ids = {cid for cid in tracked_ids if cid}
            active_tracked_ids = set(tracked_ids)
            if self.cfg.mode == "live" and self.rest_client is not None and tracked_ids:
                rest_client_any: Any = self.rest_client
                try:
                    open_orders = _run_async_blocking(
                        lambda s=symbol: rest_client_any.get_open_algo_orders(symbol=s),
                        timeout_sec=8.0,
                    )
                    open_ids = {
                        str(item.get("clientAlgoId") or item.get("clientOrderId") or "").strip()
                        for item in (open_orders or [])
                        if isinstance(item, dict)
                    }
                    active_tracked_ids = {cid for cid in tracked_ids if cid in open_ids}
                except FutureTimeoutError:
                    logger.warning("existing_bracket_fetch_timed_out symbol=%s", symbol)
                except Exception:  # noqa: BLE001
                    logger.exception("existing_bracket_fetch_failed symbol=%s", symbol)
            if active_tracked_ids:
                self._last_cycle["bracket"] = {
                    "state": "active",
                    "symbol": symbol,
                    "policy": bracket_meta,
                    "reused": True,
                }
                return

        runtime_bracket_service = BracketService(
            planner=BracketPlanner(cfg=bracket_cfg),
            storage=self.state_store.runtime_storage(),
            rest_client=self.rest_client,
            mode=self.cfg.mode,
        )

        try:

            def _create_bracket(entry_side: Literal["BUY", "SELL"]):
                return runtime_bracket_service.create_and_place(
                    symbol=symbol,
                    entry_side=entry_side,
                    entry_price=entry_price,
                    quantity=qty,
                    atr=atr_for_bracket,
                )

            out = _run_async_blocking(
                (lambda: _create_bracket("BUY"))
                if side == "BUY"
                else (lambda: _create_bracket("SELL")),
                timeout_sec=10.0,
            )
        except Exception as exc:  # noqa: BLE001
            detail = str(exc).strip()
            err = f"bracket_failed:{type(exc).__name__}"
            if detail:
                err = f"{err}:{detail}"
            logger.exception("runtime_bracket_place_failed symbol=%s", symbol)
            self._last_cycle["bracket"] = {"state": "failed", "error": err}
            self._last_cycle["last_error"] = err
            return

        planned = out.get("planned") if isinstance(out, dict) else None
        if isinstance(planned, dict):
            self._last_cycle["bracket"] = {
                "state": "active",
                "symbol": symbol,
                "take_profit": _to_float(planned.get("take_profit_price"), default=0.0),
                "stop_loss": _to_float(planned.get("stop_loss_price"), default=0.0),
                "policy": bracket_meta,
            }
            return
        self._last_cycle["bracket"] = {"state": "active", "symbol": symbol, "policy": bracket_meta}

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
        symbol = str(trigger_symbol).strip().upper()
        if not symbol:
            return
        if symbol in self._sl_flatten_inflight_symbols:
            return

        cooldown_sec = max(5.0, _to_float(self._risk.get("sl_flatten_cooldown_sec"), default=60.0))
        now_mono = time.monotonic()
        last_mono = float(self._sl_last_flatten_mono_by_symbol.get(symbol) or 0.0)
        if last_mono > 0.0 and (now_mono - last_mono) < cooldown_sec:
            return

        self._sl_flatten_inflight_symbols.add(symbol)
        self._sl_last_flatten_mono_by_symbol[symbol] = now_mono
        self._watchdog_state["last_sl_flatten_symbol"] = symbol
        self._watchdog_state["last_sl_flatten_triggered_at"] = _utcnow_iso()
        try:
            _ = _run_async_blocking(
                lambda s=symbol: self.close_position(
                    symbol=s,
                    notify_reason="stoploss_forced_close",
                ),
                timeout_sec=30.0,
            )
        except Exception:  # noqa: BLE001
            logger.exception("sl_symbol_flatten_failed symbol=%s", symbol)
        finally:
            self._sl_flatten_inflight_symbols.discard(symbol)

    @staticmethod
    def _position_pnl_pct(row: dict[str, Any]) -> float | None:
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

    def _trailing_distance_pct(self, *, row: dict[str, Any]) -> float:
        mode = str(self._risk.get("trailing_mode") or "PCT").strip().upper()
        if mode == "ATR":
            min_pct = _to_float(self._risk.get("atr_trail_min_pct"), default=0.6)
            max_pct = _to_float(self._risk.get("atr_trail_max_pct"), default=1.8)
            return _clamp(min_pct, 0.0, max(min_pct, max_pct))
        return max(0.0, _to_float(self._risk.get("trail_distance_pnl_pct"), default=0.8))

    def _maybe_trigger_trailing_exit(
        self,
        *,
        symbol: str,
        row: dict[str, Any],
        rest_client: Any,
    ) -> bool:
        if not _to_bool(self._risk.get("trailing_enabled"), default=False):
            return False

        pnl_pct = self._position_pnl_pct(row)
        if pnl_pct is None:
            return False

        now = time.monotonic()
        state = self._trailing_state.get(symbol) or {
            "first_seen_mono": now,
            "peak_pnl_pct": pnl_pct,
            "armed": False,
        }
        first_seen = _to_float(state.get("first_seen_mono"), default=now)
        peak = max(_to_float(state.get("peak_pnl_pct"), default=pnl_pct), pnl_pct)
        arm_pct = max(0.0, _to_float(self._risk.get("trail_arm_pnl_pct"), default=1.2))
        grace_minutes = max(0, int(_to_float(self._risk.get("trail_grace_minutes"), default=0.0)))
        distance_pct = self._trailing_distance_pct(row=row)

        state["peak_pnl_pct"] = peak
        state["armed"] = bool(state.get("armed")) or peak >= arm_pct
        state["first_seen_mono"] = first_seen
        self._trailing_state[symbol] = state

        self._watchdog_state["last_trailing_symbol"] = symbol
        self._watchdog_state["last_trailing_pnl_pct"] = round(float(pnl_pct), 4)
        self._watchdog_state["last_trailing_peak_pct"] = round(float(peak), 4)
        self._watchdog_state["last_trailing_distance_pct"] = round(float(distance_pct), 4)

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
            _ = _run_async_blocking(
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
            _ = _run_async_blocking(
                lambda s=symbol: self._bracket_service.cleanup_if_flat(symbol=s, position_amt=0.0),
                timeout_sec=8.0,
            )
        except Exception:  # noqa: BLE001
            logger.exception("trailing_exit_failed symbol=%s", symbol)
            return False

        self._watchdog_state["last_trailing_triggered_symbol"] = symbol
        self._watchdog_state["last_trailing_triggered_at"] = _utcnow_iso()
        self._watchdog_state["last_trailing_triggered_pnl_pct"] = round(float(pnl_pct), 4)
        self._trailing_state.pop(symbol, None)
        return True

    @staticmethod
    def _is_managed_bracket_algo_id(value: str | None) -> bool:
        text = str(value or "").strip().lower()
        return text.startswith("v2tp") or text.startswith("v2sl")

    def _list_tracked_brackets(self) -> list[dict[str, Any]]:
        rows = self.state_store.runtime_storage().list_bracket_states()
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

    def _has_recent_fill_for_client_id(
        self,
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
        fills = self.state_store.runtime_storage().recent_fills(limit=100)
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

    def _latest_recent_fill_for_client_id(
        self,
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
        fills = self.state_store.runtime_storage().recent_fills(limit=100)
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

    def _infer_flat_bracket_exit(
        self,
        *,
        symbol: str,
        tp_id: str,
        sl_id: str,
    ) -> tuple[Literal["TP", "SL"], str | None] | None:
        tp_fill = self._latest_recent_fill_for_client_id(symbol=symbol, client_id=tp_id)
        sl_fill = self._latest_recent_fill_for_client_id(symbol=symbol, client_id=sl_id)

        if tp_fill is not None or sl_fill is not None:
            tp_fill_ms = int(_to_float((tp_fill or {}).get("fill_time_ms"), default=0.0))
            sl_fill_ms = int(_to_float((sl_fill or {}).get("fill_time_ms"), default=0.0))
            if tp_fill is not None and (sl_fill is None or tp_fill_ms >= sl_fill_ms):
                return "TP", tp_id or None
            if sl_fill is not None:
                return "SL", sl_id or None

        realized = self._resolve_symbol_realized_pnl(symbol=symbol)
        if realized is None:
            return None
        return ("TP" if realized > 0.0 else "SL"), None

    def _repair_position_bracket_from_plan(
        self,
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
        return self._replace_management_bracket(
            symbol=symbol,
            entry_side=entry_side,
            position_side=position_side,
            quantity=float(quantity),
            take_profit_price=float(take_profit_price),
            stop_loss_price=float(stop_loss_price),
            reason=reason,
        )

    def _latest_symbol_realized_pnl(
        self, *, symbol: str, lookback_sec: float = 900.0
    ) -> float | None:
        symbol_u = symbol.upper()
        lookback_ms = max(1, int(float(lookback_sec) * 1000.0))
        now_ms = int(time.time() * 1000)
        for row in self.state_store.runtime_storage().recent_fills(limit=100):
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

        fills = self.state_store.get().last_fills
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

    def _latest_symbol_realized_pnl_from_income(
        self, *, symbol: str, lookback_sec: float = 900.0
    ) -> float | None:
        rest_client = self.rest_client
        if (
            self.cfg.mode != "live"
            or rest_client is None
            or not hasattr(rest_client, "signed_request")
        ):
            return None

        symbol_u = symbol.upper()
        rest_client_any: Any = rest_client
        try:
            payload = _run_async_blocking(
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

    def _resolve_symbol_realized_pnl(self, *, symbol: str) -> float | None:
        realized = self._latest_symbol_realized_pnl(symbol=symbol)
        if realized is not None:
            return realized
        return self._latest_symbol_realized_pnl_from_income(symbol=symbol)

    def _emit_bracket_exit_alert(self, *, symbol: str, outcome: Literal["TP", "SL"]) -> None:
        realized = self._resolve_symbol_realized_pnl(symbol=symbol)
        normalized_realized = realized
        if realized is not None and abs(realized) < 0.00005:
            normalized_realized = 0.0
        try:
            _ = self.notifier.send_notification(
                build_bracket_exit_notification(
                    symbol=symbol,
                    outcome=outcome,
                    realized_pnl=normalized_realized,
                    context=self._notification_context(),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("bracket_exit_notify_failed symbol=%s outcome=%s", symbol, outcome)
        self._log_event(
            "position_closed",
            symbol=symbol,
            reason="take_profit" if outcome == "TP" else "stop_loss",
            realized_pnl=normalized_realized,
            outcome=outcome,
            event_time=_utcnow_iso(),
        )

    def _poll_brackets_once(self) -> None:
        rest_client = self.rest_client
        if self.cfg.mode != "live" or rest_client is None:
            return
        rest_client_any: Any = rest_client

        tracked = self._list_tracked_brackets()
        management_state = self._load_position_management_state()
        if not tracked and not management_state:
            return

        positions, position_rows, positions_ok, _position_error = self._fetch_live_positions()
        snapshot = None
        probe = getattr(self.kernel, "probe_market_data", None)
        if callable(probe):
            try:
                snapshot = probe()
            except Exception:  # noqa: BLE001
                logger.exception("position_management_market_probe_failed")

        tracked_by_symbol = {
            str(row.get("symbol") or "").strip().upper(): row for row in tracked if str(row.get("symbol") or "").strip()
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
                    open_orders = _run_async_blocking(
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
                    if cid not in {tp_id, sl_id} and self._is_managed_bracket_algo_id(cid)
                }
                for cid in sorted(extra_managed_ids):
                    try:
                        _ = _run_async_blocking(
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
                exited, updated_plan = self._maybe_manage_open_position(
                    symbol=symbol,
                    row=position_row,
                    plan=dict(management_plan),
                    market_bar=(
                        self._extract_latest_market_bar(snapshot, symbol=symbol)
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
                if self._maybe_trigger_trailing_exit(
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
                fill_confirmed = self._has_recent_fill_for_client_id(
                    symbol=symbol,
                    client_id=filled_id,
                )
                if position_is_flat or fill_confirmed:
                    try:
                        _ = _run_async_blocking(
                            lambda s=symbol, cid=filled_id: self._bracket_service.on_leg_filled(
                                symbol=s,
                                filled_client_algo_id=cid,
                            ),
                            timeout_sec=8.0,
                        )
                        if outcome == "TP" and isinstance(management_plan, dict):
                            if handle_take_profit_rearm(
                                self,
                                symbol=symbol,
                                filled_client_id=filled_id,
                                management_plan=dict(management_plan),
                            ):
                                next_management_state[symbol] = self._load_position_management_state().get(
                                    symbol,
                                    dict(management_plan),
                                )
                                continue

                        next_management_state.pop(symbol, None)
                        self._emit_bracket_exit_alert(symbol=symbol, outcome=outcome)
                        if outcome == "SL":
                            self._maybe_trigger_symbol_sl_flatten(trigger_symbol=symbol)
                    except Exception:  # noqa: BLE001
                        logger.exception("bracket_on_leg_filled_failed symbol=%s", symbol)
                    continue

                if (
                    position_is_open
                    and isinstance(position_row, dict)
                    and isinstance(management_plan, dict)
                    and self._repair_position_bracket_from_plan(
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
                and (
                    not tp_id
                    or not sl_id
                    or not tp_open
                    or not sl_open
                )
            ):
                if self._repair_position_bracket_from_plan(
                    symbol=symbol,
                    position_row=position_row,
                    plan=management_plan,
                    reason="missing_bracket_repair",
                ):
                    continue

            if position_is_flat:
                next_management_state.pop(symbol, None)
                if tp_id or sl_id:
                    inferred_exit = self._infer_flat_bracket_exit(
                        symbol=symbol,
                        tp_id=tp_id,
                        sl_id=sl_id,
                    )
                    if inferred_exit is not None:
                        outcome, filled_id = inferred_exit
                        try:
                            if filled_id:
                                _ = _run_async_blocking(
                                    lambda s=symbol, cid=filled_id: self._bracket_service.on_leg_filled(
                                        symbol=s,
                                        filled_client_algo_id=cid,
                                    ),
                                    timeout_sec=8.0,
                                )
                            else:
                                _ = _run_async_blocking(
                                    lambda s=symbol: self._bracket_service.cleanup_if_flat(
                                        symbol=s,
                                        position_amt=0.0,
                                    ),
                                    timeout_sec=8.0,
                                )
                            self._emit_bracket_exit_alert(symbol=symbol, outcome=outcome)
                            if outcome == "SL":
                                self._maybe_trigger_symbol_sl_flatten(trigger_symbol=symbol)
                        except Exception:  # noqa: BLE001
                            logger.exception("flat_bracket_exit_recovery_failed symbol=%s", symbol)
                        continue
                    try:
                        _ = _run_async_blocking(
                            lambda s=symbol: self._bracket_service.cleanup_if_flat(
                                symbol=s,
                                position_amt=0.0,
                            ),
                            timeout_sec=8.0,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("bracket_cleanup_if_flat_failed symbol=%s", symbol)
                continue

        self._save_position_management_state(next_management_state)

    def _start_bracket_loop(self) -> None:
        if self.cfg.mode != "live" or self.rest_client is None:
            return
        if self._bracket_thread is not None and self._bracket_thread.is_alive():
            return

        def _worker() -> None:
            while not self._bracket_thread_stop.is_set():
                interval = max(
                    5.0, _to_float(self._risk.get("watchdog_interval_sec"), default=15.0)
                )
                if self._bracket_thread_stop.wait(timeout=interval):
                    break
                self._poll_brackets_once()

        self._bracket_thread = threading.Thread(target=_worker, daemon=True)
        self._bracket_thread.start()

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
        live_positions, live_rows, live_ok, _live_error = self._fetch_live_positions()
        if live_ok:
            out_live: list[tuple[str, float, float, float]] = []
            for symbol, row in sorted(live_rows.items()):
                if not isinstance(row, dict):
                    continue
                position_amt = _to_float(row.get("positionAmt"), default=0.0)
                if abs(position_amt) <= 0.0:
                    continue
                entry_price = _to_float(row.get("entryPrice"), default=0.0)
                unrealized = _to_float(
                    row.get("unRealizedProfit") or row.get("unrealizedProfit"),
                    default=0.0,
                )
                out_live.append((symbol, position_amt, unrealized, entry_price))
            if out_live:
                return out_live
            if any(abs(_to_float(v, default=0.0)) > 0.0 for v in live_positions.values()):
                return [
                    (symbol, _to_float(amount, default=0.0), 0.0, 0.0)
                    for symbol, amount in sorted(live_positions.items())
                    if abs(_to_float(amount, default=0.0)) > 0.0
                ]
            return []

        state = self.state_store.get()
        out_state: list[tuple[str, float, float, float]] = []
        for symbol, row in sorted(state.current_position.items()):
            position_amt = _to_float(row.position_amt, default=0.0)
            if abs(position_amt) <= 0.0:
                continue
            pnl = _to_float(row.unrealized_pnl, default=0.0)
            entry_price = _to_float(row.entry_price, default=0.0)
            out_state.append((symbol, position_amt, pnl, entry_price))
        return out_state

    def _status_pnl_summary(self) -> tuple[str, str]:
        return build_status_pnl_summary(
            positions=self._status_positions_source(),
            fills=self.state_store.get().last_fills,
        )

    @staticmethod
    def _translate_status_token(raw: str, labels: dict[str, str]) -> str:
        return translate_status_token(raw, labels)

    def _emit_status_update(self, *, force: bool = False) -> bool:
        if not self.notifier.supports_periodic_status():
            return False
        notify_interval = max(
            1, int(_to_float(self._risk.get("notify_interval_sec"), default=30.0))
        )
        now = datetime.now(timezone.utc)
        should_notify = force or (
            self._last_status_notify_at is None
            or (now - self._last_status_notify_at).total_seconds() >= float(notify_interval)
        )
        if not should_notify:
            return False
        try:
            self.notifier.send(self._status_summary())
            self._last_status_notify_at = now
            return True
        except Exception:  # noqa: BLE001
            logger.exception("status_notify_failed")
            return False

    def _start_status_loop(self) -> None:
        if self._status_thread is not None and self._status_thread.is_alive():
            return

        def _worker() -> None:
            while not self._status_thread_stop.is_set():
                interval = max(
                    1, int(_to_float(self._risk.get("notify_interval_sec"), default=30.0))
                )
                if self._status_thread_stop.wait(timeout=float(interval)):
                    break
                self._emit_status_update(force=False)

        self._status_thread = threading.Thread(target=_worker, daemon=True)
        self._status_thread.start()

    def _loop_worker(self) -> None:
        while not self._thread_stop.is_set():
            with self._lock:
                if not self._running:
                    break
                self._run_cycle_once_locked(trigger_source="scheduler")
            self._thread_stop.wait(timeout=max(0.2, float(self.scheduler.tick_seconds)))

    def _active_worker_thread_locked(self) -> threading.Thread | None:
        thread = self._thread
        if thread is not None and not thread.is_alive():
            self._thread = None
            return None
        return thread

    def _join_worker_thread(self, thread: threading.Thread | None, *, timeout_sec: float = 2.0) -> None:
        if thread is None or thread is threading.current_thread():
            return
        if thread.is_alive():
            thread.join(timeout=timeout_sec)
        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def start(self) -> dict[str, Any]:
        thread_to_start: threading.Thread | None = None
        resumed_running_gate = False
        with self._lock:
            self._auto_reconcile_if_recovery_required(reason="operator_start")
            active_thread = self._active_worker_thread_locked()
            if self._running and active_thread is not None:
                ops_state = self.state_store.get().operational
                if bool(ops_state.paused) or bool(ops_state.safe_mode):
                    self.ops.resume()
                    resumed_running_gate = True
                    if self._risk.get("last_block_reason") in {"ops_paused", "safe_mode"}:
                        self._risk["last_block_reason"] = None
                state = capture_runtime_state(self.state_store)
                result = build_state_response(runtime_state=state)
                self._emit_status_update(force=resumed_running_gate)
                return result
            self.state_store.set(status="RUNNING")
            self.ops.resume()
            if self._risk.get("last_block_reason") in {"ops_paused", "safe_mode"}:
                self._risk["last_block_reason"] = None
            self._running = True
            self._thread_stop.clear()
            thread_to_start = threading.Thread(target=self._loop_worker, daemon=True)
            self._thread = thread_to_start
            self._log_event("runtime_start", running=True)
            state = capture_runtime_state(self.state_store)
            result = build_state_response(runtime_state=state)
        assert thread_to_start is not None
        thread_to_start.start()
        return result

    def stop(self, *, emit_event: bool = True) -> dict[str, Any]:
        thread_to_join: threading.Thread | None = None
        with self._lock:
            self._running = False
            self._thread_stop.set()
            self.ops.pause()
            self.state_store.set(status="PAUSED")
            thread_to_join = self._thread
            if emit_event:
                self._log_event("runtime_stop", running=False)
            state = capture_runtime_state(self.state_store)
            result = build_state_response(runtime_state=state)
        self._join_worker_thread(thread_to_join)
        self._emit_status_update(force=True)
        return result

    async def panic(self) -> dict[str, Any]:
        self.stop(emit_event=False)
        self.ops.safe_mode()
        self.state_store.set(status="KILLED")
        self._log_event("panic_triggered", action="panic")
        self._emit_status_update(force=True)
        result = await self.ops.flatten(symbol=self.cfg.behavior.exchange.default_symbol)
        self._log_event(
            "flatten_requested",
            action="panic_flatten",
            symbol=result.symbol,
        )
        _ = self.notifier.send_notification(
            build_position_close_notification(
                symbol=result.symbol,
                reason="panic_close",
                context=self._notification_context(),
            )
        )
        state = capture_runtime_state(self.state_store)
        self._report_stats["closes"] += 1
        return build_panic_response(
            runtime_state=state,
            flatten_result=result,
        )

    def get_risk(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_runtime_risk_context()
            self._sync_kernel_runtime_overrides()
            return build_risk_response(capture_public_risk_config(self))

    def set_value(self, *, key: str, value: str) -> dict[str, Any]:
        with self._lock:
            normalized_key = _normalize_runtime_risk_key(key)
            parsed = _parse_value(value)
            if normalized_key == "universe_symbols":
                if isinstance(parsed, str):
                    parsed = [item.strip().upper() for item in parsed.split(",") if item.strip()]
                elif isinstance(parsed, list):
                    parsed = [str(item).strip().upper() for item in parsed if str(item).strip()]
                else:
                    parsed = [self.cfg.behavior.exchange.default_symbol]
            if normalized_key in {"notify_interval_sec", "scheduler_tick_sec"}:
                parsed = max(1, int(_to_float(parsed, default=1.0)))

            self._risk[normalized_key] = parsed
            if normalized_key == "scheduler_tick_sec":
                self._apply_scheduler_tick_change(sec=int(_to_float(parsed, default=1.0)))
            else:
                self._risk[normalized_key] = parsed
            self._refresh_runtime_risk_context()
            self._sync_kernel_runtime_overrides()

            self._persist_risk_config()
            if normalized_key == "notify_interval_sec":
                self._emit_status_update(force=True)
            return build_set_value_response(
                key=normalized_key,
                requested_value=value,
                applied_value=self._risk.get(normalized_key),
                risk_config=capture_public_risk_config(self),
            )

    def set_symbol_leverage(self, *, symbol: str, leverage: float) -> dict[str, Any]:
        with self._lock:
            symbol_u = symbol.strip().upper()
            mapping = self._risk.get("symbol_leverage_map")
            if not isinstance(mapping, dict):
                mapping = {}
            if leverage <= 0:
                mapping.pop(symbol_u, None)
            else:
                leverage_f = float(leverage)
                mapping[symbol_u] = leverage_f
                current_max = max(
                    1.0,
                    _to_float(
                        self._risk.get("max_leverage"),
                        default=float(self.cfg.behavior.risk.max_leverage),
                    ),
                )
                # Treat an explicit symbol leverage override as operator intent.
                # Lift the runtime max so the requested leverage is not silently capped.
                if leverage_f > current_max:
                    self._risk["max_leverage"] = leverage_f
            self._risk["symbol_leverage_map"] = mapping
            self._refresh_runtime_risk_context()
            self._sync_kernel_runtime_overrides()
            self._persist_risk_config()
            return build_risk_response(capture_public_risk_config(self))

    def get_scheduler(self) -> dict[str, Any]:
        return build_scheduler_response(
            tick_sec=float(self.scheduler.tick_seconds),
            running=bool(self._running),
        )

    async def _handle_user_stream_event(self, event: dict[str, Any]) -> None:
        self.state_store.apply_exchange_event(
            event=event,
            reason="user_stream_event",
        )
        _ = maybe_handle_fill_event_fast_path(self, event=event)
        self._user_stream_last_event_at = _utcnow_iso()
        self._last_private_stream_ok_at = self._user_stream_last_event_at
        self._update_stale_transitions()

    async def _handle_user_stream_resync(self, snapshot: ResyncSnapshot) -> None:
        try:
            self._apply_resync_snapshot(
                snapshot=snapshot,
                reason="user_stream_resync",
            )
            self._last_private_stream_ok_at = _utcnow_iso()
            self._user_stream_last_error = None
            self._log_event("user_stream_resync", ok=True)
        except Exception as exc:  # noqa: BLE001
            self._user_stream_last_error = f"resync_failed:{type(exc).__name__}"
            self._set_state_uncertain(reason=self._user_stream_last_error, engage_safe_mode=True)
            self._log_event("user_stream_resync", ok=False, reason=self._user_stream_last_error)
            raise

    async def _handle_user_stream_disconnect(self, reason: str) -> None:
        self._user_stream_last_disconnect_at = _utcnow_iso()
        self._user_stream_last_error = str(reason or "user_stream_disconnected")
        self._set_state_uncertain(reason=self._user_stream_last_error, engage_safe_mode=False)
        self._log_event("user_stream_disconnect", reason=self._user_stream_last_error)

    async def _handle_user_stream_private_ok(self, source: str) -> None:
        self._last_private_stream_ok_at = _utcnow_iso()
        self._update_stale_transitions()
        freshness = self._freshness_snapshot()
        if self._should_clear_uncertainty_on_private_ok() and not bool(freshness["user_ws_stale"]):
            self._user_stream_last_error = None
            self._clear_state_uncertain()
        if str(source or "") != "ws_alive":
            self._log_event("user_stream_private_ok", source=source)

    async def start_live_services(self) -> None:
        if self.cfg.mode != "live" or self.user_stream_manager is None or self._user_stream_started:
            return
        self._user_stream_started = True
        self._user_stream_started_at = _utcnow_iso()
        self.user_stream_manager.start(
            on_event=self._handle_user_stream_event,
            on_resync=self._handle_user_stream_resync,
            on_disconnect=self._handle_user_stream_disconnect,
            on_private_ok=self._handle_user_stream_private_ok,
        )
        self._maybe_probe_market_data()
        self._update_stale_transitions()

    async def stop_live_services(self) -> None:
        if self.user_stream_manager is not None and self._user_stream_started:
            await self.user_stream_manager.stop()
            self._user_stream_started = False
        self.state_store.runtime_storage().save_runtime_marker(
            marker_key="shutdown_state",
            payload={
                "profile": self.cfg.profile,
                "mode": self.cfg.mode,
                "env": self.cfg.env,
                "clean_shutdown": True,
                "shutdown_at": _utcnow_iso(),
                "engine_state": self.state_store.get().status,
                "last_reconcile_at": self.state_store.get().last_reconcile_at,
                "readyz": self._readyz_snapshot(),
            },
        )
        self._log_event("runtime_shutdown", clean_shutdown=True)

    async def reconcile_now(self) -> dict[str, Any]:
        with self._lock:
            self._perform_startup_reconcile(reason="manual_reconcile")
            if not self._state_uncertain:
                self._recover_brackets_on_boot(reason="manual_reconcile")
            state = capture_runtime_state(self.state_store)
            return build_reconcile_response(
                state_uncertain=bool(self._state_uncertain),
                state_uncertain_reason=self._state_uncertain_reason,
                startup_reconcile_ok=bool(self._startup_reconcile_ok),
                last_reconcile_at=state.last_reconcile_at,
            )

    def set_scheduler_interval(self, tick_sec: float) -> dict[str, Any]:
        with self._lock:
            sec = max(1, int(tick_sec))
            self._apply_scheduler_tick_change(sec=sec)
            self._persist_risk_config()
            return build_scheduler_response(
                tick_sec=float(self.scheduler.tick_seconds),
                running=bool(self._running),
            )

    def tick_scheduler_now(self) -> dict[str, Any]:
        if self._lock.acquire(blocking=False):
            try:
                self._auto_reconcile_if_recovery_required(reason="operator_tick")
                return self._run_cycle_once_locked(trigger_source="manual_tick")
            finally:
                self._lock.release()

        if self._running:
            deadline = time.monotonic() + 7.0
            target_seq = max(1, int(self._cycle_seq))
            while time.monotonic() < deadline:
                if int(self._cycle_done_seq) >= target_seq:
                    snapshot = capture_last_cycle_snapshot(self._last_cycle, coalesced=True)
                    return build_tick_scheduler_response(
                        ok=True,
                        tick_sec=float(self.scheduler.tick_seconds),
                        snapshot=snapshot,
                        error=None,
                    )
                if self._lock.acquire(timeout=0.1):
                    try:
                        return self._run_cycle_once_locked(trigger_source="manual_tick")
                    finally:
                        self._lock.release()
                time.sleep(0.1)

            snapshot = apply_tick_busy_cycle_state(self._last_cycle, finished_at=_utcnow_iso())
            return build_tick_scheduler_response(
                ok=False,
                tick_sec=float(self.scheduler.tick_seconds),
                snapshot=snapshot,
                error="tick_busy",
            )

        if self._lock.acquire(timeout=6.0):
            try:
                return self._run_cycle_once_locked(trigger_source="manual_tick")
            finally:
                self._lock.release()
        snapshot = apply_tick_busy_cycle_state(self._last_cycle, finished_at=_utcnow_iso())
        return build_tick_scheduler_response(
            ok=False,
            tick_sec=float(self.scheduler.tick_seconds),
            snapshot=snapshot,
            error="tick_busy",
        )

    @staticmethod
    def _format_daily_report_message(payload: dict[str, Any]) -> str:
        return build_daily_report_message(payload)

    def send_daily_report(self) -> dict[str, Any]:
        payload = build_daily_report_payload(
            day=datetime.now(timezone.utc).date().isoformat(),
            engine_state=capture_runtime_state(self.state_store).status,
            detail=dict(self._report_stats),
            notifier_enabled=bool(self.notifier.enabled),
            reported_at=_utcnow_iso(),
        )
        message = self._format_daily_report_message(payload)
        result = self.notifier.send_notification(
            build_report_notification(
                payload=payload,
                context=self._notification_context(),
            )
        )
        payload["notifier_sent"] = bool(result.sent)
        if result.error and result.error != "disabled":
            payload["notifier_error"] = result.error
        payload["summary"] = message
        self._log_event(
            "report_sent",
            status="sent" if bool(payload["notifier_sent"]) else "not_sent",
            notifier_error=payload.get("notifier_error"),
            event_time=payload.get("reported_at"),
        )
        self._last_report = {
            "reported_at": payload.get("reported_at"),
            "kind": payload.get("kind"),
            "day": payload.get("day"),
            "status": "success" if payload.get("notifier_error") in {None, "disabled"} else "failed",
            "notifier_enabled": bool(payload.get("notifier_enabled")),
            "notifier_sent": bool(payload.get("notifier_sent")),
            "notifier_error": payload.get("notifier_error"),
            "summary": message,
            "detail": dict(payload.get("detail") or {}),
        }
        return payload

    def preset(self, name: str) -> dict[str, Any]:
        with self._lock:
            profile = str(name).strip().lower()
            if profile == "conservative":
                self._risk["max_leverage"] = 5.0
                self._risk["risk_per_trade_pct"] = 5.0
            elif profile == "normal":
                self._risk["max_leverage"] = 10.0
                self._risk["risk_per_trade_pct"] = 10.0
            elif profile == "aggressive":
                self._risk["max_leverage"] = 20.0
                self._risk["risk_per_trade_pct"] = 20.0
            self._sync_kernel_runtime_overrides()
            self._persist_risk_config()
            return build_risk_response(capture_public_risk_config(self))

    async def close_position(
        self,
        *,
        symbol: str,
        notify_reason: str = "forced_close",
    ) -> dict[str, Any]:
        position_state = self.state_store.get().current_position.get(symbol.upper())
        close_qty = abs(_to_float(getattr(position_state, "position_amt", 0.0), default=0.0))
        self._log_event("flatten_requested", action="close_position", symbol=symbol.upper())
        result = await self.ops.flatten(symbol=symbol, latch_ops_mode=False)
        _ = self.notifier.send_notification(
            build_position_close_notification(
                symbol=result.symbol,
                reason=notify_reason,
                context=self._notification_context(),
            )
        )
        self._log_event(
            "position_closed",
            symbol=result.symbol,
            reason=notify_reason,
            closed_qty=round(float(close_qty), 8),
            realized_pnl=self._resolve_symbol_realized_pnl(symbol=result.symbol),
            event_time=_utcnow_iso(),
        )
        self._report_stats["closes"] += 1
        return build_trade_close_response(flatten_result=result)

    async def close_all(self, *, notify_reason: str = "forced_close") -> dict[str, Any]:
        self._log_event("flatten_requested", action="close_all")
        symbols: set[str] = set()
        live_positions, _live_rows, live_ok, _live_error = self._fetch_live_positions()
        if live_ok:
            for sym, position_amt in live_positions.items():
                if abs(_to_float(position_amt, default=0.0)) <= 0.0:
                    continue
                symbols.add(str(sym).strip().upper())
        else:
            symbols = set(
                self._risk.get("universe_symbols") or [self.cfg.behavior.exchange.default_symbol]
            )
            for sym in self.state_store.get().current_position.keys():
                symbols.add(sym)
        details: list[dict[str, Any]] = []
        for symbol in sorted({str(s).upper() for s in symbols if str(s).strip()}):
            details.append(await self.close_position(symbol=symbol, notify_reason=notify_reason))
        return build_trade_close_all_response(results=details)

    def clear_cooldown(self) -> dict[str, Any]:
        with self._lock:
            self._risk["daily_realized_pnl"] = 0.0
            self._risk["daily_realized_pct"] = 0.0
            self._risk["daily_loss_used_pct"] = 0.0
            self._risk["dd_used_pct"] = 0.0
            self._risk["lose_streak"] = 0
            self._risk["cooldown_until"] = None
            self._risk["daily_lock"] = False
            self._risk["dd_lock"] = False
            self._risk["runtime_equity_peak_usdt"] = float(
                self._risk.get("runtime_equity_now_usdt") or 0.0
            )
            self._risk["last_auto_risk_reason"] = None
            self._risk["last_auto_risk_at"] = None
            self._risk["last_block_reason"] = None
            self._risk["last_strategy_block_reason"] = None
            self._risk["last_alpha_id"] = None
            self._risk["last_entry_family"] = None
            self._risk["last_regime"] = None
            self._risk["last_alpha_blocks"] = {}
            self._risk["last_alpha_reject_focus"] = None
            self._risk["last_alpha_reject_metrics"] = {}
            self._risk["overheat_state"] = {"blocked": False, "reason": None}
            self._persist_risk_config()
            self._sync_kernel_runtime_overrides()
            return build_clear_cooldown_response(
                day=datetime.now(timezone.utc).date().isoformat(),
                risk_config=self._risk,
            )


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
