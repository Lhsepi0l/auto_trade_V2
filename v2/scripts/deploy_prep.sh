#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"

PROFILE="ra_2026_alpha_v2_expansion_verified_q070"
MODE="shadow"
ENVIRONMENT="testnet"
CONFIG_PATH="config/config.yaml"
REPORT_DIR="v2/reports"
KEEP_REPORTS=""
TEST_SCOPE="runtime"
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

usage() {
    cat <<'EOF'
Usage:
  bash v2/scripts/deploy_prep.sh [options]

Options:
  --profile <ra_2026_alpha_v2_expansion_verified_q070>
  --mode <shadow|live>
  --env <testnet|prod>
  --config <path>
  --report-dir <path>
  --keep-reports <n>
  --test-scope <runtime|full>
  --help

Examples:
  bash v2/scripts/deploy_prep.sh --profile ra_2026_alpha_v2_expansion_verified_q070 --mode shadow --env testnet
  bash v2/scripts/deploy_prep.sh --profile ra_2026_alpha_v2_expansion_verified_q070 --mode live --env prod --keep-reports 30
  bash v2/scripts/deploy_prep.sh --profile ra_2026_alpha_v2_expansion_verified_q070 --mode shadow --env testnet --test-scope full
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
        --keep-reports)
            KEEP_REPORTS="$2"
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

cd "$PROJECT_ROOT"

if [[ "$CONFIG_PATH" == v2/* ]]; then
    CONFIG_PATH="${CONFIG_PATH#v2/}"
fi

if [[ "$REPORT_DIR" == v2/* ]]; then
    REPORT_DIR="${REPORT_DIR#v2/}"
fi

RUNTIME_PREFLIGHT_CONFIG="$CONFIG_PATH"
if [[ "$RUNTIME_PREFLIGHT_CONFIG" != /* ]]; then
    RUNTIME_PREFLIGHT_CONFIG="v2/${RUNTIME_PREFLIGHT_CONFIG}"
fi

PREFLIGHT_CMD=(
    bash "v2/scripts/preflight.sh"
    --profile "$PROFILE"
    --mode "$MODE"
    --env "$ENVIRONMENT"
    --config "$CONFIG_PATH"
    --report-dir "$REPORT_DIR"
    --test-scope "$TEST_SCOPE"
)

if [[ -n "$KEEP_REPORTS" ]]; then
    PREFLIGHT_CMD+=(--keep-reports "$KEEP_REPORTS")
fi

echo "[deploy-prep] running preflight"
"${PREFLIGHT_CMD[@]}"

echo "[deploy-prep] running runtime preflight"
"$PYTHON_BIN" -m v2.run \
  --profile "$PROFILE" \
  --mode "$MODE" \
  --env "$ENVIRONMENT" \
  --env-file .env \
  --config "$RUNTIME_PREFLIGHT_CONFIG" \
  --runtime-preflight \
  --control-http-host 127.0.0.1 \
  --control-http-port 8101

echo "[deploy-prep] running runtime smoke"
if [[ "$MODE" == "live" && "$ENVIRONMENT" == "prod" ]]; then
  echo "[deploy-prep] running runtime smoke (live/prod guard path)"
  if "$PYTHON_BIN" -m v2.run \
    --profile "$PROFILE" \
    --mode "$MODE" \
    --env "$ENVIRONMENT" \
    --env-file .env \
    --config "$RUNTIME_PREFLIGHT_CONFIG"; then
    echo "[deploy-prep] expected live/prod direct boot to be blocked"
    exit 1
  fi

  if "$PYTHON_BIN" -m v2.run \
    --profile "$PROFILE" \
    --mode "$MODE" \
    --env "$ENVIRONMENT" \
    --env-file .env \
    --config "$RUNTIME_PREFLIGHT_CONFIG" \
    --ops-http; then
    echo "[deploy-prep] expected live/prod --ops-http path to be blocked"
    exit 1
  fi
else
  "$PYTHON_BIN" -m v2.run \
    --profile "$PROFILE" \
    --mode "$MODE" \
    --env "$ENVIRONMENT" \
    --env-file .env \
    --config "$RUNTIME_PREFLIGHT_CONFIG"
fi

echo "[deploy-prep] done"
