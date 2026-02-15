from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.api.schemas import (
    CapitalSnapshotSchema,
    CapitalConfigSnapshotSchema,
    EngineStateSchema,
    FieldValidationErrorSchema,
    PnLStatusSchema,
    PresetRequest,
    RiskConfigSchema,
    SetValueResponse,
    SetValueRequest,
    StatusResponse,
    SchedulerSnapshotSchema,
    WatchdogStatusSchema,
    PanicResponseSchema,
    PanicResultSchema,
    TradeCloseRequest,
    TradeEnterRequest,
    TradeResult,
)
from apps.trader_engine.services.binance_service import BinanceService
from apps.trader_engine.services.engine_service import EngineConflict, EngineService
from apps.trader_engine.services.execution_service import (
    ExecutionRejected,
    ExecutionService,
    ExecutionValidationError,
)
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.risk_config_service import RiskConfigService, RiskConfigValidationError
from apps.trader_engine.services.sizing_service import SizingService
from apps.trader_engine.services.snapshot_service import SnapshotService

logger = logging.getLogger(__name__)

router = APIRouter()


def _parse_set_validation_errors(raw: str) -> list[FieldValidationErrorSchema]:
    txt = str(raw or "").strip()
    if not txt:
        return [FieldValidationErrorSchema(field="config", message="invalid_value")]
    parts = [p.strip() for p in txt.split(";") if p.strip()]
    out: list[FieldValidationErrorSchema] = []
    for p in parts:
        if ":" in p:
            field, msg = p.split(":", 1)
            out.append(FieldValidationErrorSchema(field=field.strip(), message=msg.strip()))
            continue
        if "_for_" in p:
            # e.g. invalid_float_for_capital_pct
            left, right = p.rsplit("_for_", 1)
            out.append(FieldValidationErrorSchema(field=right.strip(), message=left.strip()))
            continue
        out.append(FieldValidationErrorSchema(field="config", message=p))
    return out


def _engine_service(request: Request) -> EngineService:
    return request.app.state.engine_service  # type: ignore[attr-defined]


def _risk_service(request: Request) -> RiskConfigService:
    return request.app.state.risk_config_service  # type: ignore[attr-defined]


def _binance_service(request: Request) -> BinanceService:
    return request.app.state.binance_service  # type: ignore[attr-defined]


def _execution_service(request: Request) -> ExecutionService:
    return request.app.state.execution_service  # type: ignore[attr-defined]


def _pnl_service(request: Request) -> PnLService:
    return request.app.state.pnl_service  # type: ignore[attr-defined]


def _sizing_service(request: Request) -> SizingService:
    return request.app.state.sizing_service  # type: ignore[attr-defined]


def _snapshot_service(request: Request) -> SnapshotService:
    return request.app.state.snapshot_service  # type: ignore[attr-defined]


def _oplog(request: Request):  # type: ignore[no-untyped-def]
    return getattr(request.app.state, "oplog", None)


def _require_test_mode(request: Request) -> None:
    if not bool(getattr(request.app.state, "test_mode", False)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")


@router.get("/", include_in_schema=False)
def root() -> Dict[str, Any]:
    return {"ok": True, "hint": "see /docs, /health, /status"}


@router.get("/health")
def health() -> dict:
    return {"ok": True}


@router.get("/status", response_model=StatusResponse)
def get_status(
    request: Request,
    engine: EngineService = Depends(_engine_service),
    risk: RiskConfigService = Depends(_risk_service),
    binance: BinanceService = Depends(_binance_service),
    pnl: PnLService = Depends(_pnl_service),
    sizing: SizingService = Depends(_sizing_service),
    snapshots: SnapshotService = Depends(_snapshot_service),
) -> StatusResponse:
    state = engine.get_state()
    cfg = risk.get_config()
    b = binance.get_status()
    settings = getattr(request.app.state, "settings", None)

    pnl_payload = None
    try:
        st = pnl.get_or_bootstrap()
        bal = (b.get("usdt_balance") or {}) if isinstance(b, dict) else {}
        pos = (b.get("positions") or {}) if isinstance(b, dict) else {}
        wallet = float(bal.get("wallet") or 0.0)
        upnl = 0.0
        if isinstance(pos, dict):
            for row in pos.values():
                if isinstance(row, dict):
                    upnl += float(row.get("unrealized_pnl") or 0.0)
        equity = wallet + upnl

        st2 = pnl.update_equity_peak(equity_usdt=equity)
        m = pnl.compute_metrics(st=st2, equity_usdt=equity)
        pnl_payload = PnLStatusSchema(
            day=st2.day,
            daily_realized_pnl=float(st2.daily_realized_pnl),
            equity_peak=float(st2.equity_peak),
            daily_pnl_pct=float(m.daily_pnl_pct),
            drawdown_pct=float(m.drawdown_pct),
            lose_streak=int(st2.lose_streak),
            cooldown_until=st2.cooldown_until,
            last_block_reason=st2.last_block_reason,
            last_fill_symbol=st2.last_fill_symbol,
            last_fill_side=st2.last_fill_side,
            last_fill_qty=st2.last_fill_qty,
            last_fill_price=st2.last_fill_price,
            last_fill_realized_pnl=st2.last_fill_realized_pnl,
            last_fill_time=st2.last_fill_time,
        )
    except Exception:
        logger.exception("pnl_status_failed")

    sched = (
        SchedulerSnapshotSchema(**request.app.state.scheduler.snapshot.__dict__)  # type: ignore[attr-defined]
        if getattr(request.app.state, "scheduler", None) and getattr(request.app.state.scheduler, "snapshot", None)
        else None
    )
    wd = (
        WatchdogStatusSchema(**request.app.state.watchdog.metrics.__dict__)  # type: ignore[attr-defined]
        if getattr(request.app.state, "watchdog", None) and getattr(request.app.state.watchdog, "metrics", None)
        else None
    )
    capital_snapshot = None
    last_snapshot_time = None
    last_unrealized_pnl_usdt = None
    last_unrealized_pnl_pct = None
    try:
        snap_obj = getattr(request.app.state, "scheduler", None)
        snap = getattr(snap_obj, "snapshot", None)
        symbol = None
        pos = (b.get("positions") or {}) if isinstance(b, dict) else {}
        if isinstance(pos, dict):
            for sym, row in pos.items():
                if not isinstance(row, dict):
                    continue
                if abs(float(row.get("position_amt") or 0.0)) > 0.0:
                    symbol = str(sym).upper()
                    break
        if not symbol and snap is not None:
            lc = getattr(snap, "last_candidate", None)
            cand = getattr(snap, "candidate", None)
            if isinstance(lc, dict) and lc.get("symbol"):
                symbol = str(lc.get("symbol")).upper()
            elif isinstance(cand, dict) and cand.get("symbol"):
                symbol = str(cand.get("symbol")).upper()
        if not symbol:
            symbol = "BTCUSDT"

        cap = sizing.compute_live(symbol=symbol, risk=cfg, leverage=float(cfg.max_leverage))
        capital_snapshot = CapitalSnapshotSchema(
            symbol=symbol,
            available_usdt=float(cap.available_usdt),
            budget_usdt=float(cap.budget_usdt),
            used_margin=float(cap.used_margin),
            leverage=float(cap.leverage),
            notional_usdt=float(cap.notional_usdt),
            mark_price=float(cap.mark_price),
            est_qty=float(cap.qty),
            blocked=bool(cap.blocked),
            block_reason=cap.block_reason,
        )
    except Exception:
        logger.exception("capital_snapshot_failed")
    try:
        last_snapshot_time, last_unrealized_pnl_usdt, last_unrealized_pnl_pct = snapshots.get_last_snapshot_meta()
    except Exception:
        logger.exception("snapshot_status_failed")

    last_error = None
    if sched and isinstance(sched, SchedulerSnapshotSchema) and sched.last_error:
        last_error = sched.last_error
    elif wd and isinstance(wd, WatchdogStatusSchema) and wd.last_trailing_reason:
        last_error = wd.last_trailing_reason
    elif wd and isinstance(wd, WatchdogStatusSchema) and wd.last_shock_reason:
        last_error = wd.last_shock_reason
    elif isinstance(b, dict) and (b.get("private_error") or b.get("startup_error")):
        last_error = str(b.get("private_error") or b.get("startup_error"))

    summary = {
        "universe_symbols": cfg.universe_symbols,
        "max_leverage": cfg.max_leverage,
        "daily_loss_limit_pct": cfg.daily_loss_limit_pct,
        "dd_limit_pct": cfg.dd_limit_pct,
        "lose_streak_n": cfg.lose_streak_n,
        "cooldown_hours": cfg.cooldown_hours,
        "min_hold_minutes": cfg.min_hold_minutes,
        "score_conf_threshold": cfg.score_conf_threshold,
        "score_gap_threshold": cfg.score_gap_threshold,
        "exec_mode_default": cfg.exec_mode_default,
        "exec_limit_timeout_sec": cfg.exec_limit_timeout_sec,
        "exec_limit_retries": cfg.exec_limit_retries,
        "spread_max_pct": cfg.spread_max_pct,
        "allow_market_when_wide_spread": cfg.allow_market_when_wide_spread,
        "capital_mode": cfg.capital_mode.value if hasattr(cfg.capital_mode, "value") else str(cfg.capital_mode),
        "capital_pct": cfg.capital_pct,
        "capital_usdt": cfg.capital_usdt,
        "margin_budget_usdt": cfg.margin_budget_usdt,
        "margin_use_pct": cfg.margin_use_pct,
        "max_position_notional_usdt": cfg.max_position_notional_usdt,
        "max_exposure_pct": cfg.max_exposure_pct,
        "fee_buffer_pct": cfg.fee_buffer_pct,
        "enable_watchdog": cfg.enable_watchdog,
        "watchdog_interval_sec": cfg.watchdog_interval_sec,
        "shock_1m_pct": cfg.shock_1m_pct,
        "shock_from_entry_pct": cfg.shock_from_entry_pct,
        "trailing_enabled": cfg.trailing_enabled,
        "trailing_mode": cfg.trailing_mode,
        "trail_arm_pnl_pct": cfg.trail_arm_pnl_pct,
        "trail_distance_pnl_pct": cfg.trail_distance_pnl_pct,
        "trail_grace_minutes": cfg.trail_grace_minutes,
        "atr_trail_timeframe": cfg.atr_trail_timeframe,
        "atr_trail_k": cfg.atr_trail_k,
        "atr_trail_min_pct": cfg.atr_trail_min_pct,
        "atr_trail_max_pct": cfg.atr_trail_max_pct,
        "tf_weight_4h": cfg.tf_weight_4h,
        "tf_weight_1h": cfg.tf_weight_1h,
        "tf_weight_30m": cfg.tf_weight_30m,
        "vol_shock_atr_mult_threshold": cfg.vol_shock_atr_mult_threshold,
        "atr_mult_mean_window": cfg.atr_mult_mean_window,
    }

    return StatusResponse(
        dry_run=bool(getattr(settings, "trading_dry_run", False)) if settings else False,
        dry_run_strict=bool(getattr(settings, "dry_run_strict", False)) if settings else False,
        config=CapitalConfigSnapshotSchema(
            capital_mode=cfg.capital_mode.value if hasattr(cfg.capital_mode, "value") else str(cfg.capital_mode),
            capital_pct=float(cfg.capital_pct),
            capital_usdt=float(cfg.capital_usdt),
            margin_budget_usdt=float(cfg.margin_budget_usdt),
            margin_use_pct=float(cfg.margin_use_pct),
            max_position_notional_usdt=(
                float(cfg.max_position_notional_usdt) if cfg.max_position_notional_usdt is not None else None
            ),
            max_exposure_pct=float(cfg.max_exposure_pct) if cfg.max_exposure_pct is not None else None,
            fee_buffer_pct=float(cfg.fee_buffer_pct),
        ),
        config_summary=summary,
        filters_last_refresh_time=sizing.filters_last_refresh_time,
        last_snapshot_time=last_snapshot_time,
        last_unrealized_pnl_usdt=last_unrealized_pnl_usdt,
        last_unrealized_pnl_pct=last_unrealized_pnl_pct,
        last_error=last_error,
        ws_connected=bool(getattr(state, "ws_connected", False)),
        listenKey_last_keepalive_ts=getattr(getattr(request.app.state, "user_stream", None), "listen_key_last_keepalive_ts", None),
        last_ws_event_ts=getattr(getattr(request.app.state, "user_stream", None), "last_ws_event_ts", None),
        safe_mode=bool(getattr(getattr(request.app.state, "user_stream", None), "safe_mode", False)),
        last_ws_event_time=getattr(state, "last_ws_event_time", None),
        last_fill=(
            {
                "symbol": getattr(pnl_payload, "last_fill_symbol", None) if pnl_payload else None,
                "side": getattr(pnl_payload, "last_fill_side", None) if pnl_payload else None,
                "qty": getattr(pnl_payload, "last_fill_qty", None) if pnl_payload else None,
                "price": getattr(pnl_payload, "last_fill_price", None) if pnl_payload else None,
                "realized_pnl": getattr(pnl_payload, "last_fill_realized_pnl", None) if pnl_payload else None,
                "time": (
                    getattr(pnl_payload, "last_fill_time", None).isoformat()
                    if pnl_payload and getattr(pnl_payload, "last_fill_time", None)
                    else None
                ),
            }
            if pnl_payload
            else None
        ),
        engine_state=EngineStateSchema(state=state.state, updated_at=state.updated_at),
        risk_config=RiskConfigSchema(**cfg.model_dump()),
        binance=b,
        pnl=pnl_payload,
        scheduler=sched,
        watchdog=wd,
        capital_snapshot=capital_snapshot,
    )


@router.post("/start", response_model=EngineStateSchema)
def start(request: Request, engine: EngineService = Depends(_engine_service)) -> EngineStateSchema:
    try:
        row = engine.start()
        oplog = _oplog(request)
        if oplog:
            try:
                oplog.log_event("ENGINE_START", {"action": "start", "reason": "api"})
            except Exception:
                logger.exception("oplog_engine_start_failed")
        return EngineStateSchema(state=row.state, updated_at=row.updated_at)
    except EngineConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/stop", response_model=EngineStateSchema)
def stop(request: Request, engine: EngineService = Depends(_engine_service)) -> EngineStateSchema:
    try:
        row = engine.stop()
        oplog = _oplog(request)
        if oplog:
            try:
                oplog.log_event("ENGINE_STOP", {"action": "stop", "reason": "api"})
            except Exception:
                logger.exception("oplog_engine_stop_failed")
        return EngineStateSchema(state=row.state, updated_at=row.updated_at)
    except EngineConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/cooldown/clear", response_model=PnLStatusSchema)
def clear_cooldown(
    request: Request,
    engine: EngineService = Depends(_engine_service),
    pnl: PnLService = Depends(_pnl_service),
    binance: BinanceService = Depends(_binance_service),
) -> PnLStatusSchema:
    st = pnl.clear_risk_guards()
    try:
        if engine.get_state().state == EngineState.COOLDOWN:
            engine.start()
    except Exception:
        logger.exception("cooldown_clear_engine_resume_failed")

    bal = (binance.get_status().get("usdt_balance") or {})
    wallet = float(bal.get("wallet") or 0.0)
    m = pnl.compute_metrics(st=st, equity_usdt=wallet)

    oplog = _oplog(request)
    if oplog:
        try:
            oplog.log_event("COOLDOWN_CLEAR", {"action": "cooldown_clear", "reason": "api"})
        except Exception:
            logger.exception("oplog_cooldown_clear_failed")

    return PnLStatusSchema(
        day=st.day,
        daily_realized_pnl=float(st.daily_realized_pnl),
        equity_peak=float(st.equity_peak),
        daily_pnl_pct=float(m.daily_pnl_pct),
        drawdown_pct=float(m.drawdown_pct),
        lose_streak=int(st.lose_streak),
        cooldown_until=st.cooldown_until,
        last_block_reason=st.last_block_reason,
        last_fill_symbol=st.last_fill_symbol,
        last_fill_side=st.last_fill_side,
        last_fill_qty=st.last_fill_qty,
        last_fill_price=st.last_fill_price,
        last_fill_realized_pnl=st.last_fill_realized_pnl,
        last_fill_time=st.last_fill_time,
    )


@router.post("/panic", response_model=PanicResponseSchema)
async def panic(
    request: Request,
    response: Response,
    engine: EngineService = Depends(_engine_service),
    exe: ExecutionService = Depends(_execution_service),
) -> PanicResponseSchema | JSONResponse:
    # PANIC should lock state + attempt best-effort cancel/close.
    try:
        out = await exe.panic()
    except ExecutionRejected as e:
        if str(getattr(e, "message", e)) == "EXECUTION_LOCK_BUSY":
            row = engine.get_state()
            retry_after_ms = max(int(float(getattr(exe, "_exec_lock_timeout_sec", 0.5)) * 1000.0), 100)
            return JSONResponse(
                status_code=status.HTTP_423_LOCKED,
                content={
                    "ok": False,
                    "code": "EXECUTION_LOCK_BUSY",
                    "message": "panic request blocked by execution lock",
                    "retry_after_ms": retry_after_ms,
                    "engine_state": {
                        "state": row.state.value,
                        "updated_at": row.updated_at.isoformat(),
                    },
                },
            )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e
    oplog = _oplog(request)
    if oplog:
        try:
            oplog.log_event("ENGINE_PANIC", {"action": "panic", "reason": "api"})
        except Exception:
            logger.exception("oplog_engine_panic_failed")
    row = engine.get_state()
    panic_result_raw = dict(out.get("panic_result") or {})
    panic_result = PanicResultSchema(
        ok=bool(panic_result_raw.get("ok", True)),
        canceled_orders_ok=bool(panic_result_raw.get("canceled_orders_ok", True)),
        close_ok=bool(panic_result_raw.get("close_ok", True)),
        errors=[str(x) for x in (panic_result_raw.get("errors") or [])],
        closed_symbol=(str(panic_result_raw.get("closed_symbol")) if panic_result_raw.get("closed_symbol") else None),
        closed_qty=(float(panic_result_raw.get("closed_qty")) if panic_result_raw.get("closed_qty") is not None else None),
    )
    if not panic_result.ok:
        response.status_code = status.HTTP_207_MULTI_STATUS
    return PanicResponseSchema(
        engine_state=EngineStateSchema(state=row.state, updated_at=row.updated_at),
        panic_result=panic_result,
    )


@router.get("/risk", response_model=RiskConfigSchema)
def get_risk(risk: RiskConfigService = Depends(_risk_service)) -> RiskConfigSchema:
    cfg = risk.get_config()
    return RiskConfigSchema(**cfg.model_dump())


@router.post("/set", response_model=SetValueResponse)
def set_value(
    request: Request,
    req: SetValueRequest,
    risk: RiskConfigService = Depends(_risk_service),
) -> SetValueResponse:
    try:
        cfg = risk.set_value(req.key, req.value)
        budget_keys = {
            "capital_mode",
            "capital_pct",
            "capital_usdt",
            "margin_budget_usdt",
            "margin_use_pct",
            "max_position_notional_usdt",
            "max_exposure_pct",
            "fee_buffer_pct",
        }
        if req.key.value in budget_keys:
            notifier = getattr(request.app.state, "notifier", None)
            if notifier and hasattr(notifier, "notify"):
                try:
                    notifier.notify({"kind": "BUDGET_UPDATED", "key": req.key.value, "value": req.value})
                except Exception:
                    logger.exception("budget_updated_notify_failed")
        applied_payload = cfg.model_dump()
        return SetValueResponse(
            key=req.key.value,
            requested_value=req.value,
            applied_value=applied_payload.get(req.key.value),
            summary=f"Applied {req.key.value}={applied_payload.get(req.key.value)}",
            risk_config=RiskConfigSchema(**applied_payload),
        )
    except RiskConfigValidationError as e:
        errs = _parse_set_validation_errors(e.message)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "validation_failed",
                "errors": [x.model_dump() for x in errs],
            },
        ) from e


@router.post("/preset", response_model=RiskConfigSchema)
def preset(
    req: PresetRequest,
    risk: RiskConfigService = Depends(_risk_service),
) -> RiskConfigSchema:
    cfg = risk.apply_preset(req.name)
    return RiskConfigSchema(**cfg.model_dump())


@router.post("/trade/enter", response_model=TradeResult)
async def trade_enter(
    req: TradeEnterRequest,
    exe: ExecutionService = Depends(_execution_service),
) -> TradeResult:
    try:
        out = await exe.enter_position(req.model_dump())
        return TradeResult(
            symbol=out.get("symbol", req.symbol),
            hint=out.get("hint"),
            orders=out.get("orders", []),
            detail=out,
        )
    except ExecutionValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.message) from e
    except ExecutionRejected as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/trade/close", response_model=TradeResult)
async def trade_close(
    req: TradeCloseRequest,
    exe: ExecutionService = Depends(_execution_service),
) -> TradeResult:
    try:
        out = await exe.close_position(req.symbol)
        return TradeResult(symbol=out.get("symbol", req.symbol), detail=out)
    except ExecutionValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.message) from e
    except ExecutionRejected as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/trade/close_all", response_model=TradeResult)
async def trade_close_all(
    exe: ExecutionService = Depends(_execution_service),
) -> TradeResult:
    try:
        out = await exe.close_all_positions()
        return TradeResult(symbol=str(out.get("symbol", "")), detail=out)
    except ExecutionRejected as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e


@router.post("/debug/tick")
async def debug_tick(request: Request) -> Dict[str, Any]:
    _require_test_mode(request)
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="scheduler_missing")
    snap = await scheduler.tick_once()
    return {"ok": True, "snapshot": snap.__dict__}
