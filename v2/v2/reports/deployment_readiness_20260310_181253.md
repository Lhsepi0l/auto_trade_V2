# Deployment Readiness Report

- Project: auto-trader
- Started: 2026-03-10T18:12:53Z
- Profile: ra_2026_alpha_v2_expansion_live_candidate
- Mode: shadow
- Environment: testnet
- Config: config/config.yaml
- Report: v2/reports/deployment_readiness_20260310_181253.md

## Checks

### 1) secrets policy and config format
- Status: PASS
- Output:
```
config_path=config/config.yaml
secrets policy check passed
```

### 2) ruff check
- Status: PASS
- Output:
```
All checks passed!
```

### 3) pytest v2/tests
- Status: PASS
- Output:
```
........................................................................ [ 22%]
........................................................................ [ 45%]
........................................................................ [ 67%]
........................................................................ [ 90%]
................................                                         [100%]
```

### 4) effective config load
- Status: PASS
- Output:
```
profile=ra_2026_alpha_v2_expansion_live_candidate
mode=shadow
env=testnet
symbol=BTCUSDT
tick_seconds=30
request_rate_limit_per_sec=5.0
```

### 5) connectivity ping/time sync
- Status: PASS
- Output:
```
env=testnet
base_url=https://testnet.binancefuture.com
ping_status=200
time_status=200
server_time=1773166397091
local_time=1773166397303
time_drift_ms=212
```

## Summary

- Result: PASS
- Ended: 2026-03-10T18:13:17Z
