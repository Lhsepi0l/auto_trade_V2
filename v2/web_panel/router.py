from __future__ import annotations

from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from v2.operator import OperatorService

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


def _read_template(name: str) -> str:
    return (_TEMPLATE_DIR / name).read_text(encoding="utf-8")


def _render_page(*, title: str, body_template: str) -> HTMLResponse:
    base = _read_template("base.html")
    body = _read_template(body_template)
    content = base.replace("{{ TITLE }}", escape(title)).replace("{{ BODY }}", body)
    return HTMLResponse(content=content)


def register_operator_web_routes(*, app: FastAPI, controller: RuntimeController) -> None:
    service = OperatorService(controller=controller)
    app.mount("/operator/static", StaticFiles(directory=str(_STATIC_DIR)), name="operator-static")

    router = APIRouter(tags=["operator-web"])

    @router.get("/operator", response_class=HTMLResponse)
    async def operator_console() -> HTMLResponse:
        return _render_page(title="웹 운영 콘솔", body_template="operator_console.html")

    @router.get("/operator/")
    async def operator_console_slash() -> RedirectResponse:
        return RedirectResponse(url="/operator", status_code=307)

    @router.get("/operator/api/console")
    async def operator_console_payload() -> dict[str, Any]:
        return service.console_payload()

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

    app.include_router(router)
