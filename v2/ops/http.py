from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from v2.ops.control import OpsController


class FlattenRequest(BaseModel):
    symbol: str


def create_ops_http_app(*, ops: OpsController) -> FastAPI:
    app = FastAPI(title="auto-trader-v2-ops", version="0.1.0")

    @app.post("/ops/pause")
    async def pause() -> dict[str, bool]:
        ops.pause()
        return {"paused": True}

    @app.post("/ops/resume")
    async def resume() -> dict[str, bool]:
        ops.resume()
        return {"paused": False, "safe_mode": False}

    @app.post("/ops/safe_mode")
    async def safe_mode() -> dict[str, bool]:
        ops.safe_mode()
        return {"paused": True, "safe_mode": True}

    @app.post("/ops/flatten")
    async def flatten(payload: FlattenRequest) -> dict[str, object]:
        result = await ops.flatten(symbol=payload.symbol)
        return {
            "symbol": result.symbol,
            "paused": result.paused,
            "safe_mode": result.safe_mode,
            "open_regular_orders": result.open_regular_orders,
            "open_algo_orders": result.open_algo_orders,
            "position_amt": result.position_amt,
        }

    return app
