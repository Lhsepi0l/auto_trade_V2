from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from v2.clean_room.contracts import KernelCycleResult
from v2.common.async_bridge import run_async_blocking
from v2.config.loader import EffectiveConfig
from v2.core import EventBus, Scheduler
from v2.engine import EngineStateStore, OrderManager
from v2.exchange import BinanceRESTError
from v2.exchange.types import ResyncSnapshot
from v2.notify import Notifier
from v2.ops import OpsController
from v2.tpsl import BracketConfig, BracketPlanner, BracketService

logger = logging.getLogger(__name__)


_ACTION_LABELS_KO: dict[str, str] = {
    "blocked": "차단",
    "no_candidate": "대기",
    "risk_rejected": "리스크거부",
    "size_invalid": "수량오류",
    "executed": "실행완료",
    "dry_run": "모의실행",
    "execution_failed": "실행실패",
    "error": "오류",
    "hold": "대기",
    "enter": "진입",
    "close": "청산",
}


_REASON_LABELS_KO: dict[str, str] = {
    "ops_paused": "운영 일시정지",
    "safe_mode": "안전모드",
    "position_open": "기존 포지션 보유중",
    "portfolio_symbol_open": "포트폴리오 동일 심볼 보유중",
    "portfolio_bucket_cap": "포트폴리오 버킷 한도 도달",
    "portfolio_cap_reached": "포트폴리오 최대 포지션 도달",
    "no_candidate": "현재 진입 후보가 없습니다",
    "no_candidate_multi": "복수 전략 후보가 정합성에서 탈락",
    "invalid_size": "유효하지 않은 주문 수량",
    "would_execute": "모의모드에서 실행 가능",
    "executed": "주문 실행 완료",
    "execution_failed": "주문 실행 실패",
    "risk_rejected": "리스크 검증에서 거부됨",
    "size_invalid": "수량 검증 실패",
    "tick_busy": "이미 판단 작업이 진행중",
    "cycle_failed": "사이클 실행 실패",
    "live_order_failed": "실주문 제출 실패",
    "bracket_failed": "TP/SL 브래킷 주문 실패",
    "no_entry": "진입 조건 미충족",
    "cooldown_active": "쿨다운 중",
    "daily_loss_limit": "일일 손실 제한 도달",
    "drawdown_limit": "드로우다운 제한 도달",
    "spread_block": "스프레드 과대",
    "edge_below_cost": "기대 수익 대비 비용 우위 부족",
    "confidence_below_threshold": "최소 신호 점수 미달",
    "gap_below_threshold": "신호 격차 미달",
    "risk_ok_scaled": "리스크 감속 적용",
    "signal_conflict": "전략 간 방향 충돌",
    "regime_conflict": "전략 간 레짐 충돌",
    "network_error": "네트워크 오류",
    "state_uncertain": "거래소 상태 정합성 불확실",
    "user_ws_stale": "프라이빗 스트림 freshness 초과",
    "market_data_stale": "마켓 데이터 freshness 초과",
    "recovery_required": "더티 재시작 복구 필요",
    "submit_recovery_required": "주문 제출 확정 복구 필요",
}


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
        self.user_stream_manager = user_stream_manager
        self._market_data_state = market_data_state if market_data_state is not None else {}
        self._runtime_lock_active = bool(runtime_lock_active)
        self._dirty_restart_detected = bool(dirty_restart_detected)
        if (not self.notifier.enabled) and str(self.notifier.webhook_url or "").strip():
            self.notifier.enabled = True
        if (
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
        self._maybe_log_ready_transition()
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

    def _log_event(self, event: str, **fields: Any) -> None:
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

    def _freshness_snapshot(self) -> dict[str, Any]:
        user_ws_stale_sec = max(
            10.0,
            _to_float(
                self._risk.get("user_ws_stale_sec"),
                default=max(float(self.scheduler.tick_seconds) * 4.0, 60.0),
            ),
        )
        market_data_stale_sec = max(
            5.0,
            _to_float(
                self._risk.get("market_data_stale_sec"),
                default=max(float(self.scheduler.tick_seconds) * 2.0, 30.0),
            ),
        )
        reconcile_max_age_sec = max(
            30.0,
            _to_float(self._risk.get("reconcile_max_age_sec"), default=300.0),
        )
        last_user_ws_event_at = self._user_stream_last_event_at
        last_private_stream_ok_at = self._last_private_stream_ok_at
        last_market_data_at = self._market_data_state.get("last_market_data_at")
        last_market_data_source_ok_at = self._market_data_state.get("last_market_data_source_ok_at")
        last_market_data_source_fail_at = self._market_data_state.get("last_market_data_source_fail_at")
        last_market_data_source_error = self._market_data_state.get("last_market_data_source_error")
        last_reconcile_at = self.state_store.get().last_reconcile_at
        user_ws_age_sec = _age_seconds(last_private_stream_ok_at)
        market_data_age_sec = _age_seconds(last_market_data_at)
        market_data_source_age_sec = _age_seconds(last_market_data_source_ok_at)
        reconcile_age_sec = _age_seconds(last_reconcile_at)

        if user_ws_age_sec is None and self._user_stream_started:
            user_ws_age_sec = _age_seconds(self._user_stream_started_at)

        user_ws_stale = (
            self.cfg.mode == "live"
            and self._user_stream_started
            and user_ws_age_sec is not None
            and user_ws_age_sec > user_ws_stale_sec
        )
        market_data_observer_stale = (
            self.cfg.mode == "live"
            and market_data_age_sec is not None
            and market_data_age_sec > market_data_stale_sec
        )
        market_data_source_stale = (
            self.cfg.mode == "live"
            and market_data_source_age_sec is not None
            and market_data_source_age_sec > market_data_stale_sec
        )
        market_data_stale = market_data_observer_stale or market_data_source_stale
        return {
            "last_user_ws_event_at": last_user_ws_event_at,
            "last_private_stream_ok_at": last_private_stream_ok_at,
            "last_market_data_at": last_market_data_at,
            "last_market_data_source_ok_at": last_market_data_source_ok_at,
            "last_market_data_source_fail_at": last_market_data_source_fail_at,
            "last_market_data_source_error": last_market_data_source_error,
            "last_reconcile_at": last_reconcile_at,
            "user_ws_age_sec": user_ws_age_sec,
            "market_data_age_sec": market_data_age_sec,
            "market_data_source_age_sec": market_data_source_age_sec,
            "reconcile_age_sec": reconcile_age_sec,
            "user_ws_stale_sec": user_ws_stale_sec,
            "market_data_stale_sec": market_data_stale_sec,
            "reconcile_max_age_sec": reconcile_max_age_sec,
            "user_ws_stale": user_ws_stale,
            "market_data_stale": market_data_stale,
            "market_data_observer_stale": market_data_observer_stale,
            "market_data_source_stale": market_data_source_stale,
            "market_data_seen": last_market_data_at is not None,
            "private_stream_seen": last_private_stream_ok_at is not None,
        }

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
        storage = self.state_store.runtime_storage()
        rows = storage.list_submission_intents(statuses=["PENDING", "SUBMIT_ERROR", "REVIEW_REQUIRED"])
        open_orders_by_client: dict[str, dict[str, Any]] = {}
        for row in snapshot.open_orders:
            if not isinstance(row, dict):
                continue
            client_order_id = str(row.get("clientOrderId") or row.get("c") or "").strip()
            if client_order_id:
                open_orders_by_client[client_order_id] = row
        live_position_symbols = {
            str(item.get("symbol") or item.get("s") or "").strip().upper()
            for item in snapshot.positions
            if isinstance(item, dict)
            and abs(_to_float(item.get("positionAmt") if item.get("positionAmt") is not None else item.get("pa"), default=0.0)) > 0.0
        }

        resolved_open = 0
        resolved_position = 0
        resolved_not_found = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            intent_id = str(row.get("intent_id") or "").strip()
            client_order_id = str(row.get("client_order_id") or "").strip()
            symbol = str(row.get("symbol") or "").strip().upper()
            if not intent_id or not client_order_id:
                continue
            open_row = open_orders_by_client.get(client_order_id)
            if open_row is not None:
                storage.mark_submission_intent_status(
                    intent_id=intent_id,
                    status="SUBMITTED",
                    order_id=str(open_row.get("orderId") or ""),
                )
                resolved_open += 1
                continue
            if symbol and symbol in live_position_symbols:
                storage.mark_submission_intent_status(
                    intent_id=intent_id,
                    status="RECOVERED_POSITION_OPEN",
                )
                resolved_position += 1
                continue
            storage.mark_submission_intent_status(
                intent_id=intent_id,
                status="NOT_FOUND_AFTER_RECONCILE",
            )
            resolved_not_found += 1

        summary = {
            "reason": reason,
            "resolved_open_orders": resolved_open,
            "resolved_positions": resolved_position,
            "resolved_not_found": resolved_not_found,
            "pending_before": len(rows),
            "pending_after": self._submission_recovery_snapshot()["pending_review_count"],
        }
        self._boot_recovery["submission_recovery"] = summary
        self._log_event("submission_recovery_sync", **summary)
        return summary

    def _gate_snapshot(self) -> dict[str, Any]:
        freshness = self._freshness_snapshot()
        submission_recovery = self._submission_recovery_snapshot()
        ops_state = self.state_store.get().operational
        last_reconcile_at = freshness["last_reconcile_at"]
        last_reconcile_ok = (
            self.cfg.mode != "live"
            or (
                self._startup_reconcile_ok is True
                and last_reconcile_at is not None
                and freshness["reconcile_age_sec"] is not None
                and freshness["reconcile_age_sec"] <= freshness["reconcile_max_age_sec"]
            )
        )
        user_ws_ok = (
            self.cfg.mode != "live"
            or (
                self._user_stream_started
                and freshness["private_stream_seen"]
                and not freshness["user_ws_stale"]
            )
        )
        market_data_ok = (
            self.cfg.mode != "live"
            or (freshness["market_data_seen"] and not freshness["market_data_stale"])
        )
        private_auth_ok = self.cfg.mode != "live" or (
            self.rest_client is not None
            and str(self._last_balance_error or "") not in {"balance_auth_failed"}
        )
        ready = all(
            [
                bool(self._runtime_lock_active or self.cfg.mode != "live"),
                not self._state_uncertain,
                not self._recovery_required,
                bool(submission_recovery["ok"]),
                last_reconcile_ok,
                user_ws_ok,
                market_data_ok,
                not bool(ops_state.paused),
                not bool(ops_state.safe_mode),
                private_auth_ok,
            ]
        )
        return {
            "ready": ready,
            "single_instance_ok": bool(self._runtime_lock_active or self.cfg.mode != "live"),
            "state_uncertain": bool(self._state_uncertain),
            "recovery_required": bool(self._recovery_required),
            "startup_reconcile_ok": self._startup_reconcile_ok,
            "submission_recovery_ok": bool(submission_recovery["ok"]),
            "last_reconcile_ok": last_reconcile_ok,
            "user_ws_ok": user_ws_ok,
            "market_data_ok": market_data_ok,
            "private_auth_ok": private_auth_ok,
            "paused": bool(ops_state.paused),
            "safe_mode": bool(ops_state.safe_mode),
            "freshness": freshness,
            "submission_recovery": submission_recovery,
        }

    def _maybe_log_ready_transition(self) -> None:
        gate = self._gate_snapshot()
        ready = bool(gate["ready"])
        if self._last_ready_state is None or self._last_ready_state != ready:
            self._last_ready_state = ready
            self._log_event(
                "ready_transition",
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
            self._recovery_required = False
            self._recovery_reason = None
            self._log_event("recovery_cleared", reason=reason)
        self._log_event("reconcile_success", reason=reason)
        self._update_stale_transitions()

    def _perform_startup_reconcile(self, *, reason: str) -> None:
        if self.cfg.mode != "live":
            self._startup_reconcile_ok = None
            return
        self._log_event("reconcile_start", reason=reason)
        try:
            snapshot = self._fetch_exchange_snapshot()
            self._apply_resync_snapshot(snapshot=snapshot, reason=reason)
        except FutureTimeoutError:
            self._startup_reconcile_ok = False
            self._set_state_uncertain(reason=f"{reason}:timeout", engage_safe_mode=True)
            self._log_event("reconcile_failure", reason=f"{reason}:timeout")
            logger.warning("startup_reconcile_timed_out")
        except Exception as exc:  # noqa: BLE001
            self._startup_reconcile_ok = False
            self._set_state_uncertain(
                reason=f"{reason}:{type(exc).__name__}",
                engage_safe_mode=True,
            )
            self._log_event("reconcile_failure", reason=f"{reason}:{type(exc).__name__}")
            logger.exception("startup_reconcile_failed")

    def _auto_reconcile_if_recovery_required(self, *, reason: str) -> bool:
        if not self._recovery_required:
            return True
        if self.cfg.mode != "live":
            self._recovery_required = False
            self._recovery_reason = None
            return True
        self._log_event("auto_reconcile_attempt", reason=reason)
        self._perform_startup_reconcile(reason="manual_reconcile")
        if not self._state_uncertain:
            self._recover_brackets_on_boot(reason="manual_reconcile")
        return not self._recovery_required

    def _recover_brackets_on_boot(self, *, reason: str = "startup_reconcile") -> None:
        if self.cfg.mode != "live" or self.rest_client is None:
            return
        self._log_event("bracket_recovery_start", reason=reason)
        try:
            result = _run_async_blocking(
                lambda: self._bracket_service.recover(),
                timeout_sec=10.0,
            )
            self._boot_recovery["bracket_recovery"] = {
                "reason": reason,
                "ok": True,
                "result": result if isinstance(result, dict) else {},
            }
            self._log_event("bracket_recovery_success", reason=reason)
        except FutureTimeoutError:
            self._boot_recovery["bracket_recovery"] = {
                "reason": reason,
                "ok": False,
                "error": "timeout",
            }
            self._log_event("bracket_recovery_failure", reason=f"{reason}:timeout")
            logger.warning("bracket_recover_timed_out")
        except Exception:  # noqa: BLE001
            self._boot_recovery["bracket_recovery"] = {
                "reason": reason,
                "ok": False,
                "error": "exception",
            }
            self._log_event("bracket_recovery_failure", reason=f"{reason}:exception")
            logger.exception("bracket_recover_failed")

    def _load_persisted_risk_config(self) -> None:
        try:
            persisted = self.state_store.load_runtime_risk_config()
        except Exception:  # noqa: BLE001
            logger.exception("runtime_risk_config_load_failed")
            return
        if not isinstance(persisted, dict) or not persisted:
            return
        for key, value in persisted.items():
            self._risk[key] = value

    def _persist_risk_config(self) -> None:
        try:
            self.state_store.save_runtime_risk_config(config=dict(self._risk))
        except Exception:  # noqa: BLE001
            logger.exception("runtime_risk_config_save_failed")

    def _sync_kernel_runtime_overrides(self) -> None:
        symbols, mapping, effective_margin, effective_leverage, _expected_notional = (
            self._runtime_budget_context()
        )
        max_leverage = max(1.0, _to_float(self._risk.get("max_leverage"), default=1.0))

        fallback_notional = float(effective_margin) * float(effective_leverage)
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
        if hasattr(self.kernel, "set_strategy_runtime_params"):
            self.kernel.set_strategy_runtime_params(  # type: ignore[attr-defined]
                trend_enter_adx_4h=_to_float(self._risk.get("trend_enter_adx_4h"), default=22.0),
                trend_exit_adx_4h=_to_float(self._risk.get("trend_exit_adx_4h"), default=18.0),
                regime_hold_bars_4h=max(
                    1,
                    int(_to_float(self._risk.get("regime_hold_bars_4h"), default=2.0)),
                ),
                breakout_buffer_bps=_to_float(self._risk.get("breakout_buffer_bps"), default=8.0),
                breakout_bar_size_atr_max=_to_float(
                    self._risk.get("breakout_bar_size_atr_max"),
                    default=1.6,
                ),
                min_volume_ratio_15m=_to_float(self._risk.get("min_volume_ratio_15m"), default=1.2),
                range_enabled=_to_bool(self._risk.get("range_enabled"), default=False),
                overheat_funding_abs=_to_float(self._risk.get("overheat_funding_abs"), default=0.0008),
                overheat_long_short_ratio_cap=_to_float(
                    self._risk.get("overheat_long_short_ratio_cap"),
                    default=1.8,
                ),
                overheat_long_short_ratio_floor=_to_float(
                    self._risk.get("overheat_long_short_ratio_floor"),
                    default=0.56,
                ),
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

    def _initial_risk_config(self) -> dict[str, Any]:
        risk_cfg = self.cfg.behavior.risk
        tpsl_cfg = self.cfg.behavior.tpsl
        sched_sec = int(self.cfg.behavior.scheduler.tick_seconds)
        day_key = datetime.now(timezone.utc).date().isoformat()
        config = {
            "per_trade_risk_pct": 10.0,
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
            "user_ws_stale_sec": max(float(sched_sec) * 4.0, 60.0),
            "market_data_stale_sec": max(float(sched_sec) * 2.0, 30.0),
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
            "watchdog_interval_sec": sched_sec,
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
            "overheat_state": {"blocked": False, "reason": None},
        }
        config.update(self._profile_runtime_risk_overrides(sched_sec=sched_sec))
        return config

    def _profile_runtime_risk_overrides(self, *, sched_sec: int) -> dict[str, Any]:
        profile = str(self.cfg.profile or "").strip().lower()
        profile_overrides: dict[str, dict[str, Any]] = {
            "ra_2026_alpha_v2_expansion_live_candidate": {
                "risk_score_min": 0.60,
                "spread_max_pct": 0.35,
                "margin_use_pct": 0.10,
                "universe_symbols": ["BTCUSDT"],
                "enable_watchdog": True,
                "watchdog_interval_sec": max(5, min(int(sched_sec), 15)),
                "lose_streak_n": 2,
                "cooldown_hours": 4,
            },
        }
        return dict(profile_overrides.get(profile, {}))

    def _live_readiness_snapshot(
        self,
        *,
        live_balance_source: str | None = None,
        private_error: str | None = None,
    ) -> dict[str, Any]:
        gate = self._gate_snapshot()
        strategies = [
            str(entry.name).strip()
            for entry in self.cfg.behavior.strategies
            if bool(getattr(entry, "enabled", False))
        ]
        symbols = self._runtime_symbols()
        max_leverage = max(
            1.0,
            _to_float(self._risk.get("max_leverage"), default=float(self.cfg.behavior.risk.max_leverage)),
        )
        margin_use_pct = max(0.0, _to_float(self._risk.get("margin_use_pct"), default=1.0))
        daily_limit = _normalize_pct(self._risk.get("daily_loss_limit_pct"), default=0.02)
        dd_limit = _normalize_pct(self._risk.get("dd_limit_pct"), default=0.15)
        tick_sec = max(1.0, _to_float(self._risk.get("scheduler_tick_sec"), default=float(self.scheduler.tick_seconds)))
        profile_name = str(self.cfg.profile or "").strip()
        mode_name = str(self.cfg.mode or "").strip()
        checks: dict[str, dict[str, Any]] = {}
        readiness_specs: dict[str, dict[str, Any]] = {
            "ra_2026_alpha_v2_expansion_live_candidate": {
                "target": "alpha_expansion_live_candidate",
                "strategy": ["ra_2026_alpha_v2"],
                "symbols": ["BTCUSDT"],
                "margin_pass": 0.10,
                "margin_warn": 0.15,
                "leverage_pass": 5.0,
                "leverage_warn": 8.0,
                "daily_pass": 0.015,
                "daily_warn": 0.02,
                "dd_pass": 0.12,
                "dd_warn": 0.15,
            },
        }
        readiness_spec = readiness_specs.get(
            profile_name,
            readiness_specs["ra_2026_alpha_v2_expansion_live_candidate"],
        )

        def _set_check(name: str, *, status: str, detail: Any) -> None:
            checks[name] = {"status": status, "detail": detail}

        _set_check(
            "profile",
            status="pass" if profile_name in readiness_specs else "warn",
            detail=profile_name,
        )
        _set_check(
            "mode",
            status="pass" if mode_name == "live" else "warn",
            detail=mode_name,
        )
        _set_check(
            "single_instance",
            status="pass" if gate["single_instance_ok"] else "fail",
            detail=bool(gate["single_instance_ok"]),
        )
        _set_check(
            "dirty_recovery",
            status="pass" if not gate["recovery_required"] else "fail",
            detail={
                "dirty_restart_detected": bool(self._dirty_restart_detected),
                "recovery_required": bool(gate["recovery_required"]),
                "recovery_reason": self._recovery_reason,
            },
        )
        _set_check(
            "state_uncertain",
            status="pass" if not gate["state_uncertain"] else "fail",
            detail={
                "state_uncertain": bool(gate["state_uncertain"]),
                "reason": self._state_uncertain_reason,
            },
        )
        _set_check(
            "strategy",
            status="pass" if strategies == readiness_spec["strategy"] else "fail",
            detail=strategies,
        )
        _set_check(
            "symbols",
            status="pass" if symbols == readiness_spec["symbols"] else "warn",
            detail=symbols,
        )
        _set_check(
            "margin_use_pct",
            status="pass"
            if margin_use_pct <= float(readiness_spec["margin_pass"])
            else "warn"
            if margin_use_pct <= float(readiness_spec["margin_warn"])
            else "fail",
            detail=round(float(margin_use_pct) * 100.0, 2),
        )
        _set_check(
            "max_leverage",
            status="pass"
            if max_leverage <= float(readiness_spec["leverage_pass"])
            else "warn"
            if max_leverage <= float(readiness_spec["leverage_warn"])
            else "fail",
            detail=float(max_leverage),
        )
        _set_check(
            "daily_loss_limit_pct",
            status="pass"
            if 0.0 < daily_limit <= float(readiness_spec["daily_pass"])
            else "warn"
            if daily_limit <= float(readiness_spec["daily_warn"])
            else "fail",
            detail=round(float(daily_limit) * 100.0, 2),
        )
        _set_check(
            "dd_limit_pct",
            status="pass"
            if 0.0 < dd_limit <= float(readiness_spec["dd_pass"])
            else "warn"
            if dd_limit <= float(readiness_spec["dd_warn"])
            else "fail",
            detail=round(float(dd_limit) * 100.0, 2),
        )
        auto_risk_ok = _to_bool(self._risk.get("auto_risk_enabled"), default=True) and _to_bool(
            self._risk.get("auto_safe_mode_on_risk"),
            default=True,
        )
        if mode_name == "live":
            auto_risk_ok = auto_risk_ok and _to_bool(
                self._risk.get("auto_flatten_on_risk"),
                default=True,
            )
        _set_check(
            "auto_risk_circuit",
            status="pass" if auto_risk_ok else "fail",
            detail={
                "auto_risk_enabled": _to_bool(self._risk.get("auto_risk_enabled"), default=True),
                "auto_safe_mode_on_risk": _to_bool(
                    self._risk.get("auto_safe_mode_on_risk"),
                    default=True,
                ),
                "auto_flatten_on_risk": _to_bool(
                    self._risk.get("auto_flatten_on_risk"),
                    default=True,
                ),
            },
        )
        _set_check(
            "scheduler_tick_sec",
            status="pass" if 15.0 <= tick_sec <= 60.0 else "warn",
            detail=float(tick_sec),
        )
        _set_check(
            "startup_reconcile",
            status="pass" if gate["last_reconcile_ok"] else "fail",
            detail={
                "startup_reconcile_ok": self._startup_reconcile_ok,
                "last_reconcile_at": gate["freshness"]["last_reconcile_at"],
                "age_sec": gate["freshness"]["reconcile_age_sec"],
                "max_age_sec": gate["freshness"]["reconcile_max_age_sec"],
            },
        )
        _set_check(
            "submit_recovery",
            status="pass" if gate["submission_recovery_ok"] else "fail",
            detail=gate["submission_recovery"],
        )
        _set_check(
            "user_ws_freshness",
            status="pass" if gate["user_ws_ok"] else "fail",
            detail={
                "started": bool(self._user_stream_started),
                "last_event_at": gate["freshness"]["last_user_ws_event_at"],
                "last_private_ok_at": gate["freshness"]["last_private_stream_ok_at"],
                "age_sec": gate["freshness"]["user_ws_age_sec"],
                "stale": gate["freshness"]["user_ws_stale"],
                "stale_sec": gate["freshness"]["user_ws_stale_sec"],
            },
        )
        _set_check(
            "market_data_freshness",
            status="pass" if gate["market_data_ok"] else "fail",
            detail={
                "last_market_data_at": gate["freshness"]["last_market_data_at"],
                "last_market_data_source_ok_at": gate["freshness"]["last_market_data_source_ok_at"],
                "age_sec": gate["freshness"]["market_data_age_sec"],
                "source_age_sec": gate["freshness"]["market_data_source_age_sec"],
                "stale": gate["freshness"]["market_data_stale"],
                "observer_stale": gate["freshness"]["market_data_observer_stale"],
                "source_stale": gate["freshness"]["market_data_source_stale"],
                "source_error": gate["freshness"]["last_market_data_source_error"],
                "stale_sec": gate["freshness"]["market_data_stale_sec"],
            },
        )
        _set_check(
            "ops_mode",
            status="pass" if (not gate["paused"] and not gate["safe_mode"]) else "fail",
            detail={"paused": gate["paused"], "safe_mode": gate["safe_mode"]},
        )

        balance_status = "pass"
        balance_detail: Any = live_balance_source or "fallback"
        if mode_name != "live":
            balance_status = "warn"
        elif private_error:
            balance_status = "fail"
            balance_detail = private_error
        elif not gate["private_auth_ok"]:
            balance_status = "fail"
            balance_detail = self._last_balance_error or "private_auth_unavailable"
        _set_check(
            "exchange_private",
            status=balance_status,
            detail=balance_detail,
        )

        statuses = [row["status"] for row in checks.values()]
        overall = "ready"
        if "fail" in statuses:
            overall = "blocked"
        elif "warn" in statuses:
            overall = "caution"

        return {
            "target": str(readiness_spec["target"]),
            "overall": overall,
            "ready": bool(gate["ready"]),
            "profile": profile_name,
            "mode": mode_name,
            "enabled_symbols": symbols,
            "checks": checks,
        }

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

    def _runtime_budget_context(self) -> tuple[list[str], dict[str, float], float, float, float]:
        symbols = self._runtime_symbols()
        mapping = self._runtime_symbol_leverage_map()
        capital_mode = str(self._risk.get("capital_mode") or "").upper()
        margin_use_pct = max(0.0, _to_float(self._risk.get("margin_use_pct"), default=1.0))
        margin_budget = _to_float(self._risk.get("margin_budget_usdt"), default=100.0)
        fixed_budget = _to_float(self._risk.get("capital_usdt"), default=100.0)
        base_margin = fixed_budget if capital_mode == "FIXED_USDT" else margin_budget
        target_margin = float(base_margin) * float(margin_use_pct)
        if target_margin <= 0:
            target_margin = 10.0

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
        cached_balance = self._cached_live_balance(max_age_sec=300.0)
        if cached_balance is not None:
            _available, wallet = cached_balance
            if wallet is not None and float(wallet) > 0.0:
                capital_base = max(float(wallet), capital_base)

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
        self.notifier.send(f"자동 리스크 차단: {cycle.reason}")

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
                    lambda s=symbol: self.close_position(symbol=s),
                    timeout_sec=30.0,
                )
            except Exception:  # noqa: BLE001
                logger.exception("auto_risk_flatten_failed symbol=%s reason=%s", symbol, cycle.reason)

    def _status_snapshot(self) -> dict[str, Any]:
        state = self.state_store.get()
        freshness = self._freshness_snapshot()
        gate = self._gate_snapshot()
        self._update_stale_transitions()
        live_trading_enabled = self.cfg.mode == "live" and str(self.cfg.env) == "prod"
        positions_payload: dict[str, dict[str, Any]] = {}
        for symbol, position_amt, unrealized_pnl, entry_price in self._status_positions_source():
            positions_payload[symbol] = {
                "position_amt": position_amt,
                "entry_price": entry_price,
                "unrealized_pnl": unrealized_pnl,
                "position_side": "LONG" if position_amt > 0 else "SHORT",
            }
        symbols, _mapping, effective_margin, leverage, expected_notional = self._runtime_budget_context()
        live_available_usdt, live_wallet_usdt, live_balance_source = self._fetch_live_usdt_balance()
        available_usdt = (
            live_available_usdt if live_available_usdt is not None else effective_margin
        )
        wallet_usdt = live_wallet_usdt if live_wallet_usdt is not None else effective_margin
        config = dict(self._risk)
        config_summary = dict(self._risk)
        config_summary["scheduler_tick_sec"] = float(self.scheduler.tick_seconds)
        config_summary["scheduler_running"] = bool(self._running)
        config_summary["scheduler_enabled"] = bool(self._running)
        config_summary["active_strategy_timeframes"] = ["10m", "15m", "30m", "1h", "4h"]
        config_summary["strategy_runtime"] = {
            "trend_enter_adx_4h": self._risk.get("trend_enter_adx_4h", 22.0),
            "trend_exit_adx_4h": self._risk.get("trend_exit_adx_4h", 18.0),
            "regime_hold_bars_4h": self._risk.get("regime_hold_bars_4h", 2),
            "breakout_buffer_bps": self._risk.get("breakout_buffer_bps", 8.0),
            "breakout_bar_size_atr_max": self._risk.get("breakout_bar_size_atr_max", 1.6),
            "min_volume_ratio_15m": self._risk.get("min_volume_ratio_15m", 1.2),
            "range_enabled": self._risk.get("range_enabled", False),
            "overheat_funding_abs": self._risk.get("overheat_funding_abs", 0.0008),
            "overheat_long_short_ratio_cap": self._risk.get("overheat_long_short_ratio_cap", 1.8),
            "overheat_long_short_ratio_floor": self._risk.get("overheat_long_short_ratio_floor", 0.56),
        }
        config_summary["risk_runtime"] = {
            "daily_loss_used_pct": float(self._risk.get("daily_loss_used_pct") or 0.0),
            "dd_used_pct": float(self._risk.get("dd_used_pct") or 0.0),
            "lose_streak": int(self._risk.get("lose_streak") or 0),
            "cooldown_until": self._risk.get("cooldown_until"),
            "recent_blocks": dict(self._risk.get("recent_blocks") or {}),
            "last_auto_risk_reason": self._risk.get("last_auto_risk_reason"),
            "last_auto_risk_at": self._risk.get("last_auto_risk_at"),
            "last_strategy_block_reason": self._risk.get("last_strategy_block_reason"),
            "last_alpha_id": self._risk.get("last_alpha_id"),
            "last_entry_family": self._risk.get("last_entry_family"),
            "last_regime": self._risk.get("last_regime"),
            "overheat_state": dict(self._risk.get("overheat_state") or {}),
        }
        return {
            "profile": self.cfg.profile,
            "mode": self.cfg.mode,
            "env": self.cfg.env,
            "live_trading_enabled": bool(live_trading_enabled),
            "runtime_identity": {
                "profile": self.cfg.profile,
                "mode": self.cfg.mode,
                "env": self.cfg.env,
                "live_trading_enabled": bool(live_trading_enabled),
                "surface_label": "실거래 활성" if live_trading_enabled else "모의/테스트 또는 비실거래",
            },
            "dry_run": self.cfg.mode == "shadow",
            "dry_run_strict": False,
            "state_uncertain": bool(self._state_uncertain),
            "state_uncertain_reason": self._state_uncertain_reason,
            "startup_reconcile_ok": self._startup_reconcile_ok,
            "last_reconcile_at": state.last_reconcile_at,
            "last_shutdown_marker": self._last_shutdown_marker,
            "recovery_required": bool(self._recovery_required),
            "recovery_reason": self._recovery_reason,
            "dirty_restart_detected": bool(self._dirty_restart_detected),
            "single_instance_lock_active": bool(self._runtime_lock_active),
            "user_ws_stale": bool(freshness["user_ws_stale"]),
            "market_data_stale": bool(freshness["market_data_stale"]),
            "engine_state": {"state": state.status, "updated_at": state.last_transition_at},
            "risk_config": dict(self._risk),
            "config": config,
            "config_summary": config_summary,
            "scheduler": {
                "tick_sec": float(self.scheduler.tick_seconds),
                "running": bool(self._running),
                **dict(self._last_cycle),
            },
            "watchdog": dict(self._watchdog_state),
            "capital_snapshot": {
                "symbol": self.cfg.behavior.exchange.default_symbol,
                "available_usdt": available_usdt,
                "budget_usdt": effective_margin,
                "used_margin": 0.0,
                "leverage": leverage,
                "notional_usdt": expected_notional,
                "mark_price": 0.0,
                "est_qty": 0.0,
                "blocked": not self.ops.can_open_new_entries(),
                "block_reason": (
                    self._risk.get("last_strategy_block_reason")
                    or self._risk.get("last_auto_risk_reason")
                    or ("ops_paused" if not self.ops.can_open_new_entries() else None)
                ),
            },
            "binance": {
                "enabled_symbols": list(
                    self._risk.get("universe_symbols")
                    or [self.cfg.behavior.exchange.default_symbol]
                ),
                "positions": positions_payload,
                "usdt_balance": {
                    "wallet": wallet_usdt,
                    "available": available_usdt,
                    "source": live_balance_source,
                },
                "startup_error": self._state_uncertain_reason if self.cfg.mode == "live" else None,
                "private_error": self._last_balance_error,
                "private_error_detail": self._last_balance_error_detail,
            },
            "pnl": {
                "daily_pnl_pct": float(self._risk.get("daily_realized_pct") or 0.0) * 100.0,
                "drawdown_pct": float(self._risk.get("dd_used_pct") or 0.0) * 100.0,
                "lose_streak": int(self._risk.get("lose_streak") or 0),
                "cooldown_until": self._risk.get("cooldown_until"),
                "daily_realized_pnl": float(self._risk.get("daily_realized_pnl") or 0.0),
                "daily_loss_used_pct": float(self._risk.get("daily_loss_used_pct") or 0.0),
                "recent_blocks": dict(self._risk.get("recent_blocks") or {}),
                "last_strategy_block_reason": self._risk.get("last_strategy_block_reason"),
                "last_alpha_id": self._risk.get("last_alpha_id"),
                "last_entry_family": self._risk.get("last_entry_family"),
                "last_regime": self._risk.get("last_regime"),
                "overheat_state": dict(self._risk.get("overheat_state") or {}),
                "last_auto_risk_reason": self._risk.get("last_auto_risk_reason"),
            },
            "live_readiness": self._live_readiness_snapshot(
                live_balance_source=live_balance_source,
                private_error=self._last_balance_error,
            ),
            "user_stream": {
                "started": bool(self._user_stream_started),
                "started_at": self._user_stream_started_at,
                "last_event_at": self._user_stream_last_event_at,
                "last_private_ok_at": self._last_private_stream_ok_at,
                "last_disconnect_at": self._user_stream_last_disconnect_at,
                "last_error": self._user_stream_last_error,
                "age_sec": freshness["user_ws_age_sec"],
                "stale": freshness["user_ws_stale"],
            },
            "market_data": {
                "last_market_data_at": freshness["last_market_data_at"],
                "age_sec": freshness["market_data_age_sec"],
                "stale": freshness["market_data_stale"],
                "observer_stale": freshness["market_data_observer_stale"],
                "source_stale": freshness["market_data_source_stale"],
                "last_source_ok_at": freshness["last_market_data_source_ok_at"],
                "last_source_fail_at": freshness["last_market_data_source_fail_at"],
                "source_age_sec": freshness["market_data_source_age_sec"],
                "source_error": freshness["last_market_data_source_error"],
                "symbol_count": self._market_data_state.get("last_market_symbol_count"),
            },
            "submission_recovery": gate["submission_recovery"],
            "boot_recovery": dict(self._boot_recovery),
            "health": {
                "ready": bool(gate["ready"]),
                "single_instance_ok": bool(gate["single_instance_ok"]),
                "private_auth_ok": bool(gate["private_auth_ok"]),
                "submission_recovery_ok": bool(gate["submission_recovery_ok"]),
            },
            "last_error": self._last_cycle.get("last_error"),
        }

    def _healthz_snapshot(self) -> dict[str, Any]:
        gate = self._gate_snapshot()
        freshness = gate["freshness"]
        return {
            "ok": True,
            "live": True,
            "mode": self.cfg.mode,
            "profile": self.cfg.profile,
            "env": self.cfg.env,
            "ready": bool(gate["ready"]),
            "state_uncertain": bool(gate["state_uncertain"]),
            "recovery_required": bool(gate["recovery_required"]),
            "submission_recovery_ok": bool(gate["submission_recovery_ok"]),
            "safe_mode": bool(gate["safe_mode"]),
            "paused": bool(gate["paused"]),
            "startup_reconcile_ok": self._startup_reconcile_ok,
            "single_instance_ok": bool(gate["single_instance_ok"]),
            "user_ws_stale": bool(freshness["user_ws_stale"]),
            "market_data_stale": bool(freshness["market_data_stale"]),
        }

    def _readyz_snapshot(self) -> dict[str, Any]:
        gate = self._gate_snapshot()
        freshness = gate["freshness"]
        return {
            "ready": bool(gate["ready"]),
            "mode": self.cfg.mode,
            "profile": self.cfg.profile,
            "env": self.cfg.env,
            "single_instance_ok": bool(gate["single_instance_ok"]),
            "state_uncertain": bool(gate["state_uncertain"]),
            "state_uncertain_reason": self._state_uncertain_reason,
            "recovery_required": bool(gate["recovery_required"]),
            "recovery_reason": self._recovery_reason,
            "submission_recovery_ok": bool(gate["submission_recovery_ok"]),
            "submission_recovery": gate["submission_recovery"],
            "startup_reconcile_ok": self._startup_reconcile_ok,
            "last_reconcile_at": freshness["last_reconcile_at"],
            "last_reconcile_age_sec": freshness["reconcile_age_sec"],
            "last_reconcile_max_age_sec": freshness["reconcile_max_age_sec"],
            "user_ws_stale": bool(freshness["user_ws_stale"]),
            "last_user_ws_event_at": freshness["last_user_ws_event_at"],
            "last_private_stream_ok_at": freshness["last_private_stream_ok_at"],
            "user_ws_age_sec": freshness["user_ws_age_sec"],
            "user_ws_stale_sec": freshness["user_ws_stale_sec"],
            "market_data_stale": bool(freshness["market_data_stale"]),
            "market_data_observer_stale": bool(freshness["market_data_observer_stale"]),
            "market_data_source_stale": bool(freshness["market_data_source_stale"]),
            "market_data_source_error": freshness["last_market_data_source_error"],
            "last_market_data_at": freshness["last_market_data_at"],
            "last_market_data_source_ok_at": freshness["last_market_data_source_ok_at"],
            "market_data_age_sec": freshness["market_data_age_sec"],
            "market_data_source_age_sec": freshness["market_data_source_age_sec"],
            "market_data_stale_sec": freshness["market_data_stale_sec"],
            "safe_mode": bool(gate["safe_mode"]),
            "paused": bool(gate["paused"]),
            "private_auth_ok": bool(gate["private_auth_ok"]),
        }

    def _cached_live_balance(
        self, *, max_age_sec: float
    ) -> tuple[float | None, float | None] | None:
        fetched_at = self._last_balance_fetched_mono
        if fetched_at is None:
            return None
        if (time.monotonic() - fetched_at) > max(0.0, float(max_age_sec)):
            return None
        if self._last_balance_available_usdt is None or self._last_balance_wallet_usdt is None:
            return None
        return self._last_balance_available_usdt, self._last_balance_wallet_usdt

    def _cached_or_fallback_balance(self) -> tuple[float | None, float | None, str]:
        cached = self._cached_live_balance(max_age_sec=1800.0)
        if cached is None:
            return None, None, "fallback"
        available, wallet = cached
        self._last_balance_error = None
        self._last_balance_error_detail = "served_from_recent_cache"
        return available, wallet, "exchange_cached"

    def _fetch_live_usdt_balance(self) -> tuple[float | None, float | None, str]:
        fresh_cache = self._cached_live_balance(max_age_sec=20.0)
        if fresh_cache is not None:
            self._last_balance_error = None
            self._last_balance_error_detail = None
            available, wallet = fresh_cache
            return available, wallet, "exchange"

        if self.rest_client is None:
            self._last_balance_error = "rest_client_unavailable"
            self._last_balance_error_detail = "balance_rest_client_not_configured"
            return self._cached_or_fallback_balance()
        rest_client: Any = self.rest_client
        assert rest_client is not None
        payload: Any = None
        fetch_exc: Exception | None = None
        for attempt in range(2):
            try:
                payload = _run_async_blocking(lambda: rest_client.get_balances(), timeout_sec=8.0)
                fetch_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                fetch_exc = exc
                if attempt == 0:
                    time.sleep(0.35)
                    continue
        try:
            if fetch_exc is not None:
                raise fetch_exc
        except FutureTimeoutError:
            logger.warning("live_balance_fetch_timed_out")
            self._last_balance_error = "balance_fetch_timeout"
            self._last_balance_error_detail = "fetch_timeout_over_8s"
            return self._cached_or_fallback_balance()
        except BinanceRESTError as e:
            logger.warning(
                "live_balance_fetch_rest_error",
                extra={
                    "status_code": e.status_code,
                    "code": e.code,
                    "path": e.path,
                },
            )
            if e.code in {-2014, -2015} or e.status_code in {401, 403}:
                self._last_balance_error = "balance_auth_failed"
            elif e.status_code == 429 or e.code in {-1003}:
                self._last_balance_error = "balance_rate_limited"
            else:
                self._last_balance_error = "balance_fetch_failed"
            self._last_balance_error_detail = (
                f"status={e.status_code} code={e.code} path={e.path} msg={e.message}"
            )
            return self._cached_or_fallback_balance()
        except Exception:  # noqa: BLE001
            logger.exception("live_balance_fetch_failed")
            self._last_balance_error = "balance_fetch_failed"
            self._last_balance_error_detail = "unexpected_exception"
            return self._cached_or_fallback_balance()

        if not isinstance(payload, list):
            self._last_balance_error = "balance_payload_invalid"
            self._last_balance_error_detail = f"payload_type={type(payload).__name__}"
            return self._cached_or_fallback_balance()

        target: dict[str, Any] | None = None
        for item in payload:
            if not isinstance(item, dict):
                continue
            asset = str(item.get("asset") or item.get("coin") or "").upper()
            if asset == "USDT":
                target = item
                break

        if target is None:
            self._last_balance_error = "usdt_asset_missing"
            self._last_balance_error_detail = "asset_usdt_not_found"
            return self._cached_or_fallback_balance()

        available = _to_float(
            target.get("availableBalance")
            or target.get("withdrawAvailable")
            or target.get("balance"),
            default=0.0,
        )
        wallet = _to_float(
            target.get("walletBalance")
            or target.get("crossWalletBalance")
            or target.get("balance"),
            default=0.0,
        )
        self._last_balance_available_usdt = available
        self._last_balance_wallet_usdt = wallet
        self._last_balance_fetched_mono = time.monotonic()
        self._last_balance_error = None
        self._last_balance_error_detail = None
        return available, wallet, "exchange"

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
    ) -> tuple[dict[str, float], dict[str, dict[str, Any]], bool]:
        rest_client = self.rest_client
        if rest_client is None or not hasattr(rest_client, "get_positions"):
            return {}, {}, False
        rest_client_any: Any = rest_client
        try:
            payload = _run_async_blocking(lambda: rest_client_any.get_positions(), timeout_sec=8.0)
        except FutureTimeoutError:
            logger.warning("live_positions_fetch_timed_out")
            return {}, {}, False
        except Exception:  # noqa: BLE001
            logger.exception("live_positions_fetch_failed")
            return {}, {}, False
        if not isinstance(payload, list):
            return {}, {}, False
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
        return out, rows_by_symbol, True

    def _is_live_reentry_blocked(self) -> bool:
        allow_reentry = _to_bool(
            self._risk.get("allow_reentry"),
            default=bool(self.cfg.behavior.engine.allow_reentry),
        )
        if allow_reentry:
            return False
        if self.cfg.mode != "live":
            return False
        if not self._running:
            return False

        symbols_raw = self._risk.get("universe_symbols")
        if isinstance(symbols_raw, list):
            symbols = [str(sym).strip().upper() for sym in symbols_raw if str(sym).strip()]
        else:
            symbols = [self.cfg.behavior.exchange.default_symbol]
        if len(symbols) > 1:
            return False

        positions, _rows, ok = self._fetch_live_positions()
        if not ok:
            return False
        return any(
            abs(_to_float(position_amt, default=0.0)) > 0.0 for position_amt in positions.values()
        )

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
                lambda s=symbol: self.close_position(symbol=s), timeout_sec=30.0
            )
            self.notifier.send(f"손절 감지: {symbol} / 해당 심볼 정리 실행")
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

    def _latest_symbol_realized_pnl(
        self, *, symbol: str, lookback_sec: float = 900.0
    ) -> float | None:
        symbol_u = symbol.upper()
        lookback_ms = max(1, int(float(lookback_sec) * 1000.0))
        now_ms = int(time.time() * 1000)
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
        if normalized_realized is None:
            headline = "익절 완료!" if outcome == "TP" else "손절 완료!"
        elif normalized_realized > 0.0:
            headline = "익절 완료!"
        elif normalized_realized < 0.0:
            headline = "손절 완료!"
        else:
            headline = "손익없음 청산!"
        if normalized_realized is None:
            message = f"{headline} {symbol} 실현PnL 집계중"
        elif normalized_realized == 0.0:
            message = f"{headline} {symbol} 0.0000 USDT"
        else:
            message = f"{headline} {symbol} {self._fmt_signed(normalized_realized)} USDT"

        try:
            self.notifier.send(message)
        except Exception:  # noqa: BLE001
            logger.exception("bracket_exit_notify_failed symbol=%s outcome=%s", symbol, outcome)

    def _poll_brackets_once(self) -> None:
        rest_client = self.rest_client
        if self.cfg.mode != "live" or rest_client is None:
            return
        rest_client_any: Any = rest_client

        tracked = self._list_tracked_brackets()
        if not tracked:
            return

        positions, position_rows, positions_ok = self._fetch_live_positions()
        for row in tracked:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            tp_id = str(row.get("tp_order_client_id") or "").strip()
            sl_id = str(row.get("sl_order_client_id") or "").strip()
            if not tp_id or not sl_id:
                continue

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

            if positions_ok:
                position_amt = _to_float(positions.get(symbol), default=0.0)
                if abs(position_amt) <= 0.0:
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

                position_row = position_rows.get(symbol)
                if isinstance(position_row, dict):
                    if self._maybe_trigger_trailing_exit(
                        symbol=symbol,
                        row=position_row,
                        rest_client=rest_client_any,
                    ):
                        continue

            tp_open = tp_id in open_ids
            sl_open = sl_id in open_ids
            if tp_open == sl_open:
                continue

            outcome: Literal["TP", "SL"] = "SL" if tp_open else "TP"
            filled_id = sl_id if outcome == "SL" else tp_id
            try:
                _ = _run_async_blocking(
                    lambda s=symbol, cid=filled_id: self._bracket_service.on_leg_filled(
                        symbol=s,
                        filled_client_algo_id=cid,
                    ),
                    timeout_sec=8.0,
                )
                self._emit_bracket_exit_alert(symbol=symbol, outcome=outcome)
                if outcome == "SL":
                    self._maybe_trigger_symbol_sl_flatten(trigger_symbol=symbol)
            except Exception:  # noqa: BLE001
                logger.exception("bracket_on_leg_filled_failed symbol=%s", symbol)

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

    def _run_cycle_once_locked(self) -> dict[str, Any]:
        self._cycle_seq += 1
        cycle_seq = self._cycle_seq
        self._last_cycle["tick_started_at"] = _utcnow_iso()
        self._last_cycle["last_error"] = None
        self._last_cycle["bracket"] = None
        try:
            self._refresh_runtime_risk_context()
            self._sync_kernel_runtime_overrides()
            if hasattr(self.kernel, "set_tick"):
                self.kernel.set_tick(cycle_seq)
            self._update_stale_transitions()
            freshness = self._freshness_snapshot()
            submission_recovery = self._submission_recovery_snapshot()
            if bool(freshness["market_data_stale"]):
                self._maybe_probe_market_data()
                self._update_stale_transitions()
                freshness = self._freshness_snapshot()
                submission_recovery = self._submission_recovery_snapshot()
            if self._recovery_required:
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
            elif self._state_uncertain:
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
            elif self._is_live_reentry_blocked():
                cycle = KernelCycleResult(
                    state="blocked",
                    reason="position_open",
                    candidate=None,
                )
            else:
                cycle = self.kernel.run_once()
            portfolio_cycle = None
            portfolio_reader = getattr(self.kernel, "last_portfolio_cycle", None)
            if callable(portfolio_reader):
                try:
                    portfolio_cycle = portfolio_reader()
                except Exception:  # noqa: BLE001
                    logger.exception("portfolio_cycle_read_failed")
            self.scheduler.run_once()
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

            if self.ops.can_open_new_entries() and self._running and not self._thread_stop.is_set():
                for actionable in actionable_cycles:
                    submit_symbol = (
                        actionable.candidate.symbol
                        if actionable.candidate is not None
                        and str(actionable.candidate.symbol).strip()
                        else self.cfg.behavior.exchange.default_symbol
                    )
                    self.order_manager.submit({"symbol": submit_symbol, "mode": self.cfg.mode})

            for actionable in actionable_cycles:
                self._place_brackets_for_cycle(cycle=actionable)

            self._last_cycle["tick_finished_at"] = _utcnow_iso()
            self._last_cycle["last_action"] = cycle.state
            self._last_cycle["last_decision_reason"] = cycle.reason
            self._last_cycle["candidate"] = (
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
            self._last_cycle["last_candidate"] = self._last_cycle["candidate"]
            self._last_cycle["portfolio"] = (
                {
                    "slots_used": int(
                        _to_float(getattr(portfolio_cycle, "open_position_count", 0), default=0.0)
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
            self._risk["last_alpha_id"] = (
                getattr(cycle.candidate, "alpha_id", None) if cycle.candidate is not None else None
            )
            self._risk["last_entry_family"] = (
                getattr(cycle.candidate, "entry_family", None) if cycle.candidate is not None else None
            )
            self._risk["last_regime"] = (
                getattr(cycle.candidate, "regime_hint", None) if cycle.candidate is not None else None
            )
            if cycle.state in {"blocked", "risk_rejected", "no_candidate"}:
                self._risk["last_strategy_block_reason"] = cycle.reason
            elif cycle.state in {"executed", "dry_run"}:
                self._risk["last_strategy_block_reason"] = None
            overheat_reason = cycle.reason if str(cycle.reason).startswith("overheat_") else None
            self._risk["overheat_state"] = {
                "blocked": overheat_reason is not None,
                "reason": overheat_reason,
            }
            cycle_error = cycle.reason if cycle.state == "execution_failed" else None
            existing_error = str(self._last_cycle.get("last_error") or "").strip()
            self._last_cycle["last_error"] = existing_error or cycle_error
            if cycle.state == "execution_failed" and "REVIEW_REQUIRED" in str(cycle.reason).upper():
                self._set_state_uncertain(
                    reason="submit_recovery_required",
                    engage_safe_mode=True,
                )

            self._report_stats["total_records"] += 1
            if cycle.state in {"executed", "dry_run"}:
                self._report_stats["entries"] += 1
            if cycle.state == "execution_failed":
                self._report_stats["errors"] += 1
            if cycle.state in {"blocked", "risk_rejected"}:
                self._report_stats["blocks"] += 1
                self._record_recent_block(cycle.reason)
            self._maybe_apply_auto_risk_circuit(cycle)
            self._persist_risk_config()
            ok = True
            error_message = None
            self._cycle_done_seq = cycle_seq
        except Exception as exc:  # noqa: BLE001
            detail = str(exc).strip()
            if detail:
                error_message = f"cycle_failed:{type(exc).__name__}:{detail}"
            else:
                error_message = f"cycle_failed:{type(exc).__name__}"
            logger.exception("runtime_cycle_failed")
            self._last_cycle["tick_finished_at"] = _utcnow_iso()
            self._last_cycle["last_action"] = "error"
            self._last_cycle["last_decision_reason"] = error_message
            self._last_cycle["candidate"] = None
            self._last_cycle["last_candidate"] = None
            self._last_cycle["portfolio"] = None
            self._last_cycle["last_error"] = error_message
            self._last_cycle["bracket"] = None
            self._report_stats["total_records"] += 1
            self._report_stats["errors"] += 1
            ok = False
            self._cycle_done_seq = cycle_seq

        self._emit_status_update()

        out: dict[str, Any] = {
            "ok": ok,
            "tick_sec": float(self.scheduler.tick_seconds),
            "snapshot": dict(self._last_cycle),
        }
        if error_message is not None:
            out["error"] = error_message
        return out

    def _status_summary(self) -> str:
        state_raw = str(self.state_store.get().status)
        state_ko = {
            "RUNNING": "실행중",
            "PAUSED": "일시정지",
            "STOPPED": "중지",
            "KILLED": "강제중지",
        }.get(state_raw, state_raw)
        live_trading_enabled = self.cfg.mode == "live" and str(self.cfg.env) == "prod"
        last_action = self._translate_status_token(
            str(self._last_cycle.get("last_action") or "-"), _ACTION_LABELS_KO
        )
        reason = self._translate_status_token(
            str(self._last_cycle.get("last_decision_reason") or "-"), _REASON_LABELS_KO
        )
        portfolio_summary = self._portfolio_slot_summary()
        position_summary, pnl_summary = self._status_pnl_summary()
        return (
            "상태 알림: "
            f"프로필={self.cfg.profile}, 모드={self.cfg.mode}, 환경={self.cfg.env}, "
            f"실거래활성={'예' if live_trading_enabled else '아니오'}, "
            f"엔진={state_ko}, 마지막판단={last_action}, 사유={reason}, "
            f"포지션={position_summary}, 슬롯={portfolio_summary}, {pnl_summary}"
        )

    def _portfolio_slot_summary(self) -> str:
        portfolio = self._last_cycle.get("portfolio")
        if not isinstance(portfolio, dict):
            return "-"
        slots_used = int(_to_float(portfolio.get("slots_used"), default=0.0))
        slots_total = int(_to_float(portfolio.get("slots_total"), default=0.0))
        if slots_total <= 0:
            return "-"
        return f"{slots_used}/{slots_total}"

    @staticmethod
    def _fmt_signed(value: float) -> str:
        return f"{float(value):+.4f}"

    @staticmethod
    def _position_side_label(position_amt: float) -> str:
        return "롱" if position_amt > 0 else "숏"

    def _status_positions_source(self) -> list[tuple[str, float, float, float]]:
        live_positions, live_rows, live_ok = self._fetch_live_positions()
        if live_ok and live_rows:
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
        positions = self._status_positions_source()
        total_unrealized = 0.0
        per_symbol: list[str] = []
        position_labels: list[str] = []
        for symbol, position_amt, pnl, _entry_price in positions:
            total_unrealized += pnl
            per_symbol.append(f"{symbol}:{self._fmt_signed(pnl)}")
            position_labels.append(f"{symbol}[{self._position_side_label(position_amt)}]")

        parts = [f"미실현PnL={self._fmt_signed(total_unrealized)} USDT"]
        if per_symbol:
            preview = ", ".join(per_symbol[:3])
            if len(per_symbol) > 3:
                preview = f"{preview}, ..."
            parts.append(f"포지션별={preview}")

        latest_realized: float | None = None
        for fill in self.state_store.get().last_fills:
            if fill.realized_pnl is None:
                continue
            latest_realized = _to_float(fill.realized_pnl, default=0.0)
            break
        if latest_realized is not None:
            parts.append(f"최근실현PnL={self._fmt_signed(latest_realized)} USDT")

        position_summary = ", ".join(position_labels[:3]) if position_labels else "없음"
        if len(position_labels) > 3:
            position_summary = f"{position_summary}, ..."

        return position_summary, ", ".join(parts)

    @staticmethod
    def _translate_status_token(raw: str, labels: dict[str, str]) -> str:
        value = str(raw or "").strip()
        if not value or value == "-":
            return "-"
        direct = labels.get(value)
        if direct is not None:
            return direct
        head, sep, tail = value.partition(":")
        head_ko = labels.get(head)
        if head_ko is None:
            return value
        if sep:
            return f"{head_ko}:{tail}"
        return head_ko

    def _emit_status_update(self, *, force: bool = False) -> bool:
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
                self._run_cycle_once_locked()
            self._thread_stop.wait(timeout=max(0.2, float(self.scheduler.tick_seconds)))

    def start(self) -> dict[str, Any]:
        with self._lock:
            self._auto_reconcile_if_recovery_required(reason="operator_start")
            self.state_store.set(status="RUNNING")
            self.ops.resume()
            if self._running:
                state = self.state_store.get()
                return {"state": state.status, "updated_at": state.last_transition_at}
            self._running = True
            self._thread_stop.clear()
            self._thread = threading.Thread(target=self._loop_worker, daemon=True)
            self._thread.start()
            self._log_event("runtime_start", running=True)
            state = self.state_store.get()
            return {"state": state.status, "updated_at": state.last_transition_at}

    def stop(self) -> dict[str, Any]:
        acquired = self._lock.acquire(timeout=1.0)
        try:
            self._running = False
            self._thread_stop.set()
            self.ops.pause()
            self.state_store.set(status="PAUSED")
            self._log_event("runtime_stop", running=False)
            self._emit_status_update(force=True)
            state = self.state_store.get()
            return {"state": state.status, "updated_at": state.last_transition_at}
        finally:
            if acquired:
                self._lock.release()

    async def panic(self) -> dict[str, Any]:
        self.stop()
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
        state = self.state_store.get()
        self._report_stats["closes"] += 1
        return {
            "engine_state": {"state": state.status, "updated_at": state.last_transition_at},
            "panic_result": {
                "ok": True,
                "canceled_orders_ok": True,
                "close_ok": True,
                "errors": [],
                "closed_symbol": result.symbol,
                "closed_qty": abs(result.position_amt),
            },
        }

    def get_risk(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_runtime_risk_context()
            self._sync_kernel_runtime_overrides()
            return dict(self._risk)

    def set_value(self, *, key: str, value: str) -> dict[str, Any]:
        with self._lock:
            parsed = _parse_value(value)
            if key == "universe_symbols":
                if isinstance(parsed, str):
                    parsed = [item.strip().upper() for item in parsed.split(",") if item.strip()]
                elif isinstance(parsed, list):
                    parsed = [str(item).strip().upper() for item in parsed if str(item).strip()]
                else:
                    parsed = [self.cfg.behavior.exchange.default_symbol]
            if key in {"notify_interval_sec", "scheduler_tick_sec"}:
                parsed = max(1, int(_to_float(parsed, default=1.0)))

            self._risk[key] = parsed
            self._refresh_runtime_risk_context()
            self._sync_kernel_runtime_overrides()

            if key == "scheduler_tick_sec":
                self.scheduler.tick_seconds = int(
                    _to_float(parsed, default=float(self.scheduler.tick_seconds))
                )

            self._persist_risk_config()
            if key == "notify_interval_sec":
                self._emit_status_update(force=True)
            return {
                "key": key,
                "requested_value": value,
                "applied_value": self._risk.get(key),
                "summary": f"Applied {key}={self._risk.get(key)}",
                "risk_config": dict(self._risk),
            }

    def set_symbol_leverage(self, *, symbol: str, leverage: float) -> dict[str, Any]:
        with self._lock:
            symbol_u = symbol.strip().upper()
            mapping = self._risk.get("symbol_leverage_map")
            if not isinstance(mapping, dict):
                mapping = {}
            if leverage <= 0:
                mapping.pop(symbol_u, None)
            else:
                mapping[symbol_u] = float(leverage)
            self._risk["symbol_leverage_map"] = mapping
            self._refresh_runtime_risk_context()
            self._sync_kernel_runtime_overrides()
            self._persist_risk_config()
            return dict(self._risk)

    def get_scheduler(self) -> dict[str, Any]:
        return {
            "tick_sec": float(self.scheduler.tick_seconds),
            "running": bool(self._running),
            "min_tick_sec": 1.0,
        }

    async def _handle_user_stream_event(self, event: dict[str, Any]) -> None:
        await asyncio.to_thread(
            self.state_store.apply_exchange_event,
            event=event,
            reason="user_stream_event",
        )
        self._user_stream_last_event_at = _utcnow_iso()
        self._last_private_stream_ok_at = self._user_stream_last_event_at
        self._update_stale_transitions()

    async def _handle_user_stream_resync(self, snapshot: ResyncSnapshot) -> None:
        try:
            await asyncio.to_thread(
                self._apply_resync_snapshot,
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
        await asyncio.to_thread(self._maybe_probe_market_data)
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
            state = self.state_store.get()
            return {
                "ok": not self._state_uncertain,
                "state_uncertain": bool(self._state_uncertain),
                "state_uncertain_reason": self._state_uncertain_reason,
                "startup_reconcile_ok": self._startup_reconcile_ok,
                "last_reconcile_at": state.last_reconcile_at,
            }

    def set_scheduler_interval(self, tick_sec: float) -> dict[str, Any]:
        with self._lock:
            sec = max(1, int(tick_sec))
            self.scheduler.tick_seconds = sec
            self._risk["scheduler_tick_sec"] = sec
            self._persist_risk_config()
            return {
                "tick_sec": float(self.scheduler.tick_seconds),
                "running": bool(self._running),
                "min_tick_sec": 1.0,
            }

    def tick_scheduler_now(self) -> dict[str, Any]:
        if self._lock.acquire(blocking=False):
            try:
                self._auto_reconcile_if_recovery_required(reason="operator_tick")
                return self._run_cycle_once_locked()
            finally:
                self._lock.release()

        if self._running:
            deadline = time.monotonic() + 7.0
            target_seq = max(1, int(self._cycle_seq))
            while time.monotonic() < deadline:
                if int(self._cycle_done_seq) >= target_seq:
                    snapshot = dict(self._last_cycle)
                    snapshot["coalesced"] = True
                    return {
                        "ok": True,
                        "tick_sec": float(self.scheduler.tick_seconds),
                        "snapshot": snapshot,
                        "error": None,
                    }
                if self._lock.acquire(timeout=0.1):
                    try:
                        return self._run_cycle_once_locked()
                    finally:
                        self._lock.release()
                time.sleep(0.1)

            self._last_cycle["tick_finished_at"] = _utcnow_iso()
            self._last_cycle["last_action"] = "blocked"
            self._last_cycle["last_decision_reason"] = "tick_busy"
            self._last_cycle["last_error"] = "tick_busy"
            self._last_cycle["candidate"] = None
            self._last_cycle["last_candidate"] = None
            self._last_cycle["portfolio"] = None
            return {
                "ok": False,
                "tick_sec": float(self.scheduler.tick_seconds),
                "snapshot": dict(self._last_cycle),
                "error": "tick_busy",
            }

        if self._lock.acquire(timeout=6.0):
            try:
                return self._run_cycle_once_locked()
            finally:
                self._lock.release()
        self._last_cycle["tick_finished_at"] = _utcnow_iso()
        self._last_cycle["last_action"] = "blocked"
        self._last_cycle["last_decision_reason"] = "tick_busy"
        self._last_cycle["last_error"] = "tick_busy"
        self._last_cycle["candidate"] = None
        self._last_cycle["last_candidate"] = None
        self._last_cycle["portfolio"] = None
        return {
            "ok": False,
            "tick_sec": float(self.scheduler.tick_seconds),
            "snapshot": dict(self._last_cycle),
            "error": "tick_busy",
        }

    @staticmethod
    def _format_daily_report_message(payload: dict[str, Any]) -> str:
        detail_raw = payload.get("detail")
        detail = detail_raw if isinstance(detail_raw, dict) else {}
        entries = int(_to_float(detail.get("entries"), default=0.0))
        closes = int(_to_float(detail.get("closes"), default=0.0))
        errors = int(_to_float(detail.get("errors"), default=0.0))
        canceled = int(_to_float(detail.get("canceled"), default=0.0))
        blocks = int(_to_float(detail.get("blocks"), default=0.0))
        total_records = int(_to_float(detail.get("total_records"), default=0.0))

        lines = [
            f"[{str(payload.get('kind') or 'DAILY_REPORT')}]",
            f"일자: {str(payload.get('day') or '-')}",
            f"엔진 상태: {str(payload.get('engine_state') or '-')}",
            f"보고 시각: {str(payload.get('reported_at') or '-')}",
            f"진입/청산: {entries} / {closes}",
            f"오류/취소: {errors} / {canceled}",
            f"차단/총건수: {blocks} / {total_records}",
        ]
        return "\n".join(lines)

    def send_daily_report(self) -> dict[str, Any]:
        payload = {
            "kind": "DAILY_REPORT",
            "day": datetime.now(timezone.utc).date().isoformat(),
            "engine_state": self.state_store.get().status,
            "detail": dict(self._report_stats),
            "notifier_enabled": bool(self.notifier.enabled),
            "notifier_sent": False,
            "notifier_error": None,
            "reported_at": _utcnow_iso(),
        }
        message = self._format_daily_report_message(payload)
        result = self.notifier.send_with_result(message)
        payload["notifier_sent"] = bool(result.sent)
        if result.error and result.error != "disabled":
            payload["notifier_error"] = result.error
        return payload

    def preset(self, name: str) -> dict[str, Any]:
        with self._lock:
            profile = str(name).strip().lower()
            if profile == "conservative":
                self._risk["max_leverage"] = 5.0
                self._risk["per_trade_risk_pct"] = 5.0
            elif profile == "normal":
                self._risk["max_leverage"] = 10.0
                self._risk["per_trade_risk_pct"] = 10.0
            elif profile == "aggressive":
                self._risk["max_leverage"] = 20.0
                self._risk["per_trade_risk_pct"] = 20.0
            self._sync_kernel_runtime_overrides()
            self._persist_risk_config()
            return dict(self._risk)

    async def close_position(self, *, symbol: str) -> dict[str, Any]:
        self._log_event("flatten_requested", action="close_position", symbol=symbol.upper())
        result = await self.ops.flatten(symbol=symbol)
        self._report_stats["closes"] += 1
        return {
            "symbol": result.symbol,
            "detail": {
                "open_regular_orders": result.open_regular_orders,
                "open_algo_orders": result.open_algo_orders,
                "position_amt": result.position_amt,
                "paused": result.paused,
                "safe_mode": result.safe_mode,
            },
        }

    async def close_all(self) -> dict[str, Any]:
        self._log_event("flatten_requested", action="close_all")
        symbols = set(
            self._risk.get("universe_symbols") or [self.cfg.behavior.exchange.default_symbol]
        )
        for sym in self.state_store.get().current_position.keys():
            symbols.add(sym)
        details: list[dict[str, Any]] = []
        for symbol in sorted({str(s).upper() for s in symbols if str(s).strip()}):
            details.append(await self.close_position(symbol=symbol))
        return {"symbol": "ALL", "detail": {"results": details}}

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
            self._risk["last_strategy_block_reason"] = None
            self._risk["last_alpha_id"] = None
            self._risk["last_entry_family"] = None
            self._risk["last_regime"] = None
            self._risk["overheat_state"] = {"blocked": False, "reason": None}
            self._persist_risk_config()
            self._sync_kernel_runtime_overrides()
            return {
                "day": datetime.now(timezone.utc).date().isoformat(),
                "daily_realized_pnl": float(self._risk.get("daily_realized_pnl") or 0.0),
                "equity_peak": float(self._risk.get("runtime_equity_peak_usdt") or 0.0),
                "daily_pnl_pct": float(self._risk.get("daily_realized_pct") or 0.0) * 100.0,
                "drawdown_pct": float(self._risk.get("dd_used_pct") or 0.0) * 100.0,
                "lose_streak": int(self._risk.get("lose_streak") or 0),
                "cooldown_until": self._risk.get("cooldown_until"),
                "last_block_reason": self._risk.get("last_block_reason"),
                "last_strategy_block_reason": self._risk.get("last_strategy_block_reason"),
                "last_auto_risk_reason": self._risk.get("last_auto_risk_reason"),
            }


class SetValueRequest(BaseModel):
    key: str
    value: str


class SetSymbolLeverageRequest(BaseModel):
    symbol: str
    leverage: float


class SchedulerIntervalRequest(BaseModel):
    tick_sec: float


class PresetRequest(BaseModel):
    name: str


class TradeCloseRequest(BaseModel):
    symbol: str


def create_control_http_app(*, controller: RuntimeController) -> FastAPI:
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        _ = _app
        await controller.start_live_services()
        try:
            yield
        finally:
            await controller.stop_live_services()

    app = FastAPI(title="auto-trader-v2-control", version="0.1.0", lifespan=_lifespan)

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return await asyncio.to_thread(controller._status_snapshot)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return await asyncio.to_thread(controller._healthz_snapshot)

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        payload = await asyncio.to_thread(controller._readyz_snapshot)
        return JSONResponse(status_code=200 if payload["ready"] else 503, content=payload)

    @app.get("/risk")
    async def risk() -> dict[str, Any]:
        return controller.get_risk()

    @app.get("/readiness")
    async def readiness() -> dict[str, Any]:
        return await asyncio.to_thread(controller._live_readiness_snapshot)

    @app.post("/start")
    async def start() -> dict[str, Any]:
        return controller.start()

    @app.post("/stop")
    async def stop() -> dict[str, Any]:
        return controller.stop()

    @app.post("/panic")
    async def panic() -> dict[str, Any]:
        return await controller.panic()

    @app.post("/cooldown/clear")
    async def clear_cooldown() -> dict[str, Any]:
        return controller.clear_cooldown()

    @app.post("/reconcile")
    async def reconcile() -> dict[str, Any]:
        return await controller.reconcile_now()

    @app.post("/set")
    async def set_value(payload: SetValueRequest) -> dict[str, Any]:
        return controller.set_value(key=payload.key, value=payload.value)

    @app.post("/symbol-leverage")
    async def set_symbol_leverage(payload: SetSymbolLeverageRequest) -> dict[str, Any]:
        return controller.set_symbol_leverage(symbol=payload.symbol, leverage=payload.leverage)

    @app.get("/scheduler")
    async def get_scheduler() -> dict[str, Any]:
        return controller.get_scheduler()

    @app.post("/scheduler/interval")
    async def scheduler_interval(payload: SchedulerIntervalRequest) -> dict[str, Any]:
        return controller.set_scheduler_interval(payload.tick_sec)

    @app.post("/scheduler/tick")
    async def scheduler_tick() -> dict[str, Any]:
        return controller.tick_scheduler_now()

    @app.post("/report")
    async def report() -> dict[str, Any]:
        return controller.send_daily_report()

    @app.post("/preset")
    async def preset(payload: PresetRequest) -> dict[str, Any]:
        return controller.preset(payload.name)

    @app.post("/trade/close")
    async def close(payload: TradeCloseRequest) -> dict[str, Any]:
        return await controller.close_position(symbol=payload.symbol)

    @app.post("/trade/close_all")
    async def close_all() -> dict[str, Any]:
        return await controller.close_all()

    return app


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
        user_stream_manager=user_stream_manager,
        market_data_state=market_data_state,
        runtime_lock_active=runtime_lock_active,
        dirty_restart_detected=dirty_restart_detected,
    )
