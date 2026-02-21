from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EnvName = Literal["testnet", "prod"]


@dataclass
class ResyncSnapshot:
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    balances: list[dict[str, Any]] = field(default_factory=list)
