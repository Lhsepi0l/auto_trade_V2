from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from v2.storage import RuntimeStorage
from v2.tpsl import BracketConfig, BracketPlanner, BracketReconcilePoller, BracketService


class _FakeAlgoREST:
    def __init__(self) -> None:
        self.place_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self.open_orders: list[dict[str, Any]] = []

    async def place_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
        self.place_calls.append(params)
        return {
            "algoId": 1000 + len(self.place_calls),
            "clientAlgoId": params.get("clientAlgoId"),
            "status": "NEW",
        }

    async def place_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"legacy order endpoint path must not be used for TP/SL: {params}")

    async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
        self.cancel_calls.append(params)
        return {"msg": "success"}

    async def get_open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        _ = symbol
        return list(self.open_orders)

    async def public_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = method
        _ = path
        symbol = str((params or {}).get("symbol") or "BTCUSDT")
        return {
            "symbols": [
                {
                    "symbol": symbol,
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                    ],
                }
            ]
        }


def _storage(tmp_path: Path) -> RuntimeStorage:
    store = RuntimeStorage(sqlite_path=str(tmp_path / "runtime.sqlite3"))
    store.ensure_schema()
    return store


def test_bracket_planner_percent_and_atr() -> None:
    percent = BracketPlanner(
        cfg=BracketConfig(method="percent", take_profit_pct=0.02, stop_loss_pct=0.01)
    )
    levels_long = percent.levels(entry_price=100.0, side="LONG")
    levels_short = percent.levels(entry_price=100.0, side="SHORT")
    assert levels_long["take_profit"] == 102.0
    assert levels_long["stop_loss"] == 99.0
    assert levels_short["take_profit"] == 98.0
    assert levels_short["stop_loss"] == 101.0

    atr = BracketPlanner(cfg=BracketConfig(method="atr", tp_atr=2.0, sl_atr=1.0))
    atr_levels = atr.levels(entry_price=100.0, side="LONG", atr=3.0)
    assert atr_levels["take_profit"] == 106.0
    assert atr_levels["stop_loss"] == 97.0


@pytest.mark.asyncio
async def test_create_and_place_shadow_persists_active_without_rest(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    service = BracketService(
        planner=BracketPlanner(cfg=BracketConfig()),
        storage=store,
        rest_client=None,
        mode="shadow",
    )

    out = await service.create_and_place(
        symbol="BTCUSDT",
        entry_side="BUY",
        position_side="BOTH",
        entry_price=100.0,
        quantity=0.01,
    )

    rows = store.list_bracket_states()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["state"] == "ACTIVE"
    assert out["tp_payload"]["type"] == "TAKE_PROFIT_MARKET"
    assert out["sl_payload"]["type"] == "STOP_MARKET"
    assert out["tp_payload"]["workingType"] == "MARK_PRICE"


@pytest.mark.asyncio
async def test_shadow_mode_prints_payloads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _storage(tmp_path)
    service = BracketService(
        planner=BracketPlanner(cfg=BracketConfig()),
        storage=store,
        rest_client=None,
        mode="shadow",
    )

    await service.create_and_place(
        symbol="BTCUSDT",
        entry_side="BUY",
        position_side="BOTH",
        entry_price=100.0,
        quantity=0.01,
    )
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["event"] == "tpsl.shadow.place"
    assert payload["tp_payload"]["type"] == "TAKE_PROFIT_MARKET"
    assert payload["sl_payload"]["type"] == "STOP_MARKET"


@pytest.mark.asyncio
async def test_live_leg_fill_cancels_counterpart_and_cleans(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    rest = _FakeAlgoREST()
    service = BracketService(
        planner=BracketPlanner(cfg=BracketConfig()),
        storage=store,
        rest_client=rest,
        mode="live",
    )

    created = await service.create_and_place(
        symbol="BTCUSDT",
        entry_side="BUY",
        position_side="BOTH",
        entry_price=100.0,
        quantity=0.01,
    )
    assert len(rest.place_calls) == 2

    tp_id = str(created["planned"]["tp_client_algo_id"])
    await service.on_leg_filled(symbol="BTCUSDT", filled_client_algo_id=tp_id)

    assert len(rest.cancel_calls) == 1
    rows = store.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    assert rows[0]["tp_order_client_id"] is None
    assert rows[0]["sl_order_client_id"] is None


@pytest.mark.asyncio
async def test_live_leg_fill_cancels_counterpart_with_algo_id_only_open_order(
    tmp_path: Path,
) -> None:
    class _AlgoIdOnlyCounterpartREST(_FakeAlgoREST):
        async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            self.cancel_calls.append(dict(params))
            if params.get("algoId") == 9002:
                self.open_orders = []
                return {"msg": "success"}
            raise RuntimeError("client_algo_cancel_rejected")

    store = _storage(tmp_path)
    rest = _AlgoIdOnlyCounterpartREST()
    service = BracketService(
        planner=BracketPlanner(cfg=BracketConfig()),
        storage=store,
        rest_client=rest,
        mode="live",
    )

    created = await service.create_and_place(
        symbol="BTCUSDT",
        entry_side="BUY",
        position_side="BOTH",
        entry_price=100.0,
        quantity=0.01,
    )
    rest.open_orders = [{"symbol": "BTCUSDT", "algoId": 9002}]

    tp_id = str(created["planned"]["tp_client_algo_id"])
    await service.on_leg_filled(symbol="BTCUSDT", filled_client_algo_id=tp_id)

    assert any(call.get("algoId") == 9002 for call in rest.cancel_calls)
    rows = store.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"
    assert rows[0]["tp_order_client_id"] is None
    assert rows[0]["sl_order_client_id"] is None


@pytest.mark.asyncio
async def test_live_create_and_place_cleans_partial_leg_when_second_order_fails(
    tmp_path: Path,
) -> None:
    class _FailSecondAlgoREST(_FakeAlgoREST):
        async def place_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            self.place_calls.append(dict(params))
            if len(self.place_calls) == 2:
                raise RuntimeError("second_leg_reject")
            return {
                "algoId": 1000 + len(self.place_calls),
                "clientAlgoId": params.get("clientAlgoId"),
                "status": "NEW",
            }

    store = _storage(tmp_path)
    rest = _FailSecondAlgoREST()
    service = BracketService(
        planner=BracketPlanner(cfg=BracketConfig()),
        storage=store,
        rest_client=rest,
        mode="live",
    )

    with pytest.raises(RuntimeError, match="second_leg_reject"):
        await service.create_and_place(
            symbol="BTCUSDT",
            entry_side="BUY",
            position_side="BOTH",
            entry_price=100.0,
            quantity=0.01,
        )

    assert len(rest.place_calls) == 2
    assert len(rest.cancel_calls) == 1
    canceled_id = str(rest.cancel_calls[0].get("clientAlgoId") or "")
    assert canceled_id.startswith("v2tp")
    rows = store.list_bracket_states()
    assert len(rows) == 1
    assert rows[0]["state"] == "CLEANED"
    assert rows[0]["tp_order_client_id"] is None
    assert rows[0]["sl_order_client_id"] is None


@pytest.mark.asyncio
async def test_cleanup_if_flat_cancels_all_open_algo_orders_for_symbol(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    rest = _FakeAlgoREST()
    service = BracketService(
        planner=BracketPlanner(cfg=BracketConfig()),
        storage=store,
        rest_client=rest,
        mode="live",
    )
    created = await service.create_and_place(
        symbol="BTCUSDT",
        entry_side="BUY",
        position_side="BOTH",
        entry_price=100.0,
        quantity=0.01,
    )
    tp_id = str(created["planned"]["tp_client_algo_id"])
    sl_id = str(created["planned"]["sl_client_algo_id"])
    rest.open_orders = [
        {"symbol": "BTCUSDT", "clientAlgoId": tp_id},
        {"symbol": "BTCUSDT", "clientAlgoId": sl_id},
        {"symbol": "BTCUSDT", "clientAlgoId": "x-extra"},
    ]

    await service.cleanup_if_flat(symbol="BTCUSDT", position_amt=0.0)

    canceled_ids = {str(call.get("clientAlgoId") or "") for call in rest.cancel_calls}
    assert {tp_id, sl_id, "x-extra"}.issubset(canceled_ids)
    rows = store.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"


@pytest.mark.asyncio
async def test_cleanup_if_flat_cancels_algo_id_only_open_order(tmp_path: Path) -> None:
    class _AlgoIdOnlyOpenOrderREST(_FakeAlgoREST):
        async def cancel_algo_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            self.cancel_calls.append(dict(params))
            if params.get("algoId") == 9101:
                self.open_orders = []
                return {"msg": "success"}
            raise RuntimeError("client_algo_cancel_rejected")

    store = _storage(tmp_path)
    rest = _AlgoIdOnlyOpenOrderREST()
    service = BracketService(
        planner=BracketPlanner(cfg=BracketConfig()),
        storage=store,
        rest_client=rest,
        mode="live",
    )
    await service.create_and_place(
        symbol="BTCUSDT",
        entry_side="BUY",
        position_side="BOTH",
        entry_price=100.0,
        quantity=0.01,
    )
    rest.open_orders = [{"symbol": "BTCUSDT", "algoId": 9101}]

    await service.cleanup_if_flat(symbol="BTCUSDT", position_amt=0.0)

    assert any(call.get("algoId") == 9101 for call in rest.cancel_calls)
    rows = store.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"


@pytest.mark.asyncio
async def test_recover_marks_cleaned_when_no_open_orders(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    rest = _FakeAlgoREST()
    service = BracketService(
        planner=BracketPlanner(cfg=BracketConfig()),
        storage=store,
        rest_client=rest,
        mode="live",
    )

    await service.create_and_place(
        symbol="BTCUSDT",
        entry_side="BUY",
        position_side="BOTH",
        entry_price=100.0,
        quantity=0.01,
    )
    rest.open_orders = []
    recovered = await service.recover(symbol="BTCUSDT")

    assert len(recovered) == 1
    assert recovered[0].state == "CLEANED"
    rows = store.list_bracket_states()
    assert rows[0]["state"] == "CLEANED"


@pytest.mark.asyncio
async def test_recover_sets_active_when_open_algo_order_exists(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    rest = _FakeAlgoREST()
    service = BracketService(
        planner=BracketPlanner(cfg=BracketConfig()),
        storage=store,
        rest_client=rest,
        mode="live",
    )
    created = await service.create_and_place(
        symbol="BTCUSDT",
        entry_side="BUY",
        position_side="BOTH",
        entry_price=100.0,
        quantity=0.01,
    )
    tp_id = str(created["planned"]["tp_client_algo_id"])
    rest.open_orders = [{"symbol": "BTCUSDT", "clientAlgoId": tp_id}]

    recovered = await service.recover(symbol="BTCUSDT")

    assert recovered[0].state == "ACTIVE"
    rows = store.list_bracket_states()
    assert rows[0]["state"] == "ACTIVE"


@pytest.mark.asyncio
async def test_reconcile_poller_run_once(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    rest = _FakeAlgoREST()
    service = BracketService(
        planner=BracketPlanner(cfg=BracketConfig()),
        storage=store,
        rest_client=rest,
        mode="live",
    )
    await service.create_and_place(
        symbol="BTCUSDT",
        entry_side="BUY",
        position_side="BOTH",
        entry_price=100.0,
        quantity=0.01,
    )
    rest.open_orders = []

    poller = BracketReconcilePoller(service=service, symbol="BTCUSDT", interval_seconds=1.0)
    out = await poller.run_once()

    assert len(out) == 1
    assert out[0].state == "CLEANED"


@pytest.mark.asyncio
async def test_live_bracket_quantizes_quantity_and_trigger_price(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    rest = _FakeAlgoREST()
    service = BracketService(
        planner=BracketPlanner(cfg=BracketConfig()),
        storage=store,
        rest_client=rest,
        mode="live",
    )

    await service.create_and_place(
        symbol="BTCUSDT",
        entry_side="BUY",
        position_side="BOTH",
        entry_price=100.123456789,
        quantity=0.123456789,
    )

    assert len(rest.place_calls) == 2
    for payload in rest.place_calls:
        assert payload["quantity"] == "0.123"
    by_type = {str(row.get("type") or ""): row for row in rest.place_calls}
    assert by_type["TAKE_PROFIT_MARKET"]["triggerPrice"] == "102.1"
    assert by_type["STOP_MARKET"]["triggerPrice"] == "99.1"
