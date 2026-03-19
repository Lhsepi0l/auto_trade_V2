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

    app.include_router(router)
