from __future__ import annotations

from v2.config.loader import EffectiveConfig

from .notifier import Notifier


def build_notifier_from_config(cfg: EffectiveConfig) -> Notifier:
    return Notifier(
        enabled=bool(cfg.behavior.notify.enabled),
        provider=str(cfg.behavior.notify.provider or "none").strip().lower(),
    )
