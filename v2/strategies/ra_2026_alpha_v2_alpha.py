from __future__ import annotations

from typing import Literal

AlphaId = Literal["alpha_breakout", "alpha_pullback", "alpha_expansion", "alpha_drift"]
RegimeName = Literal["TREND_UP", "TREND_DOWN", "UNKNOWN"]
AllowedSideName = Literal["LONG", "SHORT", "NONE"]

SUPPORTED_SYMBOLS = ("BTCUSDT",)
DEFAULT_ALPHA_IDS: tuple[AlphaId, ...] = (
    "alpha_breakout",
    "alpha_pullback",
    "alpha_expansion",
)
ALL_ALPHA_IDS: tuple[AlphaId, ...] = DEFAULT_ALPHA_IDS + ("alpha_drift",)
ALPHA_ENTRY_FAMILY: dict[AlphaId, str] = {
    "alpha_breakout": "breakout",
    "alpha_pullback": "pullback",
    "alpha_expansion": "expansion",
    "alpha_drift": "drift",
}
BLOCK_REASON_PRIORITY = {
    "regime_missing": 0,
    "regime_adx_window_missing": 0,
    "regime_adx_rising_missing": 0,
    "bias_missing": 1,
    "trigger_missing": 2,
    "short_overextension_risk": 2,
    "quality_score_missing": 2,
    "quality_score_v2_missing": 2,
    "breakout_efficiency_missing": 2,
    "breakout_stability_missing": 2,
    "breakout_stability_edge_missing": 2,
    "volume_missing": 3,
    "cost_missing": 4,
}
