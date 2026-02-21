from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PositionManager:
    positions: dict[str, float] = field(default_factory=dict)

    def set_position(self, *, symbol: str, qty: float) -> None:
        self.positions[symbol] = qty

    def flatten_all(self) -> None:
        self.positions.clear()
