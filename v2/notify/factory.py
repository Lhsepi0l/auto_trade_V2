from __future__ import annotations

from v2.config.loader import EffectiveConfig

from .notifier import Notifier


def build_notifier_from_config(cfg: EffectiveConfig) -> Notifier:
    enabled = bool(cfg.behavior.notify.enabled)
    if cfg.secrets.ntfy_enabled is not None:
        enabled = bool(cfg.secrets.ntfy_enabled)

    provider = str(cfg.behavior.notify.provider or "none").strip().lower()
    if provider == "none" and cfg.secrets.ntfy_topic:
        provider = "ntfy"

    return Notifier(
        enabled=enabled,
        provider=provider,
        ntfy_base_url=cfg.secrets.ntfy_base_url,
        ntfy_topic=cfg.secrets.ntfy_topic,
        ntfy_token=cfg.secrets.ntfy_token,
        ntfy_tags=cfg.secrets.ntfy_tags,
        ntfy_priority=cfg.secrets.ntfy_priority,
    )
