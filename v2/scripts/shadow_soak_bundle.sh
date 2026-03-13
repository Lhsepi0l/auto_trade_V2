#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="logs/shadow_soak"
SOURCE_DIR=""
OUTPUT_DIR="logs/shadow_soak_bundles"

usage() {
  cat <<'EOF'
Usage:
  bash v2/scripts/shadow_soak_bundle.sh [options]

Options:
  --source-root <dir>   Root directory containing snapshots (default: logs/shadow_soak)
  --source-dir <dir>    Exact snapshot directory to bundle (default: latest under source-root)
  --output-dir <dir>    Directory to write tar.gz bundle (default: logs/shadow_soak_bundles)

Examples:
  bash v2/scripts/shadow_soak_bundle.sh
  bash v2/scripts/shadow_soak_bundle.sh --source-dir logs/shadow_soak/20260313T120000Z_kickoff
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-root)
      SOURCE_ROOT="${2:?missing value for --source-root}"
      shift 2
      ;;
    --source-dir)
      SOURCE_DIR="${2:?missing value for --source-dir}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="${2:?missing value for --output-dir}"
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

if [[ -z "$SOURCE_DIR" ]]; then
  SOURCE_DIR="$(find "$SOURCE_ROOT" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1)"
fi

if [[ -z "$SOURCE_DIR" || ! -d "$SOURCE_DIR" ]]; then
  echo "snapshot directory not found: ${SOURCE_DIR:-<none>}" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BASENAME="$(basename "$SOURCE_DIR")"
ARCHIVE_PATH="${OUTPUT_DIR}/${BASENAME}_${STAMP}.tar.gz"

tar -czf "$ARCHIVE_PATH" -C "$(dirname "$SOURCE_DIR")" "$BASENAME"

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$ARCHIVE_PATH" >"${ARCHIVE_PATH}.sha256"
fi

echo "$ARCHIVE_PATH"
