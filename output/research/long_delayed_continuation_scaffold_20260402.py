from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Side = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class LongDelayedContinuationSetup:
    open_time_ms: int
    side: Side
    setup_close: float
    setup_low: float
    range_atr: float
    body_ratio: float
    favored_close: float
    width_expansion_frac: float
    edge_ratio: float
    expires_after_bars: int = 8


def qualifies_long_delayed_continuation_setup(
    *,
    side: str,
    range_atr: float,
    body_ratio: float,
    favored_close_long: float,
    width_expansion_frac: float,
    edge_ratio: float,
) -> bool:
    return (
        str(side) == "LONG"
        and float(range_atr) >= 1.2
        and float(body_ratio) >= 0.5
        and float(favored_close_long) < 0.6
        and float(width_expansion_frac) >= 0.10
        and float(edge_ratio) >= 1.10
    )


def build_long_setup(
    *,
    open_time_ms: int,
    setup_close: float,
    setup_low: float,
    range_atr: float,
    body_ratio: float,
    favored_close_long: float,
    width_expansion_frac: float,
    edge_ratio: float,
) -> LongDelayedContinuationSetup:
    return LongDelayedContinuationSetup(
        open_time_ms=int(open_time_ms),
        side="LONG",
        setup_close=float(setup_close),
        setup_low=float(setup_low),
        range_atr=float(range_atr),
        body_ratio=float(body_ratio),
        favored_close=float(favored_close_long),
        width_expansion_frac=float(width_expansion_frac),
        edge_ratio=float(edge_ratio),
    )


def should_confirm_long(
    *,
    setup: LongDelayedContinuationSetup,
    current_close: float,
    ema_15m: float,
    bars_since_setup: int,
) -> bool:
    if int(bars_since_setup) <= 0 or int(bars_since_setup) > int(setup.expires_after_bars):
        return False
    return float(current_close) > max(float(setup.setup_close), float(ema_15m))


def should_cancel_long(
    *,
    setup: LongDelayedContinuationSetup,
    current_low: float,
    bars_since_setup: int,
) -> bool:
    if int(bars_since_setup) > int(setup.expires_after_bars):
        return True
    return float(current_low) < float(setup.setup_low)


__all__ = [
    "LongDelayedContinuationSetup",
    "qualifies_long_delayed_continuation_setup",
    "build_long_setup",
    "should_confirm_long",
    "should_cancel_long",
]
