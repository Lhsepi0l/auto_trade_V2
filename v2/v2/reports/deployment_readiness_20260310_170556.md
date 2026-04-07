# Deployment Readiness Report

- Project: auto-trader
- Started: 2026-03-10T17:05:56Z
- Profile: ra_2026_v1_live24
- Mode: shadow
- Environment: testnet
- Config: config/config.yaml
- Report: v2/reports/deployment_readiness_20260310_170556.md

## Checks

### 1) secrets policy and config format
- Status: PASS
- Output:
```
config_path=config/config.yaml
secrets policy check passed
```

### 2) ruff check
- Status: FAIL
- Output:
```
I001 [*] Import block is un-sorted or un-formatted
 --> v2/tests/test_install_systemd_stack.py:1:1
  |
1 | / from __future__ import annotations
2 | |
3 | | import subprocess
4 | | from pathlib import Path
  | |________________________^
  |
help: Organize imports

Found 1 error.
[*] 1 fixable with the `--fix` option.
```

## Summary

- Result: FAILED
- Ended: 2026-03-10T17:05:56Z
