from __future__ import annotations

from dataclasses import dataclass


@dataclass
class KillSwitch:
    tripped: bool = False

    def trigger(self) -> None:
        self.tripped = True

    def reset(self) -> None:
        self.tripped = False
