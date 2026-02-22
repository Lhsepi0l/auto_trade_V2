#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PROFILE="normal"
MODE="live"
ENVIRONMENT="prod"
ENV_FILE=".env"
CONTROL_HOST="0.0.0.0"
CONTROL_PORT="8101"
CONTROL_HTTP_MODE="control-http"

usage() {
    cat <<'EOF'
Usage:
  bash v2/scripts/run_stack.sh [options]

Options:
  --profile <conservative|normal|aggressive>
  --mode <shadow|live>
  --env <testnet|prod>
  --env-file <path>
  --host <host>
  --port <port>
  --ops-http            # use ops-http API instead of control-http
  --help

Examples:
  bash v2/scripts/run_stack.sh
  bash v2/scripts/run_stack.sh --mode shadow --env testnet
  bash v2/scripts/run_stack.sh --host 127.0.0.1 --port 8101
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

cd "$PROJECT_ROOT"

if [[ -f ".venv/bin/activate" ]]; then
    source ".venv/bin/activate"
fi

mkdir -p "v2/logs"
CONTROL_LOG="v2/logs/control_api.log"
BOT_LOG="v2/logs/discord_bot.log"
PIDS_FILE="v2/logs/stack.pids"

CONTROL_PID=""
BOT_PID=""

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

if [[ "$CONTROL_HTTP_MODE" == "control-http" ]]; then
    python -m v2.run \
        --profile "$PROFILE" \
        --mode "$MODE" \
        --env "$ENVIRONMENT" \
        --env-file "$ENV_FILE" \
        --control-http \
        --control-http-host "$CONTROL_HOST" \
        --control-http-port "$CONTROL_PORT" \
        >"$CONTROL_LOG" 2>&1 &
else
    python -m v2.run \
        --profile "$PROFILE" \
        --mode "$MODE" \
        --env "$ENVIRONMENT" \
        --env-file "$ENV_FILE" \
        --ops-http \
        --ops-http-host "$CONTROL_HOST" \
        --ops-http-port "$CONTROL_PORT" \
        >"$CONTROL_LOG" 2>&1 &
fi

CONTROL_PID=$!

for _ in {1..5}; do
    if ! kill -0 "$CONTROL_PID" 2>/dev/null; then
        echo "control API failed to start; see $CONTROL_LOG"
        exit 1
    fi
    sleep 0.1
done

if [[ -z "${TRADER_API_BASE_URL:-}" ]]; then
    export TRADER_API_BASE_URL="http://127.0.0.1:${CONTROL_PORT}"
fi

python -m v2.discord_bot.bot >"$BOT_LOG" 2>&1 &
BOT_PID=$!

printf "%s\n%s\n" "$CONTROL_PID" "$BOT_PID" > "$PIDS_FILE"

echo "stack started"
echo "- control pid: $CONTROL_PID"
echo "- bot pid: $BOT_PID"
echo "- control log: $CONTROL_LOG"
echo "- bot log: $BOT_LOG"
echo "- base url: ${TRADER_API_BASE_URL}"

wait -n "$CONTROL_PID" "$BOT_PID"
EXIT_CODE=$?

echo "one process exited (code=$EXIT_CODE). shutting down stack..."
exit "$EXIT_CODE"
