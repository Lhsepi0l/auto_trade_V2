from __future__ import annotations

from dataclasses import dataclass, field

from v2.strategies.base import StrategyPlugin


@dataclass
class StrategyRegistry:
    _plugins: dict[str, StrategyPlugin] = field(default_factory=dict)

    def register(self, plugin: StrategyPlugin) -> None:
        self._plugins[plugin.name] = plugin

    def get(self, name: str) -> StrategyPlugin:
        if name not in self._plugins:
            raise KeyError(f"strategy not found: {name}")
        return self._plugins[name]
