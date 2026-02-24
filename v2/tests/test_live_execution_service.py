from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from v2.clean_room import BinanceLiveExecutionService, Candidate, KernelContext, SizePlan
from v2.exchange import BinanceRESTError


@dataclass
class _FakeREST:
    calls: list[dict[str, Any]]
    leverage_calls: list[dict[str, Any]]

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
                        {
                            "filterType": "LOT_SIZE",
                            "stepSize": "0.001",
                            "minQty": "0.001",
                        },
                        {
                            "filterType": "MIN_NOTIONAL",
                            "minNotional": "5",
                        },
                    ],
                }
            ]
        }

    async def change_leverage(self, *, symbol: str, leverage: int) -> dict[str, Any]:
        self.leverage_calls.append({"symbol": symbol, "leverage": leverage})
        return {"symbol": symbol, "leverage": leverage}

    async def place_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(params))
        return {"orderId": 12345, "status": "NEW"}


def test_live_execution_places_market_order() -> None:
    rest = _FakeREST(calls=[], leverage_calls=[])
    svc = BinanceLiveExecutionService(rest_client=rest)
    out = svc.execute(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0),
        size=SizePlan(symbol="BTCUSDT", qty=0.01, leverage=1.0, notional=1.0),
        context=KernelContext(
            mode="live", profile="normal", symbol="BTCUSDT", tick=1, dry_run=False
        ),
    )

    assert out.ok is True
    assert out.reason == "live_order_submitted"
    assert out.order_id == "12345"
    assert len(rest.calls) == 1
    assert len(rest.leverage_calls) == 1
    assert rest.leverage_calls[0]["symbol"] == "BTCUSDT"
    assert rest.leverage_calls[0]["leverage"] == 1
    assert rest.calls[0]["symbol"] == "BTCUSDT"
    assert rest.calls[0]["side"] == "BUY"
    assert rest.calls[0]["type"] == "MARKET"


def test_live_execution_dry_run_rejected() -> None:
    rest = _FakeREST(calls=[], leverage_calls=[])
    svc = BinanceLiveExecutionService(rest_client=rest)
    out = svc.execute(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0),
        size=SizePlan(symbol="BTCUSDT", qty=0.01, leverage=1.0, notional=1.0),
        context=KernelContext(
            mode="shadow", profile="normal", symbol="BTCUSDT", tick=1, dry_run=True
        ),
    )
    assert out.ok is False
    assert out.reason == "dry_run_mode"
    assert len(rest.calls) == 0
    assert len(rest.leverage_calls) == 0


def test_live_execution_rejects_below_min_qty() -> None:
    @dataclass
    class _MinQtyREST(_FakeREST):
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
                            {
                                "filterType": "LOT_SIZE",
                                "stepSize": "0.001",
                                "minQty": "0.010",
                            }
                        ],
                    }
                ]
            }

    rest = _MinQtyREST(calls=[], leverage_calls=[])
    svc = BinanceLiveExecutionService(rest_client=rest)
    out = svc.execute(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100000.0),
        size=SizePlan(symbol="BTCUSDT", qty=0.0002, leverage=1.0, notional=0.02),
        context=KernelContext(
            mode="live", profile="normal", symbol="BTCUSDT", tick=1, dry_run=False
        ),
    )

    assert out.ok is False
    assert out.reason == "quantity_below_min_qty"
    assert len(rest.calls) == 0


def test_live_execution_expands_qty_to_min_notional() -> None:
    rest = _FakeREST(calls=[], leverage_calls=[])
    svc = BinanceLiveExecutionService(rest_client=rest)
    out = svc.execute(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0),
        size=SizePlan(symbol="BTCUSDT", qty=0.02, leverage=1.0, notional=2.0),
        context=KernelContext(
            mode="live", profile="normal", symbol="BTCUSDT", tick=1, dry_run=False
        ),
    )

    assert out.ok is True
    assert len(rest.calls) == 1
    assert rest.calls[0]["quantity"] == "0.05000000"


def test_live_execution_includes_binance_error_code() -> None:
    @dataclass
    class _ErrREST(_FakeREST):
        async def place_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            _ = params
            raise BinanceRESTError(
                status_code=400,
                code=-1111,
                message="precision too long",
                path="/fapi/v1/order",
            )

    rest = _ErrREST(calls=[], leverage_calls=[])
    svc = BinanceLiveExecutionService(rest_client=rest)
    out = svc.execute(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0),
        size=SizePlan(symbol="BTCUSDT", qty=0.01, leverage=1.0, notional=1.0),
        context=KernelContext(
            mode="live", profile="normal", symbol="BTCUSDT", tick=1, dry_run=False
        ),
    )

    assert out.ok is False
    assert out.reason == "live_order_failed:BinanceRESTError:-1111"
