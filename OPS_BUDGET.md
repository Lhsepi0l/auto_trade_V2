# OPS Budget Guide

## Purpose
Live trading budget control uses `capital_mode` and related caps to keep each new entry within safe limits.

## Key Settings
- `capital_mode`
  - `PCT_AVAILABLE`: use percent of available USDT
  - `FIXED_USDT`: use fixed USDT amount
- `capital_pct`: used when mode is `PCT_AVAILABLE` (range `0.01..1.0`)
- `capital_usdt`: used when mode is `FIXED_USDT` (minimum `5.0`)
- `margin_use_pct`: portion of budget to use as margin (`0.10..1.0`)
- `max_position_notional_usdt`: hard cap for per-position notional (optional)
- `max_exposure_pct`: additional budget clamp (optional)
- `fee_buffer_pct`: reserve for fees/slippage (`0.0..0.02`)

## Panel Presets
- Percent mode presets: `5% / 10% / 20% / 50%`
- Fixed mode presets: `50 / 100 / 200 / 500 USDT`

## Safe Live Defaults (Recommended)
- `capital_mode=PCT_AVAILABLE`
- `capital_pct=0.05`
- `margin_use_pct=0.5`
- `max_position_notional_usdt=100` (or account-size based)
- `max_exposure_pct=0.2`
- `fee_buffer_pct=0.002`

Start small, validate behavior in `DRY_RUN`, then increase gradually.
