from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

import pytest

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.execution_service import ExecutionService, _make_client_order_id
from apps.trader_engine.storage.db import connect, migrate
from apps.trader_engine.storage.repositories import OrderRecordRepo


@dataclass
class _State:
    state: EngineState


class _Engine:
    def __init__(self) -> None:
        self._s = _State(state=EngineState.RUNNING)

    def get_state(self) -> _State:
        return self._s


class _Risk:
    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> RiskConfig:
        return self._cfg


class _ClientTimeoutThenFound:
    def __init__(self) -> None:
        self.place_calls = 0
        self.query_by_cid_calls = 0
        self.get_order_calls = 0

    def refresh_time_offset(self) -> int:
        return 0

    def get_position_mode_one_way(self) -> bool:
        return True

    def get_open_positions_any(self) -> Dict[str, Dict[str, float]]:
        return {}

    def cancel_all_open_orders(self, *, symbol: str):
        return []

    def get_book_ticker(self, symbol: str) -> Mapping[str, Any]:
        return {"bidPrice": "99", "askPrice": "100"}

    def get_symbol_filters(self, *, symbol: str) -> Mapping[str, Any]:
        return {"symbol": symbol, "step_size": 0.001, "min_qty": 0.001, "tick_size": 0.1, "min_notional": 1.0}

    def get_exchange_info_cached(self) -> Mapping[str, Any]:
        return {"symbols": [{"symbol": "BTCUSDT"}]}

    def get_account_balance_usdtm(self) -> Mapping[str, Any]:
        return {"wallet": 1000.0, "available": 1000.0}

    def get_mark_price(self, symbol: str) -> Mapping[str, Any]:
        return {"symbol": symbol, "markPrice": "100"}

    def place_order_limit(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        post_only: bool = False,
        reduce_only: bool = False,
        new_client_order_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        self.place_calls += 1
        raise TimeoutError("request timeout")

    def get_order_by_client_order_id(self, *, symbol: str, client_order_id: str) -> Mapping[str, Any]:
        self.query_by_cid_calls += 1
        return {
            "symbol": symbol,
            "orderId": 101,
            "clientOrderId": client_order_id,
            "status": "NEW",
            "price": "100",
            "origQty": "0.01",
            "executedQty": "0",
            "type": "LIMIT",
            "side": "BUY",
        }

    def get_order(self, *, symbol: str, order_id: int) -> Mapping[str, Any]:
        self.get_order_calls += 1
        return {
            "symbol": symbol,
            "orderId": order_id,
            "status": "FILLED",
            "price": "100",
            "origQty": "0.01",
            "executedQty": "0.01",
            "type": "LIMIT",
            "side": "BUY",
        }

    def place_order_market(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
        new_client_order_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        return {
            "symbol": symbol,
            "orderId": 999,
            "clientOrderId": new_client_order_id,
            "status": "FILLED",
            "type": "MARKET",
            "side": side,
            "origQty": str(quantity),
            "executedQty": str(quantity),
        }


def _cfg() -> RiskConfig:
    return RiskConfig(
        per_trade_risk_pct=1.0,
        max_exposure_pct=0.2,
        max_notional_pct=50.0,
        max_leverage=5.0,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6,
        notify_interval_sec=1800,
        exec_limit_timeout_sec=0.01,
        exec_limit_retries=2,
    )


@pytest.mark.unit
def test_client_order_id_length_lte_36() -> None:
    cid = _make_client_order_id(env="PRODUCTION", intent_id=("intent-" + ("x" * 80)), attempt=99)
    assert len(cid) <= 36


@pytest.mark.unit
def test_order_records_unique_client_order_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = connect(str(tmp_path / "idempotency.sqlite3"))
    migrate(db)
    repo = OrderRecordRepo(db)
    repo.create_created(
        intent_id="i1",
        cycle_id="c1",
        run_id="r1",
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        reduce_only=False,
        qty=0.01,
        price=100.0,
        time_in_force="GTC",
        client_order_id="BOT-PROD-unique-1",
    )
    with pytest.raises(Exception):
        repo.create_created(
            intent_id="i2",
            cycle_id="c2",
            run_id="r2",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            reduce_only=False,
            qty=0.01,
            price=100.0,
            time_in_force="GTC",
            client_order_id="BOT-PROD-unique-1",
        )


@pytest.mark.unit
def test_timeout_path_queries_before_resend() -> None:
    client = _ClientTimeoutThenFound()
    svc = ExecutionService(
        client=client,  # type: ignore[arg-type]
        engine=_Engine(),
        risk=_Risk(_cfg()),  # type: ignore[arg-type]
        pnl=None,
        policy=None,
        notifier=None,
        allowed_symbols=["BTCUSDT"],
        dry_run=False,
    )
    out = svc._enter_limit_then_market(  # type: ignore[attr-defined]
        symbol="BTCUSDT",
        side="BUY",
        qty=Decimal("0.01"),
        intent_id="intent-BTCUSDT-1",
        cycle_id="cycle-1",
    )
    assert out["symbol"] == "BTCUSDT"
    assert client.place_calls == 1
    assert client.query_by_cid_calls >= 1
