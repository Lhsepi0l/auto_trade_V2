#!/usr/bin/env bash
set -euo pipefail
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPORT_DIR="${SCRIPT_DIR}/reports"

PYTHON_BIN="${PYTHON_BIN:-python}"
PROFILE="${PROFILE:-ra_2026_alpha_v2_expansion_live_candidate}"
SYMBOLS="${SYMBOLS:-BTCUSDT,ETHUSDT}"
YEARS="${YEARS:-3}"
INITIAL_CAPITAL="${INITIAL_CAPITAL:-100}"
FEE_BPS="${FEE_BPS:-4.0}"
SLIPPAGE_BPS="${SLIPPAGE_BPS:-2.0}"
FUNDING_BPS_8H="${FUNDING_BPS_8H:-0.5}"
MARGIN_USE_PCT="${MARGIN_USE_PCT:-10.0}"
REPLAY_WORKERS="${REPLAY_WORKERS:-0}"
BACKTEST_FETCH_SLEEP_SEC="${BACKTEST_FETCH_SLEEP_SEC:-0.03}"
BACKTEST_OFFLINE="${BACKTEST_OFFLINE:-1}"
REVERSE_MIN_HOLD_BARS="${REVERSE_MIN_HOLD_BARS:-16}"
STOPLOSS_STREAK_TRIGGER="${STOPLOSS_STREAK_TRIGGER:-2}"
STOPLOSS_COOLDOWN_BARS="${STOPLOSS_COOLDOWN_BARS:-24}"
LOSS_COOLDOWN_BARS="${LOSS_COOLDOWN_BARS:-30}"
MIN_EXPECTED_EDGE_MULTIPLE="${MIN_EXPECTED_EDGE_MULTIPLE:-2.2}"
MIN_REWARD_RISK_RATIO="${MIN_REWARD_RISK_RATIO:-}"
MAX_TRADES_PER_DAY="${MAX_TRADES_PER_DAY:-}"
BACKTEST_START_UTC="${BACKTEST_START_UTC:-}"
BACKTEST_END_UTC="${BACKTEST_END_UTC:-}"
REVERSE_COOLDOWN_BARS="${REVERSE_COOLDOWN_BARS:-30}"
REVERSE_EXIT_MIN_PROFIT_PCT="${REVERSE_EXIT_MIN_PROFIT_PCT:-0.4}"
REVERSE_EXIT_MIN_SIGNAL_SCORE="${REVERSE_EXIT_MIN_SIGNAL_SCORE:-0.60}"
MIN_SIGNAL_SCORE="${MIN_SIGNAL_SCORE:-0.60}"
KEEP_REPORT_JSON="${KEEP_REPORT_JSON:-0}"

if [[ $# -gt 0 && "${1}" != -* ]]; then
  SYMBOLS="$1"
  shift
fi

if [[ -n "${BACKTEST_SYMBOLS:-}" ]]; then
  SYMBOLS="${BACKTEST_SYMBOLS}"
fi

_is_truthy() {
  case "${1,,}" in
    1|true|yes|y|on)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}
if [[ $# -gt 0 && "${1}" != -* ]]; then
  YEARS="$1"
  shift
fi
if [[ $# -gt 0 && "${1}" != -* ]]; then
  INITIAL_CAPITAL="$1"
  shift
fi

echo "[LOCAL_BACKTEST] symbols=${SYMBOLS} years=${YEARS} offline=${BACKTEST_OFFLINE}"

if [[ -z "${MAX_TRADES_PER_DAY}" ]]; then
  case "${PROFILE}" in
    ra_2026_alpha_v2_expansion_live_candidate)
      MAX_TRADES_PER_DAY="1"
      ;;
    *)
      MAX_TRADES_PER_DAY="2"
      ;;
  esac
fi

if [[ -z "${MIN_REWARD_RISK_RATIO}" ]]; then
  case "${PROFILE}" in
    ra_2026_alpha_v2_expansion_live_candidate)
      MIN_REWARD_RISK_RATIO="1.8"
      ;;
    *)
      MIN_REWARD_RISK_RATIO="1.8"
      ;;
  esac
fi

mkdir -p "${REPORT_DIR}"
RUN_LOG="$(mktemp)"
cd "${REPO_ROOT}"

cleanup() {
  rm -f "${RUN_LOG}"
}
trap cleanup EXIT

# Force line-buffered, unbuffered streaming for real-time logs.
if command -v stdbuf >/dev/null 2>&1; then
  PYTHON_CMD=(stdbuf -oL -eL "${PYTHON_BIN}" -u -m v2.run)
else
  PYTHON_CMD=("${PYTHON_BIN}" -u -m v2.run)
fi

PYTHONUNBUFFERED=1 "${PYTHON_CMD[@]}" \
  --profile "${PROFILE}" \
  --mode shadow \
  --env prod \
  --local-backtest \
  --backtest-symbols "${SYMBOLS}" \
  --backtest-years "${YEARS}" \
  --backtest-initial-capital "${INITIAL_CAPITAL}" \
  --backtest-fee-bps "${FEE_BPS}" \
  --backtest-slippage-bps "${SLIPPAGE_BPS}" \
  --backtest-funding-bps-8h "${FUNDING_BPS_8H}" \
  --backtest-margin-use-pct "${MARGIN_USE_PCT}" \
  --backtest-replay-workers "${REPLAY_WORKERS}" \
  --backtest-fetch-sleep-sec "${BACKTEST_FETCH_SLEEP_SEC}" \
  $(_is_truthy "${BACKTEST_OFFLINE}" && echo --backtest-offline) \
  --backtest-reverse-min-hold-bars "${REVERSE_MIN_HOLD_BARS}" \
  --backtest-reverse-cooldown-bars "${REVERSE_COOLDOWN_BARS}" \
  --backtest-reverse-exit-min-profit-pct "${REVERSE_EXIT_MIN_PROFIT_PCT}" \
  --backtest-reverse-exit-min-signal-score "${REVERSE_EXIT_MIN_SIGNAL_SCORE}" \
  ${BACKTEST_START_UTC:+--backtest-start-utc "${BACKTEST_START_UTC}"} \
  ${BACKTEST_END_UTC:+--backtest-end-utc "${BACKTEST_END_UTC}"} \
  --backtest-max-trades-per-day "${MAX_TRADES_PER_DAY}" \
  --backtest-min-signal-score "${MIN_SIGNAL_SCORE}" \
  --backtest-stoploss-streak-trigger "${STOPLOSS_STREAK_TRIGGER}" \
  --backtest-stoploss-cooldown-bars "${STOPLOSS_COOLDOWN_BARS}" \
  --backtest-loss-cooldown-bars "${LOSS_COOLDOWN_BARS}" \
  --backtest-min-expected-edge-multiple "${MIN_EXPECTED_EDGE_MULTIPLE}" \
  --backtest-min-reward-risk-ratio "${MIN_REWARD_RISK_RATIO}" \
  --report-dir "${REPORT_DIR}" \
  "$@" 2>&1 | tee "${RUN_LOG}"
RUN_EXIT=$?
set -e

if command -v rg >/dev/null 2>&1; then
  REPORT_JSON_PATH="$(rg -n '^REPORT_JSON=' "${RUN_LOG}" | tail -n1 | cut -d'=' -f2-)"
  REPORT_MD_PATH="$(rg -n '^REPORT_MD=' "${RUN_LOG}" | tail -n1 | cut -d'=' -f2-)"
else
  REPORT_JSON_PATH="$(grep -n '^REPORT_JSON=' "${RUN_LOG}" | tail -n1 | cut -d'=' -f2-)"
  REPORT_MD_PATH="$(grep -n '^REPORT_MD=' "${RUN_LOG}" | tail -n1 | cut -d'=' -f2-)"
fi

if [[ "${KEEP_REPORT_JSON}" == "1" ]]; then
  if [[ -n "${REPORT_JSON_PATH}" ]]; then
    echo "REPORT_JSON=${REPORT_JSON_PATH}"
  fi
  if [[ -n "${REPORT_MD_PATH}" ]]; then
    echo "REPORT_MD=${REPORT_MD_PATH}"
  fi
else
  if [[ -n "${REPORT_JSON_PATH}" && -f "${REPORT_JSON_PATH}" ]]; then
    rm -f "${REPORT_JSON_PATH}"
  fi
  if [[ -n "${REPORT_MD_PATH}" ]]; then
    echo "REPORT_MD=${REPORT_MD_PATH}"
  fi
fi
exit "${RUN_EXIT}"
