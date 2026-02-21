from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from v2.clean_room.contracts import KernelCycleResult
from v2.config.loader import EffectiveConfig
from v2.core import EventBus, Scheduler
from v2.engine import EngineStateStore, OrderManager
from v2.notify import Notifier
from v2.ops import OpsController

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


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


def _run_async_blocking(thunk: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(thunk())

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, thunk())
        return future.result()


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
        if str(self.notifier.provider or "none").strip().lower() == "none" and str(self.notifier.webhook_url or "").strip():
            self.notifier.provider = "discord"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_stop = threading.Event()
        self._status_thread_stop = threading.Event()
        self._status_thread: threading.Thread | None = None
        self._running = False
        self._last_cycle: dict[str, Any] = {
            "tick_started_at": None,
            "tick_finished_at": None,
            "last_action": "-",
            "last_decision_reason": "-",
            "last_error": None,
            "candidate": None,
            "last_candidate": None,
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
        self.state_store.set(mode=self.cfg.mode, status="STOPPED")
        self._start_status_loop()

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
        live_available_usdt, live_wallet_usdt = self._fetch_live_usdt_balance()
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
            "watchdog": {},
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
                "enabled_symbols": list(self._risk.get("universe_symbols") or [self.cfg.behavior.exchange.default_symbol]),
                "positions": positions_payload,
                "usdt_balance": {"wallet": wallet_usdt, "available": available_usdt},
                "startup_error": None,
                "private_error": None,
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

    def _fetch_live_usdt_balance(self) -> tuple[float | None, float | None]:
        if self.rest_client is None:
            return None, None
        rest_client: Any = self.rest_client
        assert rest_client is not None
        try:
            payload = _run_async_blocking(lambda: rest_client.get_balances())
        except Exception:  # noqa: BLE001
            logger.exception("live_balance_fetch_failed")
            return None, None

        if not isinstance(payload, list):
            return None, None

        target: dict[str, Any] | None = None
        for item in payload:
            if not isinstance(item, dict):
                continue
            asset = str(item.get("asset") or item.get("coin") or "").upper()
            if asset == "USDT":
                target = item
                break

        if target is None:
            return None, None

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
        return available, wallet

    def _run_cycle_once_locked(self) -> dict[str, Any]:
        self._last_cycle["tick_started_at"] = _utcnow_iso()
        try:
            cycle: KernelCycleResult = self.kernel.run_once()
            self.scheduler.run_once()
            if self.ops.can_open_new_entries():
                self.order_manager.submit({"symbol": self.cfg.behavior.exchange.default_symbol, "mode": self.cfg.mode})

            self._last_cycle["tick_finished_at"] = _utcnow_iso()
            self._last_cycle["last_action"] = cycle.state
            self._last_cycle["last_decision_reason"] = cycle.reason
            self._last_cycle["candidate"] = {
                "symbol": cycle.candidate.symbol,
                "side": cycle.candidate.side,
                "score": cycle.candidate.score,
            } if cycle.candidate is not None else None
            self._last_cycle["last_candidate"] = self._last_cycle["candidate"]
            self._last_cycle["last_error"] = cycle.reason if cycle.state in {"blocked", "risk_rejected", "execution_failed"} else None

            self._report_stats["total_records"] += 1
            if cycle.state in {"executed", "dry_run"}:
                self._report_stats["entries"] += 1
            if cycle.state == "execution_failed":
                self._report_stats["errors"] += 1
            if cycle.state in {"blocked", "risk_rejected"}:
                self._report_stats["blocks"] += 1
            ok = True
            error_message = None
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
            self._report_stats["total_records"] += 1
            self._report_stats["errors"] += 1
            ok = False

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
        return (
            f"status update: state={self.state_store.get().status}, "
            f"last_action={self._last_cycle.get('last_action')}, "
            f"reason={self._last_cycle.get('last_decision_reason')}"
        )

    def _emit_status_update(self, *, force: bool = False) -> bool:
        notify_interval = max(1, int(_to_float(self._risk.get("notify_interval_sec"), default=30.0)))
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
                interval = max(1, int(_to_float(self._risk.get("notify_interval_sec"), default=30.0)))
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
        with self._lock:
            self._running = False
            self._thread_stop.set()
            self.ops.pause()
            self.state_store.set(status="PAUSED")
            self._emit_status_update(force=True)
            state = self.state_store.get()
            return {"state": state.status, "updated_at": state.last_transition_at}

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
        self._risk[key] = parsed
        if key == "notify_interval_sec":
            self.scheduler.tick_seconds = int(_to_float(parsed, default=float(self.scheduler.tick_seconds)))
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
        self._risk["notify_interval_sec"] = sec
        return {
            "tick_sec": float(self.scheduler.tick_seconds),
            "running": bool(self._running),
            "min_tick_sec": 1.0,
        }

    def tick_scheduler_now(self) -> dict[str, Any]:
        with self._lock:
            return self._run_cycle_once_locked()

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
        symbols = set(self._risk.get("universe_symbols") or [self.cfg.behavior.exchange.default_symbol])
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
        return controller._status_snapshot()

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
