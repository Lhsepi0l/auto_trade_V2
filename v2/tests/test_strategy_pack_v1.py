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
