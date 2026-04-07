# Deployment Readiness Report

- Project: auto-trader
- Started: 2026-02-21T19:27:10Z
- Profile: normal
- Mode: shadow
- Environment: testnet
- Config: config/config.yaml
- Report: reports/preflight_shadow_pass.md

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

### 3) pytest tests/v2
- Status: PASS
- Output:
```
.........................................                                [100%]
```

### 4) effective config load
- Status: PASS
- Output:
```
profile=normal
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
server_time=1771702037176
local_time=1771702037438
time_drift_ms=262
```

## Summary

- Result: PASS
- Ended: 2026-02-21T19:27:17Z
