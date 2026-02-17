from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from pydantic import Field

from apps.trader_engine.domain.enums import Direction, EngineState, ExecHint, RiskConfigKey, RiskPresetName


class RiskConfigSchema(BaseModel):
    per_trade_risk_pct: float
    max_exposure_pct: Optional[float] = None
    max_notional_pct: float
    max_leverage: float
    daily_loss_limit_pct: float
    dd_limit_pct: float
    lose_streak_n: int
    cooldown_hours: float
    min_hold_minutes: int
    score_conf_threshold: float
    score_gap_threshold: float
    exec_mode_default: str
    exec_limit_timeout_sec: float
    exec_limit_retries: int
    notify_interval_sec: int
    spread_max_pct: float
    allow_market_when_wide_spread: bool
    capital_mode: str
    capital_pct: float
    capital_usdt: float
    margin_budget_usdt: float
    margin_use_pct: float
    max_position_notional_usdt: Optional[float] = None
    fee_buffer_pct: float
    universe_symbols: List[str]
    enable_watchdog: bool
    watchdog_interval_sec: int
    shock_1m_pct: float
    shock_from_entry_pct: float
    trailing_enabled: bool
    trailing_mode: str
    trail_arm_pnl_pct: float
    trail_distance_pnl_pct: float
    trail_grace_minutes: int
    atr_trail_timeframe: str
    atr_trail_k: float
    atr_trail_min_pct: float
    atr_trail_max_pct: float
    tf_weight_4h: float
    tf_weight_1h: float
    tf_weight_30m: float
    vol_shock_atr_mult_threshold: float
    atr_mult_mean_window: int


class EngineStateSchema(BaseModel):
    state: EngineState
    updated_at: datetime


class PanicResultSchema(BaseModel):
    ok: bool
    canceled_orders_ok: bool
    close_ok: bool
    errors: List[str] = Field(default_factory=list)
    closed_symbol: Optional[str] = None
    closed_qty: Optional[float] = None


class PanicResponseSchema(BaseModel):
    engine_state: EngineStateSchema
    panic_result: PanicResultSchema


class DisabledSymbolSchema(BaseModel):
    symbol: str
    reason: str


class BinanceStatusSchema(BaseModel):
    startup_ok: bool
    startup_error: Optional[str] = None

    enabled_symbols: List[str]
    disabled_symbols: List[DisabledSymbolSchema]

    server_time_ms: int
    time_offset_ms: int
    time_measured_at_ms: int

    private_ok: bool
    private_error: Optional[str] = None

    usdt_balance: Optional[Dict[str, float]] = None
    positions: Optional[Dict[str, Dict[str, float]]] = None
    open_orders: Optional[Dict[str, List[Dict[str, Any]]]] = None
    spreads: Dict[str, Any]


class PnLStatusSchema(BaseModel):
    day: str
    daily_realized_pnl: float
    equity_peak: float
    daily_pnl_pct: float
    drawdown_pct: float
    lose_streak: int
    cooldown_until: Optional[datetime] = None
    last_block_reason: Optional[str] = None
    last_fill_symbol: Optional[str] = None
    last_fill_side: Optional[str] = None
    last_fill_qty: Optional[float] = None
    last_fill_price: Optional[float] = None
    last_fill_realized_pnl: Optional[float] = None
    last_fill_time: Optional[datetime] = None


class CandidateSchema(BaseModel):
    symbol: str
    direction: str
    strength: float
    composite: float
    vol_tag: str
    confidence: Optional[float] = None
    regime_4h: Optional[str] = None


class AiSignalSchema(BaseModel):
    target_asset: str
    direction: str
    confidence: float
    exec_hint: str
    risk_tag: str
    notes: Optional[str] = None


class SchedulerSnapshotSchema(BaseModel):
    tick_started_at: str
    tick_finished_at: Optional[str] = None
    tick_sec: float = 1800.0
    engine_state: str
    enabled_symbols: List[str]
    candidate: Optional[CandidateSchema] = None
    ai_signal: Optional[AiSignalSchema] = None
    scores: Dict[str, Any] = Field(default_factory=dict)
    last_scores: Dict[str, Any] = Field(default_factory=dict)
    last_candidate: Optional[Dict[str, Any]] = None
    last_decision_reason: Optional[str] = None
    last_action: Optional[str] = None
    last_error: Optional[str] = None


class SchedulerIntervalRequest(BaseModel):
    tick_sec: float = Field(..., ge=30.0, description="Minimum 30 seconds.")


class SchedulerControlResponse(BaseModel):
    tick_sec: float
    running: bool
    min_tick_sec: float = 30.0


class SchedulerTickResponse(BaseModel):
    ok: bool
    snapshot: Optional[SchedulerSnapshotSchema] = None
    tick_sec: Optional[float] = None


class WatchdogStatusSchema(BaseModel):
    symbol: Optional[str] = None
    last_mark_price: Optional[float] = None
    last_1m_return: Optional[float] = None
    spread_pct: Optional[float] = None
    market_blocked_by_spread: bool = False
    last_shock_reason: Optional[str] = None
    last_trailing_reason: Optional[str] = None
    last_peak_pnl_pct: Optional[float] = None
    last_trailing_distance_pct: Optional[float] = None
    last_checked_at: Optional[str] = None


class CapitalSnapshotSchema(BaseModel):
    symbol: str
    available_usdt: float
    budget_usdt: float
    used_margin: float
    leverage: float
    notional_usdt: float
    mark_price: float
    est_qty: float
    blocked: bool
    block_reason: Optional[str] = None


class CapitalConfigSnapshotSchema(BaseModel):
    capital_mode: str
    capital_pct: float
    capital_usdt: float
    margin_budget_usdt: float
    margin_use_pct: float
    max_position_notional_usdt: Optional[float] = None
    max_exposure_pct: Optional[float] = None
    fee_buffer_pct: float


class FieldValidationErrorSchema(BaseModel):
    field: str
    message: str


class SetValueResponse(BaseModel):
    key: str
    requested_value: str
    applied_value: Any
    summary: str
    risk_config: RiskConfigSchema


class StatusResponse(BaseModel):
    dry_run: bool = False
    dry_run_strict: bool = False
    config: CapitalConfigSnapshotSchema
    config_summary: Dict[str, Any] = Field(default_factory=dict)
    filters_last_refresh_time: Optional[float] = None
    last_snapshot_time: Optional[str] = None
    last_unrealized_pnl_usdt: Optional[float] = None
    last_unrealized_pnl_pct: Optional[float] = None
    last_error: Optional[str] = None
    ws_connected: bool = False
    listenKey_last_keepalive_ts: Optional[datetime] = None
    last_ws_event_ts: Optional[datetime] = None
    safe_mode: bool = False
    last_ws_event_time: Optional[datetime] = None
    last_fill: Optional[Dict[str, Any]] = None
    engine_state: EngineStateSchema
    risk_config: RiskConfigSchema
    binance: Optional[BinanceStatusSchema] = None
    pnl: Optional[PnLStatusSchema] = None
    scheduler: Optional[SchedulerSnapshotSchema] = None
    watchdog: Optional[WatchdogStatusSchema] = None
    capital_snapshot: Optional[CapitalSnapshotSchema] = None


class SetValueRequest(BaseModel):
    key: RiskConfigKey
    value: str


class PresetRequest(BaseModel):
    name: RiskPresetName


class TradeEnterRequest(BaseModel):
    symbol: str
    direction: Direction
    exec_hint: ExecHint
    notional_usdt: Optional[float] = None
    qty: Optional[float] = None
    leverage: Optional[float] = None


class TradeCloseRequest(BaseModel):
    symbol: str


class TradeResult(BaseModel):
    symbol: str
    hint: Optional[str] = None
    orders: List[Dict[str, Any]] = Field(default_factory=list)
    detail: Optional[Dict[str, Any]] = None
