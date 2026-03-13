#!/usr/bin/env bash
set -euo pipefail

PROFILE="ra_2026_alpha_v2_expansion_verified_q070"
MODE="shadow"
ENVIRONMENT="testnet"
CONTROL_BASE_URL="http://127.0.0.1:8101"
OUTPUT_ROOT="logs/shadow_soak"
LABEL="manual"
LOG_LINES=400

usage() {
  cat <<'EOF'
Usage:
  bash v2/scripts/shadow_soak_snapshot.sh [options]

Options:
  --profile <name>          Runtime profile to record (default: ra_2026_alpha_v2_expansion_verified_q070)
  --mode <shadow|live>      Runtime mode to record (default: shadow)
  --env <testnet|prod>      Runtime env to record (default: testnet)
  --base-url <url>          Control API base URL (default: http://127.0.0.1:8101)
  --output-root <dir>       Snapshot root directory (default: logs/shadow_soak)
  --label <name>            Snapshot label suffix (default: manual)
  --log-lines <n>           Tail lines per log/journal capture (default: 400)

Example:
  bash v2/scripts/shadow_soak_snapshot.sh --label kickoff
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="${2:?missing value for --profile}"
      shift 2
      ;;
    --mode)
      MODE="${2:?missing value for --mode}"
      shift 2
      ;;
    --env)
      ENVIRONMENT="${2:?missing value for --env}"
      shift 2
      ;;
    --base-url)
      CONTROL_BASE_URL="${2:?missing value for --base-url}"
      shift 2
      ;;
    --output-root)
      OUTPUT_ROOT="${2:?missing value for --output-root}"
      shift 2
      ;;
    --label)
      LABEL="${2:?missing value for --label}"
      shift 2
      ;;
    --log-lines)
      LOG_LINES="${2:?missing value for --log-lines}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SNAPSHOT_DIR="${OUTPUT_ROOT}/${STAMP}_${LABEL}"
CONTROL_DIR="${SNAPSHOT_DIR}/control"
META_DIR="${SNAPSHOT_DIR}/meta"
GIT_DIR="${SNAPSHOT_DIR}/git"
SYSTEM_DIR="${SNAPSHOT_DIR}/system"
LOG_DIR="${SNAPSHOT_DIR}/logs"

mkdir -p "$CONTROL_DIR" "$META_DIR" "$GIT_DIR" "$SYSTEM_DIR" "$LOG_DIR"

record_cmd() {
  local output_file="$1"
  shift
  if "$@" >"$output_file" 2>&1; then
    return 0
  fi
  return 0
}

fetch_endpoint() {
  local path="$1"
  local name="${path#/}"
  name="${name//\//_}"
  local url="${CONTROL_BASE_URL}${path}"
  local body_file="${CONTROL_DIR}/${name}.json"
  local pretty_file="${CONTROL_DIR}/${name}.pretty.json"
  local headers_file="${CONTROL_DIR}/${name}.headers.txt"
  local meta_file="${CONTROL_DIR}/${name}.meta.txt"
  local stderr_file="${CONTROL_DIR}/${name}.stderr.txt"
  local body_tmp="${body_file}.tmp"
  local http_code="curl_failed"

  if command -v curl >/dev/null 2>&1; then
    http_code="$(curl -sS -D "$headers_file" -o "$body_tmp" -w '%{http_code}' "$url" 2>"$stderr_file" || true)"
    if [[ -f "$body_tmp" ]]; then
      mv "$body_tmp" "$body_file"
    else
      : >"$body_file"
    fi
  else
    echo "curl not found" >"$stderr_file"
    : >"$headers_file"
    : >"$body_file"
  fi

  {
    echo "path=${path}"
    echo "url=${url}"
    echo "http_code=${http_code}"
    echo "captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$meta_file"

  if [[ -s "$body_file" ]]; then
    python -m json.tool <"$body_file" >"$pretty_file" 2>/dev/null || cp "$body_file" "$pretty_file"
  else
    : >"$pretty_file"
  fi
}

{
  echo "snapshot_started_at=${STARTED_AT}"
  echo "profile=${PROFILE}"
  echo "mode=${MODE}"
  echo "env=${ENVIRONMENT}"
  echo "control_base_url=${CONTROL_BASE_URL}"
  echo "cwd=$(pwd)"
  echo "user=$(whoami)"
} >"${META_DIR}/runtime_context.txt"

record_cmd "${META_DIR}/host.txt" hostname
record_cmd "${META_DIR}/uname.txt" uname -a
record_cmd "${META_DIR}/python_version.txt" python -V

record_cmd "${GIT_DIR}/head.txt" git rev-parse HEAD
record_cmd "${GIT_DIR}/branch.txt" git branch --show-current
record_cmd "${GIT_DIR}/status.txt" git status --short
record_cmd "${GIT_DIR}/diff_stat.txt" git diff --stat

record_cmd "${SYSTEM_DIR}/processes.txt" pgrep -af "python -m v2.run|v2.discord_bot.bot|run_stack.sh|v2-stack.service"
record_cmd "${SYSTEM_DIR}/sockets.txt" ss -ltnp
record_cmd "${SYSTEM_DIR}/data_dirs.txt" find data logs v2/logs -maxdepth 2 -type f 2>/dev/null
record_cmd "${SYSTEM_DIR}/systemctl_v2-stack.service.txt" systemctl status v2-stack.service --no-pager
record_cmd "${SYSTEM_DIR}/journal_v2-stack.service.log" journalctl -u v2-stack.service -n "${LOG_LINES}" --no-pager

fetch_endpoint "/healthz"
fetch_endpoint "/readyz"
fetch_endpoint "/status"

while IFS= read -r logfile; do
  [[ -n "$logfile" ]] || continue
  safe_name="$(echo "$logfile" | sed 's#^\./##; s#[/ ]#_#g')"
  tail -n "$LOG_LINES" "$logfile" >"${LOG_DIR}/${safe_name}.tail.log" 2>&1 || true
done < <(find logs v2/logs -maxdepth 2 -type f \( -name '*.log' -o -name '*.txt' \) 2>/dev/null | sort)

echo "snapshot_finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"${META_DIR}/runtime_context.txt"

echo "$SNAPSHOT_DIR"
