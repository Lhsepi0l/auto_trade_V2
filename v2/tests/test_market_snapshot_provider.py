from __future__ import annotations

from datetime import datetime, timedelta, timezone

from v2.kernel.kernel import _build_market_snapshot_provider


class _FakeRestClient:
    def __init__(self, *, payloads: dict[tuple[str, str], list[list[float]]]) -> None:
        self._payloads = payloads

    async def public_request(self, method: str, path: str, params: dict[str, object]):
        assert method == "GET"
        if path == "/fapi/v1/klines":
            symbol = str(params["symbol"])
            interval = str(params["interval"])
            return list(self._payloads[(symbol, interval)])
        if path == "/fapi/v1/ticker/bookTicker":
            return {"symbol": str(params["symbol"]), "bidPrice": "1", "askPrice": "1"}
        raise AssertionError(f"unexpected path: {path}")


def _kline_row(*, open_time_ms: int, close_time_ms: int, close: float) -> list[float]:
    return [
        float(open_time_ms),
        float(close),
        float(close) + 1.0,
        float(close) - 1.0,
        float(close),
        100.0,
        float(close_time_ms),
        100.0,
        1.0,
        50.0,
        50.0,
        0.0,
    ]


def test_market_snapshot_provider_drops_incomplete_last_kline() -> None:
    now = datetime.now(timezone.utc)
    past_open = int((now - timedelta(minutes=30)).timestamp() * 1000)
    past_close = int((now - timedelta(minutes=15)).timestamp() * 1000)
    current_open = int((now - timedelta(minutes=5)).timestamp() * 1000)
    current_close = int((now + timedelta(minutes=10)).timestamp() * 1000)

    rest = _FakeRestClient(
        payloads={
            ("BTCUSDT", "15m"): [
                _kline_row(open_time_ms=past_open, close_time_ms=past_close, close=100.0),
                _kline_row(open_time_ms=current_open, close_time_ms=current_close, close=101.0),
            ]
        }
    )

    provider = _build_market_snapshot_provider(
        rest_client=rest,
        symbols=["BTCUSDT"],
        behavior=None,
    )

    assert provider is not None
    snapshot = provider()
    rows = snapshot["symbols"]["BTCUSDT"]["15m"]
    assert len(rows) == 1
    assert float(rows[-1][4]) == 100.0

