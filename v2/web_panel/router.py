from __future__ import annotations

from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from v2.operator import OperatorService
from v2.operator.debug_bundle import resolve_runtime_debug_bundle_archive

if TYPE_CHECKING:
    from v2.control.api import RuntimeController

_ROOT = Path(__file__).resolve().parent
_TEMPLATE_DIR = _ROOT / "templates"
_STATIC_DIR = _ROOT / "static"


class OperatorLeverageRequest(BaseModel):
    symbol: str
    leverage: float


class OperatorClosePositionRequest(BaseModel):
    symbol: str


class OperatorSchedulerIntervalRequest(BaseModel):
    tick_sec: float


class OperatorExecModeRequest(BaseModel):
    exec_mode: str


class OperatorMarginBudgetRequest(BaseModel):
    amount_usdt: float
    leverage: float | None = None


class OperatorRiskBasicRequest(BaseModel):
    max_leverage: float
    max_exposure_pct: float
    max_notional_pct: float
    per_trade_risk_pct: float


class OperatorRiskAdvancedRequest(BaseModel):
    daily_loss_limit_pct: float
    dd_limit_pct: float
    min_hold_minutes: int
    score_conf_threshold: float


class OperatorNotifyIntervalRequest(BaseModel):
    notify_interval_sec: int


class OperatorPresetRequest(BaseModel):
    name: str


class OperatorProfileTemplateRequest(BaseModel):
    name: str
    budget_usdt: float | None = None


class OperatorTrailingConfigRequest(BaseModel):
    trailing_enabled: bool
    trailing_mode: str
    trail_arm_pnl_pct: float
    trail_grace_minutes: int
    trail_distance_pnl_pct: float | None = None
    atr_trail_timeframe: str | None = None
    atr_trail_k: float | None = None
    atr_trail_min_pct: float | None = None
    atr_trail_max_pct: float | None = None


class OperatorUniverseRequest(BaseModel):
    symbols_text: str


class OperatorUniverseRemoveRequest(BaseModel):
    symbol: str


class OperatorScoringConfigRequest(BaseModel):
    tf_weight_10m: float
    tf_weight_15m: float
    tf_weight_30m: float
    tf_weight_1h: float
    tf_weight_4h: float
    score_conf_threshold: float
    score_gap_threshold: float
    donchian_momentum_filter: bool
    donchian_fast_ema_period: int
    donchian_slow_ema_period: int


def _read_template(name: str) -> str:
    return (_TEMPLATE_DIR / name).read_text(encoding="utf-8")


def _asset_url(filename: str) -> str:
    path = _STATIC_DIR / filename
    version = int(path.stat().st_mtime_ns) if path.exists() else 0
    return f"/operator/static/{filename}?v={version}"


def _render_page(*, title: str, body_template: str, page_id: str) -> HTMLResponse:
    base = _read_template("base.html")
    body = _read_template(body_template)
    content = (
        base.replace("{{ TITLE }}", escape(title))
        .replace("{{ BODY }}", body)
        .replace("{{ PAGE_ID }}", escape(page_id))
        .replace("{{ NAV_CONSOLE_ACTIVE }}", "is-active" if page_id == "console" else "")
        .replace("{{ NAV_LOGS_ACTIVE }}", "is-active" if page_id == "logs" else "")
        .replace("{{ OPERATOR_CSS_URL }}", _asset_url("operator.css"))
        .replace("{{ OPERATOR_JS_URL }}", _asset_url("operator.js"))
    )
    return HTMLResponse(content=content)


def register_operator_web_routes(*, app: FastAPI, controller: RuntimeController) -> None:
    service = OperatorService(controller=controller)
    app.mount("/operator/static", StaticFiles(directory=str(_STATIC_DIR)), name="operator-static")

    router = APIRouter(tags=["operator-web"])

    @router.get("/operator", response_class=HTMLResponse)
    async def operator_console() -> HTMLResponse:
        return _render_page(title="웹 운영 콘솔", body_template="operator_console.html", page_id="console")

    @router.get("/operator/logs", response_class=HTMLResponse)
    async def operator_logs() -> HTMLResponse:
        return _render_page(title="운영 로그", body_template="operator_logs.html", page_id="logs")

    @router.get("/operator/")
    async def operator_console_slash() -> RedirectResponse:
        return RedirectResponse(url="/operator", status_code=307)

    @router.get("/operator/logs/")
    async def operator_logs_slash() -> RedirectResponse:
        return RedirectResponse(url="/operator/logs", status_code=307)

    @router.get("/operator/api/console")
    async def operator_console_payload() -> dict[str, Any]:
        return service.console_payload()

    @router.get("/operator/api/events")
    async def operator_events(limit: int = 200) -> list[dict[str, Any]]:
        return service.list_operator_events(limit=limit)

    @router.get("/operator/api/logs")
    async def operator_logs_payload(
        limit: int = 500,
        offset: int = 0,
        category: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        normalized_limit = max(1, min(int(limit), 1000))
        normalized_offset = max(0, int(offset))
        items = service.list_operator_events(
            limit=normalized_limit,
            offset=normalized_offset,
            category=category,
            query=query,
        )
        total = service.count_operator_events(category=category, query=query)
        return {
            "items": items,
            "limit": normalized_limit,
            "offset": normalized_offset,
            "total": total,
            "has_prev": normalized_offset > 0,
            "has_next": normalized_offset + len(items) < total,
        }

    @router.post("/operator/actions/start")
    async def operator_start() -> dict[str, Any]:
        return service.start_or_resume()

    @router.post("/operator/actions/pause")
    async def operator_pause() -> dict[str, Any]:
        return service.pause()

    @router.post("/operator/actions/panic")
    async def operator_panic() -> dict[str, Any]:
        return await service.panic()

    @router.post("/operator/actions/tick")
    async def operator_tick() -> dict[str, Any]:
        return service.tick_now()

    @router.post("/operator/actions/symbol-leverage")
    async def operator_set_symbol_leverage(payload: OperatorLeverageRequest) -> dict[str, Any]:
        return service.set_symbol_leverage(symbol=payload.symbol, leverage=payload.leverage)

    @router.post("/operator/actions/reconcile")
    async def operator_reconcile() -> dict[str, Any]:
        return await service.reconcile()

    @router.post("/operator/actions/cooldown-clear")
    async def operator_cooldown_clear() -> dict[str, Any]:
        return service.clear_cooldown()

    @router.post("/operator/actions/positions/close")
    async def operator_close_position(payload: OperatorClosePositionRequest) -> dict[str, Any]:
        return await service.close_position(symbol=payload.symbol)

    @router.post("/operator/actions/positions/close-all")
    async def operator_close_all() -> dict[str, Any]:
        return await service.close_all()

    @router.post("/operator/actions/scheduler-interval")
    async def operator_scheduler_interval(
        payload: OperatorSchedulerIntervalRequest,
    ) -> dict[str, Any]:
        return service.set_scheduler_interval(tick_sec=payload.tick_sec)

    @router.post("/operator/actions/exec-mode")
    async def operator_exec_mode(payload: OperatorExecModeRequest) -> dict[str, Any]:
        return service.set_exec_mode(exec_mode=payload.exec_mode)

    @router.post("/operator/actions/margin-budget")
    async def operator_margin_budget(payload: OperatorMarginBudgetRequest) -> dict[str, Any]:
        return service.set_margin_budget(
            amount_usdt=payload.amount_usdt,
            leverage=payload.leverage,
        )

    @router.post("/operator/actions/risk-basic")
    async def operator_risk_basic(payload: OperatorRiskBasicRequest) -> dict[str, Any]:
        return service.set_risk_basic(
            max_leverage=payload.max_leverage,
            max_exposure_pct=payload.max_exposure_pct,
            max_notional_pct=payload.max_notional_pct,
            per_trade_risk_pct=payload.per_trade_risk_pct,
        )

    @router.post("/operator/actions/risk-advanced")
    async def operator_risk_advanced(payload: OperatorRiskAdvancedRequest) -> dict[str, Any]:
        return service.set_risk_advanced(
            daily_loss_limit_pct=payload.daily_loss_limit_pct,
            dd_limit_pct=payload.dd_limit_pct,
            min_hold_minutes=payload.min_hold_minutes,
            score_conf_threshold=payload.score_conf_threshold,
        )

    @router.post("/operator/actions/notify-interval")
    async def operator_notify_interval(payload: OperatorNotifyIntervalRequest) -> dict[str, Any]:
        return service.set_notify_interval(notify_interval_sec=payload.notify_interval_sec)

    @router.post("/operator/actions/preset")
    async def operator_preset(payload: OperatorPresetRequest) -> dict[str, Any]:
        return service.apply_preset(name=payload.name)

    @router.post("/operator/actions/profile-template")
    async def operator_profile_template(payload: OperatorProfileTemplateRequest) -> dict[str, Any]:
        return service.apply_profile_template(
            name=payload.name,
            budget_usdt=payload.budget_usdt,
        )

    @router.post("/operator/actions/trailing")
    async def operator_trailing(payload: OperatorTrailingConfigRequest) -> dict[str, Any]:
        return service.set_trailing_config(
            trailing_enabled=payload.trailing_enabled,
            trailing_mode=payload.trailing_mode,
            trail_arm_pnl_pct=payload.trail_arm_pnl_pct,
            trail_grace_minutes=payload.trail_grace_minutes,
            trail_distance_pnl_pct=payload.trail_distance_pnl_pct,
            atr_trail_timeframe=payload.atr_trail_timeframe,
            atr_trail_k=payload.atr_trail_k,
            atr_trail_min_pct=payload.atr_trail_min_pct,
            atr_trail_max_pct=payload.atr_trail_max_pct,
        )

    @router.post("/operator/actions/universe")
    async def operator_universe(payload: OperatorUniverseRequest) -> dict[str, Any]:
        return service.set_universe_symbols(symbols_text=payload.symbols_text)

    @router.post("/operator/actions/universe/remove")
    async def operator_universe_remove(payload: OperatorUniverseRemoveRequest) -> dict[str, Any]:
        return service.remove_universe_symbol(symbol=payload.symbol)

    @router.post("/operator/actions/scoring")
    async def operator_scoring(payload: OperatorScoringConfigRequest) -> dict[str, Any]:
        return service.set_scoring_config(
            tf_weight_10m=payload.tf_weight_10m,
            tf_weight_15m=payload.tf_weight_15m,
            tf_weight_30m=payload.tf_weight_30m,
            tf_weight_1h=payload.tf_weight_1h,
            tf_weight_4h=payload.tf_weight_4h,
            score_conf_threshold=payload.score_conf_threshold,
            score_gap_threshold=payload.score_gap_threshold,
            donchian_momentum_filter=payload.donchian_momentum_filter,
            donchian_fast_ema_period=payload.donchian_fast_ema_period,
            donchian_slow_ema_period=payload.donchian_slow_ema_period,
        )

    @router.post("/operator/actions/report")
    async def operator_report() -> dict[str, Any]:
        return service.trigger_report()

    @router.post("/operator/actions/debug-bundle")
    async def operator_debug_bundle(request: Request) -> dict[str, Any]:
        return service.export_debug_bundle(base_url=str(request.base_url).rstrip("/"))

    @router.get("/operator/api/debug-bundles/{archive_name}")
    async def operator_debug_bundle_download(archive_name: str) -> FileResponse:
        archive_path = resolve_runtime_debug_bundle_archive(archive_name=archive_name)
        if archive_path is None:
            raise HTTPException(status_code=404, detail="debug_bundle_archive_not_found")
        return FileResponse(
            path=str(archive_path),
            media_type="application/zip",
            filename=archive_path.name,
        )

    app.include_router(router)
