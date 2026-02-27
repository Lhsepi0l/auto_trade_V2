from __future__ import annotations

from typing import Any

from v2.strategies.strategy_pack_v1 import StrategyPackV1


def _kline(value: float) -> list[float]:
    return [0, value, value + 1.0, value - 1.0, value + 0.1]


def _snapshot_with_series(value: float) -> dict[str, Any]:
    candles_4h = [_kline(value + 0.5 * idx) for idx in range(40)]
    candles_1h = [_kline(value + 0.1 * idx) for idx in range(5)]
    candles_15m = [_kline(value + 0.2 * idx) for idx in range(5)]
    return {
        "symbol": "BTCUSDT",
        "market": {
            "4h": candles_4h,
            "1h": candles_1h,
            "15m": candles_15m,
        },
    }


def _snapshot_with_1h_closes(closes: list[float]) -> dict[str, Any]:
    snapshot = _snapshot_with_series(100.0)
    snapshot["market"]["1h"] = [_kline(value) for value in closes]
    return snapshot


def test_forbidden_short_in_long_regime(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(params={})

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("BULL", False),
    )
    monkeypatch.setattr(
        strategy,
        "_entry_signal_1h",
        lambda _candles_1h, _mode, _allowed_side, _debug: {
            "long": True,
            "short": True,
            "mode": "pullback",
            "mean_reversion": False,
        },
    )
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])

    decision = strategy.decide(_snapshot_with_series(100.0))

    assert decision["intent"] == "LONG"
    assert decision["side"] == "BUY"
    assert decision["regime"] == "BULL"
    assert decision["allowed_side"] == "LONG"


def test_short_regime_never_emits_long(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(params={})

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("BEAR", False),
    )
    monkeypatch.setattr(
        strategy,
        "_entry_signal_1h",
        lambda _candles_1h, _mode, _allowed_side, _debug: {
            "long": True,
            "short": True,
            "mode": "pullback",
            "mean_reversion": False,
        },
    )
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])

    decision = strategy.decide(_snapshot_with_series(100.0))

    assert decision["intent"] == "SHORT"
    assert decision["side"] == "SELL"
    assert decision["regime"] == "BEAR"
    assert decision["allowed_side"] == "SHORT"


def test_sideways_without_mean_reversion_blocks_entry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(params={"mean_reversion_enabled": False})

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("SIDEWAYS", True),
    )
    monkeypatch.setattr(
        strategy,
        "_entry_signal_1h",
        lambda _candles_1h, _mode, _allowed_side, _debug: {
            "long": True,
            "short": False,
            "mode": "pullback",
            "mean_reversion": False,
        },
    )
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])

    decision = strategy.decide(_snapshot_with_series(100.0))

    assert decision["intent"] == "NONE"
    assert decision["side"] == "NONE"
    assert "sideways_regime" in decision["blocks"]
    assert "sideways_mr_disabled" in decision["blocks"]


def test_sideways_with_mean_reversion_allows_entry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(params={"mean_reversion_enabled": True})

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("SIDEWAYS", True),
    )
    monkeypatch.setattr(
        strategy,
        "_entry_signal_1h",
        lambda _candles_1h, _mode, _allowed_side, _debug: {
            "long": False,
            "short": False,
            "mode": "pullback",
            "mean_reversion": False,
        },
    )

    def _mr_signal(_candles_1h, _allowed_side, _debug):  # type: ignore[no-untyped-def]
        assert _allowed_side == "BOTH"
        return (
            {
                "long": True,
                "short": False,
                "mode": "pullback",
                "mean_reversion": True,
            },
            True,
        )

    monkeypatch.setattr(strategy, "_mean_reversion_signal_1h", _mr_signal)
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])

    decision = strategy.decide(_snapshot_with_series(100.0))

    assert decision["intent"] == "LONG"
    assert decision["side"] == "BUY"
    assert decision["regime"] == "SIDEWAYS"
    assert decision["allowed_side"] == "BOTH"


def test_sideways_with_mr_enabled_keeps_pullback_signal(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(params={"mean_reversion_enabled": True})

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("SIDEWAYS", True),
    )
    monkeypatch.setattr(
        strategy,
        "_entry_signal_1h",
        lambda _candles_1h, _mode, _allowed_side, _debug: {
            "long": True,
            "short": False,
            "mode": "pullback",
            "mean_reversion": False,
        },
    )
    monkeypatch.setattr(
        strategy,
        "_mean_reversion_signal_1h",
        lambda _candles_1h, _allowed_side, _debug: (
            {
                "long": False,
                "short": False,
                "mode": "pullback",
                "mean_reversion": True,
            },
            False,
        ),
    )
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])

    decision = strategy.decide(_snapshot_with_series(100.0))

    assert decision["intent"] == "LONG"
    assert decision["side"] == "BUY"


def test_donchian_mode_can_emit_long_entry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(
        params={
            "entry_mode": "donchian",
            "donchian_period": 3,
            "donchian_anti_fake": 0.0,
        }
    )

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("BULL", False),
    )
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])

    snapshot = _snapshot_with_series(100.0)
    snapshot["market"]["1h"] = [
        _kline(100.0),
        _kline(101.0),
        _kline(102.0),
        _kline(104.0),
    ]

    decision = strategy.decide(snapshot)

    assert decision["intent"] == "LONG"
    assert decision["side"] == "BUY"
    assert decision["reason"] == "entry_donchian_long"


def test_donchian_short_breakdown_rejects_fake_down_momentum(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(
        params={
            "entry_mode": "donchian",
            "donchian_period": 3,
            "donchian_anti_fake": 0.0,
            "donchian_fast_ema_period": 3,
            "donchian_slow_ema_period": 8,
        }
    )

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("BEAR", False),
    )
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])

    snapshot = _snapshot_with_1h_closes(
        [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0, 104.0]
    )

    decision = strategy.decide(snapshot)

    assert decision["intent"] == "NONE"
    assert decision["reason"] == "no_entry:donchian"
    assert decision["filters"]["donchian_momentum_ready"] is True
    assert decision["filters"]["donchian_momentum_short_ok"] is False


def test_donchian_long_breakout_rejects_fake_up_momentum(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(
        params={
            "entry_mode": "donchian",
            "donchian_period": 3,
            "donchian_anti_fake": 0.0,
            "donchian_fast_ema_period": 3,
            "donchian_slow_ema_period": 8,
        }
    )

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("BULL", False),
    )
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])

    snapshot = _snapshot_with_1h_closes(
        [110.0, 109.0, 108.0, 107.0, 106.0, 105.0, 104.0, 103.0, 102.0, 101.0, 100.0, 106.0]
    )

    decision = strategy.decide(snapshot)

    assert decision["intent"] == "NONE"
    assert decision["reason"] == "no_entry:donchian"
    assert decision["filters"]["donchian_momentum_ready"] is True
    assert decision["filters"]["donchian_momentum_long_ok"] is False


def test_donchian_short_breakdown_enters_when_momentum_aligned(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(
        params={
            "entry_mode": "donchian",
            "donchian_period": 3,
            "donchian_anti_fake": 0.0,
            "donchian_fast_ema_period": 3,
            "donchian_slow_ema_period": 8,
        }
    )

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("BEAR", False),
    )
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])

    snapshot = _snapshot_with_1h_closes(
        [110.0, 109.0, 108.0, 107.0, 106.0, 105.0, 104.0, 103.0, 102.0, 101.0, 100.0, 98.0]
    )

    decision = strategy.decide(snapshot)

    assert decision["intent"] == "SHORT"
    assert decision["side"] == "SELL"
    assert decision["reason"] == "entry_donchian_short"


def test_pullback_mode_can_emit_long_entry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(
        params={
            "entry_mode": "pullback",
            "donchian_period": 3,
        }
    )

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("BULL", False),
    )
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])

    snapshot = _snapshot_with_series(100.0)
    snapshot["market"]["1h"] = [
        [0, 100.0, 101.0, 99.0, 100.0],
        [0, 100.5, 101.0, 99.5, 100.2],
        [0, 91.0, 92.0, 90.0, 91.0],
        [0, 95.0, 97.0, 94.0, 95.0],
    ]

    decision = strategy.decide(snapshot)

    assert decision["intent"] == "LONG"
    assert decision["side"] == "BUY"
    assert decision["reason"] == "entry_pullback_long"


def test_overheat_cache_is_scoped_per_symbol() -> None:
    calls: dict[str, int] = {}

    def _fetch(symbol: str):
        calls[symbol] = calls.get(symbol, 0) + 1
        if symbol == "BTCUSDT":
            return (0.0010, 1.60)
        return (0.0, 1.0)

    strategy = StrategyPackV1(params={"overheat_cache_ttl": 60}, overheat_fetcher=_fetch)

    btc = strategy._fetch_overheat("BTCUSDT")
    eth = strategy._fetch_overheat("ETHUSDT")
    btc_again = strategy._fetch_overheat("BTCUSDT")

    assert btc == (0.0010, 1.60)
    assert eth == (0.0, 1.0)
    assert btc_again == btc
    assert calls["BTCUSDT"] == 1
    assert calls["ETHUSDT"] == 1


def test_decide_ignores_malformed_short_ohlc_rows() -> None:
    strategy = StrategyPackV1(params={})
    snapshot: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "market": {
            "4h": [[0, 1.0, 2.0, 0.5] for _ in range(60)],
            "1h": [[0, 1.0, 2.0, 0.5] for _ in range(60)],
            "15m": [[0, 1.0, 2.0, 0.5] for _ in range(60)],
        },
    }

    decision = strategy.decide(snapshot)

    assert decision["intent"] == "NONE"
    assert "insufficient_4h_data" in decision.get("blocks", [])


def test_overheat_block_blocks_all_entries(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(params={})

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("BULL", False),
    )
    monkeypatch.setattr(
        strategy,
        "_entry_signal_1h",
        lambda _candles_1h, _mode, _allowed_side, _debug: {
            "long": True,
            "short": False,
            "mode": "pullback",
            "mean_reversion": False,
        },
    )
    monkeypatch.setattr(
        strategy,
        "_eval_overheat_blocks",
        lambda _side, _symbol: ["overheat_funding_long"],
    )

    decision = strategy.decide(_snapshot_with_series(100.0))

    assert decision["intent"] == "NONE"
    assert decision["side"] == "NONE"
    assert "overheat_funding_long" in decision["blocks"]


def test_journal_contains_regime_and_signals(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    journal_entries: list[dict[str, Any]] = []

    def _logger(payload: dict[str, Any]) -> None:
        journal_entries.append(payload)

    strategy = StrategyPackV1(params={}, logger=_logger)
    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("BULL", False),
    )
    monkeypatch.setattr(
        strategy,
        "_entry_signal_1h",
        lambda _candles_1h, _mode, _allowed_side, _debug: {
            "long": True,
            "short": False,
            "mode": "pullback",
            "mean_reversion": False,
        },
    )
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])

    decision = strategy.decide(_snapshot_with_series(100.0))

    assert decision["intent"] == "LONG"
    assert len(journal_entries) == 1
    assert journal_entries[0]["regime"] == "BULL"
    assert journal_entries[0]["allowed_side"] == "LONG"
    assert isinstance(journal_entries[0]["signals"], dict)
    assert "filters" in journal_entries[0]


def test_score_conf_threshold_blocks_entry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(
        params={
            "score_conf_threshold": 0.6,
            "score_gap_threshold": 0.0,
            "tf_weight_1h": 0.34,
            "tf_weight_4h": 0.33,
            "tf_weight_30m": 0.33,
            "tf_weight_10m": 0.0,
            "tf_weight_15m": 0.0,
        }
    )

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("BULL", False),
    )
    monkeypatch.setattr(
        strategy,
        "_entry_signal_1h",
        lambda _candles_1h, _mode, _allowed_side, _debug: {
            "long": True,
            "short": False,
            "mode": "pullback",
            "mean_reversion": False,
        },
    )
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])
    monkeypatch.setattr(strategy, "_timeframe_momentum", lambda _candles: ("LONG", 0.5))

    snapshot = _snapshot_with_series(100.0)
    snapshot["market"]["30m"] = list(snapshot["market"]["15m"])
    decision = strategy.decide(snapshot)

    assert decision["intent"] == "NONE"
    assert decision["reason"] == "confidence_below_threshold"


def test_score_gap_threshold_blocks_entry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    strategy = StrategyPackV1(
        params={
            "score_conf_threshold": 0.2,
            "score_gap_threshold": 0.1,
            "tf_weight_1h": 0.34,
            "tf_weight_4h": 0.33,
            "tf_weight_30m": 0.33,
            "tf_weight_10m": 0.0,
            "tf_weight_15m": 0.0,
        }
    )

    monkeypatch.setattr(
        strategy,
        "_regime",
        lambda _candles_4h, _debug: ("BULL", False),
    )
    monkeypatch.setattr(
        strategy,
        "_entry_signal_1h",
        lambda _candles_1h, _mode, _allowed_side, _debug: {
            "long": True,
            "short": False,
            "mode": "pullback",
            "mean_reversion": False,
        },
    )
    monkeypatch.setattr(strategy, "_eval_overheat_blocks", lambda _side, _symbol: [])

    def _tf_signal(candles):  # type: ignore[no-untyped-def]
        if len(candles) >= 40:
            return ("SHORT", 0.8)
        slope = candles[1].close - candles[0].close
        if slope >= 0.19:
            return ("SHORT", 0.1)
        return ("LONG", 0.8)

    monkeypatch.setattr(strategy, "_timeframe_momentum", _tf_signal)

    snapshot = _snapshot_with_series(100.0)
    snapshot["market"]["30m"] = list(snapshot["market"]["15m"])
    decision = strategy.decide(snapshot)

    assert decision["intent"] == "NONE"
    assert decision["reason"] == "gap_below_threshold"
