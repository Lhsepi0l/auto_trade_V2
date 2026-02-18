from __future__ import annotations

from enum import Enum


class EngineState(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    COOLDOWN = "COOLDOWN"
    PANIC = "PANIC"


class RiskConfigKey(str, Enum):
    per_trade_risk_pct = "per_trade_risk_pct"
    max_exposure_pct = "max_exposure_pct"
    max_notional_pct = "max_notional_pct"
    max_leverage = "max_leverage"
    symbol_leverage_map = "symbol_leverage_map"
    # Loss limits are stored as ratios (e.g. -0.02 for -2%)
    daily_loss_limit_pct = "daily_loss_limit_pct"
    dd_limit_pct = "dd_limit_pct"
    lose_streak_n = "lose_streak_n"
    cooldown_hours = "cooldown_hours"
    min_hold_minutes = "min_hold_minutes"
    score_conf_threshold = "score_conf_threshold"
    score_gap_threshold = "score_gap_threshold"
    exec_mode_default = "exec_mode_default"
    exec_limit_timeout_sec = "exec_limit_timeout_sec"
    exec_limit_retries = "exec_limit_retries"
    notify_interval_sec = "notify_interval_sec"
    spread_max_pct = "spread_max_pct"
    allow_market_when_wide_spread = "allow_market_when_wide_spread"
    capital_mode = "capital_mode"
    capital_pct = "capital_pct"
    capital_usdt = "capital_usdt"
    margin_budget_usdt = "margin_budget_usdt"
    margin_use_pct = "margin_use_pct"
    max_position_notional_usdt = "max_position_notional_usdt"
    fee_buffer_pct = "fee_buffer_pct"
    universe_symbols = "universe_symbols"
    enable_watchdog = "enable_watchdog"
    watchdog_interval_sec = "watchdog_interval_sec"
    shock_1m_pct = "shock_1m_pct"
    shock_from_entry_pct = "shock_from_entry_pct"
    trailing_enabled = "trailing_enabled"
    trailing_mode = "trailing_mode"
    trail_arm_pnl_pct = "trail_arm_pnl_pct"
    trail_distance_pnl_pct = "trail_distance_pnl_pct"
    trail_grace_minutes = "trail_grace_minutes"
    atr_trail_timeframe = "atr_trail_timeframe"
    atr_trail_k = "atr_trail_k"
    atr_trail_min_pct = "atr_trail_min_pct"
    atr_trail_max_pct = "atr_trail_max_pct"
    tf_weight_4h = "tf_weight_4h"
    tf_weight_1h = "tf_weight_1h"
    tf_weight_30m = "tf_weight_30m"
    tf_weight_10m = "tf_weight_10m"
    tf_weight_15m = "tf_weight_15m"
    score_tf_15m_enabled = "score_tf_15m_enabled"
    vol_shock_atr_mult_threshold = "vol_shock_atr_mult_threshold"
    atr_mult_mean_window = "atr_mult_mean_window"


class RiskPresetName(str, Enum):
    conservative = "conservative"
    normal = "normal"
    aggressive = "aggressive"


class CapitalMode(str, Enum):
    PCT_AVAILABLE = "PCT_AVAILABLE"
    FIXED_USDT = "FIXED_USDT"
    MARGIN_BUDGET_USDT = "MARGIN_BUDGET_USDT"


# Placeholders for A-stage expansion.
class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ExecHint(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SPLIT = "SPLIT"
