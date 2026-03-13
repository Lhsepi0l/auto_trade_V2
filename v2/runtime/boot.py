from __future__ import annotations

import asyncio
import json
import signal
import time
from datetime import datetime, timezone
from typing import Any

from v2 import run as run_module
from v2.clean_room import build_default_kernel
from v2.config.loader import EffectiveConfig, render_effective_config
from v2.control import build_runtime_controller
from v2.core import Event, EventBus, Scheduler
from v2.engine import EngineStateStore, OrderManager
from v2.exchange import BackoffPolicy, BinanceRESTClient
from v2.notify import Notifier
from v2.risk import KillSwitch, RiskManager
from v2.storage import RuntimeStorage
from v2.tpsl import BracketConfig, BracketPlanner

_configure_runtime_logging = run_module._configure_runtime_logging
_runtime_identity_payload = run_module._runtime_identity_payload
_dirty_runtime_marker = run_module._dirty_runtime_marker
_live_runtime_lock = run_module._live_runtime_lock
_live_trading_enabled = run_module._live_trading_enabled
_build_runtime = run_module._build_runtime
logger = run_module.logger


def build_control_balance_rest_client(
    *, cfg: EffectiveConfig, runtime_rest_client: Any | None
) -> Any | None:
    if runtime_rest_client is not None:
        return runtime_rest_client
    if not cfg.secrets.binance_api_key or not cfg.secrets.binance_api_secret:
        return None
    return BinanceRESTClient(
        env=cfg.env,
        api_key=cfg.secrets.binance_api_key,
        api_secret=cfg.secrets.binance_api_secret,
        recv_window_ms=cfg.behavior.exchange.recv_window_ms,
        time_sync_enabled=True,
        rate_limit_per_sec=cfg.behavior.exchange.request_rate_limit_per_sec,
        backoff_policy=BackoffPolicy(
            base_seconds=cfg.behavior.exchange.backoff_base_seconds,
            cap_seconds=cfg.behavior.exchange.backoff_cap_seconds,
        ),
    )


def evaluate_runtime_preflight(
    *,
    controller: Any,
    cfg: EffectiveConfig,
    host: str,
    port: int,
) -> dict[str, Any]:
    readyz = controller._readyz_snapshot()
    readiness = controller._live_readiness_snapshot()
    identity = _runtime_identity_payload(cfg, host=host, port=port)
    checks = {
        "identity": {
            "ok": True,
            "detail": {
                "profile": cfg.profile,
                "mode": cfg.mode,
                "env": cfg.env,
                "live_trading_enabled": _live_trading_enabled(cfg),
            },
        },
        "bind_localhost": {
            "ok": host == "127.0.0.1",
            "detail": {"host": host, "port": int(port)},
        },
        "readyz": {
            "ok": bool(readyz.get("ready")),
            "detail": readyz,
        },
        "readiness": {
            "ok": str(readiness.get("overall")) != "blocked",
            "detail": readiness,
        },
        "recovery_clean": {
            "ok": not bool(readyz.get("recovery_required")),
            "detail": {
                "recovery_required": bool(readyz.get("recovery_required")),
                "recovery_reason": readyz.get("recovery_reason"),
            },
        },
        "uncertainty_clean": {
            "ok": not bool(readyz.get("state_uncertain")),
            "detail": {
                "state_uncertain": bool(readyz.get("state_uncertain")),
                "reason": readyz.get("state_uncertain_reason"),
            },
        },
        "freshness_clean": {
            "ok": (not bool(readyz.get("user_ws_stale"))) and (not bool(readyz.get("market_data_stale"))),
            "detail": {
                "user_ws_stale": bool(readyz.get("user_ws_stale")),
                "market_data_stale": bool(readyz.get("market_data_stale")),
                "market_data_source_stale": bool(readyz.get("market_data_source_stale")),
                "market_data_source_error": readyz.get("market_data_source_error"),
            },
        },
        "private_auth": {
            "ok": bool(readyz.get("private_auth_ok", True)),
            "detail": {"private_auth_ok": bool(readyz.get("private_auth_ok", True))},
        },
        "paper_live_separation": {
            "ok": not (_live_trading_enabled(cfg) and str(cfg.env) != "prod"),
            "detail": {
                "mode": cfg.mode,
                "env": cfg.env,
                "live_trading_enabled": _live_trading_enabled(cfg),
            },
        },
    }
    ok = all(bool(item["ok"]) for item in checks.values())
    summary_lines = [
        f"프로필={cfg.profile} / 모드={cfg.mode} / 환경={cfg.env}",
        f"실거래활성={'예' if _live_trading_enabled(cfg) else '아니오'} / 바인드={host}:{port}",
        (
            "배포 게이트 통과"
            if ok
            else f"배포 게이트 차단: {[name for name, item in checks.items() if not bool(item['ok'])]}"
        ),
    ]
    return {
        "ok": ok,
        "identity": identity,
        "checks": checks,
        "readyz": readyz,
        "readiness": readiness,
        "summary_lines": summary_lines,
    }


def run_runtime_preflight(cfg: EffectiveConfig, *, host: str, port: int) -> int:
    _configure_runtime_logging(component="runtime_preflight")
    event_bus = EventBus()
    storage: RuntimeStorage | None = None
    state_store: EngineStateStore | None = None
    controller = None
    payload: dict[str, Any] | None = None
    runtime_state_provider = lambda: {  # noqa: E731
        **_runtime_identity_payload(cfg, host=host, port=port),
        "engine_state": state_store.get().status if state_store is not None else None,
        "last_reconcile_at": state_store.get().last_reconcile_at if state_store is not None else None,
        "readyz": controller._readyz_snapshot() if controller is not None else None,
    }
    try:
        with _live_runtime_lock(enabled=cfg.mode == "live"):
            with _dirty_runtime_marker(
                enabled=False,
                cfg=cfg,
                state_provider=runtime_state_provider,
            ):
                storage, state_store, ops, adapter, rest_client = _build_runtime(cfg)
            notifier = Notifier(
                enabled=cfg.behavior.notify.enabled,
                provider=cfg.behavior.notify.provider,
                webhook_url=cfg.secrets.notify_webhook_url,
            )
            market_data_state: dict[str, Any] = {
                "last_market_data_at": None,
                "last_market_symbol_count": 0,
                "last_market_data_source_ok_at": None,
                "last_market_data_source_fail_at": None,
                "last_market_data_source_error": None,
            }

            def _on_market_data(snapshot: dict[str, Any]) -> None:
                market_data_state["last_market_data_at"] = datetime.now(timezone.utc).isoformat()
                market_data_state["last_market_data_source_ok_at"] = market_data_state["last_market_data_at"]
                market_data_state["last_market_data_source_fail_at"] = None
                market_data_state["last_market_data_source_error"] = None
                symbols_payload = snapshot.get("symbols")
                market_data_state["last_market_symbol_count"] = (
                    len(symbols_payload) if isinstance(symbols_payload, dict) else 0
                )

            kernel = build_default_kernel(
                state_store=state_store,
                behavior=cfg.behavior,
                profile=cfg.profile,
                mode=cfg.mode,
                tick=0,
                dry_run=cfg.mode == "shadow",
                rest_client=rest_client,
                market_data_observer=_on_market_data,
                journal_logger=None,
            )
            user_stream_manager = adapter.create_user_stream_manager(cfg=cfg) if cfg.mode == "live" else None
            scheduler = Scheduler(
                tick_seconds=cfg.behavior.scheduler.tick_seconds,
                event_bus=event_bus,
            )
            balance_rest_client = build_control_balance_rest_client(
                cfg=cfg, runtime_rest_client=rest_client
            )
            controller = build_runtime_controller(
                cfg=cfg,
                state_store=state_store,
                ops=ops,
                kernel=kernel,
                scheduler=scheduler,
                event_bus=event_bus,
                notifier=notifier,
                rest_client=balance_rest_client,
                user_stream_manager=user_stream_manager,
                market_data_state=market_data_state,
                runtime_lock_active=True,
                dirty_restart_detected=False,
            )
            if cfg.mode == "live":
                asyncio.run(controller.start_live_services())
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline:
                    controller._maybe_probe_market_data()
                    readyz = controller._readyz_snapshot()
                    if bool(readyz.get("private_auth_ok")) and not bool(readyz.get("market_data_stale")):
                        if controller._user_stream_started:
                            if not bool(readyz.get("user_ws_stale")):
                                break
                        else:
                            break
                    time.sleep(0.1)
            else:
                controller._maybe_probe_market_data()

            try:
                payload = evaluate_runtime_preflight(
                    controller=controller,
                    cfg=cfg,
                    host=host,
                    port=port,
                )
                for line in payload["summary_lines"]:
                    print(f"[runtime-preflight] {line}")
                print(json.dumps(payload, ensure_ascii=True))
                return 0 if bool(payload["ok"]) else 1
            finally:
                if cfg.mode == "live":
                    asyncio.run(controller.stop_live_services())
                if storage is not None and payload is not None:
                    storage.save_runtime_marker(marker_key="runtime_preflight", payload=payload)
    except RuntimeError as exc:
        if str(exc).startswith("runtime_lock_held:"):
            print(json.dumps({"error": str(exc)}, ensure_ascii=True))
            return 1
        raise


def boot_runtime(cfg: EffectiveConfig, *, loop_enabled: bool = False, max_cycles: int = 0) -> None:
    _configure_runtime_logging(component="runtime_boot")
    storage: RuntimeStorage | None = None
    state_store: EngineStateStore | None = None
    runtime_state_provider = lambda: {  # noqa: E731
        **_runtime_identity_payload(cfg),
        "engine_state": state_store.get().status if state_store is not None else None,
        "last_reconcile_at": state_store.get().last_reconcile_at if state_store is not None else None,
    }
    with _live_runtime_lock(enabled=cfg.mode == "live"):
        with _dirty_runtime_marker(
            enabled=cfg.mode == "live",
            cfg=cfg,
            state_provider=runtime_state_provider,
        ) as dirty_restart_detected:
            logger.info(
                "runtime_boot",
                extra={
                    "event": "runtime_boot",
                    "mode": cfg.mode,
                    "profile": cfg.profile,
                    "loop_enabled": bool(loop_enabled),
                    "dirty_restart_detected": bool(dirty_restart_detected),
                },
            )
            event_bus = EventBus()
            storage, state_store, ops, adapter, rest_client = _build_runtime(cfg)
            state_store.set(mode=cfg.mode, status="RUNNING")

            if cfg.mode == "live" and dirty_restart_detected:
                ops.safe_mode()

            if cfg.behavior.ops.pause_on_start:
                ops.pause()

            risk = RiskManager(config=cfg.behavior.risk)
            _ = risk.validate_leverage(cfg.behavior.risk.max_leverage)
            kill_switch = KillSwitch()
            if kill_switch.tripped and cfg.behavior.ops.flatten_on_kill:
                _ = asyncio.run(ops.flatten(symbol=cfg.behavior.exchange.default_symbol))

            _ = adapter.ping()

            brackets = BracketPlanner(
                cfg=BracketConfig(
                    take_profit_pct=cfg.behavior.tpsl.take_profit_pct,
                    stop_loss_pct=cfg.behavior.tpsl.stop_loss_pct,
                )
            )
            _ = brackets.levels(entry_price=100.0)

            notifier = Notifier(
                enabled=cfg.behavior.notify.enabled,
                provider=cfg.behavior.notify.provider,
                webhook_url=cfg.secrets.notify_webhook_url,
            )
            notifier.send("v2 boot completed")

            journal_logger = None
            enabled_strategies = [
                str(entry.name).strip()
                for entry in cfg.behavior.strategies
                if bool(getattr(entry, "enabled", False))
            ]
            active_strategy_name = enabled_strategies[0] if enabled_strategies else "none"

            if cfg.mode == "shadow":

                def _journal_logger(payload: dict[str, Any]) -> None:
                    print(
                        json.dumps(
                            {
                                "strategy": active_strategy_name,
                                "regime": payload.get("regime"),
                                "allowed_side": payload.get("allowed_side"),
                                "signals": payload.get("signals"),
                                "blocks": payload.get("blocks"),
                                "decision": payload.get("decision", {}).get("intent")
                                if isinstance(payload.get("decision"), dict)
                                else None,
                                "reasons": payload.get("decision", {}).get("reason")
                                if isinstance(payload.get("decision"), dict)
                                else None,
                            },
                            ensure_ascii=True,
                        )
                    )

                journal_logger = _journal_logger

            order_manager = OrderManager(event_bus=event_bus)

            kernel = build_default_kernel(
                state_store=state_store,
                behavior=cfg.behavior,
                profile=cfg.profile,
                mode=cfg.mode,
                tick=0,
                dry_run=cfg.mode == "shadow",
                rest_client=rest_client,
                journal_logger=journal_logger,
            )
            scheduler = Scheduler(
                tick_seconds=cfg.behavior.scheduler.tick_seconds,
                event_bus=event_bus,
            )

            def _on_scheduler_tick(event: Event) -> None:
                _ = storage.append_journal(
                    event_type=event.topic,
                    payload_json=render_effective_config(cfg),
                )

            event_bus.subscribe(
                "scheduler.tick",
                _on_scheduler_tick,
            )
            stop_requested = False

            def _request_stop(_sig: int, _frame: object | None) -> None:
                nonlocal stop_requested
                stop_requested = True

            for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
                if sig is None:
                    continue
                try:
                    signal.signal(sig, _request_stop)
                except (OSError, RuntimeError, ValueError):
                    continue

            cycles = 0
            while True:
                if hasattr(kernel, "set_tick"):
                    kernel.set_tick(cycles)
                cycle = kernel.run_once()
                event_bus.publish(
                    Event(
                        topic="kernel.cycle",
                        payload={
                            "state": cycle.state,
                            "reason": cycle.reason,
                            "symbol": cycle.candidate.symbol if cycle.candidate is not None else None,
                        },
                    )
                )
                if cycle.state in {"executed", "execution_failed"}:
                    notifier.send(f"v2 cycle {cycle.state}: {cycle.reason}")

                scheduler.run_once()

                if ops.can_open_new_entries():
                    order_manager.submit({"symbol": cfg.behavior.exchange.default_symbol, "mode": cfg.mode})
                else:
                    event_bus.publish(
                        Event(
                            topic="order.entry_blocked",
                            payload={"symbol": cfg.behavior.exchange.default_symbol, "paused": True},
                        )
                    )
                cycles += 1
                if not loop_enabled:
                    break
                if max_cycles > 0 and cycles >= max_cycles:
                    break
                if stop_requested:
                    break
                asyncio.run(asyncio.sleep(max(0.2, float(cfg.behavior.scheduler.tick_seconds))))

            event_bus.publish(Event(topic="v2.started", payload={"profile": cfg.profile, "env": cfg.env}))
