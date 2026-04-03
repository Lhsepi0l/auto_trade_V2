from __future__ import annotations

import pytest

from v2.config.loader import load_effective_config


def test_alpha_base_profile_loads() -> None:
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    assert cfg.behavior.scheduler.tick_seconds == 30
    assert cfg.behavior.exchange.default_symbol == "BTCUSDT"
    assert cfg.behavior.exchange.market_intervals == ["15m", "1h", "4h"]
    assert cfg.behavior.risk.max_leverage == 8.0
    assert [entry.name for entry in cfg.behavior.strategies if entry.enabled] == ["ra_2026_alpha_v2"]
    enabled = [entry for entry in cfg.behavior.strategies if entry.enabled]
    assert enabled[0].params["max_effective_leverage"] == 0.0

def test_alpha_expansion_candidate_profile_loads() -> None:
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    enabled = [entry for entry in cfg.behavior.strategies if entry.enabled]
    assert [entry.name for entry in enabled] == ["ra_2026_alpha_v2"]
    assert enabled[0].params["enabled_alphas"] == ["alpha_expansion"]
    assert enabled[0].params["expansion_body_ratio_min"] == 0.18
    assert enabled[0].params["expansion_close_location_min"] == 0.35
    assert enabled[0].params["expansion_width_expansion_min"] == 0.02
    assert enabled[0].params["min_volume_ratio_15m"] == 0.9
    assert enabled[0].params["expected_move_cost_mult"] == 1.6


def test_alpha_champion_candidate_profile_loads() -> None:
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_champion_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    enabled = [entry for entry in cfg.behavior.strategies if entry.enabled]
    assert [entry.name for entry in enabled] == ["ra_2026_alpha_v2"]
    assert enabled[0].params["enabled_alphas"] == ["alpha_expansion"]
    assert enabled[0].params["expansion_quality_score_v2_min"] == 0.70


def test_alpha_verified_q070_profile_loads() -> None:
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_verified_q070",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    enabled = [entry for entry in cfg.behavior.strategies if entry.enabled]
    assert [entry.name for entry in enabled] == ["ra_2026_alpha_v2"]
    assert enabled[0].params["enabled_alphas"] == ["alpha_expansion"]
    assert enabled[0].params["squeeze_percentile_threshold"] == 0.35
    assert enabled[0].params["expansion_buffer_bps"] == 1.5
    assert enabled[0].params["expansion_body_ratio_min"] == 0.18
    assert enabled[0].params["expansion_close_location_min"] == 0.35
    assert enabled[0].params["expansion_width_expansion_min"] == 0.02
    assert enabled[0].params["min_volume_ratio_15m"] == 0.8
    assert enabled[0].params["expansion_quality_score_v2_min"] == 0.70
    assert enabled[0].params["expansion_short_break_distance_atr_max"] == 1.3


def test_alpha_live_candidate_profile_loads() -> None:
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    enabled = [entry for entry in cfg.behavior.strategies if entry.enabled]
    assert [entry.name for entry in enabled] == ["ra_2026_alpha_v2"]
    assert enabled[0].params["enabled_alphas"] == ["alpha_expansion"]
    assert cfg.behavior.exchange.market_intervals == ["15m", "1h", "4h"]
    assert enabled[0].params["expansion_quality_score_v2_min"] == 0.62
    assert enabled[0].params["expansion_short_break_distance_atr_max"] == 1.3
    assert cfg.behavior.risk.max_leverage == 5.0
    assert cfg.behavior.risk.max_exposure_pct == 0.10
    assert cfg.behavior.risk.daily_loss_limit_pct == -0.015
    assert cfg.behavior.risk.dd_limit_pct == -0.12


def test_shadow_mode_does_not_require_keys() -> None:
    cfg = load_effective_config(
        profile="ra_2026_alpha_v2_expansion_live_candidate",
        mode="shadow",
        env="testnet",
        env_map={},
    )
    assert cfg.mode == "shadow"
    assert cfg.secrets.binance_api_key is None


def test_live_mode_requires_keys() -> None:
    with pytest.raises(ValueError):
        load_effective_config(
            profile="ra_2026_alpha_v2_expansion_live_candidate",
            mode="live",
            env="prod",
            env_map={},
        )


def test_removed_legacy_profiles_raise() -> None:
    with pytest.raises(ValueError):
        load_effective_config(profile="ra_2026_v1", mode="shadow", env="testnet", env_map={})
    with pytest.raises(ValueError):
        load_effective_config(profile="ra_2026_profit_v1", mode="shadow", env="testnet", env_map={})
    with pytest.raises(ValueError):
        load_effective_config(profile="ra_2026_portfolio_v1", mode="shadow", env="testnet", env_map={})
