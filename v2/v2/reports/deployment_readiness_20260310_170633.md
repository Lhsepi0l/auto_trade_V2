# Deployment Readiness Report

- Project: auto-trader
- Started: 2026-03-10T17:06:33Z
- Profile: ra_2026_v1_live24
- Mode: shadow
- Environment: testnet
- Config: config/config.yaml
- Report: v2/reports/deployment_readiness_20260310_170633.md

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
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 69%]
........................................................................ [ 92%]
........................                                                 [100%]
```

### 4) effective config load
- Status: PASS
- Output:
```
profile=ra_2026_v1_live24
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
server_time=1773162412361
local_time=1773162412574
time_drift_ms=213
```

## Summary

- Result: PASS
- Ended: 2026-03-10T17:06:52Z
