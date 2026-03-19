from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from v2 import run as run_module
from v2.clean_room import build_default_kernel
from v2.config.loader import EffectiveConfig
from v2.control import build_runtime_controller, create_control_http_app
from v2.core import EventBus, Scheduler
from v2.engine import EngineStateStore
from v2.notify import Notifier
from v2.ops import create_ops_http_app
from v2.runtime.boot import build_control_balance_rest_client

_configure_runtime_logging = run_module._configure_runtime_logging
_runtime_identity_payload = run_module._runtime_identity_payload
_dirty_runtime_marker = run_module._dirty_runtime_marker
_live_runtime_lock = run_module._live_runtime_lock
_build_runtime = run_module._build_runtime
logger = run_module.logger
_build_control_balance_rest_client = build_control_balance_rest_client


def serve_ops_http(cfg: EffectiveConfig, *, host: str, port: int) -> int:
    import uvicorn

    _storage, _state_store, ops, _adapter, _rest = _build_runtime(cfg)
    app = create_ops_http_app(ops=ops)
    uvicorn.run(app, host=host, port=port)
    return 0


def serve_control_http(
    cfg: EffectiveConfig,
    *,
    host: str,
    port: int,
    enable_operator_web: bool = False,
) -> int:
    import uvicorn

    try:
        _configure_runtime_logging(component="control_runtime")
        state_store: EngineStateStore | None = None
        controller = None
        runtime_state_provider = lambda: {  # noqa: E731
            **_runtime_identity_payload(cfg, host=host, port=port),
            "engine_state": state_store.get().status if state_store is not None else None,
            "last_reconcile_at": state_store.get().last_reconcile_at if state_store is not None else None,
            "readyz": controller._readyz_snapshot() if controller is not None else None,
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
                        "host": host,
                        "port": port,
                        "dirty_restart_detected": bool(dirty_restart_detected),
                    },
                )
                event_bus = EventBus()
                storage, state_store, ops, adapter, rest_client = _build_runtime(cfg)
                state_store.set(mode=cfg.mode, status="STOPPED")

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
                _ = storage
                user_stream_manager = None
                if cfg.mode == "live":
                    user_stream_manager = adapter.create_user_stream_manager(cfg=cfg)
                scheduler = Scheduler(
                    tick_seconds=cfg.behavior.scheduler.tick_seconds,
                    event_bus=event_bus,
                )
                balance_rest_client = _build_control_balance_rest_client(
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
                    dirty_restart_detected=bool(dirty_restart_detected),
                )
                app = create_control_http_app(
                    controller=controller,
                    enable_operator_web=enable_operator_web,
                )
                uvicorn.run(app, host=host, port=port)
    except RuntimeError as exc:
        if str(exc).startswith("runtime_lock_held:"):
            print(json.dumps({"error": str(exc)}, ensure_ascii=True))
            return 1
        raise
    return 0
