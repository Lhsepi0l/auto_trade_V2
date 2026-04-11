#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"
cd "$PROJECT_ROOT"

PROJECT=auto-trader
PROFILE="ra_2026_alpha_v2_expansion_verified_q070"
MODE="shadow"
ENVIRONMENT="testnet"
CONFIG_PATH="config/config.yaml"
REPORT_DIR="reports"
KEEP_REPORTS=""
REPORT_PATH=""
TIME_DRIFT_MS=5000
TEST_SCOPE="runtime"
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"
PYTHON_BIN="${PYTHON_BIN:-}"

if [[ -z "$PYTHON_BIN" ]]; then
    if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
        PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
    else
        PYTHON_BIN="$(command -v python)"
    fi
fi

RUNTIME_PYTEST_TARGETS=(
    "v2/tests/test_v2_config_loader.py"
    "v2/tests/test_v2_env_and_notify.py"
    "v2/tests/test_v2_run_smoke.py"
    "v2/tests/test_control_api.py"
    "v2/tests/test_live_execution_service.py"
    "v2/tests/test_exchange_user_stream.py"
    "v2/tests/test_tpsl_brackets.py"
    "v2/tests/test_operator_web_routes.py"
    "v2/tests/test_webpush_service.py"
)

usage() {
    cat <<'EOF'
Usage:
  bash v2/scripts/preflight.sh [options]

Options:
  --profile <ra_2026_alpha_v2_expansion_verified_q070>
  --mode <shadow|live>
  --env <testnet|prod>
  --config <path>
  --report-dir <dir>
  --report-file <path>
  --keep-reports <n>
  --time-drift-ms <ms>
  --test-scope <runtime|full>
  --help

Examples:
  bash v2/scripts/preflight.sh --profile ra_2026_alpha_v2_expansion_verified_q070 --mode shadow --env testnet
  bash v2/scripts/preflight.sh --profile ra_2026_alpha_v2_expansion_verified_q070 --mode live --env prod --report-file reports/readiness.md
  bash v2/scripts/preflight.sh --profile ra_2026_alpha_v2_expansion_verified_q070 --mode shadow --env testnet --test-scope full
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)
            PROFILE="$2"
            shift 2
            ;;
        --mode)
            MODE="$2"
            shift 2
            ;;
        --env)
            ENVIRONMENT="$2"
            shift 2
            ;;
        --config)
            CONFIG_PATH="$2"
            shift 2
            ;;
        --report-dir)
            REPORT_DIR="$2"
            shift 2
            ;;
        --report-file)
            REPORT_PATH="$2"
            shift 2
            ;;
        --keep-reports)
            KEEP_REPORTS="$2"
            shift 2
            ;;
        --time-drift-ms)
            TIME_DRIFT_MS="$2"
            shift 2
            ;;
        --test-scope)
            TEST_SCOPE="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

if [[ "$MODE" != "shadow" && "$MODE" != "live" ]]; then
    echo "--mode must be shadow or live"
    exit 1
fi

if [[ "$ENVIRONMENT" != "testnet" && "$ENVIRONMENT" != "prod" ]]; then
    echo "--env must be testnet or prod"
    exit 1
fi

if [[ "$TEST_SCOPE" != "runtime" && "$TEST_SCOPE" != "full" ]]; then
    echo "--test-scope must be runtime or full"
    exit 1
fi

if [[ "$CONFIG_PATH" != /* ]]; then
    CONFIG_PATH="$REPO_ROOT/${CONFIG_PATH#./}"
fi

if [[ "$REPORT_DIR" != /* ]]; then
    REPORT_DIR="$REPO_ROOT/${REPORT_DIR#./}"
fi

if [[ -n "$REPORT_PATH" && "$REPORT_PATH" != /* ]]; then
    REPORT_PATH="$REPO_ROOT/${REPORT_PATH#./}"
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "Config file not found: $CONFIG_PATH"
    exit 1
fi

if [[ -n "$KEEP_REPORTS" && ! "$KEEP_REPORTS" =~ ^[0-9]+$ ]]; then
    echo "--keep-reports must be a non-negative integer"
    exit 1
fi

cleanup_old_reports() {
    local keep="$1"
    if [[ -z "$keep" ]]; then
        return 0
    fi

    "$PYTHON_BIN" - "$REPORT_DIR" "$keep" <<'PY'
import sys
from pathlib import Path

report_dir = Path(sys.argv[1])
keep = int(sys.argv[2])

reports = sorted(
    [p for p in report_dir.glob("deployment_readiness_*.md") if p.is_file()],
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)

for stale in reports[keep:]:
    try:
        stale.unlink()
    except OSError:
        pass
PY
}

mkdir -p "$REPORT_DIR"
if [[ -z "$REPORT_PATH" ]]; then
    TS="$(date -u +%Y%m%d_%H%M%S)"
    REPORT_PATH="$REPORT_DIR/deployment_readiness_${TS}.md"
fi

if [[ -n "$KEEP_REPORTS" ]]; then
    if (( KEEP_REPORTS > 0 )); then
        cleanup_old_reports "$((KEEP_REPORTS - 1))"
    fi
fi

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cat <<EOF > "$REPORT_PATH"
# Deployment Readiness Report

- Project: ${PROJECT}
- Started: ${STARTED_AT}
- Profile: ${PROFILE}
- Mode: ${MODE}
- Environment: ${ENVIRONMENT}
- Test Scope: ${TEST_SCOPE}
- Config: ${CONFIG_PATH}
- Report: ${REPORT_PATH}

## Checks
EOF

FAILED=0

append_result() {
    local title="$1"
    local status="$2"
    local output_file="$3"

    echo "" >> "$REPORT_PATH"
    echo "### ${title}" >> "$REPORT_PATH"
    echo "- Status: ${status}" >> "$REPORT_PATH"
    echo "- Output:" >> "$REPORT_PATH"
    echo '```' >> "$REPORT_PATH"

    if [[ -s "$output_file" ]]; then
        cat "$output_file" >> "$REPORT_PATH"
    else
        echo "(no output)" >> "$REPORT_PATH"
    fi

    echo '```' >> "$REPORT_PATH"
}

finalize_report() {
    local result="$1"

    cat <<EOF >> "$REPORT_PATH"

## Summary

- Result: ${result}
- Ended: $(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
}

run_shell_check() {
    local title="$1"
    shift

    local tmp_out
    tmp_out="$(mktemp)"

    if "$@" > "$tmp_out" 2>&1; then
        append_result "$title" "PASS" "$tmp_out"
        rm -f "$tmp_out"
        return 0
    fi

    append_result "$title" "FAIL" "$tmp_out"
    rm -f "$tmp_out"
    FAILED=1
    return 1
}

run_python_check() {
    local title="$1"
    local code="$2"

    local tmp_py
    local tmp_out
    tmp_py="$(mktemp)"
    tmp_out="$(mktemp)"

    printf "%s\n" "$code" > "$tmp_py"
    if "$PYTHON_BIN" "$tmp_py" > "$tmp_out" 2>&1; then
        append_result "$title" "PASS" "$tmp_out"
        rm -f "$tmp_py" "$tmp_out"
        return 0
    fi

    append_result "$title" "FAIL" "$tmp_out"
    rm -f "$tmp_py" "$tmp_out"
    FAILED=1
    return 1
}

run_pytest_suite() {
    local scope="$1"
    local tmp_out
    tmp_out="$(mktemp)"

    {
        echo "scope=${scope}"
        if [[ "$scope" == "runtime" ]]; then
            printf "targets=%s\n" "${RUNTIME_PYTEST_TARGETS[*]}"
            (
                cd "$PROJECT_ROOT"
                "$PYTHON_BIN" -m pytest -q "${RUNTIME_PYTEST_TARGETS[@]}"
            )
        else
            echo "targets=v2/tests"
            (
                cd "$PROJECT_ROOT"
                "$PYTHON_BIN" -m pytest -q v2/tests
            )
        fi
    } > "$tmp_out" 2>&1
    local rc=$?

    if [[ "$rc" -eq 0 ]]; then
        append_result "3) pytest (${scope})" "PASS" "$tmp_out"
        rm -f "$tmp_out"
        return 0
    fi

    append_result "3) pytest (${scope})" "FAIL" "$tmp_out"
    rm -f "$tmp_out"
    FAILED=1
    return 1
}

echo "[step] secrets policy"
SECRETS_CHECK=$(cat <<'PY'
from pathlib import Path

path = Path("__CONFIG_PATH__")
payload = path.read_text(encoding="utf-8")
forbidden = ["BINANCE_API_KEY", "BINANCE_API_SECRET"]

for key in forbidden:
    if key in payload:
        raise RuntimeError(f"forbidden key found in config: {key}")

print(f"config_path={path}")
print("secrets policy check passed")
PY
)

SECRETS_CHECK="${SECRETS_CHECK/__CONFIG_PATH__/$CONFIG_PATH}"
run_python_check "1) secrets policy and config format" "$SECRETS_CHECK" || true

echo "[step] ruff lint"
run_shell_check "2) ruff check" bash -lc "cd '$PROJECT_ROOT' && '$PYTHON_BIN' -m ruff check v2 v2/tests" || true
if [[ "$FAILED" -ne 0 ]]; then
    echo "preflight stopped: lint failed"
    finalize_report FAILED
    exit 1
fi

echo "[step] tests (${TEST_SCOPE})"
run_pytest_suite "$TEST_SCOPE" || true
if [[ "$FAILED" -ne 0 ]]; then
    echo "preflight stopped: tests failed"
    finalize_report FAILED
    exit 1
fi

echo "[step] config validation"
CONFIG_CHECK=$(cat <<'PY'
from pathlib import Path

from v2.config.loader import load_effective_config

cfg = load_effective_config(profile="__PROFILE__", mode="__MODE__", env="__ENV__", config_path="__CONFIG__")
print(f"profile={cfg.profile}")
print(f"mode={cfg.mode}")
print(f"env={cfg.env}")
print(f"symbol={cfg.behavior.exchange.default_symbol}")
print(f"tick_seconds={cfg.behavior.scheduler.tick_seconds}")
print(f"request_rate_limit_per_sec={cfg.behavior.exchange.request_rate_limit_per_sec}")
PY
)

CONFIG_CHECK="${CONFIG_CHECK/__PROFILE__/$PROFILE}"
CONFIG_CHECK="${CONFIG_CHECK/__MODE__/$MODE}"
CONFIG_CHECK="${CONFIG_CHECK/__ENV__/$ENVIRONMENT}"
CONFIG_CHECK="${CONFIG_CHECK/__CONFIG__/$CONFIG_PATH}"
run_python_check "4) effective config load" "$CONFIG_CHECK" || true
if [[ "$FAILED" -ne 0 ]]; then
    echo "preflight stopped: config validation failed"
    finalize_report FAILED
    exit 1
fi

echo "[step] connectivity checks"
CONNECTIVITY_CHECK=$(cat <<'PY'
import json
import time
import urllib.error
import urllib.request

import os

env = os.environ["PRE_FLIGHT_ENV"]
base_url = "https://testnet.binancefuture.com" if env == "testnet" else "https://fapi.binance.com"
threshold_ms = int(os.environ["PRE_FLIGHT_TIME_DRIFT_MS"])


def request(path: str) -> tuple[int, dict]:
    url = f"{base_url}{path}"
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = response.read().decode("utf-8")
        return response.status, json.loads(payload)


ping_code, ping_payload = request("/fapi/v1/ping")
if ping_code != 200:
    raise RuntimeError(f"ping failed status={ping_code}")

time_code, time_payload = request("/fapi/v1/time")
if time_code != 200:
    raise RuntimeError(f"time endpoint failed status={time_code}")

server_ms = int(time_payload["serverTime"])
local_ms = int(time.time() * 1000)
drift_ms = abs(server_ms - local_ms)
if drift_ms > threshold_ms:
    raise RuntimeError(f"time drift too high: {drift_ms}ms > {threshold_ms}ms")

print(f"env={env}")
print(f"base_url={base_url}")
print(f"ping_status={ping_code}")
print(f"time_status={time_code}")
print(f"server_time={server_ms}")
print(f"local_time={local_ms}")
print(f"time_drift_ms={drift_ms}")
PY
)

PRE_FLIGHT_ENV="$ENVIRONMENT" PRE_FLIGHT_TIME_DRIFT_MS="$TIME_DRIFT_MS" \
    run_python_check "5) connectivity ping/time sync" "$CONNECTIVITY_CHECK" || true

if [[ "$FAILED" -ne 0 ]]; then
    echo "preflight failed"
    finalize_report FAILED
    exit 1
fi

finalize_report PASS
