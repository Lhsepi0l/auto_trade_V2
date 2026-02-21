from __future__ import annotations

from dataclasses import dataclass

from v2.config.loader import RiskConfig


@dataclass
class RiskManager:
    config: RiskConfig

    def validate_leverage(self, leverage: float) -> bool:
        return leverage <= self.config.max_leverage
