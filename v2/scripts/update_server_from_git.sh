#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

REMOTE="origin"
BRANCH="migration/web-operator-panel"
SERVICE_NAME="v2-stack.service"
RESTART="true"
DRY_RUN="false"

usage() {
  cat <<'EOF'
Usage:
  bash v2/scripts/update_server_from_git.sh [options]

Options:
  --remote <name>         Git remote name (default: origin)
  --branch <name>         Branch to deploy (default: migration/web-operator-panel)
  --service <name>        systemd unit to restart (default: v2-stack.service)
  --restart               Restart service after update (default)
  --no-restart            Skip service restart
  --dry-run               Print commands only
  --help

Examples:
  bash v2/scripts/update_server_from_git.sh --branch migration/web-operator-panel --restart
  bash v2/scripts/update_server_from_git.sh --branch main --restart
  bash v2/scripts/update_server_from_git.sh --branch main --no-restart --dry-run
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote)
      REMOTE="${2:?missing value for --remote}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:?missing value for --branch}"
      shift 2
      ;;
    --service)
      SERVICE_NAME="${2:?missing value for --service}"
      shift 2
      ;;
    --restart)
      RESTART="true"
      shift 1
      ;;
    --no-restart)
      RESTART="false"
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
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

run_cmd() {
  if [[ "$DRY_RUN" == "true" ]]; then
    printf '+'
    for arg in "$@"; do
      printf ' %q' "$arg"
    done
    printf '\n'
    return 0
  fi
  "$@"
}

cd "$PROJECT_ROOT"

run_cmd git fetch "$REMOTE"
run_cmd git checkout "$BRANCH"
run_cmd git pull --ff-only "$REMOTE" "$BRANCH"

if [[ "$RESTART" == "true" ]]; then
  run_cmd sudo systemctl restart "$SERVICE_NAME"
  run_cmd sudo systemctl status "$SERVICE_NAME" --no-pager
fi

run_cmd git branch --show-current
run_cmd git rev-parse HEAD
