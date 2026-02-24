from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel

from v2.clean_room.contracts import KernelCycleResult
from v2.common.async_bridge import run_async_blocking
from v2.config.loader import EffectiveConfig
from v2.core import EventBus, Scheduler
from v2.engine import EngineStateStore, OrderManager
from v2.exchange import BinanceRESTError
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
    "no_candidate": "현재 진입 후보가 없습니다",
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
    "network_error": "네트워크 오류",
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
    ) -> None:
        self.cfg = cfg
        self.state_store = state_store
        self.ops = ops
        self.kernel = kernel
        self.scheduler = scheduler
        self.order_manager = order_manager
        self.notifier = notifier
        self.rest_client = rest_client
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
        self._trailing_state: dict[str, dict[str, Any]] = {}
        self._watchdog_state: dict[str, Any] = {}
        self._cycle_seq = 0
        self._cycle_done_seq = 0
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
        self._recover_brackets_on_boot()
        self._start_bracket_loop()
        self._start_status_loop()
        self._sync_kernel_runtime_overrides()

    def _recover_brackets_on_boot(self) -> None:
        if self.cfg.mode != "live" or self.rest_client is None:
            return
        try:
            _ = _run_async_blocking(
                lambda: self._bracket_service.recover(),
                timeout_sec=10.0,
            )
        except FutureTimeoutError:
            logger.warning("bracket_recover_timed_out")
        except Exception:  # noqa: BLE001
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
        symbols_raw = self._risk.get("universe_symbols")
        symbols: list[str]
        if isinstance(symbols_raw, list):
            symbols = [str(sym).strip().upper() for sym in symbols_raw if str(sym).strip()]
        else:
            symbols = [self.cfg.behavior.exchange.default_symbol]
        if not symbols:
            symbols = [self.cfg.behavior.exchange.default_symbol]

        max_leverage = max(1.0, _to_float(self._risk.get("max_leverage"), default=1.0))
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

        if hasattr(self.kernel, "set_universe_symbols"):
            self.kernel.set_universe_symbols(symbols)  # type: ignore[attr-defined]
        if hasattr(self.kernel, "set_symbol_leverage_map"):
            self.kernel.set_symbol_leverage_map(  # type: ignore[attr-defined]
                mapping,
                max_leverage=max_leverage,
            )

    def _initial_risk_config(self) -> dict[str, Any]:
        risk_cfg = self.cfg.behavior.risk
        tpsl_cfg = self.cfg.behavior.tpsl
        sched_sec = int(self.cfg.behavior.scheduler.tick_seconds)
        return {
            "per_trade_risk_pct": 10.0,
            "max_exposure_pct": float(risk_cfg.max_exposure_pct),
            "max_notional_pct": 1000.0,
            "max_leverage": float(risk_cfg.max_leverage),
            "daily_loss_limit_pct": float(risk_cfg.daily_loss_limit_pct),
            "dd_limit_pct": float(risk_cfg.dd_limit_pct),
            "lose_streak_n": 3,
            "cooldown_hours": 2,
            "min_hold_minutes": 0,
            "score_conf_threshold": 0.6,
            "score_gap_threshold": 0.15,
            "exec_mode_default": "MARKET",
            "exec_limit_timeout_sec": 3.0,
            "exec_limit_retries": 2,
            "scheduler_tick_sec": sched_sec,
            "notify_interval_sec": sched_sec,
            "spread_max_pct": 0.5,
            "allow_market_when_wide_spread": False,
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
            "tf_weight_4h": 0.25,
            "tf_weight_1h": 0.25,
            "tf_weight_30m": 0.25,
            "tf_weight_10m": 0.25,
            "tf_weight_15m": 0.0,
            "score_tf_15m_enabled": False,
            "vol_shock_atr_mult_threshold": 0.0,
            "atr_mult_mean_window": 0,
            "symbol_leverage_map": {},
        }

    def _status_snapshot(self) -> dict[str, Any]:
        state = self.state_store.get()
        positions_payload: dict[str, dict[str, Any]] = {}
        for symbol, row in state.current_position.items():
            positions_payload[symbol] = {
                "position_amt": row.position_amt,
                "entry_price": row.entry_price,
                "unrealized_pnl": row.unrealized_pnl,
                "position_side": "LONG" if row.position_amt > 0 else "SHORT",
            }
        budget = _to_float(self._risk.get("margin_budget_usdt"), default=100.0)
        live_available_usdt, live_wallet_usdt, live_balance_source = self._fetch_live_usdt_balance()
        available_usdt = live_available_usdt if live_available_usdt is not None else budget
        wallet_usdt = live_wallet_usdt if live_wallet_usdt is not None else budget
        config = dict(self._risk)
        config_summary = dict(self._risk)
        config_summary["scheduler_tick_sec"] = float(self.scheduler.tick_seconds)
        config_summary["scheduler_running"] = bool(self._running)
        config_summary["scheduler_enabled"] = bool(self._running)
        config_summary["active_scoring_timeframes"] = ["10m", "30m", "1h", "4h"]
        config_summary["scoring_weights"] = {
            "10m": self._risk.get("tf_weight_10m", 0.25),
            "15m": self._risk.get("tf_weight_15m", 0.0),
            "30m": self._risk.get("tf_weight_30m", 0.25),
            "1h": self._risk.get("tf_weight_1h", 0.25),
            "4h": self._risk.get("tf_weight_4h", 0.25),
        }
        return {
            "dry_run": self.cfg.mode == "shadow",
            "dry_run_strict": False,
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
                "budget_usdt": budget,
                "used_margin": 0.0,
                "leverage": float(self._risk.get("max_leverage", 1.0)),
                "notional_usdt": budget,
                "mark_price": 0.0,
                "est_qty": 0.0,
                "blocked": not self.ops.can_open_new_entries(),
                "block_reason": "ops_paused" if not self.ops.can_open_new_entries() else None,
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
                "startup_error": None,
                "private_error": self._last_balance_error,
                "private_error_detail": self._last_balance_error_detail,
            },
            "pnl": {
                "daily_pnl_pct": 0.0,
                "drawdown_pct": 0.0,
                "lose_streak": 0,
                "cooldown_until": None,
                "daily_realized_pnl": 0.0,
            },
            "last_error": self._last_cycle.get("last_error"),
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

            filled_id = sl_id if tp_open else tp_id
            try:
                _ = _run_async_blocking(
                    lambda s=symbol, cid=filled_id: self._bracket_service.on_leg_filled(
                        symbol=s,
                        filled_client_algo_id=cid,
                    ),
                    timeout_sec=8.0,
                )
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
            cycle: KernelCycleResult = self.kernel.run_once()
            self.scheduler.run_once()
            if self.ops.can_open_new_entries() and self._running and not self._thread_stop.is_set():
                submit_symbol = (
                    cycle.candidate.symbol
                    if cycle.candidate is not None and str(cycle.candidate.symbol).strip()
                    else self.cfg.behavior.exchange.default_symbol
                )
                self.order_manager.submit({"symbol": submit_symbol, "mode": self.cfg.mode})

            self._place_brackets_for_cycle(cycle=cycle)

            self._last_cycle["tick_finished_at"] = _utcnow_iso()
            self._last_cycle["last_action"] = cycle.state
            self._last_cycle["last_decision_reason"] = cycle.reason
            self._last_cycle["candidate"] = (
                {
                    "symbol": cycle.candidate.symbol,
                    "side": cycle.candidate.side,
                    "score": cycle.candidate.score,
                    "regime_hint": getattr(cycle.candidate, "regime_hint", None),
                    "volatility_hint": getattr(cycle.candidate, "volatility_hint", None),
                }
                if cycle.candidate is not None
                else None
            )
            self._last_cycle["last_candidate"] = self._last_cycle["candidate"]
            cycle_error = (
                cycle.reason
                if cycle.state in {"blocked", "risk_rejected", "execution_failed"}
                else None
            )
            existing_error = str(self._last_cycle.get("last_error") or "").strip()
            self._last_cycle["last_error"] = existing_error or cycle_error

            self._report_stats["total_records"] += 1
            if cycle.state in {"executed", "dry_run"}:
                self._report_stats["entries"] += 1
            if cycle.state == "execution_failed":
                self._report_stats["errors"] += 1
            if cycle.state in {"blocked", "risk_rejected"}:
                self._report_stats["blocks"] += 1
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
        last_action = self._translate_status_token(
            str(self._last_cycle.get("last_action") or "-"), _ACTION_LABELS_KO
        )
        reason = self._translate_status_token(
            str(self._last_cycle.get("last_decision_reason") or "-"), _REASON_LABELS_KO
        )
        pnl_summary = self._status_pnl_summary()
        return f"상태 알림: 엔진={state_ko}, 마지막판단={last_action}, 사유={reason}, {pnl_summary}"

    @staticmethod
    def _fmt_signed(value: float) -> str:
        return f"{float(value):+.4f}"

    def _status_pnl_summary(self) -> str:
        state = self.state_store.get()
        total_unrealized = 0.0
        per_symbol: list[str] = []
        for symbol, row in sorted(state.current_position.items()):
            pnl = _to_float(row.unrealized_pnl, default=0.0)
            total_unrealized += pnl
            per_symbol.append(f"{symbol}:{self._fmt_signed(pnl)}")

        parts = [f"미실현PnL={self._fmt_signed(total_unrealized)} USDT"]
        if per_symbol:
            preview = ", ".join(per_symbol[:3])
            if len(per_symbol) > 3:
                preview = f"{preview}, ..."
            parts.append(f"포지션별={preview}")

        latest_realized: float | None = None
        for fill in state.last_fills:
            if fill.realized_pnl is None:
                continue
            latest_realized = _to_float(fill.realized_pnl, default=0.0)
            break
        if latest_realized is not None:
            parts.append(f"최근실현PnL={self._fmt_signed(latest_realized)} USDT")

        return ", ".join(parts)

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
                if self._running:
                    continue
                self._emit_status_update(force=True)

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
            self.state_store.set(status="RUNNING")
            self.ops.resume()
            if self._running:
                state = self.state_store.get()
                return {"state": state.status, "updated_at": state.last_transition_at}
            self._running = True
            self._thread_stop.clear()
            self._thread = threading.Thread(target=self._loop_worker, daemon=True)
            self._thread.start()
            state = self.state_store.get()
            return {"state": state.status, "updated_at": state.last_transition_at}

    def stop(self) -> dict[str, Any]:
        acquired = self._lock.acquire(timeout=1.0)
        try:
            self._running = False
            self._thread_stop.set()
            self.ops.pause()
            self.state_store.set(status="PAUSED")
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
        self._emit_status_update(force=True)
        result = await self.ops.flatten(symbol=self.cfg.behavior.exchange.default_symbol)
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
        return dict(self._risk)

    def set_value(self, *, key: str, value: str) -> dict[str, Any]:
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
        symbol_u = symbol.strip().upper()
        mapping = self._risk.get("symbol_leverage_map")
        if not isinstance(mapping, dict):
            mapping = {}
        if leverage <= 0:
            mapping.pop(symbol_u, None)
        else:
            mapping[symbol_u] = float(leverage)
        self._risk["symbol_leverage_map"] = mapping
        self._sync_kernel_runtime_overrides()
        self._persist_risk_config()
        return dict(self._risk)

    def get_scheduler(self) -> dict[str, Any]:
        return {
            "tick_sec": float(self.scheduler.tick_seconds),
            "running": bool(self._running),
            "min_tick_sec": 1.0,
        }

    def set_scheduler_interval(self, tick_sec: float) -> dict[str, Any]:
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
        return {
            "ok": False,
            "tick_sec": float(self.scheduler.tick_seconds),
            "snapshot": dict(self._last_cycle),
            "error": "tick_busy",
        }

    def send_daily_report(self) -> dict[str, Any]:
        payload = {
            "kind": "DAILY_REPORT",
            "day": datetime.now(timezone.utc).date().isoformat(),
            "engine_state": self.state_store.get().status,
            "detail": dict(self._report_stats),
            "notifier_sent": bool(self.cfg.behavior.notify.enabled),
            "notifier_error": None,
            "reported_at": _utcnow_iso(),
        }
        self.notifier.send(f"daily report: {payload['detail']}")
        return payload

    def preset(self, name: str) -> dict[str, Any]:
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
        return {
            "day": datetime.now(timezone.utc).date().isoformat(),
            "daily_realized_pnl": 0.0,
            "equity_peak": 0.0,
            "daily_pnl_pct": 0.0,
            "drawdown_pct": 0.0,
            "lose_streak": 0,
            "cooldown_until": None,
            "last_block_reason": None,
            "last_fill_symbol": None,
            "last_fill_side": None,
            "last_fill_qty": None,
            "last_fill_price": None,
            "last_fill_realized_pnl": None,
            "last_fill_time": None,
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
    app = FastAPI(title="auto-trader-v2-control", version="0.1.0")

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return await asyncio.to_thread(controller._status_snapshot)

    @app.get("/risk")
    async def risk() -> dict[str, Any]:
        return controller.get_risk()

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
    )
