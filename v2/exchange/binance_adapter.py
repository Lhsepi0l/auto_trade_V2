from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from v2.config.loader import EffectiveConfig
from v2.exchange.client_order_id import generate_client_order_id
from v2.exchange.market_ws import BinanceMarketWS
from v2.exchange.rate_limit import BackoffPolicy
from v2.exchange.rest_client import BinanceRESTClient
from v2.exchange.types import EnvName
from v2.exchange.user_ws import ShadowUserStreamManager, ShadowUserStreamREST, UserStreamManager

ModeName = Literal["shadow", "live"]


class MockMarketWS:
    async def stream_klines(self, *, symbols: list[str], intervals: list[str] | None = None) -> AsyncIterator[dict[str, Any]]:
        _ = symbols
        _ = intervals
        if False:
            yield {}


@dataclass
class BinanceAdapter:
    mode: ModeName
    env: EnvName
    api_key: str | None = None
    api_secret: str | None = None

    @classmethod
    def shadow(cls, env: EnvName) -> "BinanceAdapter":
        return cls(mode="shadow", env=env, api_key=None, api_secret=None)

    @classmethod
    def from_effective_config(cls, cfg: EffectiveConfig) -> "BinanceAdapter":
        return cls(
            mode=cfg.mode,
            env=cfg.env,
            api_key=cfg.secrets.binance_api_key,
            api_secret=cfg.secrets.binance_api_secret,
        )

    def ping(self) -> dict[str, str]:
        return {
            "venue": "binance-usdm",
            "env": self.env,
            "mode": self.mode,
            "status": "ok",
        }

    def create_rest_client(self, *, cfg: EffectiveConfig) -> BinanceRESTClient | None:
        return BinanceRESTClient(
            env=self.env,
            api_key=self.api_key,
            api_secret=self.api_secret,
            recv_window_ms=cfg.behavior.exchange.recv_window_ms,
            time_sync_enabled=True,
            rate_limit_per_sec=cfg.behavior.exchange.request_rate_limit_per_sec,
            backoff_policy=BackoffPolicy(
                base_seconds=cfg.behavior.exchange.backoff_base_seconds,
                cap_seconds=cfg.behavior.exchange.backoff_cap_seconds,
            ),
        )

    def create_market_ws(self, *, cfg: EffectiveConfig) -> BinanceMarketWS | MockMarketWS:
        if self.mode == "shadow":
            return MockMarketWS()
        return BinanceMarketWS(env=self.env)

    def create_user_stream_manager(self, *, cfg: EffectiveConfig) -> UserStreamManager | ShadowUserStreamManager:
        if self.mode == "shadow":
            _ = ShadowUserStreamREST()
            return ShadowUserStreamManager()
        else:
            rest = BinanceRESTClient(
                env=self.env,
                api_key=self.api_key,
                api_secret=self.api_secret,
                recv_window_ms=cfg.behavior.exchange.recv_window_ms,
                time_sync_enabled=True,
                rate_limit_per_sec=cfg.behavior.exchange.request_rate_limit_per_sec,
                backoff_policy=BackoffPolicy(
                    base_seconds=cfg.behavior.exchange.backoff_base_seconds,
                    cap_seconds=cfg.behavior.exchange.backoff_cap_seconds,
                ),
            )

        return UserStreamManager(
            env=self.env,
            rest=rest,
            keepalive_interval_sec=cfg.behavior.exchange.user_stream_keepalive_seconds,
            reconnect_min_sec=cfg.behavior.exchange.user_stream_reconnect_min_seconds,
            reconnect_max_sec=cfg.behavior.exchange.user_stream_reconnect_max_seconds,
            connection_ttl_sec=cfg.behavior.exchange.user_stream_connection_ttl_seconds,
            reorder_window_ms=cfg.behavior.exchange.user_stream_reorder_window_ms,
            backoff_policy=BackoffPolicy(
                base_seconds=cfg.behavior.exchange.backoff_base_seconds,
                cap_seconds=cfg.behavior.exchange.backoff_cap_seconds,
            ),
        )

    def next_client_order_id(self, *, prefix: str = "v2") -> str:
        return generate_client_order_id(prefix=prefix, max_length=31)
