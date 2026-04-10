# Deployment Readiness Report

- Project: auto-trader
- Started: 2026-02-21T19:26:38Z
- Profile: normal
- Mode: live
- Environment: prod
- Config: config/config.yaml
- Report: reports/preflight_live_fail.md

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
- Status: FAIL
- Output:
```
Traceback (most recent call last):
  File "/tmp/tmp.EmwtQs6qbu", line 5, in <module>
    cfg = load_effective_config(profile="normal", mode="live", env="prod", config_path="config/config.yaml")
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/user/project/auto-trader/v2/config/loader.py", line 219, in load_effective_config
    raise ValueError("live mode requires BINANCE_API_KEY and BINANCE_API_SECRET in environment")
ValueError: live mode requires BINANCE_API_KEY and BINANCE_API_SECRET in environment
```
