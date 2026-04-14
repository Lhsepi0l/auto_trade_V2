from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class _ReplayDecision:
    payload: dict[str, Any] = field(default_factory=dict)

    def __call__(self, payload: dict[str, Any]) -> None:
        if isinstance(payload, dict):
            self.payload = payload

    def take(self) -> dict[str, Any]:
        out = dict(self.payload)
        self.payload = {}
        return out


@dataclass
class _ReplayDecisionBySymbol:
    payload: dict[str, dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key in (
            "symbol",
            "side",
            "reason",
            "regime",
            "alpha_id",
            "entry_family",
            "entry_tier",
            "entry_price",
            "risk_per_trade_pct",
            "max_effective_leverage",
        ):
            if key in payload:
                compact[key] = payload.get(key)

        sl_tp = payload.get("sl_tp")
        if isinstance(sl_tp, dict):
            compact["sl_tp"] = {
                key: sl_tp.get(key)
                for key in ("take_profit", "take_profit_final", "stop_loss")
                if key in sl_tp
            }

        execution = payload.get("execution")
        if isinstance(execution, dict):
            compact["execution"] = {
                key: execution.get(key)
                for key in (
                    "time_stop_bars",
                    "stop_exit_cooldown_bars",
                    "profit_exit_cooldown_bars",
                    "reward_risk_reference_r",
                )
                if key in execution
            }

        alpha_blocks = payload.get("alpha_blocks")
        if isinstance(alpha_blocks, dict):
            compact["alpha_blocks"] = {
                str(key): str(value)
                for key, value in alpha_blocks.items()
                if str(key).strip() and str(value).strip()
            }
        indicators = payload.get("indicators")
        if isinstance(indicators, dict):
            keep = {}
            for key in ("volume_ratio_15m", "close_30m", "ema20_30m", "close_1h", "ema20_1h"):
                if key in indicators:
                    keep[key] = indicators.get(key)
            if keep:
                compact["indicators"] = keep
        return compact

    def __call__(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            return
        self.payload[symbol] = self._compact_payload(payload)

    def take(self) -> dict[str, dict[str, Any]]:
        out = {symbol: dict(value) for symbol, value in self.payload.items()}
        self.payload = {}
        return out
