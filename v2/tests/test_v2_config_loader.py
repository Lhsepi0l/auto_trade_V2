from __future__ import annotations

import pytest

from v2.config.loader import load_effective_config


def test_profile_inheritance_applies() -> None:
    cfg = load_effective_config(profile="aggressive", mode="shadow", env="testnet", env_map={})
    assert cfg.behavior.scheduler.tick_seconds == 15
    assert cfg.behavior.risk.max_leverage == 12.0
    assert cfg.behavior.notify.enabled is False


def test_shadow_mode_does_not_require_keys() -> None:
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    assert cfg.mode == "shadow"
    assert cfg.secrets.binance_api_key is None


def test_live_mode_requires_keys() -> None:
    with pytest.raises(ValueError):
        load_effective_config(profile="normal", mode="live", env="prod", env_map={})
