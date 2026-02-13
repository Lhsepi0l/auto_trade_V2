from __future__ import annotations

from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TraderSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = Field(default="dev", description="Runtime environment name")
    test_mode: bool = Field(default=False, description="Test-only mode for deterministic app behavior")

    db_path: str = Field(default="./data/auto_trader.sqlite3", description="SQLite DB file path")

    log_level: str = Field(default="INFO", description="Root log level")
    log_dir: str = Field(default="./logs", description="Directory for log files")
    log_json: bool = Field(default=False, description="Emit JSON logs")

    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000)

    # Exchange settings (Binance USDT-M Futures; 조회 전용)
    binance_api_key: str = Field(default="")
    binance_api_secret: str = Field(default="")
    binance_base_url: str = Field(default="https://fapi.binance.com")

    # NOTE: Symbol universe is stored in DB config (risk_config table). This env is kept
    # only for backward compatibility and initial bootstrapping.
    allowed_symbols: str = Field(default="BTCUSDT,ETHUSDT,XAUUSDT")

    request_timeout_sec: float = Field(default=8.0)
    retry_count: int = Field(default=3)
    retry_backoff: float = Field(default=0.25)

    binance_recv_window_ms: int = Field(default=5000)

    # Safety switches (real account protection)
    trading_dry_run: bool = Field(
        default=True,
        description="If true, block any NEW entry/scale/rebalance orders. Close/PANIC are allowed unless DRY_RUN_STRICT.",
    )
    dry_run_strict: bool = Field(
        default=False,
        description="If true AND TRADING_DRY_RUN=true, also block close/PANIC orders (maximum safety).",
    )

    # Optional notifications (if empty => disabled)
    discord_webhook_url: str = Field(default="", description="Discord webhook for critical alerts (optional)")

    # Execution (trade) controls (MVP)
    exec_limit_timeout_sec: float = Field(default=3.0)
    exec_limit_retries: int = Field(default=2)
    exec_split_parts: int = Field(default=3)

    # Policy guards (risk engine)
    spread_guard_max_pct: float = Field(default=0.5, description="Max spread percent allowed before guard triggers")
    spread_guard_action: str = Field(default="block_market", description="block_market|block_all")
    risk_stop_on_daily_loss: bool = Field(default=False, description="If true, STOP engine when daily loss limit hit")

    # Scheduler (STEP6)
    scheduler_enabled: bool = Field(default=False, description="If true, run scheduler loop inside API process")
    scheduler_tick_sec: int = Field(default=1800, description="Scheduler decision tick interval in seconds (default 30m)")
    score_threshold: float = Field(default=0.35, description="Entry threshold for long/short score (0..1)")
    reverse_threshold: float = Field(default=0.55, description="Exit threshold for strong reverse signal (0..1)")
    vol_shock_threshold_pct: float = Field(default=2.0, description="ATR%% threshold to tag VOL_SHOCK")

    # AI signal (STEP7) - signal only, no execution authority
    ai_mode: str = Field(default="stub", description="stub|openai|local")
    ai_conf_threshold: float = Field(default=0.65, description="AI confidence threshold; below => HOLD")
    manual_risk_tag: str = Field(default="", description="Optional manual risk tag override (e.g. NEWS_RISK)")

    # Behavior defaults
    engine_poll_interval_sec: int = Field(default=2)

    @property
    def allowed_symbols_list(self) -> List[str]:
        return [s.strip().upper() for s in self.allowed_symbols.split(",") if s.strip()]


def load_settings() -> TraderSettings:
    return TraderSettings()
