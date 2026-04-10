from __future__ import annotations

import argparse

CANONICAL_LIVE_PROFILE = "ra_2026_alpha_v2_expansion_verified_q070"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V2 scaffold runner")
    parser.add_argument("--profile", default=CANONICAL_LIVE_PROFILE)
    parser.add_argument("--mode", default="shadow", choices=["shadow", "live"])
    parser.add_argument("--env", default="testnet", choices=["testnet", "prod"])
    parser.add_argument("--env-file", default=".env", help="path to dotenv file for secrets")
    parser.add_argument("--config", default=None, help="path to v2 config.yaml")
    parser.add_argument(
        "--ops-action",
        default="none",
        choices=["none", "pause", "resume", "safe_mode", "flatten"],
    )
    parser.add_argument("--ops-symbol", default=None, help="symbol for ops actions like flatten")
    parser.add_argument(
        "--ops-http",
        action="store_true",
        help="run optional HTTP ops control server (shadow/testnet only; live/prod blocked)",
    )
    parser.add_argument("--ops-http-host", default="127.0.0.1")
    parser.add_argument("--ops-http-port", type=int, default=8102)
    parser.add_argument(
        "--control-http",
        action="store_true",
        help="run v2 full control HTTP API (Discord compatible)",
    )
    parser.add_argument(
        "--operator-web",
        action="store_true",
        help="mount server-rendered web operator console on the control HTTP app",
    )
    parser.add_argument("--control-http-host", default="127.0.0.1")
    parser.add_argument("--control-http-port", type=int, default=8101)
    parser.add_argument("--replay", default=None, help="path to replay source")
    parser.add_argument("--report-dir", default="v2/reports", help="directory for replay reports")
    parser.add_argument("--report-path", default=None, help="write report to exact path")
    parser.add_argument(
        "--local-backtest",
        action="store_true",
        help="download historical candles and run local replay backtest",
    )
    parser.add_argument(
        "--backtest-symbols",
        default="BTCUSDT,ETHUSDT",
        help="comma-separated symbols for local backtest",
    )
    parser.add_argument(
        "--backtest-years",
        type=int,
        default=3,
        help="historical lookback years for local backtest",
    )
    parser.add_argument(
        "--backtest-start-utc",
        default=None,
        help="absolute UTC start time for local backtest (ISO 8601, e.g. 2024-01-01T00:00:00Z). if set, requires backtest-end-utc.",
    )
    parser.add_argument(
        "--backtest-end-utc",
        default=None,
        help="absolute UTC end time for local backtest (ISO 8601, e.g. 2024-12-31T23:59:59Z). if set, requires backtest-start-utc.",
    )
    parser.add_argument(
        "--backtest-initial-capital",
        type=float,
        default=30.0,
        help="ignored at runtime (locked to 30.00 USDT for local backtest)",
    )
    parser.add_argument(
        "--backtest-fee-bps",
        type=float,
        default=4.0,
        help="per-side execution fee in basis points for local backtest",
    )
    parser.add_argument(
        "--backtest-slippage-bps",
        type=float,
        default=2.0,
        help="per-side slippage in basis points for local backtest",
    )
    parser.add_argument(
        "--backtest-funding-bps-8h",
        type=float,
        default=0.5,
        help="funding rate basis points applied every 8h in local backtest",
    )
    parser.add_argument(
        "--backtest-margin-use-pct",
        type=float,
        default=12.0,
        help="position margin usage percent for fixed-leverage local backtest",
    )
    parser.add_argument(
        "--backtest-replay-workers",
        type=int,
        default=0,
        help="parallel worker count for symbol replay (0 = auto by CPU and symbols)",
    )
    parser.add_argument(
        "--backtest-fetch-sleep-sec",
        type=float,
        default=0.03,
        help="sleep seconds between paginated klines requests (0 disables)",
    )
    parser.add_argument(
        "--backtest-offline",
        action="store_true",
        help="skip klines network download; fail fast with cache-only data",
    )
    parser.add_argument(
        "--backtest-reverse-min-hold-bars",
        type=int,
        default=16,
        help="minimum holding bars before reverse-signal close",
    )
    parser.add_argument(
        "--backtest-reverse-cooldown-bars",
        type=int,
        default=18,
        help="cooldown bars after exit before next entry",
    )
    parser.add_argument(
        "--backtest-min-expected-edge-multiple",
        type=float,
        default=2.2,
        help="required expected edge multiplier over roundtrip cost",
    )
    parser.add_argument(
        "--backtest-min-reward-risk-ratio",
        type=float,
        default=1.4,
        help="minimum reward/risk ratio required for entry",
    )
    parser.add_argument(
        "--backtest-max-trades-per-day",
        type=int,
        default=3,
        help="max entries per symbol per day in local backtest",
    )
    parser.add_argument(
        "--backtest-daily-loss-limit-pct",
        type=float,
        default=2.5,
        help="daily realized loss stop percent (0 disables)",
    )
    parser.add_argument(
        "--backtest-equity-floor-pct",
        type=float,
        default=50.0,
        help="equity floor percent of initial capital (0 disables)",
    )
    parser.add_argument(
        "--backtest-max-trade-margin-loss-fraction",
        type=float,
        default=30.0,
        help="max loss cap as percent of used margin per trade",
    )
    parser.add_argument(
        "--backtest-min-signal-score",
        type=float,
        default=0.40,
        help="minimum signal score to allow entry",
    )
    parser.add_argument(
        "--backtest-enabled-alphas",
        default=None,
        help="comma-separated enabled_alphas override for ra_2026_alpha_v2 local backtest (e.g. alpha_expansion,alpha_drift)",
    )
    parser.add_argument(
        "--backtest-alpha-squeeze-percentile-max",
        type=float,
        default=0.40,
        help="15m squeeze percentile threshold override for ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-expansion-buffer-bps",
        type=float,
        default=2.0,
        help="expansion breakout confirmation buffer in basis points for ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-expansion-range-atr-min",
        type=float,
        default=1.0,
        help="minimum 15m expansion range in ATR for ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-expansion-body-ratio-min",
        type=float,
        default=0.0,
        help="minimum candle body/range ratio for alpha_expansion in ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-expansion-close-location-min",
        type=float,
        default=0.0,
        help="minimum favored close location within candle range for alpha_expansion in ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-expansion-width-expansion-min",
        type=float,
        default=0.0,
        help="minimum bollinger bandwidth expansion fraction for alpha_expansion in ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-expansion-break-distance-atr-min",
        type=float,
        default=0.0,
        help="minimum breakout distance beyond donchian channel in ATR units for alpha_expansion in ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-expansion-breakout-efficiency-min",
        type=float,
        default=0.0,
        help="minimum breakout efficiency beyond donchian channel relative to candle range for alpha_expansion in ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-expansion-breakout-stability-score-min",
        type=float,
        default=0.0,
        help="minimum penalty-based breakout stability score for alpha_expansion in ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-expansion-breakout-stability-edge-score-min",
        type=float,
        default=0.0,
        help="minimum breakout stability score after cost-edge interaction for alpha_expansion in ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-expansion-quality-score-min",
        type=float,
        default=0.0,
        help="minimum composite expansion quality score for alpha_expansion in ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-expansion-quality-score-v2-min",
        type=float,
        default=None,
        help="minimum penalty-based breakout quality score v2 for alpha_expansion in ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-min-volume-ratio",
        type=float,
        default=1.0,
        help="minimum 15m volume ratio for ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-take-profit-r",
        type=float,
        default=2.0,
        help="take-profit R multiple override for ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-time-stop-bars",
        type=int,
        default=24,
        help="time-stop bars override for ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-trend-adx-min-4h",
        type=float,
        default=14.0,
        help="4h trend ADX floor override for ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-trend-adx-max-4h",
        type=float,
        default=0.0,
        help="optional 4h trend ADX cap override for ra_2026_alpha_v2 (0 disables)",
    )
    parser.add_argument(
        "--backtest-alpha-trend-adx-rising-lookback-4h",
        type=int,
        default=0,
        help="optional 4h ADX rising lookback bars for ra_2026_alpha_v2 (0 disables)",
    )
    parser.add_argument(
        "--backtest-alpha-trend-adx-rising-min-delta-4h",
        type=float,
        default=0.0,
        help="minimum 4h ADX increase over lookback for ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-alpha-expected-move-cost-mult",
        type=float,
        default=2.0,
        help="expected move cost multiplier override for ra_2026_alpha_v2",
    )
    parser.add_argument(
        "--backtest-drift-side-mode",
        default="BOTH",
        help="drift side mode override for alpha_drift (BOTH, LONG, SHORT)",
    )
    parser.add_argument(
        "--backtest-drift-take-profit-r",
        type=float,
        default=1.8,
        help="take-profit R override for alpha_drift",
    )
    parser.add_argument(
        "--backtest-drift-time-stop-bars",
        type=int,
        default=16,
        help="time-stop bars override for alpha_drift",
    )
    parser.add_argument(
        "--backtest-fb-failed-break-buffer-bps",
        type=float,
        default=4.0,
        help="failed-breakout overshoot buffer override for local backtest",
    )
    parser.add_argument(
        "--backtest-fb-wick-ratio-min",
        type=float,
        default=1.25,
        help="minimum wick/body rejection ratio override for local backtest",
    )
    parser.add_argument(
        "--backtest-fb-take-profit-r",
        type=float,
        default=1.6,
        help="failed-breakout take-profit R override for local backtest",
    )
    parser.add_argument(
        "--backtest-fb-time-stop-bars",
        type=int,
        default=8,
        help="failed-breakout time-stop bars override for local backtest",
    )
    parser.add_argument(
        "--backtest-cbr-squeeze-percentile-max",
        type=float,
        default=0.35,
        help="15m squeeze percentile threshold override for local backtest",
    )
    parser.add_argument(
        "--backtest-cbr-breakout-buffer-bps",
        type=float,
        default=3.0,
        help="breakout confirmation buffer override for local backtest",
    )
    parser.add_argument(
        "--backtest-cbr-take-profit-r",
        type=float,
        default=2.1,
        help="compression-breakout take-profit R override for local backtest",
    )
    parser.add_argument(
        "--backtest-cbr-time-stop-bars",
        type=int,
        default=14,
        help="compression-breakout time-stop bars override for local backtest",
    )
    parser.add_argument(
        "--backtest-cbr-trend-adx-min-4h",
        type=float,
        default=14.0,
        help="4h trend ADX floor override for local backtest",
    )
    parser.add_argument(
        "--backtest-cbr-ema-gap-trend-min-frac-4h",
        type=float,
        default=0.0030,
        help="4h EMA gap floor override for local backtest",
    )
    parser.add_argument(
        "--backtest-cbr-breakout-min-range-atr",
        type=float,
        default=0.90,
        help="minimum breakout range in ATR override for local backtest",
    )
    parser.add_argument(
        "--backtest-cbr-breakout-min-volume-ratio",
        type=float,
        default=1.0,
        help="minimum breakout volume ratio override for local backtest",
    )
    parser.add_argument(
        "--backtest-sfd-reclaim-sweep-buffer-bps",
        type=float,
        default=3.0,
        help="legacy reclaim sweep buffer in basis points",
    )
    parser.add_argument(
        "--backtest-sfd-reclaim-wick-ratio-min",
        type=float,
        default=1.2,
        help="legacy minimum reclaim wick/body ratio",
    )
    parser.add_argument(
        "--backtest-sfd-drive-breakout-range-atr-min",
        type=float,
        default=0.9,
        help="legacy minimum breakout range in ATR for session-drive path",
    )
    parser.add_argument(
        "--backtest-sfd-take-profit-r",
        type=float,
        default=1.5,
        help="legacy shared take-profit R override",
    )
    parser.add_argument(
        "--backtest-pfd-premium-z-min",
        type=float,
        default=2.0,
        help="legacy minimum absolute premium z-score",
    )
    parser.add_argument(
        "--backtest-pfd-funding-24h-min",
        type=float,
        default=0.00020,
        help="legacy minimum absolute 24h funding sum",
    )
    parser.add_argument(
        "--backtest-pfd-reclaim-buffer-atr",
        type=float,
        default=0.15,
        help="legacy ATR fraction buffer around reclaim levels",
    )
    parser.add_argument(
        "--backtest-pfd-take-profit-r",
        type=float,
        default=1.8,
        help="legacy take-profit R override",
    )
    parser.add_argument(
        "--backtest-reverse-exit-min-profit-pct",
        type=float,
        default=0.4,
        help="minimum unrealized profit percent required for reverse-signal close",
    )
    parser.add_argument(
        "--backtest-reverse-exit-min-signal-score",
        type=float,
        default=0.60,
        help="minimum reverse signal score required for reverse-signal close",
    )
    parser.add_argument(
        "--backtest-drawdown-scale-start-pct",
        type=float,
        default=12.0,
        help="drawdown percent where margin scaling starts",
    )
    parser.add_argument(
        "--backtest-drawdown-scale-end-pct",
        type=float,
        default=32.0,
        help="drawdown percent where margin scaling reaches minimum",
    )
    parser.add_argument(
        "--backtest-drawdown-margin-scale-min",
        type=float,
        default=35.0,
        help="minimum margin scale percent at deep drawdown",
    )
    parser.add_argument(
        "--backtest-stoploss-streak-trigger",
        type=int,
        default=3,
        help="consecutive stop-loss count to trigger temporary entry cooldown",
    )
    parser.add_argument(
        "--backtest-stoploss-cooldown-bars",
        type=int,
        default=20,
        help="bars to pause new entries after stop-loss streak trigger",
    )
    parser.add_argument(
        "--backtest-loss-cooldown-bars",
        type=int,
        default=0,
        help="bars to pause new entries after any losing close",
    )
    parser.add_argument("--loop", action="store_true", help="run continuous tick loop")
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="max cycle count when --loop is enabled (0 = unlimited)",
    )
    parser.add_argument(
        "--deploy-prep", action="store_true", help="run one-command deployment preparation"
    )
    parser.add_argument(
        "--runtime-preflight",
        action="store_true",
        help="run runtime deployment gate checks without serving HTTP",
    )
    parser.add_argument(
        "--keep-reports", type=int, default=None, help="retention count for deploy-prep reports"
    )
    parser.add_argument(
        "--test-scope",
        choices=["runtime", "full"],
        default="runtime",
        help="test scope for deploy-prep/preflight (runtime=server-minimal, full=workstation full suite)",
    )
    return parser
