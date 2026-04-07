from __future__ import annotations

from typing import Any

from v2.config.loader import EffectiveConfig

from .notifier import Notifier


def build_notifier_from_config(
    cfg: EffectiveConfig,
    *,
    webpush_send: Any | None = None,
    webpush_public_key: str | None = None,
) -> Notifier:
    enabled = bool(cfg.behavior.notify.enabled)
    if cfg.secrets.ntfy_enabled is not None:
        enabled = bool(cfg.secrets.ntfy_enabled)
    if cfg.secrets.webpush_enabled is not None:
        enabled = bool(cfg.secrets.webpush_enabled)

    provider = str(cfg.behavior.notify.provider or "none").strip().lower()
    if provider == "none":
        if bool(cfg.secrets.webpush_enabled):
            provider = "webpush"
        elif cfg.secrets.ntfy_topic:
            provider = "ntfy"
        elif cfg.secrets.notify_webhook_url:
            provider = "discord"

    return Notifier(
        enabled=enabled,
        provider=provider,
        webhook_url=cfg.secrets.notify_webhook_url,
        ntfy_base_url=cfg.secrets.ntfy_base_url,
        ntfy_topic=cfg.secrets.ntfy_topic,
        ntfy_token=cfg.secrets.ntfy_token,
        ntfy_tags=cfg.secrets.ntfy_tags,
        ntfy_priority=cfg.secrets.ntfy_priority,
        webpush_send=webpush_send,
        webpush_public_key=webpush_public_key,
    )
