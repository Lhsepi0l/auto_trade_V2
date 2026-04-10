#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

SERVICE_NAME="v2-stack"
RUN_USER="$(id -un)"
WORKDIR="$PROJECT_ROOT"
PROFILE="ra_2026_alpha_v2_expansion_verified_q070"
MODE="live"
ENVIRONMENT="prod"
ENV_FILE=".env"
HOST="127.0.0.1"
PORT="8101"
ENABLE_OPERATOR_WEB="true"
DRY_RUN="false"

usage() {
    cat <<'EOF'
Usage:
  bash v2/scripts/install_systemd_stack.sh [options]

Options:
  --service-name <name>         (default: v2-stack)
  --user <linux-user>           (default: current user)
  --workdir <absolute-path>     (default: current repo root)
  --profile <profile-name>      (default: ra_2026_alpha_v2_expansion_verified_q070)
  --mode <shadow|live>          (default: live)
  --env <testnet|prod>          (default: prod)
  --env-file <path>             (default: .env)
  --host <host>                 (default: 127.0.0.1)
  --port <port>                 (default: 8101)
  --operator-web                enable /operator web console (default: enabled)
  --dry-run                     print generated unit then exit
  --help

Examples:
  bash v2/scripts/install_systemd_stack.sh --user bot --workdir /home/bot/autotrade/auto_trade_V2 --profile ra_2026_alpha_v2_expansion_verified_q070
  bash v2/scripts/install_systemd_stack.sh --profile ra_2026_alpha_v2_expansion_verified_q070 --mode shadow --env testnet --port 8101
  bash v2/scripts/install_systemd_stack.sh --mode shadow --env testnet
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --service-name)
            SERVICE_NAME="$2"
            shift 2
            ;;
        --user)
            RUN_USER="$2"
            shift 2
            ;;
        --workdir)
            WORKDIR="$2"
            shift 2
            ;;
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
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --operator-web)
            ENABLE_OPERATOR_WEB="true"
            shift 1
            ;;
        --dry-run)
            DRY_RUN="true"
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

if [[ "$WORKDIR" != /* ]]; then
    echo "--workdir must be an absolute path"
    exit 1
fi

if [[ "$WORKDIR" =~ [[:space:]] ]]; then
    echo "--workdir must not contain spaces"
    exit 1
fi

if [[ "$ENV_FILE" == /* ]]; then
    ENV_FILE_ABS="$ENV_FILE"
else
    ENV_FILE_ABS="$WORKDIR/$ENV_FILE"
fi

if [[ ! -f "$WORKDIR/v2/scripts/run_stack.sh" ]]; then
    echo "run_stack.sh not found under $WORKDIR/v2/scripts"
    exit 1
fi

UNIT_NAME="${SERVICE_NAME}.service"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
EXTRA_ARGS=""
if [[ "$ENABLE_OPERATOR_WEB" == "true" ]]; then
    EXTRA_ARGS+=" --operator-web"
fi

TMP_UNIT="$(mktemp)"
cat > "$TMP_UNIT" <<EOF
[Unit]
Description=Auto Trader V2 web-first stack (control API + operator web)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$WORKDIR
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$ENV_FILE_ABS
ExecStart=/usr/bin/env bash $WORKDIR/v2/scripts/run_stack.sh --profile $PROFILE --mode $MODE --env $ENVIRONMENT --env-file $ENV_FILE --host $HOST --port $PORT$EXTRA_ARGS
Restart=always
RestartSec=2
KillMode=control-group
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

if [[ "$DRY_RUN" == "true" ]]; then
    cat "$TMP_UNIT"
    rm -f "$TMP_UNIT"
    exit 0
fi

sudo install -m 644 "$TMP_UNIT" "$UNIT_PATH"
rm -f "$TMP_UNIT"

sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT_NAME"
sudo systemctl status "$UNIT_NAME" --no-pager

echo "installed: $UNIT_PATH"
echo "logs: journalctl -u $UNIT_NAME -f"
