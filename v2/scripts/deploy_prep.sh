#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"

PROFILE="normal"
MODE="shadow"
ENVIRONMENT="testnet"
CONFIG_PATH="config/config.yaml"
REPORT_DIR="v2/reports"
KEEP_REPORTS=""

usage() {
    cat <<'EOF'
Usage:
  bash v2/scripts/deploy_prep.sh [options]

Options:
  --profile <normal|conservative|aggressive>
  --mode <shadow|live>
  --env <testnet|prod>
  --config <path>
  --report-dir <path>
  --keep-reports <n>
  --help

Examples:
  bash v2/scripts/deploy_prep.sh --profile normal --mode shadow --env testnet
  bash v2/scripts/deploy_prep.sh --profile normal --mode live --env prod --keep-reports 30
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

PREFLIGHT_CMD=(
    bash "v2/scripts/preflight.sh"
    --profile "$PROFILE"
    --mode "$MODE"
    --env "$ENVIRONMENT"
    --config "$CONFIG_PATH"
    --report-dir "$REPORT_DIR"
)

if [[ -n "$KEEP_REPORTS" ]]; then
    PREFLIGHT_CMD+=(--keep-reports "$KEEP_REPORTS")
fi

echo "[deploy-prep] running preflight"
"${PREFLIGHT_CMD[@]}"

echo "[deploy-prep] running runtime smoke"
python -m v2.run --profile "$PROFILE" --mode shadow --env testnet --env-file .env

echo "[deploy-prep] done"
