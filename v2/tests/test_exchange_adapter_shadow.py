from __future__ import annotations

from v2.config.loader import load_effective_config
from v2.exchange.binance_adapter import BinanceAdapter, MockMarketWS
from v2.exchange.user_ws import ShadowUserStreamManager


def test_shadow_mode_uses_mock_providers_without_keys() -> None:
    cfg = load_effective_config(profile="normal", mode="shadow", env="testnet", env_map={})
    adapter = BinanceAdapter.from_effective_config(cfg)

    assert adapter.mode == "shadow"
    assert adapter.create_rest_client(cfg=cfg) is None
    assert isinstance(adapter.create_market_ws(cfg=cfg), MockMarketWS)
    assert isinstance(adapter.create_user_stream_manager(cfg=cfg), ShadowUserStreamManager)
