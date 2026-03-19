#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PROFILE="ra_2026_alpha_v2_expansion_verified_q070"
MODE="shadow"
ENVIRONMENT="testnet"
ENV_FILE=".env"
CONTROL_HOST="127.0.0.1"
CONTROL_PORT="8101"
CONTROL_HTTP_MODE="control-http"
ENABLE_OPERATOR_WEB="false"
WITH_DISCORD_BOT="true"
PYTHON_BIN="${PYTHON_BIN:-python}"
STACK_LOCK_FILE="${STACK_LOCK_FILE:-v2/logs/stack.lock}"
READY_TIMEOUT_SEC="${STACK_READY_TIMEOUT_SEC:-30}"
READY_POLL_SEC="${STACK_READY_POLL_SEC:-0.5}"

usage() {
    cat <<'EOF'
Usage:
  bash v2/scripts/run_stack.sh [options]

Options:
  --profile <profile-name>
  --mode <shadow|live>
  --env <testnet|prod>
  --env-file <path>
  --host <host>
  --port <port>
  --operator-web
  --no-discord-bot
  --ops-http            # rejected: stack requires control-http + /readyz
  --help

Examples:
  bash v2/scripts/run_stack.sh
  bash v2/scripts/run_stack.sh --profile ra_2026_alpha_v2_expansion_verified_q070 --mode shadow --env testnet
  bash v2/scripts/run_stack.sh --profile ra_2026_alpha_v2_expansion_verified_q070 --mode live --env prod --env-file .env --host 127.0.0.1 --port 8101
  bash v2/scripts/run_stack.sh --mode shadow --env testnet --operator-web --no-discord-bot
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
        --env-file)
            ENV_FILE="$2"
            shift 2
            ;;
        --host)
            CONTROL_HOST="$2"
            shift 2
            ;;
        --port)
            CONTROL_PORT="$2"
            shift 2
            ;;
        --operator-web)
            ENABLE_OPERATOR_WEB="true"
            shift 1
            ;;
        --no-discord-bot)
            WITH_DISCORD_BOT="false"
            shift 1
            ;;
        --ops-http)
            CONTROL_HTTP_MODE="ops-http"
            shift 1
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

if [[ "$CONTROL_HTTP_MODE" != "control-http" ]]; then
    echo "run_stack.sh requires control-http. --ops-http is blocked because /readyz and Discord bot wiring depend on the full control API."
    exit 1
fi

cd "$PROJECT_ROOT"

if [[ -f ".venv/bin/activate" ]]; then
    source ".venv/bin/activate"
fi

mkdir -p "v2/logs"
CONTROL_LOG="v2/logs/control_api.log"
BOT_LOG="v2/logs/discord_bot.log"
PIDS_FILE="v2/logs/stack.pids"

exec 9>"$STACK_LOCK_FILE"
if ! flock -n 9; then
    echo "another stack instance is already running; lock=${STACK_LOCK_FILE}"
    exit 1
fi

CONTROL_PID=""
BOT_PID=""

show_control_failure() {
    echo "control log: $CONTROL_LOG"
    if [[ -f "$CONTROL_LOG" ]]; then
        tail -n 80 "$CONTROL_LOG" || true
    fi
}

show_bot_failure() {
    echo "bot log: $BOT_LOG"
    if [[ -f "$BOT_LOG" ]]; then
        tail -n 80 "$BOT_LOG" || true
    fi
}

cleanup() {
    set +e

    terminate_pid() {
        local pid="$1"
        local grace_sec="${2:-5}"
        local waited=0

        if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
            return 0
        fi

        kill "$pid" 2>/dev/null || true
        while kill -0 "$pid" 2>/dev/null; do
            if (( waited >= grace_sec * 10 )); then
                kill -9 "$pid" 2>/dev/null || true
                break
            fi
            sleep 0.1
            waited=$((waited + 1))
        done
        wait "$pid" 2>/dev/null || true
    }

    terminate_pid "$BOT_PID" 5
    terminate_pid "$CONTROL_PID" 5
    rm -f "$PIDS_FILE"
}

trap cleanup EXIT INT TERM

verify_bot_import() {
    if [[ "$WITH_DISCORD_BOT" != "true" ]]; then
        return 0
    fi
    local import_check_log
    import_check_log="$(mktemp)"
    if "$PYTHON_BIN" -c "import importlib; importlib.import_module('v2.discord_bot.bot')" >"$import_check_log" 2>&1; then
        rm -f "$import_check_log"
        return 0
    fi
    echo "discord bot import failed: module=v2.discord_bot.bot"
    cat "$import_check_log"
    rm -f "$import_check_log"
    exit 1
}

wait_for_control_ready() {
    local ready_url="http://${CONTROL_HOST}:${CONTROL_PORT}/readyz"
    local reconcile_url="http://${CONTROL_HOST}:${CONTROL_PORT}/reconcile"
    local start_url="http://${CONTROL_HOST}:${CONTROL_PORT}/start"
    local start_ts
    local now_ts
    local elapsed
    local ready_output=""
    local reconcile_attempted=0
    local start_attempted=0

    start_ts="$(date +%s)"
    while true; do
        if ! kill -0 "$CONTROL_PID" 2>/dev/null; then
            echo "control API exited before /readyz became ready"
            show_control_failure
            exit 1
        fi

        if ready_output=$("$PYTHON_BIN" - "$ready_url" <<'PY' 2>&1
import json
import sys
import urllib.error
import urllib.request

url = sys.argv[1]

try:
    with urllib.request.urlopen(url, timeout=2.0) as response:
        body = response.read().decode("utf-8")
        payload = json.loads(body)
        if response.status == 200 and bool(payload.get("ready")):
            print(body)
            raise SystemExit(0)
        print(body)
        raise SystemExit(1)
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8")
    if body:
        print(body)
    else:
        print(f"http_error:{exc.code}")
    raise SystemExit(1)
except Exception as exc:
    print(f"request_error:{type(exc).__name__}:{exc}")
    raise SystemExit(1)
PY
        ); then
            return 0
        fi

        if (( reconcile_attempted == 0 )) && [[ "$ready_output" != request_error:* ]]; then
            if READY_OUTPUT="$ready_output" "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import json
import os
import sys

try:
    payload = json.loads(os.environ.get("READY_OUTPUT", ""))
except Exception:
    raise SystemExit(1)

raise SystemExit(0 if bool(payload.get("recovery_required")) else 1)
PY
            then
                if "$PYTHON_BIN" - "$reconcile_url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

url = sys.argv[1]
request = urllib.request.Request(url, method="POST")
with urllib.request.urlopen(request, timeout=10.0) as response:
    if response.status != 200:
        raise SystemExit(1)
raise SystemExit(0)
PY
                then
                    reconcile_attempted=1
                    sleep "$READY_POLL_SEC"
                    continue
                fi
            fi
        fi

        if (( start_attempted == 0 )) && [[ "$ready_output" != request_error:* ]]; then
            if "$PYTHON_BIN" - "$start_url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

url = sys.argv[1]
request = urllib.request.Request(url, method="POST")
with urllib.request.urlopen(request, timeout=5.0) as response:
    if response.status != 200:
        raise SystemExit(1)
raise SystemExit(0)
PY
            then
                start_attempted=1
            fi
        fi

        now_ts="$(date +%s)"
        elapsed=$((now_ts - start_ts))
        if (( elapsed >= READY_TIMEOUT_SEC )); then
            echo "control API did not become ready via /readyz within ${READY_TIMEOUT_SEC}s"
            echo "last /readyz response: ${ready_output}"
            show_control_failure
            exit 1
        fi
        sleep "$READY_POLL_SEC"
    done
}

verify_bot_import

RUN_ARGS=(
    -m v2.run
    --profile "$PROFILE"
    --mode "$MODE"
    --env "$ENVIRONMENT"
    --env-file "$ENV_FILE"
    --control-http
    --control-http-host "$CONTROL_HOST"
    --control-http-port "$CONTROL_PORT"
)
if [[ "$ENABLE_OPERATOR_WEB" == "true" ]]; then
    RUN_ARGS+=(--operator-web)
fi

"$PYTHON_BIN" "${RUN_ARGS[@]}" >"$CONTROL_LOG" 2>&1 &

CONTROL_PID=$!
wait_for_control_ready

BASE_URL="http://${CONTROL_HOST}:${CONTROL_PORT}"
if [[ "$WITH_DISCORD_BOT" == "true" ]]; then
    export TRADER_API_BASE_URL="$BASE_URL"

    "$PYTHON_BIN" -m v2.discord_bot.bot >"$BOT_LOG" 2>&1 &
    BOT_PID=$!

    for _ in {1..5}; do
        if ! kill -0 "$BOT_PID" 2>/dev/null; then
            echo "discord bot failed to start"
            show_bot_failure
            exit 1
        fi
        sleep 0.1
    done
fi

printf "%s\n%s\n" "$CONTROL_PID" "$BOT_PID" > "$PIDS_FILE"

echo "stack started"
echo "- control pid: $CONTROL_PID"
echo "- control log: $CONTROL_LOG"
echo "- base url: ${BASE_URL}"
if [[ "$ENABLE_OPERATOR_WEB" == "true" ]]; then
    echo "- operator web: ${BASE_URL}/operator"
fi
if [[ "$WITH_DISCORD_BOT" == "true" ]]; then
    echo "- bot pid: $BOT_PID"
    echo "- bot log: $BOT_LOG"
    wait -n "$CONTROL_PID" "$BOT_PID"
else
    echo "- discord bot: skipped"
    wait -n "$CONTROL_PID"
fi
EXIT_CODE=$?

echo "one process exited (code=$EXIT_CODE). shutting down stack..."
exit "$EXIT_CODE"
