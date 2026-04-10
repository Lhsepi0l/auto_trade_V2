from __future__ import annotations

import sqlite3
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from v2.exchange import BinanceRESTError
from v2.kernel import BinanceLiveExecutionService, Candidate, KernelContext, SizePlan
from v2.storage import RuntimeStorage


@dataclass
class _FakeREST:
    calls: list[dict[str, Any]]
    leverage_calls: list[dict[str, Any]]
    queried_orders: dict[str, dict[str, Any]] | None = None

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

    async def get_balances(self) -> list[dict[str, Any]]:
        return [{"asset": "USDT", "availableBalance": "1000"}]

    async def place_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(params))
        return {"orderId": 12345, "status": "NEW"}

    async def signed_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = method
        _ = path
        client_order_id = str((params or {}).get("origClientOrderId") or "")
        payload = dict((self.queried_orders or {}).get(client_order_id) or {})
        return payload


def test_live_execution_places_market_order() -> None:
    rest = _FakeREST(calls=[], leverage_calls=[])
    svc = BinanceLiveExecutionService(rest_client=rest)
    out = svc.execute(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0),
        size=SizePlan(symbol="BTCUSDT", qty=0.01, leverage=1.0, notional=1.0),
        context=KernelContext(
            mode="live", profile="ra_2026_alpha_v2_expansion_live_candidate", symbol="BTCUSDT", tick=1, dry_run=False
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
            mode="shadow", profile="ra_2026_alpha_v2_expansion_live_candidate", symbol="BTCUSDT", tick=1, dry_run=True
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
            mode="live", profile="ra_2026_alpha_v2_expansion_live_candidate", symbol="BTCUSDT", tick=1, dry_run=False
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
            mode="live", profile="ra_2026_alpha_v2_expansion_live_candidate", symbol="BTCUSDT", tick=1, dry_run=False
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
            mode="live", profile="ra_2026_alpha_v2_expansion_live_candidate", symbol="BTCUSDT", tick=1, dry_run=False
        ),
    )

    assert out.ok is False
    assert out.reason == "live_order_failed:BinanceRESTError:-1111"


def test_live_execution_rejects_when_available_margin_insufficient() -> None:
    @dataclass
    class _LowBalanceREST(_FakeREST):
        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "0.20"}]

    rest = _LowBalanceREST(calls=[], leverage_calls=[])
    svc = BinanceLiveExecutionService(rest_client=rest)
    out = svc.execute(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0),
        size=SizePlan(symbol="BTCUSDT", qty=0.05, leverage=1.0, notional=5.0),
        context=KernelContext(
            mode="live", profile="ra_2026_alpha_v2_expansion_live_candidate", symbol="BTCUSDT", tick=1, dry_run=False
        ),
    )

    assert out.ok is False
    assert str(out.reason).startswith("insufficient_available_margin:")
    assert len(rest.calls) == 0


def test_live_execution_downsizes_qty_to_fit_available_margin() -> None:
    @dataclass
    class _TightBalanceREST(_FakeREST):
        async def get_balances(self) -> list[dict[str, Any]]:
            return [{"asset": "USDT", "availableBalance": "10.0"}]

    rest = _TightBalanceREST(calls=[], leverage_calls=[])
    svc = BinanceLiveExecutionService(rest_client=rest)
    out = svc.execute(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0),
        size=SizePlan(symbol="BTCUSDT", qty=1.0, leverage=10.0, notional=100.0),
        context=KernelContext(
            mode="live",
            profile="ra_2026_alpha_v2_expansion_live_candidate",
            symbol="BTCUSDT",
            tick=1,
            dry_run=False,
        ),
    )

    assert out.ok is True
    assert len(rest.calls) == 1
    assert rest.calls[0]["quantity"] == "0.99000000"


def test_live_execution_reuses_existing_submission_for_same_intent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "live_execution_idempotency.sqlite3"))
    storage.ensure_schema()
    rest = _FakeREST(calls=[], leverage_calls=[])
    svc = BinanceLiveExecutionService(rest_client=rest, storage=storage)
    candidate = Candidate(
        symbol="BTCUSDT",
        side="BUY",
        score=1.0,
        entry_price=100.0,
        alpha_id="alpha_expansion",
        entry_family="breakout",
    )
    size = SizePlan(symbol="BTCUSDT", qty=0.01, leverage=1.0, notional=1.0)
    context = KernelContext(
        mode="live",
        profile="ra_2026_alpha_v2_expansion_candidate",
        symbol="BTCUSDT",
        tick=1,
        dry_run=False,
    )

    first = svc.execute(candidate=candidate, size=size, context=context)
    second = svc.execute(candidate=candidate, size=size, context=context)

    assert first.ok is True
    assert second.ok is True
    assert second.reason == "live_order_reused:SUBMITTED"
    assert len(rest.calls) == 1
    assert len(rest.leverage_calls) == 1


def test_live_execution_allows_distinct_tick_intents(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = RuntimeStorage(sqlite_path=str(tmp_path / "live_execution_tick.sqlite3"))
    storage.ensure_schema()
    rest = _FakeREST(calls=[], leverage_calls=[])
    svc = BinanceLiveExecutionService(rest_client=rest, storage=storage)
    candidate = Candidate(
        symbol="BTCUSDT",
        side="BUY",
        score=1.0,
        entry_price=100.0,
        alpha_id="alpha_expansion",
        entry_family="breakout",
    )
    size = SizePlan(symbol="BTCUSDT", qty=0.01, leverage=1.0, notional=1.0)

    first = svc.execute(
        candidate=candidate,
        size=size,
        context=KernelContext(
            mode="live",
            profile="ra_2026_alpha_v2_expansion_candidate",
            symbol="BTCUSDT",
            tick=10,
            dry_run=False,
        ),
    )
    second = svc.execute(
        candidate=candidate,
        size=size,
        context=KernelContext(
            mode="live",
            profile="ra_2026_alpha_v2_expansion_candidate",
            symbol="BTCUSDT",
            tick=11,
            dry_run=False,
        ),
    )

    assert first.ok is True
    assert second.ok is True
    assert second.reason == "live_order_submitted"
    assert len(rest.calls) == 2


def test_live_execution_allows_retry_after_idempotency_window_expires(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sqlite_path = tmp_path / "live_execution_window.sqlite3"
    storage = RuntimeStorage(sqlite_path=str(sqlite_path))
    storage.ensure_schema()
    rest = _FakeREST(calls=[], leverage_calls=[])
    svc = BinanceLiveExecutionService(
        rest_client=rest,
        storage=storage,
        idempotency_window_sec=5.0,
    )
    candidate = Candidate(
        symbol="BTCUSDT",
        side="BUY",
        score=1.0,
        entry_price=100.0,
        alpha_id="alpha_expansion",
        entry_family="breakout",
    )
    size = SizePlan(symbol="BTCUSDT", qty=0.01, leverage=1.0, notional=1.0)
    context = KernelContext(
        mode="live",
        profile="ra_2026_alpha_v2_expansion_candidate",
        symbol="BTCUSDT",
        tick=10,
        dry_run=False,
    )

    first = svc.execute(candidate=candidate, size=size, context=context)
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute(
            "UPDATE submission_intents SET updated_at=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),),
        )
    second = svc.execute(candidate=candidate, size=size, context=context)

    assert first.ok is True
    assert second.ok is True
    assert second.reason == "live_order_submitted"
    assert len(rest.calls) == 2


def test_live_execution_recovers_submitted_order_after_submit_timeout(tmp_path) -> None:  # type: ignore[no-untyped-def]
    @dataclass
    class _TimeoutThenQueryREST(_FakeREST):
        async def place_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            raise FutureTimeoutError()

        async def signed_request(
            self,
            method: str,
            path: str,
            *,
            params: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if path == "/fapi/v1/order":
                return {
                    "orderId": 98765,
                    "clientOrderId": str((params or {}).get("origClientOrderId") or ""),
                    "status": "NEW",
                }
            return await super().signed_request(method, path, params=params)

    storage = RuntimeStorage(sqlite_path=str(tmp_path / "live_execution_recover.sqlite3"))
    storage.ensure_schema()
    rest = _TimeoutThenQueryREST(calls=[], leverage_calls=[], queried_orders={})
    svc = BinanceLiveExecutionService(rest_client=rest, storage=storage)

    out = svc.execute(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0),
        size=SizePlan(symbol="BTCUSDT", qty=0.01, leverage=1.0, notional=1.0),
        context=KernelContext(
            mode="live",
            profile="ra_2026_alpha_v2_expansion_candidate",
            symbol="BTCUSDT",
            tick=20,
            dry_run=False,
        ),
    )

    assert out.ok is True
    assert out.reason == "live_order_recovered_after_submit_error"
    rows = storage.list_submission_intents()
    assert rows[0]["status"] == "SUBMITTED"
    assert rows[0]["order_id"] == "98765"


def test_live_execution_marks_review_required_when_submit_timeout_not_found(tmp_path) -> None:  # type: ignore[no-untyped-def]
    @dataclass
    class _TimeoutNotFoundREST(_FakeREST):
        async def place_order(self, *, params: dict[str, Any]) -> dict[str, Any]:
            raise FutureTimeoutError()

        async def signed_request(
            self,
            method: str,
            path: str,
            *,
            params: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            _ = method
            _ = params
            if path == "/fapi/v1/order":
                raise BinanceRESTError(
                    status_code=400,
                    code=-2013,
                    message="order does not exist",
                    path=path,
                )
            return await super().signed_request(method, path, params=params)

    storage = RuntimeStorage(sqlite_path=str(tmp_path / "live_execution_review.sqlite3"))
    storage.ensure_schema()
    rest = _TimeoutNotFoundREST(calls=[], leverage_calls=[], queried_orders={})
    svc = BinanceLiveExecutionService(rest_client=rest, storage=storage)

    out = svc.execute(
        candidate=Candidate(symbol="BTCUSDT", side="BUY", score=1.0, entry_price=100.0),
        size=SizePlan(symbol="BTCUSDT", qty=0.01, leverage=1.0, notional=1.0),
        context=KernelContext(
            mode="live",
            profile="ra_2026_alpha_v2_expansion_candidate",
            symbol="BTCUSDT",
            tick=21,
            dry_run=False,
        ),
    )

    assert out.ok is False
    assert out.reason == "live_order_review_required:not_found_after_submit_error"
    rows = storage.list_submission_intents()
    assert rows[0]["status"] == "REVIEW_REQUIRED"
