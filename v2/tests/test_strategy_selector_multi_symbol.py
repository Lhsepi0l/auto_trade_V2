from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from v2.clean_room.contracts import KernelContext
from v2.strategies.strategy_pack_v1 import StrategyPackV1CandidateSelector


@dataclass
class _FakeStrategy:
    decisions: dict[str, dict[str, Any]]
    name: str = "fake_strategy"

    def decide(self, market_snapshot: dict[str, Any]) -> dict[str, Any]:
        symbol = str(market_snapshot.get("symbol") or "")
        base = self.decisions.get(symbol, {})
        out = dict(base)
        out.setdefault("symbol", symbol)
        out.setdefault("intent", "NONE")
        out.setdefault("side", "NONE")
        out.setdefault("score", 0.0)
        return out


def test_selector_picks_highest_score_from_universe_symbols() -> None:
    strategy = _FakeStrategy(
        decisions={
            "BTCUSDT": {"intent": "LONG", "side": "BUY", "score": 0.4, "entry_price": 100.0},
            "ETHUSDT": {"intent": "LONG", "side": "BUY", "score": 0.9, "entry_price": 200.0},
        }
    )
    selector = StrategyPackV1CandidateSelector(
        strategy=strategy,  # type: ignore[arg-type]
        symbols=["BTCUSDT", "ETHUSDT"],
        snapshot_provider=lambda: {
            "symbols": {
                "BTCUSDT": {"4h": [], "1h": [], "15m": []},
                "ETHUSDT": {"4h": [], "1h": [], "15m": []},
            }
        },
    )

    candidate = selector.select(
        context=KernelContext(
            mode="live", profile="normal", symbol="BTCUSDT", tick=1, dry_run=False
        )
    )
    assert candidate is not None
    assert candidate.symbol == "ETHUSDT"
    assert candidate.side == "BUY"
    assert candidate.score == 0.9


def test_selector_respects_updated_universe_symbols() -> None:
    strategy = _FakeStrategy(
        decisions={
            "BTCUSDT": {"intent": "LONG", "side": "BUY", "score": 0.8, "entry_price": 100.0},
            "ETHUSDT": {"intent": "LONG", "side": "BUY", "score": 0.7, "entry_price": 200.0},
        }
    )
    selector = StrategyPackV1CandidateSelector(
        strategy=strategy,  # type: ignore[arg-type]
        symbols=["ETHUSDT"],
        snapshot_provider=lambda: {},
    )
    selector.set_symbols(["BTCUSDT"])

    candidate = selector.select(
        context=KernelContext(
            mode="live", profile="normal", symbol="ETHUSDT", tick=1, dry_run=False
        )
    )
    assert candidate is not None
    assert candidate.symbol == "BTCUSDT"
