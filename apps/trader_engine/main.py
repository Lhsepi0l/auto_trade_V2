from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping, Optional

from fastapi import FastAPI

from apps.trader_engine.api.routes import router
from apps.trader_engine.config import load_settings
from apps.trader_engine.exchange.binance_usdm import BinanceCredentials, BinanceUSDMClient
from apps.trader_engine.exchange.time_sync import TimeSync
from apps.trader_engine.logging_setup import LoggingConfig, setup_logging
from apps.trader_engine.services.binance_service import BinanceService
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.execution_service import ExecutionService
from apps.trader_engine.services.market_data_service import MarketDataService
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.ai_service import AiService
from apps.trader_engine.services.notifier_service import build_notifier
from apps.trader_engine.services.risk_service import RiskService
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.services.scoring_service import ScoringService
from apps.trader_engine.services.reconcile_service import ReconcileService
from apps.trader_engine.services.sizing_service import SizingService
from apps.trader_engine.services.strategy_service import StrategyService
from apps.trader_engine.services.user_stream_service import UserStreamService
from apps.trader_engine.services.watchdog_service import WatchdogService
from apps.trader_engine.services.oplog import OperationalLogger
from apps.trader_engine.services.snapshot_service import SnapshotService
from apps.trader_engine.storage.db import close, connect, migrate
from apps.trader_engine.storage.repositories import (
    EngineStateRepo,
    OrderRecordRepo,
    PnLStateRepo,
    RiskConfigRepo,
    StatusSnapshotRepo,
)
from apps.trader_engine.scheduler import TraderScheduler

logger = logging.getLogger(__name__)


def _build_lifespan(
    *,
    forced_test_mode: Optional[bool] = None,
    test_overrides: Optional[Mapping[str, Any]] = None,
):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = load_settings()
        if forced_test_mode is not None:
            settings = settings.model_copy(update={"test_mode": bool(forced_test_mode)})
        setup_logging(LoggingConfig(level=settings.log_level, log_dir=settings.log_dir, json=settings.log_json, component="engine"))

        db = connect(settings.db_path)
        migrate(db)

        engine_state_repo = EngineStateRepo(db)
        risk_config_repo = RiskConfigRepo(db)
        _status_snapshot_repo = StatusSnapshotRepo(db)  # reserved for later wiring
        pnl_state_repo = PnLStateRepo(db)
        order_record_repo = OrderRecordRepo(db)
        oplog = OperationalLogger.create(db=db, component="engine")

        engine_service = EngineService(engine_state_repo=engine_state_repo)
        risk_config_service = RiskConfigService(risk_config_repo=risk_config_repo)
        pnl_service = PnLService(repo=pnl_state_repo)
    # Ensure defaults exist at boot.
        _ = engine_service.get_state()
        cfg = risk_config_service.get_config()
        _ = pnl_service.get_or_bootstrap()

    # Binance USDT-M Futures (조회 전용)
        binance_client = BinanceUSDMClient(
            BinanceCredentials(api_key=settings.binance_api_key, api_secret=settings.binance_api_secret),
            base_url=settings.binance_base_url,
            time_sync=TimeSync(),
            timeout_sec=settings.request_timeout_sec,
            retry_count=settings.retry_count,
            retry_backoff=settings.retry_backoff,
            recv_window_ms=settings.binance_recv_window_ms,
        )
        if test_overrides and test_overrides.get("binance_client") is not None:
            binance_client = test_overrides["binance_client"]
        snapshot_service = SnapshotService(db=db, client=binance_client, pnl=pnl_service, oplog=oplog)
        binance_service = BinanceService(
            client=binance_client,
            allowed_symbols=cfg.universe_symbols,
            spread_wide_pct=cfg.spread_max_pct,
        )
        if not (settings.test_mode and test_overrides and test_overrides.get("skip_binance_startup")):
            binance_service.startup()

        policy = RiskService(
            risk=risk_config_service,
            engine=engine_service,
            pnl=pnl_service,
            stop_on_daily_loss=bool(settings.risk_stop_on_daily_loss),
        )

        notifier = build_notifier(settings.discord_webhook_url)
        if test_overrides and test_overrides.get("notifier") is not None:
            notifier = test_overrides["notifier"]

        sizing_service = SizingService(client=binance_client)
        reconcile_service = ReconcileService(
            client=binance_client,
            risk=risk_config_service,
            engine=engine_service,
            order_records=order_record_repo,
            oplog=oplog,
        )

        execution_service = ExecutionService(
            client=binance_client,
            engine=engine_service,
            risk=risk_config_service,
            pnl=pnl_service,
            policy=policy,
            notifier=notifier,
            sizing=sizing_service,
            allowed_symbols=binance_service.enabled_symbols,
            split_parts=settings.exec_split_parts,
            dry_run=bool(settings.trading_dry_run),
            dry_run_strict=bool(settings.dry_run_strict),
            oplog=oplog,
            snapshot=snapshot_service,
            order_records=order_record_repo,
        )

        market_data_service = MarketDataService(
        client=binance_client,
        cache_ttl_sec=20.0,
        retry_attempts=settings.retry_count,
        retry_backoff_sec=settings.retry_backoff,
    )
        scoring_service = ScoringService()
        strategy_service = StrategyService()
        ai_service = AiService(
        mode=settings.ai_mode,
        conf_threshold=settings.ai_conf_threshold,
        manual_risk_tag=settings.manual_risk_tag,
    )
        scheduler = TraderScheduler(
        engine=engine_service,
        risk=risk_config_service,
        pnl=pnl_service,
        binance=binance_service,
        market_data=market_data_service,
        scoring=scoring_service,
        strategy=strategy_service,
        ai=ai_service,
        sizing=sizing_service,
        execution=execution_service,
        notifier=notifier,
        tick_sec=float(settings.scheduler_tick_sec),
        reverse_threshold=float(settings.reverse_threshold),
        oplog=oplog,
        snapshot=snapshot_service,
    )
        watchdog = WatchdogService(
        client=binance_client,
        engine=engine_service,
        risk=risk_config_service,
        execution=execution_service,
        notifier=notifier,
        oplog=oplog,
        market_data=market_data_service,
        reconcile=reconcile_service,
    )
        user_stream = UserStreamService(
        client=binance_client,
        engine=engine_service,
        pnl=pnl_service,
        execution=execution_service,
        notifier=notifier,
        snapshot=snapshot_service,
        order_records=order_record_repo,
        reconcile=reconcile_service,
    )

        app.state.settings = settings
        app.state.test_mode = bool(settings.test_mode)
        app.state.notifier = notifier
        app.state.db = db
        app.state.engine_service = engine_service
        app.state.risk_config_service = risk_config_service
        app.state.pnl_service = pnl_service
        app.state.risk_service = policy
        app.state.binance_service = binance_service
        app.state.execution_service = execution_service
        app.state.market_data_service = market_data_service
        app.state.scoring_service = scoring_service
        app.state.strategy_service = strategy_service
        app.state.ai_service = ai_service
        app.state.sizing_service = sizing_service
        app.state.scheduler = scheduler
        app.state.watchdog = watchdog
        app.state.user_stream = user_stream
        app.state.oplog = oplog
        app.state.snapshot_service = snapshot_service
        app.state.order_record_repo = order_record_repo
        app.state.scheduler_snapshot = None
        app.state.reconcile_service = reconcile_service

        logger.info("api_boot", extra={"db_path": settings.db_path, "test_mode": bool(settings.test_mode)})
        try:
            oplog.log_event("ENGINE_BOOT", {"action": "boot", "test_mode": bool(settings.test_mode)})
        except Exception:
            logger.exception("oplog_boot_event_failed")
        try:
            auto_start_bg = not bool(settings.test_mode and test_overrides and test_overrides.get("disable_background_tasks"))
            # Recovery lock: block new entries until startup reconcile succeeds.
            engine_service.set_recovery_lock(True)
            reconcile_ok = await asyncio.to_thread(reconcile_service.startup_reconcile)
            logger.info("startup_reconcile_done", extra={"ok": bool(reconcile_ok)})
            if auto_start_bg:
                if bool(settings.scheduler_enabled):
                    scheduler.start()
                    logger.info(
                        "scheduler_started",
                        extra={"tick_sec": settings.scheduler_tick_sec, "score_threshold": settings.score_threshold},
                    )
                user_stream.start()
                logger.info("user_stream_started")
                watchdog.start()
                logger.info(
                    "watchdog_started",
                    extra={"interval_sec": cfg.watchdog_interval_sec, "enabled": cfg.enable_watchdog},
                )
            yield
        finally:
            try:
                try:
                    await user_stream.stop()
                except Exception:
                    pass
                try:
                    await watchdog.stop()
                except Exception:
                    pass
                try:
                    await scheduler.stop()
                except Exception:
                    pass
                if hasattr(binance_service, "close"):
                    binance_service.close()
            except Exception:
                pass
            try:
                oplog.log_event("ENGINE_SHUTDOWN", {"action": "shutdown"})
            except Exception:
                pass
            close(db)

    return lifespan


def create_app(*, test_mode: Optional[bool] = None, test_overrides: Optional[Mapping[str, Any]] = None) -> FastAPI:
    app = FastAPI(
        title="auto-trader control api",
        version="0.2.0",
        lifespan=_build_lifespan(forced_test_mode=test_mode, test_overrides=test_overrides),
    )
    app.include_router(router)
    return app


# FastAPI entrypoint for uvicorn:
#   uvicorn apps.trader_engine.main:app --reload
app = create_app()


def main() -> int:
    parser = argparse.ArgumentParser(prog="auto-trader")
    parser.add_argument("--api", action="store_true", help="run control API via uvicorn")
    args = parser.parse_args()

    if not args.api:
        # Simple non-server boot: initializes DB + defaults then exits.
        settings = load_settings()
        setup_logging(LoggingConfig(level=settings.log_level, log_dir=settings.log_dir, json=settings.log_json, component="engine"))
        db = connect(settings.db_path)
        migrate(db)
        try:
            EngineService(engine_state_repo=EngineStateRepo(db))
            RiskConfigService(risk_config_repo=RiskConfigRepo(db)).get_config()
            logger.info("boot_ok", extra={"db_path": settings.db_path})
            return 0
        finally:
            close(db)

    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "apps.trader_engine.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
