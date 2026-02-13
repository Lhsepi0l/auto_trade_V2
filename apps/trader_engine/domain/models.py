from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from apps.trader_engine.domain.enums import CapitalMode, EngineState


class RiskConfig(BaseModel):
    # Percent units: 0..100
    per_trade_risk_pct: float = Field(ge=0, le=100)
    max_exposure_pct: float | None = Field(default=None, ge=0.01, le=1.0)
    max_notional_pct: float = Field(ge=0, le=100)

    # Hardcap is enforced again in RiskService, but keep it here too.
    max_leverage: float = Field(ge=1, le=50)

    # Loss limits are negative ratios (allowed range: -1..0)
    # Example: -0.02 == -2%
    daily_loss_limit_pct: float = Field(ge=-1, le=0, default=-0.02)
    dd_limit_pct: float = Field(ge=-1, le=0, default=-0.15)

    lose_streak_n: int = Field(ge=1, le=10)
    cooldown_hours: float = Field(ge=1, le=72)

    # Strategy/execution controls (stored in the same singleton config row).
    min_hold_minutes: int = Field(ge=0, le=24 * 60, default=240)
    score_conf_threshold: float = Field(ge=0, le=1, default=0.65)
    score_gap_threshold: float = Field(ge=0, le=1, default=0.20)
    exec_mode_default: str = Field(default="LIMIT")

    exec_limit_timeout_sec: float = Field(gt=0, le=60, default=5.0)
    exec_limit_retries: int = Field(ge=0, le=10, default=2)
    notify_interval_sec: int = Field(ge=10, le=3600, default=1800)

    spread_max_pct: float = Field(ge=0, le=0.1, default=0.0015)
    allow_market_when_wide_spread: bool = Field(default=False)
    capital_mode: CapitalMode = Field(default=CapitalMode.PCT_AVAILABLE)
    capital_pct: float = Field(ge=0.01, le=1.0, default=0.20)
    capital_usdt: float = Field(ge=5.0, default=100.0)
    margin_budget_usdt: float = Field(ge=5.0, default=100.0)
    margin_use_pct: float = Field(ge=0.10, le=1.0, default=0.90)
    max_position_notional_usdt: float | None = Field(default=None)
    fee_buffer_pct: float = Field(ge=0.0, le=0.02, default=0.002)

    universe_symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "XAUUSDT"])

    enable_watchdog: bool = Field(default=True)
    watchdog_interval_sec: int = Field(ge=1, le=300, default=10)

    shock_1m_pct: float = Field(ge=0, le=0.5, default=0.010)
    shock_from_entry_pct: float = Field(ge=0, le=0.5, default=0.012)
    trailing_enabled: bool = Field(default=True)
    trailing_mode: str = Field(default="PCT")
    trail_arm_pnl_pct: float = Field(ge=0.0, le=100.0, default=1.2)
    trail_distance_pnl_pct: float = Field(ge=0.0, le=100.0, default=0.8)
    trail_grace_minutes: int = Field(ge=0, le=24 * 60, default=30)
    atr_trail_timeframe: str = Field(default="1h")
    atr_trail_k: float = Field(ge=0.0, le=20.0, default=2.0)
    atr_trail_min_pct: float = Field(ge=0.0, le=100.0, default=0.6)
    atr_trail_max_pct: float = Field(ge=0.0, le=100.0, default=1.8)

    # Scoring config (rotation strategy)
    tf_weight_4h: float = Field(ge=0, le=1, default=0.5)
    tf_weight_1h: float = Field(ge=0, le=1, default=0.3)
    tf_weight_30m: float = Field(ge=0, le=1, default=0.2)

    vol_shock_atr_mult_threshold: float = Field(ge=1, le=10, default=2.5)
    atr_mult_mean_window: int = Field(ge=10, le=500, default=50)

    @field_validator("universe_symbols", mode="before")
    @classmethod
    def _parse_universe_symbols(cls, v):  # type: ignore[no-untyped-def]
        # Accept list[str] or CSV-like strings from DB/env.
        alias = {"XAUTUSDT": "XAUUSDT"}
        if v is None:
            return ["BTCUSDT", "ETHUSDT", "XAUUSDT"]
        if isinstance(v, str):
            parts = [alias.get(p.strip().upper(), p.strip().upper()) for p in v.split(",") if p.strip()]
            return parts
        if isinstance(v, (list, tuple)):
            return [alias.get(str(x).strip().upper(), str(x).strip().upper()) for x in v if str(x).strip()]
        return v

    @field_validator("exec_mode_default", mode="before")
    @classmethod
    def _parse_exec_mode_default(cls, v):  # type: ignore[no-untyped-def]
        s = str(v or "LIMIT").strip().upper()
        if s not in {"LIMIT", "MARKET", "SPLIT"}:
            raise ValueError("exec_mode_default_must_be_LIMIT_MARKET_SPLIT")
        return s

    @field_validator("trailing_mode", mode="before")
    @classmethod
    def _parse_trailing_mode(cls, v):  # type: ignore[no-untyped-def]
        s = str(v or "PCT").strip().upper()
        if s not in {"PCT", "ATR"}:
            raise ValueError("trailing_mode_must_be_PCT_ATR")
        return s

    @field_validator("atr_trail_timeframe", mode="before")
    @classmethod
    def _parse_atr_trail_timeframe(cls, v):  # type: ignore[no-untyped-def]
        s = str(v or "1h").strip().lower()
        if s not in {"15m", "1h", "4h"}:
            raise ValueError("atr_trail_timeframe_must_be_15m_1h_4h")
        return s

    @field_validator("atr_trail_max_pct")
    @classmethod
    def _validate_atr_trail_bounds(cls, v: float, info) -> float:  # type: ignore[no-untyped-def]
        lo = float(info.data.get("atr_trail_min_pct", 0.0))
        hi = float(v)
        if hi < lo:
            raise ValueError("atr_trail_max_pct_must_be_gte_min_pct")
        return hi

    @field_validator("max_position_notional_usdt")
    @classmethod
    def _validate_max_position_notional_usdt(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if float(v) <= 0.0:
            raise ValueError("max_position_notional_usdt_must_be_gt_0_or_null")
        return float(v)


class EngineStateRow(BaseModel):
    state: EngineState
    updated_at: datetime
    ws_connected: bool = False
    last_ws_event_time: datetime | None = None


class PnLState(BaseModel):
    # Stored as a singleton row (id=1). "day" is YYYY-MM-DD in UTC.
    day: str
    daily_realized_pnl: float = 0.0
    equity_peak: float = 0.0
    lose_streak: int = 0
    cooldown_until: datetime | None = None
    last_entry_symbol: str | None = None
    last_entry_at: datetime | None = None
    last_fill_symbol: str | None = None
    last_fill_side: str | None = None
    last_fill_qty: float | None = None
    last_fill_price: float | None = None
    last_fill_realized_pnl: float | None = None
    last_fill_time: datetime | None = None
    last_block_reason: str | None = None
    updated_at: datetime
