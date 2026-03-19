from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from v2.control.api import RuntimeController


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


def register_readonly_control_routes(*, app: FastAPI, controller: RuntimeController) -> None:
    @app.get("/status")
    async def status() -> dict[str, Any]:
        return controller._status_snapshot()

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return controller._healthz_snapshot()

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        payload = controller._readyz_snapshot()
        return JSONResponse(status_code=200 if payload["ready"] else 503, content=payload)

    @app.get("/risk")
    async def risk() -> dict[str, Any]:
        return controller.get_risk()

    @app.get("/readiness")
    async def readiness() -> dict[str, Any]:
        return controller._live_readiness_snapshot()

    @app.get("/scheduler")
    async def get_scheduler() -> dict[str, Any]:
        return controller.get_scheduler()


def register_mutating_control_routes(*, app: FastAPI, controller: RuntimeController) -> None:
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


def create_control_http_app(
    *,
    controller: RuntimeController,
    enable_operator_web: bool = False,
) -> FastAPI:
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        _ = _app
        await controller.start_live_services()
        try:
            yield
        finally:
            await controller.stop_live_services()

    app = FastAPI(title="auto-trader-v2-control", version="0.1.0", lifespan=_lifespan)
    app.state.controller = controller
    register_readonly_control_routes(app=app, controller=controller)
    register_mutating_control_routes(app=app, controller=controller)
    if enable_operator_web:
        from v2.web_panel import register_operator_web_routes

        register_operator_web_routes(app=app, controller=controller)
    return app
