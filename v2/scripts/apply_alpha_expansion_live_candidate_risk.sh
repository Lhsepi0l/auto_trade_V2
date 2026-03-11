#!/usr/bin/env bash

set -euo pipefail

PROFILE="ra_2026_alpha_v2_expansion_live_candidate"
BASE_URL="http://127.0.0.1:8101"
DRY_RUN="false"
SKIP_VERIFY="false"
ALLOW_REMOTE="false"

usage() {
    cat <<'EOF'
Usage:
  bash v2/scripts/apply_alpha_expansion_live_candidate_risk.sh [options]

Options:
  --base-url <http://127.0.0.1:8101>
  --dry-run
  --skip-verify
  --allow-remote
  --help

Examples:
  bash v2/scripts/apply_alpha_expansion_live_candidate_risk.sh
  bash v2/scripts/apply_alpha_expansion_live_candidate_risk.sh --dry-run
  bash v2/scripts/apply_alpha_expansion_live_candidate_risk.sh --base-url http://127.0.0.1:8101
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-url)
            BASE_URL="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN="true"
            shift 1
            ;;
        --skip-verify)
            SKIP_VERIFY="true"
            shift 1
            ;;
        --allow-remote)
            ALLOW_REMOTE="true"
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

if [[ "$ALLOW_REMOTE" != "true" ]]; then
    case "$BASE_URL" in
        http://127.0.0.1:*|http://localhost:*)
            ;;
        *)
            echo "--base-url must stay on localhost unless --allow-remote is set"
            exit 1
            ;;
    esac
fi

declare -a RISK_SETTINGS=(
    "capital_mode=MARGIN_BUDGET_USDT"
    "margin_budget_usdt=30"
    "margin_use_pct=0.10"
    "max_leverage=5"
    "daily_loss_limit_pct=-0.015"
    "dd_limit_pct=-0.12"
    "universe_symbols=BTCUSDT"
    "risk_score_min=0.60"
    "spread_max_pct=0.35"
    "enable_watchdog=true"
    "watchdog_interval_sec=15"
    "lose_streak_n=2"
    "cooldown_hours=4"
    "auto_risk_enabled=true"
    "auto_pause_on_risk=true"
    "auto_safe_mode_on_risk=true"
    "auto_flatten_on_risk=true"
    "allow_market_when_wide_spread=false"
)

apply_setting() {
    local key="$1"
    local value="$2"
    local payload
    payload=$(printf '{"key":"%s","value":"%s"}' "$key" "$value")

    if [[ "$DRY_RUN" == "true" ]]; then
        printf "curl -fsS -X POST %s/set -H 'content-type: application/json' -d '%s'\n" "$BASE_URL" "$payload"
        return 0
    fi

    curl -fsS -X POST "$BASE_URL/set" \
        -H "content-type: application/json" \
        -d "$payload" >/dev/null
    printf "[applied] %s=%s\n" "$key" "$value"
}

verify_endpoint() {
    local path="$1"

    if [[ "$DRY_RUN" == "true" ]]; then
        printf "curl -fsS %s%s\n" "$BASE_URL" "$path"
        return 0
    fi

    printf "[verify] %s%s\n" "$BASE_URL" "$path"
    curl -fsS "$BASE_URL$path"
    printf "\n"
}

printf "[profile] %s\n" "$PROFILE"
printf "[base-url] %s\n" "$BASE_URL"

for item in "${RISK_SETTINGS[@]}"; do
    key="${item%%=*}"
    value="${item#*=}"
    apply_setting "$key" "$value"
done

if [[ "$SKIP_VERIFY" != "true" ]]; then
    verify_endpoint "/risk"
    verify_endpoint "/readiness"
    verify_endpoint "/status"
fi
