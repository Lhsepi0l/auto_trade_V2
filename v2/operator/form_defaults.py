from __future__ import annotations

SCORING_DEFAULTS: dict[str, object] = {
    "score_conf_threshold": 0.60,
    "score_gap_threshold": 0.15,
    "tf_weight_10m": 0.25,
    "tf_weight_15m": 0.0,
    "tf_weight_30m": 0.25,
    "tf_weight_1h": 0.25,
    "tf_weight_4h": 0.25,
    "score_tf_15m_enabled": False,
    "donchian_momentum_filter": True,
    "donchian_fast_ema_period": 8,
    "donchian_slow_ema_period": 21,
}

TRAILING_FORM_DEFAULTS: dict[str, object] = {
    "trailing_enabled": True,
    "trailing_mode": "PCT",
    "trail_arm_pnl_pct": 1.2,
    "trail_grace_minutes": 30,
    "trail_distance_pnl_pct": 0.8,
    "atr_trail_timeframe": "1h",
    "atr_trail_k": 2.0,
    "atr_trail_min_pct": 0.6,
    "atr_trail_max_pct": 1.8,
}
