---
name: "alpha-expansion-research"
description: "Run bounded local-backtest sweeps for `ra_2026_alpha_v2_expansion`, compare alpha override sets, and stage the best candidate for follow-up verification."
---

# Alpha Expansion Research

Use this skill when the goal is to improve the `ra_2026_alpha_v2_expansion` edge without opening new strategy branches.

## Workflow
1. Fix the profile to `ra_2026_alpha_v2_expansion`.
2. Sweep only these alpha knobs unless the user explicitly broadens scope:
   - `alpha_squeeze_percentile_max`
   - `alpha_expansion_buffer_bps`
   - `alpha_expansion_range_atr_min`
   - `alpha_min_volume_ratio`
   - `alpha_take_profit_r`
   - `alpha_time_stop_bars`
   - `alpha_trend_adx_min_4h`
   - `alpha_expected_move_cost_mult`
3. Keep other strategy-family axes locked to their first value.
4. Run a bounded 1y sweep first, then promote only the strongest case to longer verification.
5. Write the top case, report path, and next verification command in concise Korean.

## Quick Start
```bash
python local_backtest/param_sweep.py \
  --profile ra_2026_alpha_v2_expansion \
  --env prod \
  --symbols BTCUSDT \
  --years 1 \
  --verify-years '' \
  --case-workers 2 \
  --year-workers 1 \
  --replay-workers 0 \
  --max-cases 8
```

## Output
- Best case parameters
- Best report path
- Whether the candidate deserves 3y verification
