# Deployment Readiness Report

- Project: auto-trader
- Started: 2026-02-21T21:43:48Z
- Profile: normal
- Mode: shadow
- Environment: testnet
- Config: config/config.yaml
- Report: v2/reports/deployment_readiness_20260221_214348.md

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
........................................................................ [ 96%]
...                                                                      [100%]
=============================== warnings summary ===============================
../../.local/lib/python3.12/site-packages/discord/player.py:30
  /home/user/.local/lib/python3.12/site-packages/discord/player.py:30: DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
    import audioop

v2/tests/test_discord_help.py:51
  /home/user/project/auto-trader/v2/tests/test_discord_help.py:51: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_help.py:72
  /home/user/project/auto-trader/v2/tests/test_discord_help.py:72: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel.py:95
  /home/user/project/auto-trader/v2/tests/test_discord_panel.py:95: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel.py:110
  /home/user/project/auto-trader/v2/tests/test_discord_panel.py:110: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel.py:144
  /home/user/project/auto-trader/v2/tests/test_discord_panel.py:144: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel.py:154
  /home/user/project/auto-trader/v2/tests/test_discord_panel.py:154: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel.py:177
  /home/user/project/auto-trader/v2/tests/test_discord_panel.py:177: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel.py:208
  /home/user/project/auto-trader/v2/tests/test_discord_panel.py:208: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel.py:227
  /home/user/project/auto-trader/v2/tests/test_discord_panel.py:227: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel.py:255
  /home/user/project/auto-trader/v2/tests/test_discord_panel.py:255: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel.py:278
  /home/user/project/auto-trader/v2/tests/test_discord_panel.py:278: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel.py:302
  /home/user/project/auto-trader/v2/tests/test_discord_panel.py:302: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel_budget_ui.py:70
  /home/user/project/auto-trader/v2/tests/test_discord_panel_budget_ui.py:70: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel_budget_ui.py:82
  /home/user/project/auto-trader/v2/tests/test_discord_panel_budget_ui.py:82: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel_budget_ui.py:107
  /home/user/project/auto-trader/v2/tests/test_discord_panel_budget_ui.py:107: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel_budget_ui.py:124
  /home/user/project/auto-trader/v2/tests/test_discord_panel_budget_ui.py:124: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel_margin_budget.py:71
  /home/user/project/auto-trader/v2/tests/test_discord_panel_margin_budget.py:71: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel_margin_budget.py:79
  /home/user/project/auto-trader/v2/tests/test_discord_panel_margin_budget.py:79: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel_margin_budget.py:104
  /home/user/project/auto-trader/v2/tests/test_discord_panel_margin_budget.py:104: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel_margin_budget.py:121
  /home/user/project/auto-trader/v2/tests/test_discord_panel_margin_budget.py:121: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel_trailing_ui.py:70
  /home/user/project/auto-trader/v2/tests/test_discord_panel_trailing_ui.py:70: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel_trailing_ui.py:80
  /home/user/project/auto-trader/v2/tests/test_discord_panel_trailing_ui.py:80: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

v2/tests/test_discord_panel_trailing_ui.py:133
  /home/user/project/auto-trader/v2/tests/test_discord_panel_trailing_ui.py:133: PytestUnknownMarkWarning: Unknown pytest.mark.unit - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html
    @pytest.mark.unit

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
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
server_time=1771710235123
local_time=1771710235238
time_drift_ms=115
```

## Summary

- Result: PASS
- Ended: 2026-02-21T21:43:55Z
