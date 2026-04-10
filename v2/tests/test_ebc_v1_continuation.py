from __future__ import annotations

from v2.strategies.ebc_v1_continuation import EBCV1Continuation


def _bars(closes: list[float], *, spread: float = 0.4) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    prev_close = float(closes[0])
    for close in closes:
        close_v = float(close)
        open_v = prev_close
        rows.append(
            {
                "open": float(open_v),
                "high": float(max(open_v, close_v) + spread),
                "low": float(min(open_v, close_v) - spread),
                "close": float(close_v),
                "volume": 1000.0,
            }
        )
        prev_close = close_v
    return rows


def _market_for_ebc_long() -> dict[str, list[dict[str, float]]]:
    closes_12h = [100.0 + (idx * 0.8) for idx in range(80)]
    closes_2h = [200.0 + (idx * 0.18) for idx in range(120)]
    closes_30m = [300.0 + ((idx % 2) * 0.01) for idx in range(90)] + [300.05]
    closes_5m = [400.0 + ((idx % 3) * 0.02) for idx in range(120)] + [400.25]
    return {
        "12h": _bars(closes_12h, spread=0.8),
        "2h": _bars(closes_2h, spread=0.5),
        "30m": _bars(closes_30m, spread=0.03),
        "5m": _bars(closes_5m, spread=0.02),
    }


def _market_for_ebc_short() -> dict[str, list[dict[str, float]]]:
    closes_12h = [200.0 - (idx * 0.8) for idx in range(80)]
    closes_2h = [300.0 - (idx * 0.18) for idx in range(120)]
    closes_30m = [400.0 - ((idx % 2) * 0.01) for idx in range(90)] + [399.95]
    closes_5m = [500.0 - ((idx % 3) * 0.02) for idx in range(120)] + [499.75]
    return {
        "12h": _bars(closes_12h, spread=0.8),
        "2h": _bars(closes_2h, spread=0.5),
        "30m": _bars(closes_30m, spread=0.03),
        "5m": _bars(closes_5m, spread=0.02),
    }


def test_ebc_v1_continuation_emits_long_signal() -> None:
    strategy = EBCV1Continuation(params={"supported_symbols": ["BTCUSDT"]})

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_ebc_long()})

    assert decision["intent"] == "LONG"
    assert decision["alpha_id"] == "alpha_ebc"
    assert decision["entry_family"] == "ebc_continuation"
    assert float(decision["execution"]["entry_quality_score_v2"]) == float(decision["score"])
    assert float(decision["execution"]["entry_regime_strength"]) > 0.0
    assert float(decision["execution"]["entry_bias_strength"]) > 0.0


def test_ebc_v1_continuation_blocks_when_quality_score_below_minimum() -> None:
    strategy = EBCV1Continuation(
        params={
            "supported_symbols": ["BTCUSDT"],
            "min_entry_score": 0.90,
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_ebc_long()})

    assert decision["intent"] == "NONE"
    assert decision["reason"] == "quality_score_below_min"
    assert float(decision["score"]) > 0.0
    assert decision["blocks"] == ["min_entry_score"]


def test_ebc_v1_continuation_supports_short_only_mode() -> None:
    strategy = EBCV1Continuation(
        params={
            "supported_symbols": ["BTCUSDT"],
            "side_mode": "SHORT",
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_ebc_short()})

    assert decision["intent"] == "SHORT"
    assert decision["side"] == "SELL"
    assert decision["allowed_side"] == "SHORT"


def test_ebc_v1_continuation_blocks_disallowed_side_mode() -> None:
    strategy = EBCV1Continuation(
        params={
            "supported_symbols": ["BTCUSDT"],
            "side_mode": "SHORT",
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_ebc_long()})

    assert decision["intent"] == "NONE"
    assert decision["reason"] == "side_not_allowed"
    assert decision["allowed_side"] == "SHORT"
    assert decision["blocks"] == ["side_mode"]
