from __future__ import annotations

import pytest

from apps.trader_engine.domain.enums import Direction, EngineState, ExecHint
from apps.trader_engine.exchange.binance_usdm import BinanceUSDMClient
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.execution_service import ExecutionRejected, ExecutionService
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.storage.db import connect, migrate
from apps.trader_engine.storage.repositories import EngineStateRepo, RiskConfigRepo


class _FakeBinance(BinanceUSDMClient):  # type: ignore[misc]
    def __init__(self) -> None:  # pragma: no cover
        # Avoid real init; tests override used methods.
        pass

    def get_position_mode_one_way(self) -> bool:
        return True

    def get_open_positions_any(self):
        return {}

    def get_book_ticker(self, symbol: str):
        return {"bidPrice": "100", "askPrice": "101"}

    def get_symbol_filters(self, *, symbol: str):
        return {"step_size": 0.001, "min_qty": 0.001, "tick_size": 0.1, "min_notional": 5}

    def get_exchange_info_cached(self):
        return {"symbols": [{"symbol": "BTCUSDT"}]}

    def get_account_balance_usdtm(self):
        return {"wallet": 1000.0, "available": 1000.0}

    def get_mark_price(self, symbol: str):
        return {"symbol": symbol, "markPrice": "100"}

    def place_order_market(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
        new_client_order_id: str | None = None,
    ):
        return {
            "symbol": symbol,
            "orderId": 1,
            "clientOrderId": new_client_order_id,
            "side": side,
            "type": "MARKET",
            "status": "FILLED",
            "executedQty": quantity,
        }

    def cancel_all_open_orders(self, *, symbol: str):
        return []


def _mk_services(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    db = connect(str(db_path))
    migrate(db)
    engine = EngineService(engine_state_repo=EngineStateRepo(db))
    risk = RiskConfigService(risk_config_repo=RiskConfigRepo(db))
    _ = risk.get_config()
    return engine, risk


@pytest.mark.asyncio
async def test_enter_requires_running(tmp_path):
    engine, risk = _mk_services(tmp_path)
    exe = ExecutionService(client=_FakeBinance(), engine=engine, risk=risk, allowed_symbols=["BTCUSDT"])

    with pytest.raises(ExecutionRejected):
        await exe.enter_position(
            {
                "symbol": "BTCUSDT",
                "direction": Direction.LONG,
                "exec_hint": ExecHint.MARKET,
                "notional_usdt": 10,
            }
        )


@pytest.mark.asyncio
async def test_close_rejected_in_panic(tmp_path):
    engine, risk = _mk_services(tmp_path)
    engine.panic()

    exe = ExecutionService(client=_FakeBinance(), engine=engine, risk=risk, allowed_symbols=["BTCUSDT"])
    with pytest.raises(ExecutionRejected):
        await exe.close_position("BTCUSDT")
