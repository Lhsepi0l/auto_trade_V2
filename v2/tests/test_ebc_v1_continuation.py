from __future__ import annotations

from v2.kernel.contracts import KernelContext
from v2.strategies.ebc_v1_continuation import (
    EBCV1Continuation,
    EBCV1ContinuationCandidateSelector,
)


def _test_signal_params() -> dict[str, object]:
    return {
        "band_location_max_long": 1.0,
        "band_location_min_short": 0.0,
        "max_extension_risk": 1.0,
        "trigger_cross_lookback_5m": 12,
        "trigger_cci_reset_level": 50.0,
        "bias_max_distance_frac_2h": 0.02,
    }


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
    closes_30m = ([300.0 + ((idx % 2) * 0.002) for idx in range(70)] + [299.96, 299.98, 300.00, 300.02, 300.04, 300.07, 300.10, 300.14, 300.18, 300.22, 300.26])
    closes_5m = ([400.0 + ((idx % 2) * 0.003) for idx in range(110)] + [399.94, 399.96, 399.98, 400.00, 400.03, 400.07, 400.12, 400.18, 400.24, 400.30, 400.36])
    return {
        "12h": _bars(closes_12h, spread=0.8),
        "2h": _bars(closes_2h, spread=0.5),
        "30m": _bars(closes_30m, spread=0.03),
        "5m": _bars(closes_5m, spread=0.02),
    }


def _market_for_ebc_short() -> dict[str, list[dict[str, float]]]:
    closes_12h = [200.0 - (idx * 0.8) for idx in range(80)]
    closes_2h = [300.0 - (idx * 0.18) for idx in range(120)]
    closes_30m = ([400.0 - ((idx % 2) * 0.002) for idx in range(70)] + [400.04, 400.02, 400.00, 399.98, 399.96, 399.93, 399.90, 399.86, 399.82, 399.78, 399.74])
    closes_5m = ([500.0 - ((idx % 2) * 0.003) for idx in range(110)] + [500.06, 500.04, 500.02, 500.00, 499.97, 499.93, 499.88, 499.82, 499.76, 499.70, 499.64])
    return {
        "12h": _bars(closes_12h, spread=0.8),
        "2h": _bars(closes_2h, spread=0.5),
        "30m": _bars(closes_30m, spread=0.03),
        "5m": _bars(closes_5m, spread=0.02),
    }


def test_ebc_v1_continuation_emits_long_signal() -> None:
    strategy = EBCV1Continuation(params={"supported_symbols": ["BTCUSDT"], **_test_signal_params()})

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_ebc_long()})

    assert decision["intent"] == "LONG"
    assert decision["alpha_id"] == "alpha_ebc"
    assert decision["entry_family"] == "ebc_continuation"
    assert float(decision["portfolio_score"]) >= float(decision["score"])
    assert float(decision["risk_per_trade_pct"]) > 0.0
    assert float(decision["max_effective_leverage"]) >= 1.0
    assert float(decision["stop_distance_frac"]) > 0.0
    assert decision["sl_tp"]["stop_loss"] > 0.0
    assert float(decision["execution"]["entry_quality_score_v2"]) == float(decision["score"])
    assert float(decision["execution"]["entry_regime_strength"]) > 0.0
    assert float(decision["execution"]["entry_bias_strength"]) > 0.0


def test_ebc_v1_continuation_blocks_when_quality_score_below_minimum() -> None:
    strategy = EBCV1Continuation(
        params={
            "supported_symbols": ["BTCUSDT"],
            "min_entry_score": 0.90,
            **_test_signal_params(),
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
            **_test_signal_params(),
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
            **_test_signal_params(),
        }
    )

    decision = strategy.decide({"symbol": "BTCUSDT", "market": _market_for_ebc_long()})

    assert decision["intent"] == "NONE"
    assert decision["reason"] == "side_not_allowed"
    assert decision["allowed_side"] == "SHORT"
    assert decision["blocks"] == ["side_mode"]


def test_ebc_candidate_selector_ranks_rotation_candidates() -> None:
    strategy = EBCV1Continuation(
        params={
            "supported_symbols": ["BTCUSDT", "ETHUSDT"],
            "min_entry_score": 0.0,
            **_test_signal_params(),
        }
    )

    selector = EBCV1ContinuationCandidateSelector(
        strategy=strategy,
        symbols=["BTCUSDT", "ETHUSDT"],
        snapshot_provider=lambda: {
            "symbols": {
                "BTCUSDT": _market_for_ebc_long(),
                "ETHUSDT": _market_for_ebc_short(),
            }
        },
    )

    ranked = selector.rank(
        context=KernelContext(
            mode="shadow",
            profile="ebc_v1_continuation_research",
            symbol="BTCUSDT",
            tick=0,
            dry_run=True,
        )
    )

    assert len(ranked) == 2
    assert float(ranked[0].portfolio_score or 0.0) >= float(ranked[1].portfolio_score or 0.0)
    assert {candidate.symbol for candidate in ranked} == {"BTCUSDT", "ETHUSDT"}
