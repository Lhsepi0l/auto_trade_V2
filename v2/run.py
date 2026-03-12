from __future__ import annotations

import argparse
import asyncio
import calendar
import csv
import fcntl
import json
import logging
import os
import signal
import subprocess
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from v2.clean_room import Candidate, build_default_kernel
from v2.clean_room.kernel import _build_strategy_selector
from v2.clean_room.portfolio import (
    PortfolioRoutingConfig,
    portfolio_bucket_for_symbol,
    route_ranked_candidates,
)
from v2.common.logging_setup import LoggingConfig, setup_logging
from v2.config.loader import (
    EffectiveConfig,
    EnvName,
    ModeName,
    load_effective_config,
    render_effective_config,
)
from v2.control import build_runtime_controller, create_control_http_app
from v2.core import Event, EventBus, Scheduler
from v2.engine import EngineStateStore, OrderManager
from v2.exchange import BackoffPolicy, BinanceAdapter, BinanceRESTClient
from v2.notify import Notifier
from v2.ops import OpsController, create_ops_http_app
from v2.risk import KillSwitch, RiskManager
from v2.storage import RuntimeStorage
from v2.strategies.alpha_shared import _Bar
from v2.tpsl import BracketConfig, BracketPlanner

LOCAL_BACKTEST_INITIAL_CAPITAL_USDT = 30.0
logger = logging.getLogger(__name__)
_VOL_TARGET_STRATEGIES = frozenset(
    {
        "ra_2026_alpha_v2",
    }
)
_LEGACY_PORTFOLIO_BACKTEST_STRATEGIES: frozenset[str] = frozenset()


def _configure_runtime_logging(*, component: str = "runtime") -> None:
    setup_logging(
        LoggingConfig(
            level=str(os.getenv("V2_LOG_LEVEL", "INFO")),
            log_dir=str(os.getenv("V2_LOG_DIR", "v2/logs")),
            json=True,
            component=component,
        )
    )


def _live_trading_enabled(cfg: EffectiveConfig) -> bool:
    return str(cfg.mode) == "live" and str(cfg.env) == "prod"


def _runtime_identity_payload(
    cfg: EffectiveConfig,
    *,
    host: str | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    return {
        "profile": cfg.profile,
        "mode": cfg.mode,
        "env": cfg.env,
        "live_trading_enabled": _live_trading_enabled(cfg),
        "bind_host": host,
        "bind_port": port,
        "surface_label": (
            "실거래 활성" if _live_trading_enabled(cfg) else "모의/테스트 또는 비실거래"
        ),
    }


def _print_runtime_banner(
    cfg: EffectiveConfig,
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    print("[v2] runtime banner")
    print(json.dumps(_runtime_identity_payload(cfg, host=host, port=port), ensure_ascii=True))


def _safe_runtime_state(
    state_provider: Callable[[], dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if state_provider is None:
        return None
    try:
        payload = state_provider()
    except Exception as exc:  # noqa: BLE001
        return {"state_provider_error": type(exc).__name__}
    return payload if isinstance(payload, dict) else None


@contextmanager
def _dirty_runtime_marker(
    *,
    enabled: bool = True,
    cfg: EffectiveConfig | None = None,
    state_provider: Callable[[], dict[str, Any]] | None = None,
):  # type: ignore[no-untyped-def]
    if not enabled:
        yield False
        return
    marker_path = Path(os.getenv("V2_RUNTIME_DIRTY_MARKER_FILE", "v2/logs/live_runtime.dirty"))
    state_path = Path(os.getenv("V2_RUNTIME_STATE_FILE", "v2/logs/live_runtime_state.json"))
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    dirty_restart_detected = marker_path.exists()
    payload = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "profile": cfg.profile if cfg is not None else None,
        "mode": cfg.mode if cfg is not None else None,
        "env": cfg.env if cfg is not None else None,
    }
    marker_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                **payload,
                "dirty_restart_detected": bool(dirty_restart_detected),
                "clean_shutdown": False,
                "shutdown_reason": None,
                "last_state": _safe_runtime_state(state_provider),
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    clean_shutdown = False
    try:
        yield dirty_restart_detected
        clean_shutdown = True
    finally:
        ended_payload = {
            **payload,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "dirty_restart_detected": bool(dirty_restart_detected),
            "clean_shutdown": bool(clean_shutdown),
            "shutdown_reason": "graceful_shutdown" if clean_shutdown else "unclean_exit",
            "last_state": _safe_runtime_state(state_provider),
        }
        state_path.write_text(json.dumps(ended_payload, ensure_ascii=True), encoding="utf-8")
        if clean_shutdown and marker_path.exists():
            marker_path.unlink()


@contextmanager
def _live_runtime_lock(*, enabled: bool = True):  # type: ignore[no-untyped-def]
    if not enabled:
        yield
        return
    lock_path = Path(os.getenv("V2_RUNTIME_LOCK_FILE", "v2/logs/live_runtime.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"runtime_lock_held:{lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V2 scaffold runner")
    parser.add_argument("--profile", default="ra_2026_alpha_v2_expansion_live_candidate")
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
        "--ops-http", action="store_true", help="run optional HTTP ops control server"
    )
    parser.add_argument("--ops-http-host", default="127.0.0.1")
    parser.add_argument("--ops-http-port", type=int, default=8102)
    parser.add_argument(
        "--control-http",
        action="store_true",
        help="run v2 full control HTTP API (Discord compatible)",
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
        default=0.0,
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


def _build_runtime(
    cfg: EffectiveConfig,
) -> tuple[RuntimeStorage, EngineStateStore, OpsController, BinanceAdapter, Any | None]:
    storage = RuntimeStorage(sqlite_path=cfg.behavior.storage.sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    adapter = BinanceAdapter.from_effective_config(cfg)
    rest = adapter.create_rest_client(cfg=cfg)
    ops = OpsController(state_store=state_store, exchange=rest)
    return storage, state_store, ops, adapter, rest


@dataclass(frozen=True)
class _ReplayFrame:
    symbol: str
    market: dict[str, Any]
    meta: dict[str, Any]


@dataclass(frozen=True)
class _Kline15m:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class _FundingRateRow:
    funding_time_ms: int
    funding_rate: float


@dataclass
class _OpenTrade:
    symbol: str
    side: str
    entry_price: float
    quantity: float
    initial_quantity: float
    entry_fee: float
    entry_notional: float
    margin_used: float
    max_loss_cap: float | None
    tp: float | None
    sl: float | None
    initial_risk_abs: float | None
    time_stop_bars: int | None = None
    progress_check_bars: int = 0
    progress_min_mfe_r: float = 0.0
    progress_extend_trigger_r: float = 0.0
    progress_extend_bars: int = 0
    reverse_exit_min_r: float = 0.0
    allow_reverse_exit: bool = True
    effective_leverage: float | None = None
    regime: str | None = None
    entry_tick: int = 0
    entry_time_ms: int = 0
    stop_exit_cooldown_bars: int = 0
    loss_streak_trigger: int = 0
    loss_streak_cooldown_bars: int = 0
    profit_exit_cooldown_bars: int = 0
    alpha_id: str | None = None
    entry_family: str | None = None
    regime_lost_exit_required: bool = False
    tp_partial_ratio: float = 0.0
    tp_partial_price: float | None = None
    tp_partial_at_r: float = 0.0
    break_even_move_r: float = 0.0
    runner_exit_mode: str | None = None
    runner_trailing_atr_mult: float = 0.0
    stalled_trend_timeout_bars: int | None = None
    stalled_volume_ratio_floor: float = 0.0
    entry_quality_score_v2: float = 0.0
    entry_regime_strength: float = 0.0
    entry_bias_strength: float = 0.0
    quality_exit_applied: bool = False
    selective_extension_proof_bars: int = 0
    selective_extension_min_mfe_r: float = 0.0
    selective_extension_min_regime_strength: float = 0.0
    selective_extension_min_bias_strength: float = 0.0
    selective_extension_min_quality_score_v2: float = 0.0
    selective_extension_time_stop_bars: int = 0
    selective_extension_take_profit_r: float = 0.0
    selective_extension_move_stop_to_be_at_r: float = 0.0
    selective_extension_activated: bool = False
    selective_extension_activation_tick: int | None = None
    selective_extension_tp_applied: bool = False
    selective_extension_protection_applied: bool = False
    partial_taken: bool = False
    runner_stop: float | None = None
    peak_price: float | None = None
    trough_price: float | None = None
    realized_gross_pnl: float = 0.0
    realized_exit_fees: float = 0.0
    realized_funding_pnl: float = 0.0


@dataclass(frozen=True)
class _BacktestExecutionModel:
    fee_bps: float = 0.0
    slippage_bps: float = 0.0
    funding_bps_per_8h: float = 0.0

    def _slippage_ratio(self) -> float:
        return max(float(self.slippage_bps), 0.0) / 10000.0

    def _fee_ratio(self) -> float:
        return max(float(self.fee_bps), 0.0) / 10000.0

    def filled_entry_price(self, *, side: str, raw_price: float) -> float:
        slip = self._slippage_ratio()
        if side == "BUY":
            return float(raw_price) * (1.0 + slip)
        return float(raw_price) * (1.0 - slip)

    def filled_exit_price(self, *, side: str, raw_price: float) -> float:
        slip = self._slippage_ratio()
        if side == "BUY":
            return float(raw_price) * (1.0 - slip)
        return float(raw_price) * (1.0 + slip)

    def fee(self, *, notional: float) -> float:
        return max(float(notional), 0.0) * self._fee_ratio()

    def funding_pnl(
        self,
        *,
        side: str,
        entry_time_ms: int,
        exit_time_ms: int,
        notional: float,
    ) -> float:
        interval_ms = 8 * 60 * 60 * 1000
        held_ms = max(int(exit_time_ms) - int(entry_time_ms), 0)
        periods = held_ms // interval_ms
        if periods <= 0:
            return 0.0
        rate = max(float(self.funding_bps_per_8h), 0.0) / 10000.0
        funding_amount = max(float(notional), 0.0) * rate * float(periods)
        if side == "BUY":
            return -funding_amount
        return funding_amount


class _RollingAggregator:
    def __init__(self, *, timeframe_minutes: int, history_limit: int) -> None:
        self._bucket_ms = int(timeframe_minutes) * 60 * 1000
        self._history: deque[dict[str, float]] = deque(maxlen=max(int(history_limit), 1))
        self._active_key: int | None = None
        self._active: dict[str, float] | None = None

    def _flush_active(self) -> None:
        if self._active is not None:
            self._history.append(dict(self._active))
        self._active = None
        self._active_key = None

    def update(self, *, open_time_ms: int, o: float, h: float, low: float, c: float) -> None:
        bucket_key = int(open_time_ms // self._bucket_ms)
        if self._active_key is None:
            self._active_key = bucket_key
            self._active = {"open": o, "high": h, "low": low, "close": c}
            return
        if bucket_key != self._active_key:
            self._flush_active()
            self._active_key = bucket_key
            self._active = {"open": o, "high": h, "low": low, "close": c}
            return
        if self._active is None:
            self._active = {"open": o, "high": h, "low": low, "close": c}
            return
        self._active["high"] = max(float(self._active["high"]), h)
        self._active["low"] = min(float(self._active["low"]), low)
        self._active["close"] = c

    def candles(self) -> list[dict[str, float]]:
        out = list(self._history)
        if self._active is not None:
            out.append(dict(self._active))
        return out


def _zscore_latest(values: list[float], lookback: int) -> float | None:
    window = [float(item) for item in values[-max(int(lookback), 1) :]]
    if len(window) < max(int(lookback), 2):
        return None
    mean_value = sum(window) / float(len(window))
    variance = sum((item - mean_value) ** 2 for item in window) / float(len(window))
    stdev = variance ** 0.5
    if stdev <= 1e-12:
        return 0.0
    return (float(window[-1]) - float(mean_value)) / stdev


def _sum_recent_funding(
    rows: list[_FundingRateRow],
    *,
    current_time_ms: int,
    window_ms: int,
) -> float | None:
    relevant = [
        float(row.funding_rate)
        for row in rows
        if int(row.funding_time_ms) <= int(current_time_ms)
        and int(row.funding_time_ms) > int(current_time_ms) - int(window_ms)
    ]
    if not relevant:
        return None
    return sum(relevant)


class _HistoricalSnapshotProvider:
    def __init__(
        self,
        *,
        symbol: str,
        candles_15m: list[_Kline15m],
        market_candles: dict[str, list[_Kline15m]] | None = None,
        premium_rows_15m: list[_Kline15m] | None = None,
        funding_rows: list[_FundingRateRow] | None = None,
        market_intervals: list[str] | None = None,
        candles_10m: list[_Kline15m] | None = None,
        candles_30m: list[_Kline15m] | None = None,
        candles_1h: list[_Kline15m] | None = None,
        candles_4h: list[_Kline15m] | None = None,
        history_limit: int = 260,
    ) -> None:
        self._symbol = symbol
        self._candles_15m = sorted(list(candles_15m), key=lambda row: int(row.open_time_ms))
        self._idx = -1
        limit = max(int(history_limit), 1)

        legacy_market_candles: dict[str, list[_Kline15m]] = {}
        if candles_10m is not None:
            legacy_market_candles["10m"] = list(candles_10m)
        if candles_30m is not None:
            legacy_market_candles["30m"] = list(candles_30m)
        if candles_1h is not None:
            legacy_market_candles["1h"] = list(candles_1h)
        if candles_4h is not None:
            legacy_market_candles["4h"] = list(candles_4h)

        merged_market: dict[str, list[_Kline15m]] = {}
        if market_candles is not None:
            for interval, rows in market_candles.items():
                key = str(interval).strip()
                if not key:
                    continue
                merged_market[key] = list(rows)
        for interval, rows in legacy_market_candles.items():
            merged_market.setdefault(interval, rows)

        configured_intervals = []
        if isinstance(market_intervals, list):
            for raw_interval in market_intervals:
                interval = str(raw_interval).strip()
                if interval:
                    configured_intervals.append(interval)
        if not configured_intervals:
            configured_intervals = ["10m", "15m", "30m", "1h", "4h"]
            for interval in merged_market.keys():
                interval_key = str(interval).strip()
                if interval_key and interval_key not in configured_intervals:
                    configured_intervals.append(interval_key)
        if "15m" not in configured_intervals:
            configured_intervals.insert(0, "15m")

        seen_intervals: set[str] = set()
        ordered_intervals: list[str] = []
        for interval in configured_intervals:
            if interval in seen_intervals:
                continue
            seen_intervals.add(interval)
            ordered_intervals.append(interval)
        self._intervals = ordered_intervals

        self._histories: dict[str, deque[dict[str, float]]] = {
            interval: deque(maxlen=limit) for interval in self._intervals
        }
        self._sources: dict[str, list[_Kline15m]] = {}
        self._source_index: dict[str, int] = {}
        self._premium_source = sorted(
            list(premium_rows_15m or []),
            key=lambda row: int(row.open_time_ms),
        )
        self._premium_index = -1
        self._premium_history: deque[_Kline15m] = deque(maxlen=max(limit, 320))
        self._funding_source = sorted(
            list(funding_rows or []),
            key=lambda row: int(row.funding_time_ms),
        )
        self._funding_index = -1
        self._funding_history: deque[_FundingRateRow] = deque(maxlen=32)
        for interval in self._intervals:
            if interval == "15m":
                continue
            rows = merged_market.get(interval, [])
            self._sources[interval] = sorted(list(rows), key=lambda row: int(row.open_time_ms))
            self._source_index[interval] = -1

    @staticmethod
    def _row_to_ohlc(row: _Kline15m) -> dict[str, float]:
        return {
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
        }

    def _advance_interval(self, *, interval: str, current_close_time_ms: int) -> None:
        source = self._sources.get(interval, [])
        history = self._histories[interval]
        idx = int(self._source_index.get(interval, -1))
        while idx + 1 < len(source):
            nxt = source[idx + 1]
            if int(nxt.close_time_ms) > int(current_close_time_ms):
                break
            idx += 1
            history.append(self._row_to_ohlc(nxt))
        self._source_index[interval] = idx

    def _advance_premium(self, *, current_close_time_ms: int) -> None:
        idx = int(self._premium_index)
        while idx + 1 < len(self._premium_source):
            nxt = self._premium_source[idx + 1]
            if int(nxt.close_time_ms) > int(current_close_time_ms):
                break
            idx += 1
            self._premium_history.append(nxt)
        self._premium_index = idx

    def _advance_funding(self, *, current_close_time_ms: int) -> None:
        idx = int(self._funding_index)
        while idx + 1 < len(self._funding_source):
            nxt = self._funding_source[idx + 1]
            if int(nxt.funding_time_ms) > int(current_close_time_ms):
                break
            idx += 1
            self._funding_history.append(nxt)
        self._funding_index = idx

    def __len__(self) -> int:
        return len(self._candles_15m)

    def candle_at(self, idx: int) -> _Kline15m:
        return self._candles_15m[idx]

    def __call__(self) -> dict[str, Any]:
        self._idx += 1
        if self._idx >= len(self._candles_15m):
            return {}
        row = self._candles_15m[self._idx]
        self._histories["15m"].append(self._row_to_ohlc(row))

        for interval in self._intervals:
            if interval == "15m":
                continue
            self._advance_interval(interval=interval, current_close_time_ms=row.close_time_ms)
        self._advance_premium(current_close_time_ms=row.close_time_ms)
        self._advance_funding(current_close_time_ms=row.close_time_ms)

        market_payload = {
            interval: list(self._histories[interval]) for interval in self._intervals
        }
        premium_rows = list(self._premium_history)
        funding_rows = list(self._funding_history)
        market_payload["premium"] = {
            "close_15m": (
                float(premium_rows[-1].close) if premium_rows else None
            ),
            "zscore_24h": (
                _zscore_latest([float(item.close) for item in premium_rows], 96)
                if premium_rows
                else None
            ),
            "zscore_3d": (
                _zscore_latest([float(item.close) for item in premium_rows], 288)
                if premium_rows
                else None
            ),
        }
        market_payload["funding"] = {
            "last": float(funding_rows[-1].funding_rate) if funding_rows else None,
            "sum_24h": _sum_recent_funding(
                funding_rows,
                current_time_ms=row.close_time_ms,
                window_ms=24 * 60 * 60 * 1000,
            ),
            "sum_3d": _sum_recent_funding(
                funding_rows,
                current_time_ms=row.close_time_ms,
                window_ms=3 * 24 * 60 * 60 * 1000,
            ),
        }

        return {
            "symbol": self._symbol,
            "market": market_payload,
            "timestamp": datetime.fromtimestamp(
                row.open_time_ms / 1000, tz=timezone.utc
            ).isoformat(),
            "open_time": row.open_time_ms,
            "close_time": row.close_time_ms,
        }


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _locked_local_backtest_initial_capital(_requested: float | None = None) -> float:
    return float(LOCAL_BACKTEST_INITIAL_CAPITAL_USDT)


def _resolve_market_intervals(cfg: EffectiveConfig) -> list[str]:
    intervals = ["10m", "15m", "30m", "1h", "4h"]
    configured = getattr(getattr(cfg.behavior, "exchange", None), "market_intervals", None)
    if isinstance(configured, list) and configured:
        normalized: list[str] = []
        for value in configured:
            interval = str(value).strip()
            if interval:
                normalized.append(interval)
        if normalized:
            intervals = normalized

    if "15m" not in intervals:
        intervals.insert(0, "15m")

    seen: set[str] = set()
    ordered: list[str] = []
    for interval in intervals:
        if interval in seen:
            continue
        seen.add(interval)
        ordered.append(interval)
    return ordered


def _is_vol_target_backtest_strategy(strategy_name: str) -> bool:
    normalized = str(strategy_name).strip()
    return normalized in _VOL_TARGET_STRATEGIES


def _safe_json_loads(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _extract_meta(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "event_time",
        "timestamp",
        "time",
        "datetime",
        "open_time",
        "close_time",
    ):
        if key in payload:
            out[key] = payload[key]
    return out


def _extract_snapshot_time(meta: dict[str, Any]) -> Any:
    for key in ("event_time", "timestamp", "time", "datetime", "open_time", "close_time"):
        if key in meta:
            return meta[key]
    return None


def _normalize_snapshot(payload: dict[str, Any], *, default_symbol: str) -> _ReplayFrame | None:
    symbol = str(payload.get("symbol") or payload.get("default_symbol") or default_symbol).upper()

    raw_market = payload.get("market")
    if not isinstance(raw_market, dict):
        raw_market = {key: payload.get(key) for key in ("4h", "1h", "15m")}

    market: dict[str, Any] = {}
    for interval in ("4h", "1h", "15m"):
        rows = raw_market.get(interval)
        if rows is None:
            continue
        if isinstance(rows, str):
            rows = _safe_json_loads(rows)
        if isinstance(rows, list):
            market[interval] = rows

    if len(market) == 0:
        return None
    return _ReplayFrame(symbol=symbol, market=market, meta=_extract_meta(payload))


def _normalize_replay_rows(items: list[Any], *, default_symbol: str) -> list[_ReplayFrame]:
    normalized = [
        _normalize_snapshot(row, default_symbol=default_symbol)
        for row in items
        if isinstance(row, dict)
    ]
    return [row for row in normalized if row is not None]


def _load_replay_rows_json(
    *, path: Path, default_symbol: str, is_jsonl: bool = False
) -> list[_ReplayFrame]:
    if is_jsonl:
        jsonl_rows: list[Any] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            raw_line = line.strip()
            if not raw_line:
                continue
            try:
                parsed = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                jsonl_rows.append(parsed)
        return _normalize_replay_rows(jsonl_rows, default_symbol=default_symbol)

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        rows_payload: Any = raw.get("rows")
        if isinstance(rows_payload, list):
            return _normalize_replay_rows(rows_payload, default_symbol=default_symbol)
        if isinstance(rows_payload, str):
            nested = _safe_json_loads(rows_payload)
            if isinstance(nested, list):
                return _normalize_replay_rows(nested, default_symbol=default_symbol)
        normalized = _normalize_snapshot(raw, default_symbol=default_symbol)
        return [normalized] if normalized is not None else []
    if isinstance(raw, list):
        return _normalize_replay_rows(raw, default_symbol=default_symbol)
    return []


def _load_replay_rows_csv(*, path: Path, default_symbol: str) -> list[_ReplayFrame]:
    frames: list[_ReplayFrame] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return frames

    reader = csv.DictReader(lines)
    rows = [row for row in reader if isinstance(row, dict)]
    if len(rows) == 0:
        return frames

    first = rows[0]
    has_market_fields = any(k in first for k in ("market_4h", "market_1h", "market_15m"))
    if has_market_fields:
        for raw in rows:
            if raw is None:
                continue
            payload = dict(raw)
            for key in ("market_4h", "market_1h", "market_15m"):
                if key in payload:
                    raw_market = _safe_json_loads(payload[key])
                    if isinstance(raw_market, list):
                        payload[key.replace("market_", "")] = raw_market
            normalized = _normalize_snapshot(payload, default_symbol=default_symbol)
            if normalized is not None:
                frames.append(normalized)
        return frames

    market_frames: dict[str, list[dict[str, float]]] = {"4h": [], "1h": [], "15m": []}
    default_symbol_upper = default_symbol.upper()
    for raw in rows:
        if raw is None:
            continue
        interval = str(raw.get("interval") or "").strip()
        if interval not in market_frames:
            continue

        open_v = _to_float(raw.get("open"))
        high_v = _to_float(raw.get("high"))
        low_v = _to_float(raw.get("low"))
        close_v = _to_float(raw.get("close"))
        if open_v is None or high_v is None or low_v is None or close_v is None:
            continue

        candle = {
            "open": open_v,
            "high": high_v,
            "low": low_v,
            "close": close_v,
        }
        market_frames[interval].append(candle)

        if all(market_frames[k] for k in ("4h", "1h", "15m")):
            payload: dict[str, Any] = {
                "symbol": str(raw.get("symbol") or default_symbol_upper),
                "market": {
                    "4h": list(market_frames["4h"]),
                    "1h": list(market_frames["1h"]),
                    "15m": list(market_frames["15m"]),
                },
            }
            for key in ("timestamp", "time", "datetime", "open_time", "close_time", "event_time"):
                if key in raw:
                    payload[key] = raw[key]
            normalized = _normalize_snapshot(payload, default_symbol=default_symbol_upper)
            if normalized is not None:
                frames.append(normalized)
    return frames


def _load_replay_frames(path: str | None, default_symbol: str) -> list[_ReplayFrame]:
    if path is None:
        return []
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"replay path not found: {source}")
    suffix = source.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        return _load_replay_rows_json(path=source, default_symbol=default_symbol, is_jsonl=True)
    if suffix == ".json":
        return _load_replay_rows_json(path=source, default_symbol=default_symbol)
    if suffix == ".csv":
        return _load_replay_rows_csv(path=source, default_symbol=default_symbol)

    try:
        rows = _load_replay_rows_json(path=source, default_symbol=default_symbol)
        if rows:
            return rows
    except json.JSONDecodeError:
        pass
    return _load_replay_rows_csv(path=source, default_symbol=default_symbol)


class _ReplaySnapshotProvider:
    def __init__(self, frames: list[_ReplayFrame]) -> None:
        self._frames = frames
        self._index = -1

    def __call__(self) -> dict[str, Any]:
        self._index += 1
        if self._index >= len(self._frames):
            return {}
        frame = self._frames[self._index]
        payload: dict[str, Any] = {
            "symbol": frame.symbol,
            "market": frame.market,
            "meta": frame.meta,
            "tick": self._index,
        }
        return payload


@dataclass
class _ReplayDecision:
    payload: dict[str, Any] = field(default_factory=dict)

    def __call__(self, payload: dict[str, Any]) -> None:
        if isinstance(payload, dict):
            self.payload = payload

    def take(self) -> dict[str, Any]:
        out = dict(self.payload)
        self.payload = {}
        return out


@dataclass
class _ReplayDecisionBySymbol:
    payload: dict[str, dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key in (
            "symbol",
            "side",
            "reason",
            "regime",
            "alpha_id",
            "entry_family",
            "entry_price",
            "risk_per_trade_pct",
            "max_effective_leverage",
        ):
            if key in payload:
                compact[key] = payload.get(key)

        sl_tp = payload.get("sl_tp")
        if isinstance(sl_tp, dict):
            compact["sl_tp"] = {
                key: sl_tp.get(key)
                for key in ("take_profit", "take_profit_final", "stop_loss")
                if key in sl_tp
            }

        execution = payload.get("execution")
        if isinstance(execution, dict):
            compact["execution"] = {
                key: execution.get(key)
                for key in (
                    "time_stop_bars",
                    "stop_exit_cooldown_bars",
                    "profit_exit_cooldown_bars",
                    "reward_risk_reference_r",
                )
                if key in execution
            }

        alpha_blocks = payload.get("alpha_blocks")
        if isinstance(alpha_blocks, dict):
            compact["alpha_blocks"] = {
                str(key): str(value)
                for key, value in alpha_blocks.items()
                if str(key).strip() and str(value).strip()
            }
        return compact

    def __call__(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            return
        self.payload[symbol] = self._compact_payload(payload)

    def take(self) -> dict[str, dict[str, Any]]:
        out = {symbol: dict(value) for symbol, value in self.payload.items()}
        self.payload = {}
        return out


class _HistoricalPortfolioSnapshotProvider:
    def __init__(
        self,
        *,
        candles_by_symbol: dict[str, dict[str, list[_Kline15m]]],
        premium_by_symbol: dict[str, list[_Kline15m]] | None = None,
        funding_by_symbol: dict[str, list[_FundingRateRow]] | None = None,
        market_intervals: list[str] | None = None,
        history_limit: int = 260,
    ) -> None:
        self._symbols = sorted(
            str(symbol).strip().upper() for symbol in candles_by_symbol.keys() if str(symbol).strip()
        )
        configured_intervals = list(market_intervals or ["15m", "1h", "4h"])
        if "15m" not in configured_intervals:
            configured_intervals.insert(0, "15m")
        seen_intervals: set[str] = set()
        self._intervals: list[str] = []
        for interval in configured_intervals:
            normalized = str(interval).strip()
            if not normalized or normalized in seen_intervals:
                continue
            seen_intervals.add(normalized)
            self._intervals.append(normalized)

        self._limit = max(int(history_limit), 1)
        self._histories: dict[str, dict[str, list[_Bar]]] = {}
        self._sources: dict[str, dict[str, list[_Kline15m]]] = {}
        self._source_index: dict[str, dict[str, int]] = {}
        self._premium_sources: dict[str, list[_Kline15m]] = {}
        self._premium_index: dict[str, int] = {}
        self._premium_histories: dict[str, deque[_Kline15m]] = {}
        self._funding_sources: dict[str, list[_FundingRateRow]] = {}
        self._funding_index: dict[str, int] = {}
        self._funding_histories: dict[str, deque[_FundingRateRow]] = {}
        self._latest_rows: dict[str, _Kline15m | None] = {}
        timeline: set[int] = set()

        for symbol in self._symbols:
            raw = candles_by_symbol.get(symbol, {})
            self._histories[symbol] = {interval: [] for interval in self._intervals}
            self._sources[symbol] = {}
            self._source_index[symbol] = {}
            self._premium_sources[symbol] = sorted(
                list((premium_by_symbol or {}).get(symbol, [])),
                key=lambda row: int(row.open_time_ms),
            )
            self._premium_index[symbol] = -1
            self._premium_histories[symbol] = deque(maxlen=max(self._limit, 320))
            self._funding_sources[symbol] = sorted(
                list((funding_by_symbol or {}).get(symbol, [])),
                key=lambda row: int(row.funding_time_ms),
            )
            self._funding_index[symbol] = -1
            self._funding_histories[symbol] = deque(maxlen=32)
            self._latest_rows[symbol] = None
            for interval in self._intervals:
                rows = sorted(
                    list(raw.get(interval, [])),
                    key=lambda row: int(row.open_time_ms),
                )
                self._sources[symbol][interval] = rows
                self._source_index[symbol][interval] = -1
                if interval == "15m":
                    for row in rows:
                        timeline.add(int(row.open_time_ms))

        self._timeline = sorted(timeline)
        self._idx = -1

    def __len__(self) -> int:
        return len(self._timeline)

    @staticmethod
    def _row_to_ohlc(row: _Kline15m) -> _Bar:
        return _Bar(
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )

    def _append_history(self, *, symbol: str, interval: str, row: _Kline15m) -> None:
        history = self._histories[symbol][interval]
        history.append(self._row_to_ohlc(row))
        if len(history) > self._limit:
            del history[0]

    def _advance_symbol_15m(self, *, symbol: str, open_time_ms: int) -> None:
        rows = self._sources[symbol].get("15m", [])
        idx = int(self._source_index[symbol].get("15m", -1))
        while idx + 1 < len(rows):
            nxt = rows[idx + 1]
            nxt_open_time = int(nxt.open_time_ms)
            if nxt_open_time > int(open_time_ms):
                break
            idx += 1
            self._append_history(symbol=symbol, interval="15m", row=nxt)
            self._latest_rows[symbol] = nxt
            if nxt_open_time == int(open_time_ms):
                break
        self._source_index[symbol]["15m"] = idx

    def _advance_interval(
        self,
        *,
        symbol: str,
        interval: str,
        current_close_time_ms: int,
    ) -> None:
        rows = self._sources[symbol].get(interval, [])
        idx = int(self._source_index[symbol].get(interval, -1))
        while idx + 1 < len(rows):
            nxt = rows[idx + 1]
            if int(nxt.close_time_ms) > int(current_close_time_ms):
                break
            idx += 1
            self._append_history(symbol=symbol, interval=interval, row=nxt)
        self._source_index[symbol][interval] = idx

    def _advance_premium(self, *, symbol: str, current_close_time_ms: int) -> None:
        rows = self._premium_sources.get(symbol, [])
        idx = int(self._premium_index.get(symbol, -1))
        while idx + 1 < len(rows):
            nxt = rows[idx + 1]
            if int(nxt.close_time_ms) > int(current_close_time_ms):
                break
            idx += 1
            self._premium_histories[symbol].append(nxt)
        self._premium_index[symbol] = idx

    def _advance_funding(self, *, symbol: str, current_close_time_ms: int) -> None:
        rows = self._funding_sources.get(symbol, [])
        idx = int(self._funding_index.get(symbol, -1))
        while idx + 1 < len(rows):
            nxt = rows[idx + 1]
            if int(nxt.funding_time_ms) > int(current_close_time_ms):
                break
            idx += 1
            self._funding_histories[symbol].append(nxt)
        self._funding_index[symbol] = idx

    def current_candles(self) -> dict[str, dict[str, float]]:
        payload: dict[str, dict[str, float]] = {}
        for symbol, row in self._latest_rows.items():
            if row is None:
                continue
            payload[symbol] = {
                "open_time_ms": float(row.open_time_ms),
                "close_time_ms": float(row.close_time_ms),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
            }
        return payload

    def __call__(self) -> dict[str, Any]:
        self._idx += 1
        if self._idx >= len(self._timeline):
            return {}
        open_time_ms = int(self._timeline[self._idx])
        close_time_ms = open_time_ms + (15 * 60 * 1000)

        for symbol in self._symbols:
            self._advance_symbol_15m(symbol=symbol, open_time_ms=open_time_ms)
            latest = self._latest_rows.get(symbol)
            if latest is None:
                continue
            current_close_ms = int(latest.close_time_ms)
            for interval in self._intervals:
                if interval == "15m":
                    continue
                self._advance_interval(
                    symbol=symbol,
                    interval=interval,
                    current_close_time_ms=current_close_ms,
                )
            self._advance_premium(symbol=symbol, current_close_time_ms=current_close_ms)
            self._advance_funding(symbol=symbol, current_close_time_ms=current_close_ms)

        symbols_payload: dict[str, dict[str, Any]] = {}
        for symbol in self._symbols:
            latest = self._latest_rows.get(symbol)
            if latest is None:
                continue
            premium_rows = list(self._premium_histories.get(symbol, deque()))
            funding_rows = list(self._funding_histories.get(symbol, deque()))
            symbol_payload = {
                interval: self._histories[symbol][interval] for interval in self._intervals
            }
            symbol_payload["premium"] = {
                "close_15m": float(premium_rows[-1].close) if premium_rows else None,
                "zscore_24h": (
                    _zscore_latest([float(item.close) for item in premium_rows], 96)
                    if premium_rows
                    else None
                ),
                "zscore_3d": (
                    _zscore_latest([float(item.close) for item in premium_rows], 288)
                    if premium_rows
                    else None
                ),
            }
            symbol_payload["funding"] = {
                "last": float(funding_rows[-1].funding_rate) if funding_rows else None,
                "sum_24h": _sum_recent_funding(
                    funding_rows,
                    current_time_ms=latest.close_time_ms,
                    window_ms=24 * 60 * 60 * 1000,
                ),
                "sum_3d": _sum_recent_funding(
                    funding_rows,
                    current_time_ms=latest.close_time_ms,
                    window_ms=3 * 24 * 60 * 60 * 1000,
                ),
            }
            symbols_payload[symbol] = symbol_payload

        if not symbols_payload:
            return {}
        primary_symbol = next(iter(symbols_payload.keys()))
        return {
            "symbol": primary_symbol,
            "symbols": symbols_payload,
            "market": symbols_payload[primary_symbol],
            "timestamp": datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).isoformat(),
            "open_time": open_time_ms,
            "close_time": close_time_ms,
            "candles": self.current_candles(),
        }


def _build_replay_cycle_record(
    *,
    tick: int,
    cycle: Any,
    decision: dict[str, Any],
    symbol: str,
    bracket_planner: BracketPlanner,
) -> dict[str, Any]:
    candidate = cycle.candidate
    risk = cycle.risk
    size = cycle.size
    execution = cycle.execution

    if candidate is None:
        would_enter = False
    else:
        would_enter = cycle.state in {"dry_run", "executed"}

    decision_payload = decision if isinstance(decision, dict) else {}
    strategy_decision = decision_payload.get("decision")
    if not isinstance(strategy_decision, dict):
        strategy_decision = {}
    base_decision: dict[str, Any] = strategy_decision if strategy_decision else decision_payload

    entry_price = _to_float(strategy_decision.get("entry_price"))
    if (entry_price is None or entry_price <= 0) and candidate is not None:
        entry_price = _to_float(candidate.entry_price)
    sl_tp: dict[str, float] | None = None
    strategy_sl_tp = base_decision.get("sl_tp")
    if isinstance(strategy_sl_tp, dict):
        tp_price = _to_float(strategy_sl_tp.get("take_profit"))
        tp_final_price = _to_float(strategy_sl_tp.get("take_profit_final"))
        sl_price = _to_float(strategy_sl_tp.get("stop_loss"))
        if (
            entry_price is not None
            and entry_price > 0
            and tp_price is not None
            and tp_price > 0
            and sl_price is not None
            and sl_price > 0
        ):
            sl_tp = {
                "take_profit": float(tp_price),
                "stop_loss": float(sl_price),
            }
            if tp_final_price is not None and tp_final_price > 0:
                sl_tp["take_profit_final"] = float(tp_final_price)
    if entry_price and entry_price > 0 and candidate is not None:
        if sl_tp is None:
            try:
                levels = bracket_planner.levels(
                    entry_price=entry_price,
                    side="LONG" if candidate.side == "BUY" else "SHORT",
                )
                sl_tp = {
                    "take_profit": float(levels["take_profit"]),
                    "stop_loss": float(levels["stop_loss"]),
                }
            except (TypeError, ValueError):
                sl_tp = None

    cycle_record: dict[str, Any] = {
        "tick": tick,
        "symbol": symbol,
        "state": cycle.state,
        "reason": cycle.reason,
        "would_enter": would_enter,
    }
    if candidate is not None:
        cycle_record["candidate"] = {
            "symbol": candidate.symbol,
            "side": candidate.side,
            "score": candidate.score,
            "alpha_id": getattr(candidate, "alpha_id", None),
            "entry_price": candidate.entry_price,
            "reason": candidate.reason,
        }
    else:
        cycle_record["candidate"] = None

    if risk is not None:
        cycle_record["risk"] = {
            "allow": bool(risk.allow),
            "reason": risk.reason,
            "max_notional": risk.max_notional,
        }

    if size is not None:
        cycle_record["size"] = {
            "qty": size.qty,
            "notional": size.notional,
            "leverage": size.leverage,
            "reason": size.reason,
        }

    if execution is not None:
        cycle_record["execution"] = {
            "ok": bool(execution.ok),
            "order_id": execution.order_id,
            "reason": execution.reason,
        }

    if base_decision or candidate is not None:
        side_value = base_decision.get("side")
        if side_value is None and candidate is not None:
            side_value = candidate.side
        intent_value = base_decision.get("intent")
        if intent_value is None and isinstance(side_value, str):
            if side_value == "BUY":
                intent_value = "LONG"
            elif side_value == "SELL":
                intent_value = "SHORT"

        cycle_record["decision"] = {
            "intent": intent_value,
            "side": side_value,
            "score": base_decision.get("score"),
            "alpha_id": base_decision.get("alpha_id"),
            "regime": base_decision.get("regime"),
            "allowed_side": base_decision.get("allowed_side"),
            "reason": base_decision.get("reason"),
            "blocks": base_decision.get("blocks"),
            "filters": base_decision.get("filters"),
            "signals": base_decision.get("signals"),
            "alpha_blocks": base_decision.get("alpha_blocks"),
            "indicators": base_decision.get("indicators"),
            "entry_mode": base_decision.get("entry_mode"),
            "sideways": base_decision.get("sideways"),
            "stop_hint": base_decision.get("stop_hint"),
            "management_hint": base_decision.get("management_hint"),
            "risk_per_trade_pct": base_decision.get("risk_per_trade_pct"),
            "max_effective_leverage": base_decision.get("max_effective_leverage"),
            "reverse_exit_min_r": base_decision.get("reverse_exit_min_r"),
            "execution": base_decision.get("execution"),
            "entry_price": entry_price,
            "sl_tp": sl_tp,
        }

    return cycle_record


def _build_local_backtest_cycle_input(
    *,
    cycle: Any,
    decision: dict[str, Any],
    bracket_planner: BracketPlanner,
) -> tuple[dict[str, Any], str]:
    candidate = cycle.candidate
    size = cycle.size
    state = str(cycle.state)
    would_enter = candidate is not None and state in {"dry_run", "executed"}

    decision_payload = decision if isinstance(decision, dict) else {}
    strategy_decision = decision_payload.get("decision")
    if not isinstance(strategy_decision, dict):
        strategy_decision = {}
    base_decision: dict[str, Any] = strategy_decision if strategy_decision else decision_payload

    entry_price = _to_float(strategy_decision.get("entry_price"))
    if (entry_price is None or entry_price <= 0) and candidate is not None:
        entry_price = _to_float(candidate.entry_price)

    sl_tp: dict[str, float] | None = None
    strategy_sl_tp = base_decision.get("sl_tp")
    if isinstance(strategy_sl_tp, dict):
        tp_price = _to_float(strategy_sl_tp.get("take_profit"))
        tp_final_price = _to_float(strategy_sl_tp.get("take_profit_final"))
        sl_price = _to_float(strategy_sl_tp.get("stop_loss"))
        if (
            entry_price is not None
            and entry_price > 0
            and tp_price is not None
            and tp_price > 0
            and sl_price is not None
            and sl_price > 0
        ):
            sl_tp = {
                "take_profit": float(tp_price),
                "stop_loss": float(sl_price),
            }
            if tp_final_price is not None and tp_final_price > 0:
                sl_tp["take_profit_final"] = float(tp_final_price)
    if entry_price and entry_price > 0 and candidate is not None:
        if sl_tp is None:
            try:
                levels = bracket_planner.levels(
                    entry_price=float(entry_price),
                    side="LONG" if candidate.side == "BUY" else "SHORT",
                )
                sl_tp = {
                    "take_profit": float(levels["take_profit"]),
                    "stop_loss": float(levels["stop_loss"]),
                }
            except (TypeError, ValueError):
                sl_tp = None

    decision_side = base_decision.get("side")
    if decision_side is None and candidate is not None:
        decision_side = candidate.side

    row: dict[str, Any] = {
        "state": state,
        "reason": cycle.reason,
        "would_enter": bool(would_enter),
        "candidate": None,
        "size": None,
        "decision": {},
    }
    if candidate is not None:
        row["candidate"] = {
            "side": candidate.side,
            "score": candidate.score,
            "alpha_id": getattr(candidate, "alpha_id", None),
            "entry_price": candidate.entry_price,
        }
    if size is not None:
        row["size"] = {
            "qty": size.qty,
        }
    if base_decision or candidate is not None:
        row["decision"] = {
            "side": decision_side,
            "score": base_decision.get("score"),
            "alpha_id": base_decision.get("alpha_id"),
            "regime": base_decision.get("regime"),
            "entry_price": entry_price,
            "alpha_blocks": base_decision.get("alpha_blocks"),
            "risk_per_trade_pct": base_decision.get("risk_per_trade_pct"),
            "max_effective_leverage": base_decision.get("max_effective_leverage"),
            "reverse_exit_min_r": base_decision.get("reverse_exit_min_r"),
            "sl_tp": sl_tp,
            "execution": base_decision.get("execution"),
        }

    return row, state


def _write_replay_report(
    *,
    cfg: EffectiveConfig,
    replay_source: str,
    rows: list[dict[str, Any]],
    report_dir: str,
    report_path: str | None,
) -> str:
    report_root = Path(report_dir)
    report_root.mkdir(parents=True, exist_ok=True)

    state_counter = Counter(row.get("state") for row in rows)
    regime_counter = Counter()
    block_counter = Counter()
    would_enter = 0
    for row in rows:
        if row.get("would_enter"):
            would_enter += 1
        decision = row.get("decision")
        if isinstance(decision, dict):
            blocks_raw = decision.get("blocks")
            if isinstance(blocks_raw, list):
                for block in blocks_raw:
                    if isinstance(block, str):
                        block_counter[block] += 1
            regime = decision.get("regime")
            if isinstance(regime, str):
                regime_counter[regime] += 1

    report_payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "replay": {
            "source": replay_source,
            "profile": cfg.profile,
            "mode": cfg.mode,
            "symbol": cfg.behavior.exchange.default_symbol,
        },
        "summary": {
            "total_cycles": len(rows),
            "would_enter": would_enter,
            "state_distribution": dict(state_counter),
            "regime_distribution": dict(regime_counter),
            "block_distribution": dict(block_counter),
            "first_tick": rows[0].get("tick") if rows else None,
            "last_tick": rows[-1].get("tick") if rows else None,
        },
        "cycles": rows,
    }

    if report_path is not None:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        symbol = cfg.behavior.exchange.default_symbol.lower()
        target = report_root / f"replay_{symbol}_{stamp}.json"

    target.write_text(json.dumps(report_payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return str(target)


def _run_replay(
    cfg: EffectiveConfig,
    *,
    replay_path: str,
    report_dir: str,
    report_path: str | None = None,
) -> int:
    _storage, state_store, _ops, _adapter, _rest_client = _build_runtime(cfg)
    state_store.set(mode=cfg.mode, status="RUNNING")
    frames = _load_replay_frames(
        path=replay_path, default_symbol=cfg.behavior.exchange.default_symbol
    )
    if not frames:
        print(
            json.dumps(
                {"replay": {"source": replay_path, "cycles": 0}, "error": "no replay data"},
                ensure_ascii=True,
            )
        )
        return 1

    if cfg.behavior.ops.pause_on_start:
        _ops.pause()

    collector = _ReplayDecision()
    snapshot_provider = _ReplaySnapshotProvider(frames)
    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        tick=0,
        dry_run=True,
        rest_client=_rest_client,
        snapshot_provider=snapshot_provider,
        overheat_fetcher=None,
        journal_logger=collector,
    )

    bracket_planner = BracketPlanner(
        cfg=BracketConfig(
            take_profit_pct=cfg.behavior.tpsl.take_profit_pct,
            stop_loss_pct=cfg.behavior.tpsl.stop_loss_pct,
        )
    )

    rows: list[dict[str, Any]] = []
    for tick in range(len(frames)):
        cycle = kernel.run_once()
        decision = collector.take()
        symbol = cfg.behavior.exchange.default_symbol
        if tick < len(frames):
            symbol = frames[tick].symbol or symbol
        row = _build_replay_cycle_record(
            tick=tick,
            cycle=cycle,
            decision=decision,
            symbol=symbol,
            bracket_planner=bracket_planner,
        )

        if isinstance(frames[tick].meta, dict):
            row.update({k: frames[tick].meta[k] for k in frames[tick].meta if k not in row})
        snapshot_time = _extract_snapshot_time(frames[tick].meta)
        if snapshot_time is not None:
            row["snapshot_time"] = snapshot_time
        rows.append(row)

    report_file = _write_replay_report(
        cfg=cfg,
        replay_source=replay_path,
        rows=rows,
        report_dir=report_dir,
        report_path=report_path,
    )
    print(json.dumps({"replay": {"status": "completed", "report": report_file}}, ensure_ascii=True))
    return 0


def _parse_symbols(raw: str) -> list[str]:
    out: list[str] = []
    for token in str(raw).split(","):
        value = token.strip().upper()
        if not value:
            continue
        if value not in out:
            out.append(value)
    return out


def _parse_utc_datetime_ms(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    if "T" not in text and len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = f"{text}T00:00:00+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _format_utc_iso(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _calc_max_drawdown_pct(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        if value > peak:
            peak = value
            continue
        if peak <= 0:
            continue
        drawdown = (value - peak) / peak
        if drawdown < worst:
            worst = drawdown
    return abs(worst) * 100.0


def _interval_to_ms(interval: str) -> int:
    code = str(interval).strip().lower()
    mapping = {
        "1m": 1 * 60 * 1000,
        "3m": 3 * 60 * 1000,
        "5m": 5 * 60 * 1000,
        "10m": 10 * 60 * 1000,
        "15m": 15 * 60 * 1000,
        "30m": 30 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "2h": 2 * 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
        "6h": 6 * 60 * 60 * 1000,
        "8h": 8 * 60 * 60 * 1000,
        "12h": 12 * 60 * 60 * 1000,
        "1d": 24 * 60 * 60 * 1000,
    }
    if code in mapping:
        return int(mapping[code])
    if len(code) >= 2 and code[:-1].isdigit():
        qty = int(code[:-1])
        unit = code[-1]
        if qty > 0 and unit == "m":
            return int(qty * 60 * 1000)
        if qty > 0 and unit == "h":
            return int(qty * 60 * 60 * 1000)
        if qty > 0 and unit == "d":
            return int(qty * 24 * 60 * 60 * 1000)
    return 15 * 60 * 1000


def _aggregate_klines_to_interval(
    rows: list[_Kline15m], *, target_interval_ms: int
) -> list[_Kline15m]:
    if target_interval_ms <= 0 or not rows:
        return list(rows)
    grouped: list[_Kline15m] = []
    sorted_rows = sorted(rows, key=lambda row: int(row.open_time_ms))
    active_bucket: int | None = None
    active: _Kline15m | None = None

    for row in sorted_rows:
        bucket = int(row.open_time_ms // target_interval_ms)
        if active_bucket is None or active is None:
            active_bucket = bucket
            active = row
            continue
        if bucket != active_bucket:
            grouped.append(active)
            active_bucket = bucket
            active = row
            continue
        active = _Kline15m(
            open_time_ms=active.open_time_ms,
            close_time_ms=max(int(active.close_time_ms), int(row.close_time_ms)),
            open=float(active.open),
            high=max(float(active.high), float(row.high)),
            low=min(float(active.low), float(row.low)),
            close=float(row.close),
            volume=float(active.volume) + float(row.volume),
        )

    if active is not None:
        grouped.append(active)
    return grouped


async def _fetch_klines_interval(
    *,
    rest_client: BinanceRESTClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    on_progress: Callable[[int], None] | None = None,
    sleep_sec: float = 0.03,
) -> list[_Kline15m]:
    interval_code = str(interval).strip().lower()
    request_interval = "5m" if interval_code == "10m" else interval_code
    request_interval_ms = _interval_to_ms(request_interval)
    current = int(start_ms)
    rows: list[_Kline15m] = []
    seen_open_time: set[int] = set()
    span_ms = max(int(end_ms) - int(start_ms), 1)
    next_mark = 0

    if on_progress is not None:
        on_progress(0)
        next_mark = 5

    while current < end_ms:
        payload = await rest_client.public_request(
            "GET",
            "/fapi/v1/klines",
            params={
                "symbol": symbol,
                "interval": request_interval,
                "startTime": current,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not isinstance(payload, list) or len(payload) == 0:
            break

        fetched = 0
        for item in payload:
            if not isinstance(item, (list, tuple)) or len(item) < 7:
                continue
            try:
                open_time_ms = int(item[0])
                close_time_ms = int(item[6])
                o = float(item[1])
                h = float(item[2])
                low_price = float(item[3])
                c = float(item[4])
                volume = float(item[5])
            except (TypeError, ValueError):
                continue
            if open_time_ms < start_ms or open_time_ms >= end_ms:
                continue
            if open_time_ms in seen_open_time:
                continue
            seen_open_time.add(open_time_ms)
            rows.append(
                _Kline15m(
                    open_time_ms=open_time_ms,
                    close_time_ms=close_time_ms,
                    open=o,
                    high=h,
                    low=low_price,
                    close=c,
                    volume=volume,
                )
            )
            fetched += 1

        if fetched == 0:
            break
        last_open_ms = rows[-1].open_time_ms
        current = last_open_ms + request_interval_ms
        if on_progress is not None:
            progress_ms = min(int(current), int(end_ms))
            progress_pct = max(0, min(100, int(((progress_ms - int(start_ms)) * 100) / span_ms)))
            while next_mark <= progress_pct:
                on_progress(next_mark)
                next_mark += 5
        delay = max(float(sleep_sec), 0.0)
        if delay > 0.0:
            await asyncio.sleep(delay)

    if on_progress is not None:
        while next_mark <= 100:
            on_progress(next_mark)
            next_mark += 5

    rows.sort(key=lambda row: row.open_time_ms)
    if interval_code == "10m":
        return _aggregate_klines_to_interval(rows, target_interval_ms=_interval_to_ms("10m"))
    return rows


async def _fetch_klines_15m(
    *,
    rest_client: BinanceRESTClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
    on_progress: Callable[[int], None] | None = None,
    sleep_sec: float = 0.03,
) -> list[_Kline15m]:
    return await _fetch_klines_interval(
        rest_client=rest_client,
        symbol=symbol,
        interval="15m",
        start_ms=start_ms,
        end_ms=end_ms,
        on_progress=on_progress,
        sleep_sec=sleep_sec,
    )


async def _fetch_premium_index_klines_15m(
    *,
    rest_client: BinanceRESTClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
    on_progress: Callable[[int], None] | None = None,
    sleep_sec: float = 0.03,
) -> list[_Kline15m]:
    request_interval_ms = _interval_to_ms("15m")
    current = int(start_ms)
    rows: list[_Kline15m] = []
    seen_open_time: set[int] = set()
    span_ms = max(int(end_ms) - int(start_ms), 1)
    next_mark = 0

    if on_progress is not None:
        on_progress(0)
        next_mark = 5

    while current < end_ms:
        payload = await rest_client.public_request(
            "GET",
            "/fapi/v1/premiumIndexKlines",
            params={
                "symbol": symbol,
                "interval": "15m",
                "startTime": current,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not isinstance(payload, list) or len(payload) == 0:
            break

        fetched = 0
        for item in payload:
            if not isinstance(item, (list, tuple)) or len(item) < 7:
                continue
            try:
                open_time_ms = int(item[0])
                close_time_ms = int(item[6])
                o = float(item[1])
                h = float(item[2])
                low_price = float(item[3])
                c = float(item[4])
            except (TypeError, ValueError):
                continue
            if open_time_ms < start_ms or open_time_ms >= end_ms:
                continue
            if open_time_ms in seen_open_time:
                continue
            seen_open_time.add(open_time_ms)
            rows.append(
                _Kline15m(
                    open_time_ms=open_time_ms,
                    close_time_ms=close_time_ms,
                    open=o,
                    high=h,
                    low=low_price,
                    close=c,
                    volume=0.0,
                )
            )
            fetched += 1

        if fetched == 0:
            break
        last_open_ms = rows[-1].open_time_ms
        current = last_open_ms + request_interval_ms
        if on_progress is not None:
            progress_ms = min(int(current), int(end_ms))
            progress_pct = max(0, min(100, int(((progress_ms - int(start_ms)) * 100) / span_ms)))
            while next_mark <= progress_pct:
                on_progress(next_mark)
                next_mark += 5
        delay = max(float(sleep_sec), 0.0)
        if delay > 0.0:
            await asyncio.sleep(delay)

    if on_progress is not None:
        while next_mark <= 100:
            on_progress(next_mark)
            next_mark += 5

    rows.sort(key=lambda row: row.open_time_ms)
    return rows


async def _fetch_funding_rate_history(
    *,
    rest_client: BinanceRESTClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
    sleep_sec: float = 0.03,
) -> list[_FundingRateRow]:
    current = int(start_ms)
    rows: list[_FundingRateRow] = []
    seen_funding_time: set[int] = set()

    while current < end_ms:
        payload = await rest_client.public_request(
            "GET",
            "/fapi/v1/fundingRate",
            params={
                "symbol": symbol,
                "startTime": current,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not isinstance(payload, list) or len(payload) == 0:
            break

        fetched = 0
        last_funding_time_ms: int | None = None
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                funding_time_ms = int(item.get("fundingTime") or 0)
                funding_rate = float(item.get("fundingRate") or 0.0)
            except (TypeError, ValueError):
                continue
            if funding_time_ms < start_ms or funding_time_ms >= end_ms:
                continue
            last_funding_time_ms = funding_time_ms
            if funding_time_ms in seen_funding_time:
                continue
            seen_funding_time.add(funding_time_ms)
            rows.append(
                _FundingRateRow(
                    funding_time_ms=funding_time_ms,
                    funding_rate=funding_rate,
                )
            )
            fetched += 1

        if fetched == 0:
            break
        if last_funding_time_ms is None:
            break
        current = int(last_funding_time_ms) + 1
        delay = max(float(sleep_sec), 0.0)
        if delay > 0.0:
            await asyncio.sleep(delay)

    rows.sort(key=lambda row: row.funding_time_ms)
    return rows


def _write_klines_csv(*, path: Path, symbol: str, rows: list[_Kline15m]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            ["symbol", "open_time_ms", "open", "high", "low", "close", "volume", "close_time_ms"]
        )
        for row in rows:
            writer.writerow(
                [
                    symbol,
                    row.open_time_ms,
                    f"{row.open:.8f}",
                    f"{row.high:.8f}",
                    f"{row.low:.8f}",
                    f"{row.close:.8f}",
                    f"{row.volume:.8f}",
                    row.close_time_ms,
                ]
            )


def _cache_file_for_klines(*, cache_root: Path, symbol: str, interval: str, years: int) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    code = str(interval).strip().lower()
    return cache_root / f"klines_{str(symbol).strip().lower()}_{code}_{int(years)}y.csv"


def _cache_file_for_premium(*, cache_root: Path, symbol: str, interval: str, years: int) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    code = str(interval).strip().lower()
    return cache_root / f"premium_{str(symbol).strip().lower()}_{code}_{int(years)}y.csv"


def _cache_file_for_funding(*, cache_root: Path, symbol: str, years: int) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root / f"funding_{str(symbol).strip().lower()}_{int(years)}y.csv"


def _klines_csv_has_volume_column(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        with path.open("r", encoding="utf-8", newline="") as fp:
            header = fp.readline().strip().split(",")
    except OSError:
        return False
    return "volume" in {str(item).strip() for item in header}


def _read_klines_csv_rows(path: Path) -> list[_Kline15m]:
    if not path.exists() or not path.is_file():
        return []
    rows: list[_Kline15m] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as fp:
            reader = csv.DictReader(fp)
            for item in reader:
                if not isinstance(item, dict):
                    continue
                try:
                    rows.append(
                        _Kline15m(
                            open_time_ms=int(item.get("open_time_ms") or 0),
                            close_time_ms=int(item.get("close_time_ms") or 0),
                            open=float(item.get("open") or 0.0),
                            high=float(item.get("high") or 0.0),
                            low=float(item.get("low") or 0.0),
                            close=float(item.get("close") or 0.0),
                            volume=float(item.get("volume") or 0.0),
                        )
                    )
                except (TypeError, ValueError):
                    continue
    except OSError:
        return []
    rows.sort(key=lambda row: row.open_time_ms)
    return rows


def _load_cached_klines_for_range(
    *,
    path: Path,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[_Kline15m]:
    rows = _read_klines_csv_rows(path)
    if not rows:
        return []

    interval_ms = _interval_to_ms(interval)
    filtered = [
        row
        for row in rows
        if int(row.open_time_ms) >= int(start_ms) and int(row.open_time_ms) < int(end_ms)
    ]
    if not filtered:
        return []

    expected = max(int((int(end_ms) - int(start_ms)) // max(interval_ms, 1)), 1)
    coverage_ok = int(filtered[0].open_time_ms) <= int(start_ms) + interval_ms and int(
        filtered[-1].open_time_ms
    ) >= int(end_ms) - (interval_ms * 2)
    density_ok = len(filtered) >= int(expected * 0.90)
    if coverage_ok and density_ok:
        return filtered
    return []


def _load_cached_premium_for_range(
    *,
    path: Path,
    start_ms: int,
    end_ms: int,
) -> list[_Kline15m]:
    return _load_cached_klines_for_range(
        path=path,
        interval="15m",
        start_ms=start_ms,
        end_ms=end_ms,
    )


def _write_funding_csv(*, path: Path, symbol: str, rows: list[_FundingRateRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["symbol", "funding_time_ms", "funding_rate"])
        for row in rows:
            writer.writerow(
                [
                    symbol,
                    int(row.funding_time_ms),
                    f"{row.funding_rate:.10f}",
                ]
            )


def _read_funding_csv_rows(path: Path) -> list[_FundingRateRow]:
    if not path.exists() or not path.is_file():
        return []
    rows: list[_FundingRateRow] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as fp:
            reader = csv.DictReader(fp)
            for item in reader:
                if not isinstance(item, dict):
                    continue
                try:
                    rows.append(
                        _FundingRateRow(
                            funding_time_ms=int(item.get("funding_time_ms") or 0),
                            funding_rate=float(item.get("funding_rate") or 0.0),
                        )
                    )
                except (TypeError, ValueError):
                    continue
    except OSError:
        return []
    rows.sort(key=lambda row: row.funding_time_ms)
    return rows


def _load_cached_funding_for_range(
    *,
    path: Path,
    start_ms: int,
    end_ms: int,
) -> list[_FundingRateRow]:
    rows = _read_funding_csv_rows(path)
    if not rows:
        return []
    filtered = [
        row
        for row in rows
        if int(row.funding_time_ms) >= int(start_ms) and int(row.funding_time_ms) < int(end_ms)
    ]
    if not filtered:
        return []
    coverage_ok = int(filtered[0].funding_time_ms) <= int(start_ms) + (8 * 60 * 60 * 1000) and int(
        filtered[-1].funding_time_ms
    ) >= int(end_ms) - (2 * 8 * 60 * 60 * 1000)
    if coverage_ok:
        return filtered
    return []


def _cleanup_local_backtest_artifacts(report_root: Path) -> int:
    removed = 0
    if not report_root.exists():
        return removed
    for pattern in ("backtest_*.csv", "backtest_*.sqlite3"):
        for path in report_root.glob(pattern):
            if not path.is_file():
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def _sort_trade_events_for_stats(trade_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        trade_events,
        key=lambda item: (
            int(item.get("exit_time_ms") or 0),
            int(item.get("entry_time_ms") or 0),
            int(item.get("exit_tick") or 0),
            int(item.get("entry_tick") or 0),
        ),
    )


def _calc_trade_event_drawdown_pct(
    *, trade_events: list[dict[str, Any]], initial_equity: float
) -> float:
    equity = float(initial_equity)
    equity_curve = [equity]
    for item in _sort_trade_events_for_stats(trade_events):
        equity += float(item.get("pnl") or 0.0)
        equity_curve.append(equity)
    return _calc_max_drawdown_pct(equity_curve)


def _top_reason_counts(source: Counter[str], *, limit: int = 5) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for reason, count in sorted(source.items(), key=lambda item: (-item[1], item[0]))[: max(int(limit), 1)]:
        items.append({"reason": str(reason), "count": int(count)})
    return items


def _summarize_alpha_stats(
    *,
    trade_events: list[dict[str, Any]],
    alpha_block_distribution: dict[str, Counter[str]],
    initial_capital: float,
) -> dict[str, dict[str, Any]]:
    alpha_ids: set[str] = set(alpha_block_distribution.keys())
    alpha_trade_events: dict[str, list[dict[str, Any]]] = {}
    for item in trade_events:
        alpha_id = str(item.get("alpha_id") or "").strip()
        if not alpha_id:
            continue
        alpha_ids.add(alpha_id)
        alpha_trade_events.setdefault(alpha_id, []).append(dict(item))

    payload: dict[str, dict[str, Any]] = {}
    for alpha_id in sorted(alpha_ids):
        events = _sort_trade_events_for_stats(alpha_trade_events.get(alpha_id, []))
        gross_profit = sum(max(0.0, float(item.get("pnl") or 0.0)) for item in events)
        gross_loss = abs(sum(min(0.0, float(item.get("pnl") or 0.0)) for item in events))
        net_profit = sum(float(item.get("pnl") or 0.0) for item in events)
        trades = len(events)
        wins = sum(1 for item in events if float(item.get("pnl") or 0.0) > 0.0)
        losses = sum(1 for item in events if float(item.get("pnl") or 0.0) < 0.0)
        profit_factor = None if gross_loss <= 0.0 else round(gross_profit / gross_loss, 6)
        payload[alpha_id] = {
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "net_profit": round(net_profit, 6),
            "gross_profit": round(gross_profit, 6),
            "gross_loss": round(gross_loss, 6),
            "profit_factor": profit_factor,
            "max_drawdown_pct": round(
                _calc_trade_event_drawdown_pct(
                    trade_events=events,
                    initial_equity=float(initial_capital),
                ),
                6,
            ),
            "block_top": _top_reason_counts(alpha_block_distribution.get(alpha_id, Counter())),
        }
    return payload


def _add_months_utc(dt: datetime, months: int) -> datetime:
    total_month = (int(dt.month) - 1) + int(months)
    year = int(dt.year) + (total_month // 12)
    month = (total_month % 12) + 1
    day = min(int(dt.day), calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _build_half_year_window_summaries(
    *,
    symbol_reports: list[dict[str, Any]],
    start_ms: int,
    end_ms: int,
    total_initial_capital: float,
) -> list[dict[str, Any]]:
    if int(start_ms) >= int(end_ms):
        return []

    start_dt = datetime.fromtimestamp(int(start_ms) / 1000.0, tz=timezone.utc).replace(
        microsecond=0
    )
    end_dt = datetime.fromtimestamp(int(end_ms) / 1000.0, tz=timezone.utc).replace(microsecond=0)
    windows: list[tuple[int, int, str]] = []
    cursor = start_dt
    while cursor < end_dt:
        next_dt = _add_months_utc(cursor, 6)
        if next_dt <= cursor:
            break
        if next_dt > end_dt:
            next_dt = end_dt
        window_start_ms = int(cursor.timestamp() * 1000)
        window_end_ms = int(next_dt.timestamp() * 1000)
        windows.append(
            (
                window_start_ms,
                window_end_ms,
                f"{cursor.date().isoformat()} ~ {(next_dt - timedelta(milliseconds=1)).date().isoformat()}",
            )
        )
        cursor = next_dt

    payload: list[dict[str, Any]] = []
    for window_start_ms, window_end_ms, label in windows:
        events: list[dict[str, Any]] = []
        for item in symbol_reports:
            trade_events = item.get("trade_events")
            if not isinstance(trade_events, list):
                continue
            for trade in trade_events:
                if not isinstance(trade, dict):
                    continue
                try:
                    exit_time_ms = int(trade.get("exit_time_ms") or 0)
                except (TypeError, ValueError):
                    continue
                if int(window_start_ms) <= exit_time_ms < int(window_end_ms):
                    events.append(dict(trade))

        gross_profit = sum(max(0.0, float(item.get("pnl") or 0.0)) for item in events)
        gross_loss = abs(sum(min(0.0, float(item.get("pnl") or 0.0)) for item in events))
        net_profit = sum(float(item.get("pnl") or 0.0) for item in events)
        total_fees = sum(
            max(
                float(item.get("entry_fee") or 0.0) + float(item.get("exit_fee") or 0.0),
                0.0,
            )
            for item in events
        )
        gross_trade_pnl = sum(float(item.get("gross_pnl") or 0.0) for item in events)
        profit_factor = None if gross_loss <= 0.0 else round(gross_profit / gross_loss, 6)
        payload.append(
            {
                "label": label,
                "start_ms": int(window_start_ms),
                "end_ms": int(window_end_ms),
                "start_utc": _format_utc_iso(window_start_ms),
                "end_utc": _format_utc_iso(window_end_ms),
                "trades": len(events),
                "net_profit": round(net_profit, 6),
                "profit_factor": profit_factor,
                "fee_to_trade_gross_pct": (
                    round((total_fees / gross_trade_pnl) * 100.0, 6)
                    if gross_trade_pnl > 0.0
                    else None
                ),
                "max_drawdown_pct": round(
                    _calc_trade_event_drawdown_pct(
                        trade_events=events,
                        initial_equity=float(total_initial_capital),
                    ),
                    6,
                ),
            }
        )
    return payload


def _portfolio_research_gate(
    *,
    years: int,
    total_net_profit: float,
    profit_factor: float | None,
    max_drawdown_pct: float,
    fee_to_trade_gross_pct: float | None,
) -> dict[str, Any]:
    if int(years) >= 3:
        net_threshold = 18.0
    elif int(years) <= 1:
        net_threshold = 12.0
    else:
        net_threshold = 15.0
    pf_threshold = 1.8
    max_drawdown_threshold = 18.0
    fee_to_trade_gross_threshold = 60.0
    passed = (
        float(total_net_profit) >= net_threshold
        and (profit_factor is not None and float(profit_factor) >= pf_threshold)
        and float(max_drawdown_pct) <= max_drawdown_threshold
        and (
            fee_to_trade_gross_pct is not None
            and float(fee_to_trade_gross_pct) <= fee_to_trade_gross_threshold
        )
    )
    return {
        "track": "profit-max-research",
        "verdict": "GO" if passed else "NO-GO",
        "thresholds": {
            "net_profit_usdt": net_threshold,
            "profit_factor": pf_threshold,
            "max_drawdown_pct": max_drawdown_threshold,
            "fee_to_trade_gross_pct": fee_to_trade_gross_threshold,
        },
    }


def _sfd_research_gate(
    *,
    start_ms: int,
    end_ms: int,
    total_net_profit: float,
    profit_factor: float | None,
    max_drawdown_pct: float,
    total_trades: int,
    fee_to_trade_gross_pct: float | None,
    window_slices_6m: list[dict[str, Any]],
) -> dict[str, Any]:
    def _gate_check(
        *,
        name: str,
        metrics: dict[str, Any],
        net_threshold: float,
        pf_threshold: float,
        max_dd_threshold: float,
        trades_threshold: int,
        fee_threshold: float | None = None,
    ) -> dict[str, Any]:
        net_profit = float(metrics.get("net_profit", 0.0))
        pf_value = _to_float(metrics.get("profit_factor"))
        max_dd_value = float(metrics.get("max_drawdown_pct", 0.0))
        raw_trades = _to_float(metrics.get("trades"))
        trades = int(raw_trades) if raw_trades is not None else 0
        fee_value = _to_float(metrics.get("fee_to_trade_gross_pct"))
        reasons: list[str] = []
        if float(net_profit) <= float(net_threshold) if name == "slice_6m" else float(net_profit) < float(net_threshold):
            reasons.append(f"net<{net_threshold:.2f}")
        if pf_value is None or float(pf_value) < float(pf_threshold):
            reasons.append(f"pf<{pf_threshold:.2f}")
        if float(max_dd_value) > float(max_dd_threshold):
            reasons.append(f"dd>{max_dd_threshold:.2f}")
        if int(trades) < int(trades_threshold):
            reasons.append(f"trades<{trades_threshold}")
        if fee_threshold is not None:
            if fee_value is None or float(fee_value) > float(fee_threshold):
                reasons.append(f"fee>{fee_threshold:.2f}")
        return {
            "name": name,
            "verdict": "KEEP" if not reasons else "KILL",
            "thresholds": {
                "net_profit_usdt": float(net_threshold),
                "profit_factor": float(pf_threshold),
                "max_drawdown_pct": float(max_dd_threshold),
                "trades": int(trades_threshold),
                "fee_to_trade_gross_pct": None if fee_threshold is None else float(fee_threshold),
            },
            "metrics": {
                "net_profit": round(float(net_profit), 6),
                "profit_factor": pf_value,
                "max_drawdown_pct": round(float(max_dd_value), 6),
                "trades": int(trades),
                "fee_to_trade_gross_pct": fee_value,
            },
            "reasons": reasons,
        }

    checks: list[dict[str, Any]] = []
    slice_rows = [item for item in window_slices_6m if isinstance(item, dict)]
    if slice_rows:
        first_slice = dict(slice_rows[0])
        checks.append(
            _gate_check(
                name="slice_6m",
                metrics={
                    "net_profit": first_slice.get("net_profit", 0.0),
                    "profit_factor": first_slice.get("profit_factor"),
                    "max_drawdown_pct": first_slice.get("max_drawdown_pct", 0.0),
                    "trades": first_slice.get("trades", 0),
                    "fee_to_trade_gross_pct": first_slice.get("fee_to_trade_gross_pct"),
                },
                net_threshold=0.0,
                pf_threshold=1.15,
                max_dd_threshold=12.0,
                trades_threshold=40,
                fee_threshold=72.0,
            )
        )

    duration_days = max(int(end_ms) - int(start_ms), 0) / float(86_400_000)
    if duration_days >= 320.0:
        checks.append(
            _gate_check(
                name="full_1y",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=10.0,
                pf_threshold=1.30,
                max_dd_threshold=15.0,
                trades_threshold=90,
                fee_threshold=65.0,
            )
        )
    else:
        checks.append(
            _gate_check(
                name="full_window",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=0.0,
                pf_threshold=1.15,
                max_dd_threshold=12.0,
                trades_threshold=40,
                fee_threshold=72.0,
            )
        )

    if duration_days >= 1000.0:
        checks.append(
            _gate_check(
                name="full_3y",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=18.0,
                pf_threshold=1.35,
                max_dd_threshold=18.0,
                trades_threshold=140,
                fee_threshold=60.0,
            )
        )

    verdict = "KEEP" if checks and all(item.get("verdict") == "KEEP" for item in checks) else "KILL"
    return {
        "track": "sfd-session-core",
        "verdict": verdict,
        "stop_rule": "baseline_6m_fail_stops_branch_before_1y",
        "checks": checks,
    }


def _pfd_research_gate(
    *,
    start_ms: int,
    end_ms: int,
    total_net_profit: float,
    profit_factor: float | None,
    max_drawdown_pct: float,
    total_trades: int,
    fee_to_trade_gross_pct: float | None,
    window_slices_6m: list[dict[str, Any]],
) -> dict[str, Any]:
    def _gate_check(
        *,
        name: str,
        metrics: dict[str, Any],
        net_threshold: float,
        pf_threshold: float,
        max_dd_threshold: float,
        trades_threshold: int,
        fee_threshold: float | None = None,
    ) -> dict[str, Any]:
        net_profit = float(metrics.get("net_profit", 0.0))
        pf_value = _to_float(metrics.get("profit_factor"))
        max_dd_value = float(metrics.get("max_drawdown_pct", 0.0))
        raw_trades = _to_float(metrics.get("trades"))
        trades = int(raw_trades) if raw_trades is not None else 0
        fee_value = _to_float(metrics.get("fee_to_trade_gross_pct"))
        reasons: list[str] = []
        if float(net_profit) <= float(net_threshold) if name == "slice_6m" else float(net_profit) < float(net_threshold):
            reasons.append(f"net<{net_threshold:.2f}")
        if pf_value is None or float(pf_value) < float(pf_threshold):
            reasons.append(f"pf<{pf_threshold:.2f}")
        if float(max_dd_value) > float(max_dd_threshold):
            reasons.append(f"dd>{max_dd_threshold:.2f}")
        if int(trades) < int(trades_threshold):
            reasons.append(f"trades<{trades_threshold}")
        if fee_threshold is not None:
            if fee_value is None or float(fee_value) > float(fee_threshold):
                reasons.append(f"fee>{fee_threshold:.2f}")
        return {
            "name": name,
            "verdict": "KEEP" if not reasons else "KILL",
            "thresholds": {
                "net_profit_usdt": float(net_threshold),
                "profit_factor": float(pf_threshold),
                "max_drawdown_pct": float(max_dd_threshold),
                "trades": int(trades_threshold),
                "fee_to_trade_gross_pct": None if fee_threshold is None else float(fee_threshold),
            },
            "metrics": {
                "net_profit": round(float(net_profit), 6),
                "profit_factor": pf_value,
                "max_drawdown_pct": round(float(max_dd_value), 6),
                "trades": int(trades),
                "fee_to_trade_gross_pct": fee_value,
            },
            "reasons": reasons,
        }

    checks: list[dict[str, Any]] = []
    slice_rows = [item for item in window_slices_6m if isinstance(item, dict)]
    if slice_rows:
        first_slice = dict(slice_rows[0])
        checks.append(
            _gate_check(
                name="slice_6m",
                metrics={
                    "net_profit": first_slice.get("net_profit", 0.0),
                    "profit_factor": first_slice.get("profit_factor"),
                    "max_drawdown_pct": first_slice.get("max_drawdown_pct", 0.0),
                    "trades": first_slice.get("trades", 0),
                    "fee_to_trade_gross_pct": first_slice.get("fee_to_trade_gross_pct"),
                },
                net_threshold=0.0,
                pf_threshold=1.20,
                max_dd_threshold=12.0,
                trades_threshold=30,
                fee_threshold=60.0,
            )
        )

    duration_days = max(int(end_ms) - int(start_ms), 0) / float(86_400_000)
    if duration_days >= 900.0:
        checks.append(
            _gate_check(
                name="full_3y",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=20.0,
                pf_threshold=1.35,
                max_dd_threshold=18.0,
                trades_threshold=70,
                fee_threshold=55.0,
            )
        )
    elif duration_days >= 320.0:
        checks.append(
            _gate_check(
                name="full_1y",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=12.0,
                pf_threshold=1.35,
                max_dd_threshold=15.0,
                trades_threshold=70,
                fee_threshold=55.0,
            )
        )
    else:
        checks.append(
            _gate_check(
                name="full_window",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=0.0,
                pf_threshold=1.20,
                max_dd_threshold=12.0,
                trades_threshold=30,
                fee_threshold=60.0,
            )
        )

    verdict = "KEEP" if checks and all(item.get("verdict") == "KEEP" for item in checks) else "KILL"
    return {
        "track": "pfd-crowding-core",
        "verdict": verdict,
        "stop_rule": "baseline_6m_fail_stops_branch_before_1y",
        "checks": checks,
    }


def _mr_research_gate(
    *,
    start_ms: int,
    end_ms: int,
    total_net_profit: float,
    profit_factor: float | None,
    max_drawdown_pct: float,
    total_trades: int,
    fee_to_trade_gross_pct: float | None,
    window_slices_6m: list[dict[str, Any]],
) -> dict[str, Any]:
    def _gate_check(
        *,
        name: str,
        metrics: dict[str, Any],
        net_threshold: float,
        pf_threshold: float,
        max_dd_threshold: float,
        trades_threshold: int,
        fee_threshold: float | None = None,
    ) -> dict[str, Any]:
        net_profit = float(metrics.get("net_profit", 0.0))
        pf_value = _to_float(metrics.get("profit_factor"))
        max_dd_value = float(metrics.get("max_drawdown_pct", 0.0))
        raw_trades = _to_float(metrics.get("trades"))
        trades = int(raw_trades) if raw_trades is not None else 0
        fee_value = _to_float(metrics.get("fee_to_trade_gross_pct"))
        reasons: list[str] = []
        if float(net_profit) <= float(net_threshold) if name == "slice_6m" else float(net_profit) < float(net_threshold):
            reasons.append(f"net<{net_threshold:.2f}")
        if pf_value is None or float(pf_value) < float(pf_threshold):
            reasons.append(f"pf<{pf_threshold:.2f}")
        if float(max_dd_value) > float(max_dd_threshold):
            reasons.append(f"dd>{max_dd_threshold:.2f}")
        if int(trades) < int(trades_threshold):
            reasons.append(f"trades<{trades_threshold}")
        if fee_threshold is not None:
            if fee_value is None or float(fee_value) > float(fee_threshold):
                reasons.append(f"fee>{fee_threshold:.2f}")
        return {
            "name": name,
            "verdict": "KEEP" if not reasons else "KILL",
            "thresholds": {
                "net_profit_usdt": float(net_threshold),
                "profit_factor": float(pf_threshold),
                "max_drawdown_pct": float(max_dd_threshold),
                "trades": int(trades_threshold),
                "fee_to_trade_gross_pct": None if fee_threshold is None else float(fee_threshold),
            },
            "metrics": {
                "net_profit": round(float(net_profit), 6),
                "profit_factor": pf_value,
                "max_drawdown_pct": round(float(max_dd_value), 6),
                "trades": int(trades),
                "fee_to_trade_gross_pct": fee_value,
            },
            "reasons": reasons,
        }

    checks: list[dict[str, Any]] = []
    slice_rows = [item for item in window_slices_6m if isinstance(item, dict)]
    if slice_rows:
        first_slice = dict(slice_rows[0])
        checks.append(
            _gate_check(
                name="slice_6m",
                metrics={
                    "net_profit": first_slice.get("net_profit", 0.0),
                    "profit_factor": first_slice.get("profit_factor"),
                    "max_drawdown_pct": first_slice.get("max_drawdown_pct", 0.0),
                    "trades": first_slice.get("trades", 0),
                },
                net_threshold=0.0,
                pf_threshold=1.15,
                max_dd_threshold=12.0,
                trades_threshold=30,
            )
        )

    duration_days = max(int(end_ms) - int(start_ms), 0) / float(86_400_000)
    if duration_days >= 320.0:
        checks.append(
            _gate_check(
                name="full_1y",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=8.0,
                pf_threshold=1.30,
                max_dd_threshold=15.0,
                trades_threshold=30,
                fee_threshold=65.0,
            )
        )
    else:
        checks.append(
            _gate_check(
                name="full_window",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                },
                net_threshold=0.0,
                pf_threshold=1.15,
                max_dd_threshold=12.0,
                trades_threshold=30,
            )
        )

    verdict = "KEEP" if checks and all(item.get("verdict") == "KEEP" for item in checks) else "KILL"
    return {
        "track": "mr-fast-kill",
        "verdict": verdict,
        "stop_rule": "baseline_6m_fail_stops_branch_before_1y",
        "checks": checks,
    }


def _fb_research_gate(
    *,
    start_ms: int,
    end_ms: int,
    total_net_profit: float,
    profit_factor: float | None,
    max_drawdown_pct: float,
    total_trades: int,
    fee_to_trade_gross_pct: float | None,
    window_slices_6m: list[dict[str, Any]],
) -> dict[str, Any]:
    def _gate_check(
        *,
        name: str,
        metrics: dict[str, Any],
        net_threshold: float,
        pf_threshold: float,
        max_dd_threshold: float,
        trades_threshold: int,
        fee_threshold: float | None = None,
    ) -> dict[str, Any]:
        net_profit = float(metrics.get("net_profit", 0.0))
        pf_value = _to_float(metrics.get("profit_factor"))
        max_dd_value = float(metrics.get("max_drawdown_pct", 0.0))
        raw_trades = _to_float(metrics.get("trades"))
        trades = int(raw_trades) if raw_trades is not None else 0
        fee_value = _to_float(metrics.get("fee_to_trade_gross_pct"))
        reasons: list[str] = []
        if float(net_profit) <= float(net_threshold) if name == "slice_6m" else float(net_profit) < float(net_threshold):
            reasons.append(f"net<{net_threshold:.2f}")
        if pf_value is None or float(pf_value) < float(pf_threshold):
            reasons.append(f"pf<{pf_threshold:.2f}")
        if float(max_dd_value) > float(max_dd_threshold):
            reasons.append(f"dd>{max_dd_threshold:.2f}")
        if int(trades) < int(trades_threshold):
            reasons.append(f"trades<{trades_threshold}")
        if fee_threshold is not None:
            if fee_value is None or float(fee_value) > float(fee_threshold):
                reasons.append(f"fee>{fee_threshold:.2f}")
        return {
            "name": name,
            "verdict": "KEEP" if not reasons else "KILL",
            "thresholds": {
                "net_profit_usdt": float(net_threshold),
                "profit_factor": float(pf_threshold),
                "max_drawdown_pct": float(max_dd_threshold),
                "trades": int(trades_threshold),
                "fee_to_trade_gross_pct": None if fee_threshold is None else float(fee_threshold),
            },
            "metrics": {
                "net_profit": round(float(net_profit), 6),
                "profit_factor": pf_value,
                "max_drawdown_pct": round(float(max_dd_value), 6),
                "trades": int(trades),
                "fee_to_trade_gross_pct": fee_value,
            },
            "reasons": reasons,
        }

    checks: list[dict[str, Any]] = []
    slice_rows = [item for item in window_slices_6m if isinstance(item, dict)]
    if slice_rows:
        first_slice = dict(slice_rows[0])
        checks.append(
            _gate_check(
                name="slice_6m",
                metrics={
                    "net_profit": first_slice.get("net_profit", 0.0),
                    "profit_factor": first_slice.get("profit_factor"),
                    "max_drawdown_pct": first_slice.get("max_drawdown_pct", 0.0),
                    "trades": first_slice.get("trades", 0),
                    "fee_to_trade_gross_pct": first_slice.get("fee_to_trade_gross_pct"),
                },
                net_threshold=0.0,
                pf_threshold=1.15,
                max_dd_threshold=12.0,
                trades_threshold=40,
                fee_threshold=70.0,
            )
        )

    duration_days = max(int(end_ms) - int(start_ms), 0) / float(86_400_000)
    if duration_days >= 320.0:
        checks.append(
            _gate_check(
                name="full_1y",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=8.0,
                pf_threshold=1.30,
                max_dd_threshold=15.0,
                trades_threshold=60,
                fee_threshold=65.0,
            )
        )
    else:
        checks.append(
            _gate_check(
                name="full_window",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=0.0,
                pf_threshold=1.15,
                max_dd_threshold=12.0,
                trades_threshold=40,
                fee_threshold=70.0,
            )
        )

    if duration_days >= 1000.0:
        checks.append(
            _gate_check(
                name="full_3y",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=18.0,
                pf_threshold=1.35,
                max_dd_threshold=20.0,
                trades_threshold=120,
                fee_threshold=65.0,
            )
        )

    verdict = "KEEP" if checks and all(item.get("verdict") == "KEEP" for item in checks) else "KILL"
    return {
        "track": "fb-fast-kill",
        "verdict": verdict,
        "stop_rule": "baseline_6m_fail_stops_branch_before_1y",
        "checks": checks,
    }


def _cbr_research_gate(
    *,
    start_ms: int,
    end_ms: int,
    total_net_profit: float,
    profit_factor: float | None,
    max_drawdown_pct: float,
    total_trades: int,
    fee_to_trade_gross_pct: float | None,
    window_slices_6m: list[dict[str, Any]],
) -> dict[str, Any]:
    def _gate_check(
        *,
        name: str,
        metrics: dict[str, Any],
        net_threshold: float,
        pf_threshold: float,
        max_dd_threshold: float,
        trades_threshold: int,
        fee_threshold: float | None = None,
    ) -> dict[str, Any]:
        net_profit = float(metrics.get("net_profit", 0.0))
        pf_value = _to_float(metrics.get("profit_factor"))
        max_dd_value = float(metrics.get("max_drawdown_pct", 0.0))
        raw_trades = _to_float(metrics.get("trades"))
        trades = int(raw_trades) if raw_trades is not None else 0
        fee_value = _to_float(metrics.get("fee_to_trade_gross_pct"))
        reasons: list[str] = []
        if float(net_profit) <= float(net_threshold) if name == "slice_6m" else float(net_profit) < float(net_threshold):
            reasons.append(f"net<{net_threshold:.2f}")
        if pf_value is None or float(pf_value) < float(pf_threshold):
            reasons.append(f"pf<{pf_threshold:.2f}")
        if float(max_dd_value) > float(max_dd_threshold):
            reasons.append(f"dd>{max_dd_threshold:.2f}")
        if int(trades) < int(trades_threshold):
            reasons.append(f"trades<{trades_threshold}")
        if fee_threshold is not None:
            if fee_value is None or float(fee_value) > float(fee_threshold):
                reasons.append(f"fee>{fee_threshold:.2f}")
        return {
            "name": name,
            "verdict": "KEEP" if not reasons else "KILL",
            "thresholds": {
                "net_profit_usdt": float(net_threshold),
                "profit_factor": float(pf_threshold),
                "max_drawdown_pct": float(max_dd_threshold),
                "trades": int(trades_threshold),
                "fee_to_trade_gross_pct": None if fee_threshold is None else float(fee_threshold),
            },
            "metrics": {
                "net_profit": round(float(net_profit), 6),
                "profit_factor": pf_value,
                "max_drawdown_pct": round(float(max_dd_value), 6),
                "trades": int(trades),
                "fee_to_trade_gross_pct": fee_value,
            },
            "reasons": reasons,
        }

    checks: list[dict[str, Any]] = []
    slice_rows = [item for item in window_slices_6m if isinstance(item, dict)]
    if slice_rows:
        first_slice = dict(slice_rows[0])
        checks.append(
            _gate_check(
                name="slice_6m",
                metrics={
                    "net_profit": first_slice.get("net_profit", 0.0),
                    "profit_factor": first_slice.get("profit_factor"),
                    "max_drawdown_pct": first_slice.get("max_drawdown_pct", 0.0),
                    "trades": first_slice.get("trades", 0),
                    "fee_to_trade_gross_pct": first_slice.get("fee_to_trade_gross_pct"),
                },
                net_threshold=0.0,
                pf_threshold=1.15,
                max_dd_threshold=12.0,
                trades_threshold=25,
                fee_threshold=70.0,
            )
        )

    duration_days = max(int(end_ms) - int(start_ms), 0) / float(86_400_000)
    if duration_days >= 320.0:
        checks.append(
            _gate_check(
                name="full_1y",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=8.0,
                pf_threshold=1.25,
                max_dd_threshold=14.0,
                trades_threshold=50,
                fee_threshold=65.0,
            )
        )
    else:
        checks.append(
            _gate_check(
                name="full_window",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=0.0,
                pf_threshold=1.15,
                max_dd_threshold=12.0,
                trades_threshold=25,
                fee_threshold=70.0,
            )
        )

    if duration_days >= 1000.0:
        checks.append(
            _gate_check(
                name="full_3y",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=18.0,
                pf_threshold=1.30,
                max_dd_threshold=18.0,
                trades_threshold=100,
                fee_threshold=65.0,
            )
        )

    verdict = "KEEP" if checks and all(item.get("verdict") == "KEEP" for item in checks) else "KILL"
    return {
        "track": "cbr-fast-kill",
        "verdict": verdict,
        "stop_rule": "baseline_6m_fail_stops_branch_before_1y",
        "checks": checks,
    }


def _lsr_research_gate(
    *,
    start_ms: int,
    end_ms: int,
    total_net_profit: float,
    profit_factor: float | None,
    max_drawdown_pct: float,
    total_trades: int,
    fee_to_trade_gross_pct: float | None,
    window_slices_6m: list[dict[str, Any]],
) -> dict[str, Any]:
    def _gate_check(
        *,
        name: str,
        metrics: dict[str, Any],
        net_threshold: float,
        pf_threshold: float,
        max_dd_threshold: float,
        trades_threshold: int,
        fee_threshold: float,
    ) -> dict[str, Any]:
        net_profit = float(_to_float(metrics.get("net_profit")) or 0.0)
        pf_value_raw = _to_float(metrics.get("profit_factor"))
        pf_value = None if pf_value_raw is None else round(float(pf_value_raw), 6)
        max_dd_value = float(_to_float(metrics.get("max_drawdown_pct")) or 0.0)
        trades = int(_to_int(metrics.get("trades")) or 0)
        fee_value_raw = _to_float(metrics.get("fee_to_trade_gross_pct"))
        fee_value = None if fee_value_raw is None else round(float(fee_value_raw), 6)

        reasons: list[str] = []
        if net_profit < float(net_threshold):
            reasons.append(f"net<{net_threshold:.2f}")
        if pf_value_raw is None or float(pf_value_raw) < float(pf_threshold):
            reasons.append(f"pf<{pf_threshold:.2f}")
        if max_dd_value > float(max_dd_threshold):
            reasons.append(f"dd>{max_dd_threshold:.2f}")
        if trades < int(trades_threshold):
            reasons.append(f"trades<{int(trades_threshold)}")
        if fee_value_raw is None or float(fee_value_raw) > float(fee_threshold):
            reasons.append(f"fee>{fee_threshold:.2f}")

        return {
            "name": name,
            "verdict": "KEEP" if not reasons else "KILL",
            "thresholds": {
                "net_profit_usdt": round(float(net_threshold), 6),
                "profit_factor": round(float(pf_threshold), 6),
                "max_drawdown_pct": round(float(max_dd_threshold), 6),
                "trades": int(trades_threshold),
                "fee_to_trade_gross_pct": round(float(fee_threshold), 6),
            },
            "metrics": {
                "net_profit": round(float(net_profit), 6),
                "profit_factor": pf_value,
                "max_drawdown_pct": round(float(max_dd_value), 6),
                "trades": int(trades),
                "fee_to_trade_gross_pct": fee_value,
            },
            "reasons": reasons,
        }

    checks: list[dict[str, Any]] = []
    slice_rows = [item for item in window_slices_6m if isinstance(item, dict)]
    if slice_rows:
        first_slice = dict(slice_rows[0])
        checks.append(
            _gate_check(
                name="slice_6m",
                metrics={
                    "net_profit": first_slice.get("net_profit", 0.0),
                    "profit_factor": first_slice.get("profit_factor"),
                    "max_drawdown_pct": first_slice.get("max_drawdown_pct", 0.0),
                    "trades": first_slice.get("trades", 0),
                    "fee_to_trade_gross_pct": first_slice.get("fee_to_trade_gross_pct"),
                },
                net_threshold=0.0,
                pf_threshold=1.15,
                max_dd_threshold=12.0,
                trades_threshold=25,
                fee_threshold=70.0,
            )
        )

    duration_days = max(int(end_ms) - int(start_ms), 0) / float(86_400_000)
    if duration_days >= 320.0:
        checks.append(
            _gate_check(
                name="full_1y",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=8.0,
                pf_threshold=1.25,
                max_dd_threshold=14.0,
                trades_threshold=50,
                fee_threshold=65.0,
            )
        )
    else:
        checks.append(
            _gate_check(
                name="full_window",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=0.0,
                pf_threshold=1.15,
                max_dd_threshold=12.0,
                trades_threshold=25,
                fee_threshold=70.0,
            )
        )

    if duration_days >= 1000.0:
        checks.append(
            _gate_check(
                name="full_3y",
                metrics={
                    "net_profit": total_net_profit,
                    "profit_factor": profit_factor,
                    "max_drawdown_pct": max_drawdown_pct,
                    "trades": total_trades,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                },
                net_threshold=18.0,
                pf_threshold=1.30,
                max_dd_threshold=18.0,
                trades_threshold=100,
                fee_threshold=65.0,
            )
        )

    verdict = "KEEP" if checks and all(item.get("verdict") == "KEEP" for item in checks) else "KILL"
    return {
        "track": "lsr-fast-kill",
        "verdict": verdict,
        "stop_rule": "baseline_6m_fail_stops_branch_before_1y",
        "checks": checks,
    }


def _risk_pct_from_levels(
    *,
    signal_side: str,
    entry_fill: float,
    stop_loss: float | None,
) -> float:
    if stop_loss is None or stop_loss <= 0.0 or entry_fill <= 0.0:
        return 0.0
    if signal_side == "BUY":
        return max((entry_fill - float(stop_loss)) / entry_fill, 0.0)
    return max((float(stop_loss) - entry_fill) / entry_fill, 0.0)


def _reward_pct_from_levels(
    *,
    signal_side: str,
    entry_fill: float,
    take_profit: float | None,
) -> float:
    if take_profit is None or take_profit <= 0.0 or entry_fill <= 0.0:
        return 0.0
    if signal_side == "BUY":
        return max((float(take_profit) - entry_fill) / entry_fill, 0.0)
    return max((entry_fill - float(take_profit)) / entry_fill, 0.0)


def _entry_reward_and_risk_pct(
    *,
    signal_side: str,
    entry_fill: float,
    sl_tp: dict[str, Any] | None,
    execution_hints: dict[str, Any] | None,
) -> tuple[float, float]:
    stop_loss = _to_float((sl_tp or {}).get("stop_loss")) if isinstance(sl_tp, dict) else None
    risk_pct = _risk_pct_from_levels(
        signal_side=signal_side,
        entry_fill=float(entry_fill),
        stop_loss=stop_loss,
    )
    reward_reference_r = (
        _to_float(execution_hints.get("reward_risk_reference_r"))
        if isinstance(execution_hints, dict)
        else None
    )
    if reward_reference_r is not None and reward_reference_r > 0.0 and risk_pct > 0.0:
        return float(reward_reference_r) * risk_pct, risk_pct

    take_profit_final = (
        _to_float((sl_tp or {}).get("take_profit_final")) if isinstance(sl_tp, dict) else None
    )
    if take_profit_final is not None and take_profit_final > 0.0:
        return (
            _reward_pct_from_levels(
                signal_side=signal_side,
                entry_fill=float(entry_fill),
                take_profit=float(take_profit_final),
            ),
            risk_pct,
        )

    take_profit = _to_float((sl_tp or {}).get("take_profit")) if isinstance(sl_tp, dict) else None
    return (
        _reward_pct_from_levels(
            signal_side=signal_side,
            entry_fill=float(entry_fill),
            take_profit=take_profit,
        ),
        risk_pct,
    )


def _simulate_symbol_metrics(
    *,
    symbol: str,
    rows: list[dict[str, Any]],
    candles_15m: list[_Kline15m],
    initial_capital: float,
    execution_model: _BacktestExecutionModel | None = None,
    fixed_leverage: float | None = None,
    fixed_leverage_margin_use_pct: float = 0.20,
    reverse_min_hold_bars: int = 8,
    reverse_cooldown_bars: int = 6,
    min_expected_edge_over_roundtrip_cost: float = 1.2,
    min_reward_risk_ratio: float = 0.0,
    max_trades_per_day_per_symbol: int = 12,
    daily_loss_limit_pct: float = 0.04,
    equity_floor_pct: float = 0.35,
    max_trade_margin_loss_fraction: float = 0.45,
    min_signal_score: float = 0.35,
    reverse_exit_min_profit_pct: float = 0.004,
    reverse_exit_min_signal_score: float = 0.60,
    default_reverse_exit_min_r: float = 0.0,
    default_risk_per_trade_pct: float = 0.0,
    default_max_effective_leverage: float = 30.0,
    drawdown_scale_start_pct: float = 0.12,
    drawdown_scale_end_pct: float = 0.32,
    drawdown_margin_scale_min: float = 0.35,
    stoploss_streak_trigger: int = 3,
    stoploss_cooldown_bars: int = 20,
    loss_cooldown_bars: int = 0,
    max_peak_drawdown_pct: float | None = None,
) -> dict[str, Any]:
    model = execution_model or _BacktestExecutionModel()
    open_trade: _OpenTrade | None = None
    realized_pnl = 0.0
    trade_events: list[dict[str, Any]] = []
    equity_curve: list[float] = []
    total_fees = 0.0
    total_funding_pnl = 0.0
    gross_trade_pnl_total = 0.0
    last_exit_tick = -(10**9)
    last_exit_cooldown_bars = max(int(reverse_cooldown_bars), 0)
    day_trade_counter: dict[int, int] = {}
    entry_block_counter: Counter[str] = Counter()
    day_ms = 24 * 60 * 60 * 1000
    current_day_key: int | None = None
    day_start_equity = float(initial_capital)
    day_locked = False
    trading_locked = False
    trading_lock_reason: str | None = None
    effective_daily_loss_limit = max(float(daily_loss_limit_pct), 0.0)
    effective_equity_floor_pct = max(float(equity_floor_pct), 0.0)
    effective_margin_loss_fraction = max(0.0, min(float(max_trade_margin_loss_fraction), 1.0))
    effective_min_signal_score = max(float(min_signal_score), 0.0)
    effective_reverse_exit_min_profit_pct = max(float(reverse_exit_min_profit_pct), 0.0)
    effective_reverse_exit_min_signal_score = max(float(reverse_exit_min_signal_score), 0.0)
    effective_default_reverse_exit_min_r = max(float(default_reverse_exit_min_r), 0.0)
    effective_default_risk_per_trade_pct = max(float(default_risk_per_trade_pct), 0.0)
    effective_default_max_effective_leverage = max(float(default_max_effective_leverage), 1.0)
    effective_min_reward_risk_ratio = max(float(min_reward_risk_ratio), 0.0)
    effective_drawdown_scale_start_pct = max(float(drawdown_scale_start_pct), 0.0)
    effective_drawdown_scale_end_pct = max(float(drawdown_scale_end_pct), 0.0)
    effective_drawdown_margin_scale_min = max(
        0.05,
        min(float(drawdown_margin_scale_min), 1.0),
    )
    effective_stoploss_streak_trigger = max(int(stoploss_streak_trigger), 0)
    effective_stoploss_cooldown_bars = max(int(stoploss_cooldown_bars), 0)
    effective_loss_cooldown_bars = max(int(loss_cooldown_bars), 0)
    effective_max_peak_drawdown_pct = (
        max(float(max_peak_drawdown_pct), 0.0)
        if max_peak_drawdown_pct is not None
        else None
    )
    peak_realized_equity = float(initial_capital)
    stoploss_streak = 0
    consecutive_loss_count = 0
    stoploss_cooldown_until_tick = -(10**9)
    loss_cooldown_until_tick = -(10**9)
    loss_streak_cooldown_until_tick = -(10**9)

    def _record_no_candidate_reason(reason: str) -> None:
        normalized = str(reason or "").strip()
        if not normalized:
            return
        if normalized.startswith("no_candidate_multi:"):
            detail = normalized.split(":", 1)[1]
            for token in detail.split(";"):
                item = str(token).strip()
                if not item:
                    continue
                _, _, reason_part = item.partition(":")
                entry_block_counter[str(reason_part or "no_candidate").strip()] += 1
            return
        if normalized != "no_candidate":
            entry_block_counter[normalized] += 1

    def _risk_pct_from_levels(
        *,
        signal_side: str,
        entry_fill: float,
        stop_loss: float | None,
    ) -> float:
        if stop_loss is None or stop_loss <= 0.0 or entry_fill <= 0.0:
            return 0.0
        if signal_side == "BUY":
            return max((entry_fill - float(stop_loss)) / entry_fill, 0.0)
        return max((float(stop_loss) - entry_fill) / entry_fill, 0.0)

    def _reward_pct_from_levels(
        *,
        signal_side: str,
        entry_fill: float,
        take_profit: float | None,
    ) -> float:
        if take_profit is None or take_profit <= 0.0 or entry_fill <= 0.0:
            return 0.0
        if signal_side == "BUY":
            return max((float(take_profit) - entry_fill) / entry_fill, 0.0)
        return max((entry_fill - float(take_profit)) / entry_fill, 0.0)

    def _entry_reward_and_risk_pct(
        *,
        signal_side: str,
        entry_fill: float,
        sl_tp: dict[str, Any] | None,
        execution_hints: dict[str, Any] | None,
    ) -> tuple[float, float]:
        stop_loss = _to_float((sl_tp or {}).get("stop_loss")) if isinstance(sl_tp, dict) else None
        risk_pct = _risk_pct_from_levels(
            signal_side=signal_side,
            entry_fill=float(entry_fill),
            stop_loss=stop_loss,
        )
        reward_reference_r = (
            _to_float(execution_hints.get("reward_risk_reference_r"))
            if isinstance(execution_hints, dict)
            else None
        )
        if reward_reference_r is not None and reward_reference_r > 0.0 and risk_pct > 0.0:
            return float(reward_reference_r) * risk_pct, risk_pct

        take_profit_final = (
            _to_float((sl_tp or {}).get("take_profit_final")) if isinstance(sl_tp, dict) else None
        )
        if take_profit_final is not None and take_profit_final > 0.0:
            return (
                _reward_pct_from_levels(
                    signal_side=signal_side,
                    entry_fill=float(entry_fill),
                    take_profit=float(take_profit_final),
                ),
                risk_pct,
            )

        take_profit = _to_float((sl_tp or {}).get("take_profit")) if isinstance(sl_tp, dict) else None
        return (
            _reward_pct_from_levels(
                signal_side=signal_side,
                entry_fill=float(entry_fill),
                take_profit=take_profit,
            ),
            risk_pct,
        )

    def _move_stop_to_break_even(trade: _OpenTrade) -> None:
        if trade.side == "BUY":
            trade.sl = max(float(trade.sl or 0.0), float(trade.entry_price))
            return
        stop_candidates = [float(trade.entry_price)]
        if trade.sl is not None:
            stop_candidates.append(float(trade.sl))
        trade.sl = min(stop_candidates)

    def _apply_selective_extension(
        *,
        trade: _OpenTrade,
        tick: int,
        current_mfe_r: float,
        per_unit_risk: float,
    ) -> None:
        if trade.selective_extension_activated:
            return
        if int(trade.selective_extension_proof_bars) <= 0:
            return
        if (
            int(trade.selective_extension_time_stop_bars) <= 0
            and float(trade.selective_extension_take_profit_r) <= 0.0
            and float(trade.selective_extension_move_stop_to_be_at_r) <= 0.0
        ):
            return
        held_bars = int(tick) - int(trade.entry_tick)
        if held_bars > max(int(trade.selective_extension_proof_bars), 0):
            return
        if float(current_mfe_r) < float(trade.selective_extension_min_mfe_r):
            return
        if float(trade.entry_regime_strength) < float(trade.selective_extension_min_regime_strength):
            return
        if float(trade.entry_bias_strength) < float(trade.selective_extension_min_bias_strength):
            return
        if float(trade.entry_quality_score_v2) < float(trade.selective_extension_min_quality_score_v2):
            return

        trade.selective_extension_activated = True
        trade.selective_extension_activation_tick = int(tick)

        if int(trade.selective_extension_time_stop_bars) > 0:
            if trade.time_stop_bars is None:
                trade.time_stop_bars = int(trade.selective_extension_time_stop_bars)
            else:
                trade.time_stop_bars = max(
                    int(trade.time_stop_bars),
                    int(trade.selective_extension_time_stop_bars),
                )

        if float(trade.selective_extension_take_profit_r) > 0.0 and float(per_unit_risk) > 0.0:
            tp_candidate = (
                float(trade.entry_price)
                + (float(per_unit_risk) * float(trade.selective_extension_take_profit_r))
                if trade.side == "BUY"
                else float(trade.entry_price)
                - (float(per_unit_risk) * float(trade.selective_extension_take_profit_r))
            )
            if trade.side == "BUY":
                if trade.tp is None or float(tp_candidate) > float(trade.tp):
                    trade.tp = float(tp_candidate)
                    trade.selective_extension_tp_applied = True
            elif trade.tp is None or float(tp_candidate) < float(trade.tp):
                trade.tp = float(tp_candidate)
                trade.selective_extension_tp_applied = True

        be_trigger_r = float(trade.selective_extension_move_stop_to_be_at_r)
        if be_trigger_r > 0.0:
            if float(current_mfe_r) >= be_trigger_r:
                _move_stop_to_break_even(trade)
                trade.selective_extension_protection_applied = True
            elif float(trade.break_even_move_r) <= 0.0 or be_trigger_r < float(trade.break_even_move_r):
                trade.break_even_move_r = be_trigger_r

    def _realize_trade_slice(
        *,
        qty_to_close: float,
        candle: _Kline15m,
        exit_price: float,
    ) -> tuple[float, float, float, float, float]:
        if open_trade is None:
            return 0.0, 0.0, 0.0, 0.0, 0.0
        qty = max(min(float(qty_to_close), float(open_trade.quantity)), 0.0)
        if qty <= 0.0:
            return 0.0, 0.0, 0.0, 0.0, 0.0
        exit_fill = model.filled_exit_price(side=open_trade.side, raw_price=float(exit_price))
        gross_pnl = (
            (exit_fill - open_trade.entry_price) * qty
            if open_trade.side == "BUY"
            else (open_trade.entry_price - exit_fill) * qty
        )
        exit_notional = abs(exit_fill * qty)
        exit_fee = model.fee(notional=exit_notional)
        funding_base_notional = float(open_trade.entry_notional)
        if float(open_trade.initial_quantity) > 0.0:
            funding_base_notional *= qty / float(open_trade.initial_quantity)
        funding_pnl = model.funding_pnl(
            side=open_trade.side,
            entry_time_ms=open_trade.entry_time_ms,
            exit_time_ms=candle.close_time_ms,
            notional=funding_base_notional,
        )
        net_pnl = gross_pnl - exit_fee + funding_pnl
        return qty, exit_fill, gross_pnl, exit_fee, net_pnl

    def _close_partial(
        *,
        tick: int,
        candle: _Kline15m,
        exit_price: float,
        qty_to_close: float,
    ) -> None:
        nonlocal open_trade, realized_pnl, total_fees, total_funding_pnl, gross_trade_pnl_total
        if open_trade is None:
            return
        qty, exit_fill, gross_pnl, exit_fee, net_pnl = _realize_trade_slice(
            qty_to_close=qty_to_close,
            candle=candle,
            exit_price=exit_price,
        )
        if qty <= 0.0:
            return
        funding_pnl = net_pnl - gross_pnl + exit_fee
        realized_pnl += net_pnl
        total_fees += exit_fee
        total_funding_pnl += funding_pnl
        gross_trade_pnl_total += gross_pnl
        open_trade.realized_gross_pnl += gross_pnl
        open_trade.realized_exit_fees += exit_fee
        open_trade.realized_funding_pnl += funding_pnl
        open_trade.quantity = max(float(open_trade.quantity) - qty, 0.0)
        open_trade.partial_taken = True
        if open_trade.break_even_move_r > 0.0:
            if open_trade.side == "BUY":
                open_trade.sl = max(float(open_trade.sl or 0.0), float(open_trade.entry_price))
            else:
                stop_candidates = [float(open_trade.entry_price)]
                if open_trade.sl is not None:
                    stop_candidates.append(float(open_trade.sl))
                open_trade.sl = min(stop_candidates)

    def _close_trade(*, tick: int, candle: _Kline15m, exit_price: float, reason: str) -> None:
        nonlocal \
            open_trade, \
            realized_pnl, \
            total_fees, \
            total_funding_pnl, \
            gross_trade_pnl_total, \
            last_exit_tick, \
            last_exit_cooldown_bars, \
            stoploss_streak, \
            consecutive_loss_count, \
            stoploss_cooldown_until_tick, \
            loss_cooldown_until_tick, \
            loss_streak_cooldown_until_tick
        if open_trade is None:
            return
        qty = max(float(open_trade.quantity), 0.0)
        if qty <= 0.0:
            return
        qty, exit_fill, gross_pnl, exit_fee, net_slice = _realize_trade_slice(
            qty_to_close=qty,
            candle=candle,
            exit_price=exit_price,
        )
        funding_pnl = net_slice - gross_pnl + exit_fee
        gross_total = float(open_trade.realized_gross_pnl) + gross_pnl
        exit_fee_total = float(open_trade.realized_exit_fees) + exit_fee
        funding_total = float(open_trade.realized_funding_pnl) + funding_pnl
        partial_net_total = (
            float(open_trade.realized_gross_pnl)
            - float(open_trade.realized_exit_fees)
            + float(open_trade.realized_funding_pnl)
        )
        net_pnl = gross_total - exit_fee_total + funding_total
        if open_trade.max_loss_cap is not None:
            trade_floor = -float(open_trade.max_loss_cap) + float(open_trade.entry_fee)
            if net_pnl < trade_floor:
                net_pnl = trade_floor
        total_fees += exit_fee
        total_funding_pnl += funding_pnl
        gross_trade_pnl_total += gross_pnl
        realized_pnl += net_pnl - partial_net_total
        trade_events.append(
            {
                "symbol": symbol,
                "side": "LONG" if open_trade.side == "BUY" else "SHORT",
                "regime": open_trade.regime,
                "alpha_id": open_trade.alpha_id,
                "entry_price": open_trade.entry_price,
                "exit_price": exit_fill,
                "quantity": float(open_trade.initial_quantity),
                "gross_pnl": gross_total,
                "entry_fee": open_trade.entry_fee,
                "exit_fee": exit_fee_total,
                "funding_pnl": funding_total,
                "pnl": net_pnl,
                "entry_tick": open_trade.entry_tick,
                "exit_tick": tick,
                "entry_time_ms": open_trade.entry_time_ms,
                "exit_time_ms": candle.close_time_ms,
                "initial_risk_abs": open_trade.initial_risk_abs,
                "effective_leverage": open_trade.effective_leverage,
                "entry_family": open_trade.entry_family,
                "entry_quality_score_v2": open_trade.entry_quality_score_v2,
                "entry_regime_strength": open_trade.entry_regime_strength,
                "entry_bias_strength": open_trade.entry_bias_strength,
                "quality_exit_applied": bool(open_trade.quality_exit_applied),
                "time_stop_bars": open_trade.time_stop_bars,
                "progress_check_bars": open_trade.progress_check_bars,
                "progress_min_mfe_r": open_trade.progress_min_mfe_r,
                "progress_extend_trigger_r": open_trade.progress_extend_trigger_r,
                "progress_extend_bars": open_trade.progress_extend_bars,
                "selective_extension_activated": bool(open_trade.selective_extension_activated),
                "selective_extension_activation_tick": open_trade.selective_extension_activation_tick,
                "selective_extension_tp_applied": bool(open_trade.selective_extension_tp_applied),
                "selective_extension_protection_applied": bool(
                    open_trade.selective_extension_protection_applied
                ),
                "partial_taken": bool(open_trade.partial_taken),
                "reason": reason,
            }
        )
        exit_cooldown_bars = max(int(reverse_cooldown_bars), 0)
        if net_pnl > 0.0:
            stoploss_streak = 0
            consecutive_loss_count = 0
            exit_cooldown_bars = max(int(open_trade.profit_exit_cooldown_bars), 0)
        else:
            consecutive_loss_count += 1
            if reason == "stop_loss":
                stoploss_streak += 1
                exit_cooldown_bars = max(int(open_trade.stop_exit_cooldown_bars), 0)
                if (
                    effective_stoploss_streak_trigger > 0
                    and stoploss_streak >= effective_stoploss_streak_trigger
                    and effective_stoploss_cooldown_bars > 0
                ):
                    stoploss_cooldown_until_tick = max(
                        int(stoploss_cooldown_until_tick),
                        int(tick) + int(effective_stoploss_cooldown_bars),
                    )
            else:
                stoploss_streak = 0
            if (
                int(open_trade.loss_streak_trigger) > 0
                and consecutive_loss_count >= int(open_trade.loss_streak_trigger)
                and int(open_trade.loss_streak_cooldown_bars) > 0
            ):
                loss_streak_cooldown_until_tick = max(
                    int(loss_streak_cooldown_until_tick),
                    int(tick) + int(open_trade.loss_streak_cooldown_bars),
                )
        if net_pnl < 0.0 and effective_loss_cooldown_bars > 0:
            loss_cooldown_until_tick = max(
                int(loss_cooldown_until_tick),
                int(tick) + int(effective_loss_cooldown_bars),
            )
        last_exit_tick = int(tick)
        last_exit_cooldown_bars = max(int(exit_cooldown_bars), 0)
        open_trade = None

    for tick, row in enumerate(rows):
        candle = candles_15m[tick]
        high = float(candle.high)
        low = float(candle.low)
        close = float(candle.close)
        day_key = int(candle.open_time_ms // day_ms)
        if current_day_key != day_key:
            current_day_key = day_key
            day_start_equity = max(float(initial_capital) + realized_pnl, 0.0)
            day_locked = False

        current_mfe_r = 0.0
        per_unit_risk = 0.0
        if open_trade is not None:
            decision_now = row.get("decision") if isinstance(row.get("decision"), dict) else {}
            indicators_now = (
                decision_now.get("indicators")
                if isinstance(decision_now.get("indicators"), dict)
                else {}
            )
            atr_now = _to_float(indicators_now.get("atr14_15m"))
            volume_ratio_now = _to_float(indicators_now.get("volume_ratio_15m"))
            close_30m_now = _to_float(indicators_now.get("close_30m"))
            ema_30m_now = _to_float(indicators_now.get("ema20_30m"))
            close_1h_now = _to_float(indicators_now.get("close_1h"))
            ema_1h_now = _to_float(indicators_now.get("ema20_1h"))
            if open_trade.side == "BUY":
                open_trade.peak_price = max(float(open_trade.peak_price or open_trade.entry_price), high)
            else:
                baseline_trough = float(open_trade.trough_price or open_trade.entry_price)
                open_trade.trough_price = min(baseline_trough, low)

            if open_trade.initial_risk_abs is not None and open_trade.initial_quantity > 0.0:
                per_unit_risk = float(open_trade.initial_risk_abs) / max(
                    float(open_trade.initial_quantity),
                    1e-9,
                )
                if per_unit_risk > 0.0:
                    favorable_move = (
                        max(high - float(open_trade.entry_price), 0.0)
                        if open_trade.side == "BUY"
                        else max(float(open_trade.entry_price) - low, 0.0)
                    )
                    current_mfe_r = float(favorable_move / per_unit_risk)

            if open_trade.side == "BUY":
                effective_stop = float(open_trade.sl) if open_trade.sl is not None else None
                if open_trade.runner_stop is not None:
                    effective_stop = (
                        max(float(effective_stop), float(open_trade.runner_stop))
                        if effective_stop is not None
                        else float(open_trade.runner_stop)
                    )
                if effective_stop is not None and low <= float(effective_stop):
                    _close_trade(
                        tick=tick,
                        candle=candle,
                        exit_price=float(effective_stop),
                        reason="trail_stop" if open_trade.runner_stop is not None else "stop_loss",
                    )
                elif open_trade is not None:
                    _apply_selective_extension(
                        trade=open_trade,
                        tick=tick,
                        current_mfe_r=current_mfe_r,
                        per_unit_risk=per_unit_risk,
                    )
                    if (
                        not open_trade.partial_taken
                        and open_trade.tp_partial_price is not None
                        and open_trade.tp_partial_ratio > 0.0
                        and high >= float(open_trade.tp_partial_price)
                    ):
                        partial_qty = float(open_trade.initial_quantity) * float(open_trade.tp_partial_ratio)
                        _close_partial(
                            tick=tick,
                            candle=candle,
                            exit_price=float(open_trade.tp_partial_price),
                            qty_to_close=partial_qty,
                        )
                    elif (
                        open_trade.tp is not None
                        and high >= float(open_trade.tp)
                        and str(open_trade.runner_exit_mode or "").lower() != "trail_only"
                    ):
                        _close_trade(
                            tick=tick,
                            candle=candle,
                            exit_price=float(open_trade.tp),
                            reason="take_profit",
                        )
            elif open_trade is not None:
                effective_stop = float(open_trade.sl) if open_trade.sl is not None else None
                if open_trade.runner_stop is not None:
                    effective_stop = (
                        min(float(effective_stop), float(open_trade.runner_stop))
                        if effective_stop is not None
                        else float(open_trade.runner_stop)
                    )
                if effective_stop is not None and high >= float(effective_stop):
                    _close_trade(
                        tick=tick,
                        candle=candle,
                        exit_price=float(effective_stop),
                        reason="trail_stop" if open_trade.runner_stop is not None else "stop_loss",
                    )
                elif open_trade is not None:
                    _apply_selective_extension(
                        trade=open_trade,
                        tick=tick,
                        current_mfe_r=current_mfe_r,
                        per_unit_risk=per_unit_risk,
                    )
                    if (
                        not open_trade.partial_taken
                        and open_trade.tp_partial_price is not None
                        and open_trade.tp_partial_ratio > 0.0
                        and low <= float(open_trade.tp_partial_price)
                    ):
                        partial_qty = float(open_trade.initial_quantity) * float(open_trade.tp_partial_ratio)
                        _close_partial(
                            tick=tick,
                            candle=candle,
                            exit_price=float(open_trade.tp_partial_price),
                            qty_to_close=partial_qty,
                        )
                    elif (
                        open_trade.tp is not None
                        and low <= float(open_trade.tp)
                        and str(open_trade.runner_exit_mode or "").lower() != "trail_only"
                    ):
                        _close_trade(
                            tick=tick,
                            candle=candle,
                            exit_price=float(open_trade.tp),
                            reason="take_profit",
                        )

            if open_trade is not None and per_unit_risk > 0.0:
                if current_mfe_r >= float(open_trade.break_even_move_r):
                    _move_stop_to_break_even(open_trade)

                if (
                    open_trade.partial_taken
                    and str(open_trade.runner_exit_mode or "").lower() == "trail_only"
                    and atr_now is not None
                    and atr_now > 0.0
                ):
                    if open_trade.side == "BUY":
                        peak_price = float(open_trade.peak_price or high)
                        trail_candidate = peak_price - (
                            float(open_trade.runner_trailing_atr_mult) * float(atr_now)
                        )
                        open_trade.runner_stop = max(
                            float(open_trade.runner_stop or trail_candidate),
                            float(trail_candidate),
                            float(open_trade.entry_price),
                        )
                    else:
                        trough_price = float(open_trade.trough_price or low)
                        trail_candidate = trough_price + (
                            float(open_trade.runner_trailing_atr_mult) * float(atr_now)
                        )
                        current_runner = (
                            float(open_trade.runner_stop)
                            if open_trade.runner_stop is not None
                            else float(trail_candidate)
                        )
                        open_trade.runner_stop = min(
                            current_runner,
                            float(trail_candidate),
                            float(open_trade.entry_price),
                        )

                structure_lost = False
                if open_trade.side == "BUY":
                    structure_lost = bool(
                        (ema_30m_now is not None and close_30m_now is not None and close_30m_now < ema_30m_now)
                        or (ema_1h_now is not None and close_1h_now is not None and close_1h_now < ema_1h_now)
                    )
                else:
                    structure_lost = bool(
                        (ema_30m_now is not None and close_30m_now is not None and close_30m_now > ema_30m_now)
                        or (ema_1h_now is not None and close_1h_now is not None and close_1h_now > ema_1h_now)
                    )

                if open_trade.partial_taken and structure_lost:
                    _close_trade(
                        tick=tick,
                        candle=candle,
                        exit_price=close,
                        reason="structure_trail_exit",
                    )

            if open_trade is not None and open_trade.stalled_trend_timeout_bars is not None:
                held_bars = int(tick) - int(open_trade.entry_tick)
                if held_bars >= max(int(open_trade.stalled_trend_timeout_bars), 0):
                    progress_r = 0.0
                    if per_unit_risk > 0.0:
                        progress_move = (
                            max(close - float(open_trade.entry_price), 0.0)
                            if open_trade.side == "BUY"
                            else max(float(open_trade.entry_price) - close, 0.0)
                        )
                        progress_r = progress_move / per_unit_risk
                    if (
                        progress_r < 0.8
                        and (volume_ratio_now or 0.0) < float(open_trade.stalled_volume_ratio_floor or 0.0)
                    ):
                        structure_lost = False
                        if open_trade.side == "BUY":
                            structure_lost = bool(
                                ema_30m_now is not None and close_30m_now is not None and close_30m_now < ema_30m_now
                            )
                        else:
                            structure_lost = bool(
                                ema_30m_now is not None and close_30m_now is not None and close_30m_now > ema_30m_now
                            )
                        if structure_lost:
                            _close_trade(
                                tick=tick,
                                candle=candle,
                                exit_price=close,
                                reason="stalled_trend_exit",
                            )

            if open_trade is not None and open_trade.time_stop_bars is not None:
                held_bars = int(tick) - int(open_trade.entry_tick)
                if (
                    int(open_trade.progress_check_bars) > 0
                    and held_bars >= int(open_trade.progress_check_bars)
                    and float(open_trade.progress_min_mfe_r) > 0.0
                    and float(current_mfe_r) < float(open_trade.progress_min_mfe_r)
                ):
                    _close_trade(
                        tick=tick,
                        candle=candle,
                        exit_price=close,
                        reason="progress_time_stop",
                    )
                elif held_bars >= max(
                    int(open_trade.time_stop_bars),
                    0,
                ) + (
                    int(open_trade.progress_extend_bars)
                    if (
                        float(open_trade.progress_extend_trigger_r) > 0.0
                        and float(current_mfe_r) >= float(open_trade.progress_extend_trigger_r)
                    )
                    else 0
                ):
                    _close_trade(
                        tick=tick,
                        candle=candle,
                        exit_price=close,
                        reason="time_stop",
                    )

        realized_equity = float(initial_capital) + realized_pnl
        if (
            not trading_locked
            and effective_daily_loss_limit > 0.0
            and day_start_equity > 0.0
            and ((day_start_equity - realized_equity) / day_start_equity)
            >= effective_daily_loss_limit
        ):
            if open_trade is not None:
                _close_trade(
                    tick=tick,
                    candle=candle,
                    exit_price=close,
                    reason="daily_loss_stop",
                )
                realized_equity = float(initial_capital) + realized_pnl
            day_locked = True

        floor_equity = float(initial_capital) * effective_equity_floor_pct
        if (
            not trading_locked
            and effective_equity_floor_pct > 0.0
            and realized_equity <= floor_equity
        ):
            if open_trade is not None:
                _close_trade(
                    tick=tick,
                    candle=candle,
                    exit_price=close,
                    reason="equity_floor_stop",
                )
                realized_equity = float(initial_capital) + realized_pnl
            trading_locked = True
            trading_lock_reason = "equity_floor"

        if realized_equity > peak_realized_equity:
            peak_realized_equity = float(realized_equity)

        if (
            not trading_locked
            and effective_max_peak_drawdown_pct is not None
            and effective_max_peak_drawdown_pct > 0.0
            and peak_realized_equity > 0.0
        ):
            realized_drawdown = (
                (float(peak_realized_equity) - float(realized_equity))
                / float(peak_realized_equity)
            )
            if realized_drawdown >= effective_max_peak_drawdown_pct:
                if open_trade is not None:
                    _close_trade(
                        tick=tick,
                        candle=candle,
                        exit_price=close,
                        reason="peak_drawdown_kill",
                    )
                    realized_equity = float(initial_capital) + realized_pnl
                trading_locked = True
                trading_lock_reason = "peak_drawdown_kill"
                entry_block_counter["peak_drawdown_kill"] += 1

        candidate = row.get("candidate")
        decision = row.get("decision")
        size = row.get("size")
        cycle_state = str(row.get("state") or "")
        cycle_reason = str(row.get("reason") or "")
        would_enter = bool(row.get("would_enter"))

        if not would_enter and candidate is None and cycle_state == "no_candidate":
            _record_no_candidate_reason(cycle_reason)
            continue

        if would_enter and isinstance(candidate, dict):
            signal_side = str(candidate.get("side") or "")
            if signal_side not in {"BUY", "SELL"}:
                signal_side = str((decision or {}).get("side") or "")
            if signal_side in {"BUY", "SELL"}:
                if open_trade is not None and open_trade.side != signal_side:
                    if not bool(open_trade.allow_reverse_exit):
                        entry_block_counter["reverse_disabled_block"] += 1
                        continue
                    held_bars = int(tick) - int(open_trade.entry_tick)
                    if held_bars >= max(int(reverse_min_hold_bars), 0):
                        reverse_score = _to_float((candidate or {}).get("score"))
                        if reverse_score is None:
                            reverse_score = _to_float((decision or {}).get("score"))
                        if (
                            reverse_score is not None
                            and reverse_score < effective_reverse_exit_min_signal_score
                        ):
                            entry_block_counter["reverse_score_block"] += 1
                        else:
                            unrealized_pct = 0.0
                            unrealized_abs = 0.0
                            if open_trade.entry_price > 0:
                                if open_trade.side == "BUY":
                                    unrealized_abs = max(
                                        (close - float(open_trade.entry_price))
                                        * float(open_trade.quantity),
                                        0.0,
                                    )
                                    unrealized_pct = max(
                                        (close - float(open_trade.entry_price))
                                        / float(open_trade.entry_price),
                                        0.0,
                                    )
                                else:
                                    unrealized_abs = max(
                                        (float(open_trade.entry_price) - close)
                                        * float(open_trade.quantity),
                                        0.0,
                                    )
                                    unrealized_pct = max(
                                        (float(open_trade.entry_price) - close)
                                        / float(open_trade.entry_price),
                                        0.0,
                                    )
                            if unrealized_pct < effective_reverse_exit_min_profit_pct:
                                entry_block_counter["reverse_profit_block"] += 1
                            else:
                                required_r = max(
                                    float(open_trade.reverse_exit_min_r),
                                    effective_default_reverse_exit_min_r,
                                )
                                regime_lost = False
                                current_regime = str((decision or {}).get("regime") or "").strip().upper()
                                if open_trade.regime and current_regime and current_regime != str(open_trade.regime).upper():
                                    regime_lost = True
                                if bool((decision or {}).get("regime_lost")):
                                    regime_lost = True
                                if bool(getattr(open_trade, "regime_lost_exit_required", False)) and not regime_lost:
                                    entry_block_counter["reverse_regime_block"] += 1
                                    continue
                                if required_r > 0.0:
                                    initial_risk_abs = float(open_trade.initial_risk_abs or 0.0)
                                    if initial_risk_abs <= 0.0:
                                        entry_block_counter["reverse_r_missing_block"] += 1
                                        continue
                                    current_r = unrealized_abs / initial_risk_abs
                                    if current_r < required_r:
                                        entry_block_counter["reverse_r_block"] += 1
                                        continue
                                _close_trade(
                                    tick=tick,
                                    candle=candle,
                                    exit_price=close,
                                    reason="reverse_signal",
                                )
                    else:
                        entry_block_counter["reverse_hold_block"] += 1
                if open_trade is None:
                    if trading_locked:
                        if trading_lock_reason == "peak_drawdown_kill":
                            entry_block_counter["peak_drawdown_kill_block"] += 1
                        else:
                            entry_block_counter["equity_floor_block"] += 1
                        continue
                    if day_locked:
                        entry_block_counter["daily_loss_stop_block"] += 1
                        continue
                    if int(tick) < int(stoploss_cooldown_until_tick):
                        entry_block_counter["stoploss_cooldown_block"] += 1
                        continue
                    if int(tick) < int(loss_cooldown_until_tick):
                        entry_block_counter["loss_cooldown_block"] += 1
                        continue
                    if int(tick) < int(loss_streak_cooldown_until_tick):
                        entry_block_counter["loss_streak_cooldown_block"] += 1
                        continue

                    signal_score = _to_float((candidate or {}).get("score"))
                    if signal_score is None:
                        signal_score = _to_float((decision or {}).get("score"))
                    if signal_score is not None and signal_score < effective_min_signal_score:
                        entry_block_counter["score_quality_block"] += 1
                        continue

                    cooldown_bars = max(int(last_exit_cooldown_bars), 0)
                    if int(tick) - int(last_exit_tick) < cooldown_bars:
                        entry_block_counter["reverse_cooldown_block"] += 1
                        continue

                    if max(int(max_trades_per_day_per_symbol), 0) > 0 and day_trade_counter.get(
                        day_key, 0
                    ) >= int(max_trades_per_day_per_symbol):
                        entry_block_counter["daily_trade_cap_block"] += 1
                        continue

                    entry_price = _to_float((candidate or {}).get("entry_price"))
                    if entry_price is None:
                        entry_price = _to_float((decision or {}).get("entry_price"))
                    if entry_price is None or entry_price <= 0:
                        entry_price = close
                    qty = _to_float((size or {}).get("qty")) if isinstance(size, dict) else None
                    if qty is None or qty <= 0:
                        qty = 1.0
                    entry_fill = model.filled_entry_price(
                        side=signal_side, raw_price=float(entry_price)
                    )

                    sl_tp = (decision or {}).get("sl_tp") if isinstance(decision, dict) else None
                    tp = (
                        _to_float((sl_tp or {}).get("take_profit"))
                        if isinstance(sl_tp, dict)
                        else None
                    )
                    sl = (
                        _to_float((sl_tp or {}).get("stop_loss"))
                        if isinstance(sl_tp, dict)
                        else None
                    )
                    execution_hints = (
                        (decision or {}).get("execution") if isinstance(decision, dict) else None
                    )
                    time_stop_bars: int | None = None
                    progress_check_bars = 0
                    progress_min_mfe_r = 0.0
                    progress_extend_trigger_r = 0.0
                    progress_extend_bars = 0
                    stalled_trend_timeout_bars: int | None = None
                    stop_exit_cooldown_bars = max(int(reverse_cooldown_bars), 0)
                    loss_streak_trigger_hint = 0
                    loss_streak_cooldown_bars_hint = 0
                    profit_exit_cooldown_bars = max(int(reverse_cooldown_bars), 0)
                    tp_partial_ratio = 0.0
                    tp_partial_price: float | None = None
                    tp_partial_at_r = 0.0
                    break_even_move_r = 0.0
                    runner_exit_mode: str | None = None
                    runner_trailing_atr_mult = 0.0
                    stalled_volume_ratio_floor = 0.0
                    regime_lost_exit_required = True
                    allow_reverse_exit = True
                    entry_quality_score_v2 = 0.0
                    entry_regime_strength = 0.0
                    entry_bias_strength = 0.0
                    quality_exit_applied = False
                    selective_extension_proof_bars = 0
                    selective_extension_min_mfe_r = 0.0
                    selective_extension_min_regime_strength = 0.0
                    selective_extension_min_bias_strength = 0.0
                    selective_extension_min_quality_score_v2 = 0.0
                    selective_extension_time_stop_bars = 0
                    selective_extension_take_profit_r = 0.0
                    selective_extension_move_stop_to_be_at_r = 0.0
                    alpha_id = str((decision or {}).get("alpha_id") or "").strip() or None
                    entry_family = str((decision or {}).get("entry_family") or "").strip() or None
                    if isinstance(execution_hints, dict):
                        hint_time_stop_bars = _to_int(execution_hints.get("time_stop_bars"))
                        if hint_time_stop_bars is not None and hint_time_stop_bars > 0:
                            time_stop_bars = int(hint_time_stop_bars)
                        hint_progress_check_bars = _to_int(execution_hints.get("progress_check_bars"))
                        if hint_progress_check_bars is not None and hint_progress_check_bars >= 0:
                            progress_check_bars = int(hint_progress_check_bars)
                        hint_progress_min_mfe_r = _to_float(execution_hints.get("progress_min_mfe_r"))
                        if hint_progress_min_mfe_r is not None and hint_progress_min_mfe_r >= 0.0:
                            progress_min_mfe_r = float(hint_progress_min_mfe_r)
                        hint_progress_extend_trigger_r = _to_float(
                            execution_hints.get("progress_extend_trigger_r")
                        )
                        if (
                            hint_progress_extend_trigger_r is not None
                            and hint_progress_extend_trigger_r >= 0.0
                        ):
                            progress_extend_trigger_r = float(hint_progress_extend_trigger_r)
                        hint_progress_extend_bars = _to_int(execution_hints.get("progress_extend_bars"))
                        if hint_progress_extend_bars is not None and hint_progress_extend_bars >= 0:
                            progress_extend_bars = int(hint_progress_extend_bars)
                        hint_stalled_trend_timeout_bars = _to_int(
                            execution_hints.get("stalled_trend_timeout_bars")
                        )
                        if (
                            hint_stalled_trend_timeout_bars is not None
                            and hint_stalled_trend_timeout_bars > 0
                        ):
                            stalled_trend_timeout_bars = int(hint_stalled_trend_timeout_bars)
                        hint_stop_exit_cooldown_bars = _to_int(
                            execution_hints.get("stop_exit_cooldown_bars")
                        )
                        if (
                            hint_stop_exit_cooldown_bars is not None
                            and hint_stop_exit_cooldown_bars >= 0
                        ):
                            stop_exit_cooldown_bars = int(hint_stop_exit_cooldown_bars)
                        hint_loss_streak_trigger = _to_int(execution_hints.get("loss_streak_trigger"))
                        if hint_loss_streak_trigger is not None and hint_loss_streak_trigger >= 0:
                            loss_streak_trigger_hint = int(hint_loss_streak_trigger)
                        hint_loss_streak_cooldown_bars = _to_int(
                            execution_hints.get("loss_streak_cooldown_bars")
                        )
                        if (
                            hint_loss_streak_cooldown_bars is not None
                            and hint_loss_streak_cooldown_bars >= 0
                        ):
                            loss_streak_cooldown_bars_hint = int(hint_loss_streak_cooldown_bars)
                        hint_profit_exit_cooldown_bars = _to_int(
                            execution_hints.get("profit_exit_cooldown_bars")
                        )
                        if (
                            hint_profit_exit_cooldown_bars is not None
                            and hint_profit_exit_cooldown_bars >= 0
                        ):
                            profit_exit_cooldown_bars = int(hint_profit_exit_cooldown_bars)
                        hint_tp_partial_ratio = _to_float(execution_hints.get("tp_partial_ratio"))
                        if hint_tp_partial_ratio is not None:
                            tp_partial_ratio = min(max(float(hint_tp_partial_ratio), 0.0), 1.0)
                        hint_tp_partial_price = _to_float(execution_hints.get("tp_partial_price"))
                        if hint_tp_partial_price is not None and hint_tp_partial_price > 0.0:
                            tp_partial_price = float(hint_tp_partial_price)
                        hint_tp_partial_at_r = _to_float(execution_hints.get("tp_partial_at_r"))
                        if hint_tp_partial_at_r is not None and hint_tp_partial_at_r > 0.0:
                            tp_partial_at_r = float(hint_tp_partial_at_r)
                        hint_break_even_move_r = _to_float(execution_hints.get("move_stop_to_be_at_r"))
                        if hint_break_even_move_r is not None and hint_break_even_move_r >= 0.0:
                            break_even_move_r = float(hint_break_even_move_r)
                        hint_runner_exit_mode = execution_hints.get("runner_exit_mode")
                        if hint_runner_exit_mode is not None:
                            runner_exit_mode = str(hint_runner_exit_mode).strip().lower() or None
                        hint_runner_trailing_atr_mult = _to_float(
                            execution_hints.get("runner_trailing_atr_mult")
                        )
                        if (
                            hint_runner_trailing_atr_mult is not None
                            and hint_runner_trailing_atr_mult > 0.0
                        ):
                            runner_trailing_atr_mult = float(hint_runner_trailing_atr_mult)
                        hint_stalled_volume_ratio_floor = _to_float(
                            execution_hints.get("stalled_volume_ratio_floor")
                        )
                        if (
                            hint_stalled_volume_ratio_floor is not None
                            and hint_stalled_volume_ratio_floor >= 0.0
                        ):
                            stalled_volume_ratio_floor = float(hint_stalled_volume_ratio_floor)
                        hint_allow_reverse_exit = execution_hints.get("allow_reverse_exit")
                        if isinstance(hint_allow_reverse_exit, bool):
                            allow_reverse_exit = bool(hint_allow_reverse_exit)
                        elif isinstance(hint_allow_reverse_exit, str):
                            allow_reverse_exit = hint_allow_reverse_exit.strip().lower() in {
                                "1",
                                "true",
                                "yes",
                                "y",
                                "on",
                            }
                        hint_entry_quality_score_v2 = _to_float(
                            execution_hints.get("entry_quality_score_v2")
                        )
                        if hint_entry_quality_score_v2 is not None and hint_entry_quality_score_v2 >= 0.0:
                            entry_quality_score_v2 = float(hint_entry_quality_score_v2)
                        hint_entry_regime_strength = _to_float(
                            execution_hints.get("entry_regime_strength")
                        )
                        if hint_entry_regime_strength is not None and hint_entry_regime_strength >= 0.0:
                            entry_regime_strength = float(hint_entry_regime_strength)
                        hint_entry_bias_strength = _to_float(
                            execution_hints.get("entry_bias_strength")
                        )
                        if hint_entry_bias_strength is not None and hint_entry_bias_strength >= 0.0:
                            entry_bias_strength = float(hint_entry_bias_strength)
                        hint_quality_exit_applied = execution_hints.get("quality_exit_applied")
                        if isinstance(hint_quality_exit_applied, bool):
                            quality_exit_applied = bool(hint_quality_exit_applied)
                        elif isinstance(hint_quality_exit_applied, str):
                            quality_exit_applied = hint_quality_exit_applied.strip().lower() in {
                                "1",
                                "true",
                                "yes",
                                "y",
                                "on",
                            }
                        hint_selective_extension_proof_bars = _to_int(
                            execution_hints.get("selective_extension_proof_bars")
                        )
                        if (
                            hint_selective_extension_proof_bars is not None
                            and hint_selective_extension_proof_bars >= 0
                        ):
                            selective_extension_proof_bars = int(hint_selective_extension_proof_bars)
                        hint_selective_extension_min_mfe_r = _to_float(
                            execution_hints.get("selective_extension_min_mfe_r")
                        )
                        if (
                            hint_selective_extension_min_mfe_r is not None
                            and hint_selective_extension_min_mfe_r >= 0.0
                        ):
                            selective_extension_min_mfe_r = float(hint_selective_extension_min_mfe_r)
                        hint_selective_extension_min_regime_strength = _to_float(
                            execution_hints.get("selective_extension_min_regime_strength")
                        )
                        if (
                            hint_selective_extension_min_regime_strength is not None
                            and hint_selective_extension_min_regime_strength >= 0.0
                        ):
                            selective_extension_min_regime_strength = float(
                                hint_selective_extension_min_regime_strength
                            )
                        hint_selective_extension_min_bias_strength = _to_float(
                            execution_hints.get("selective_extension_min_bias_strength")
                        )
                        if (
                            hint_selective_extension_min_bias_strength is not None
                            and hint_selective_extension_min_bias_strength >= 0.0
                        ):
                            selective_extension_min_bias_strength = float(
                                hint_selective_extension_min_bias_strength
                            )
                        hint_selective_extension_min_quality_score_v2 = _to_float(
                            execution_hints.get("selective_extension_min_quality_score_v2")
                        )
                        if (
                            hint_selective_extension_min_quality_score_v2 is not None
                            and hint_selective_extension_min_quality_score_v2 >= 0.0
                        ):
                            selective_extension_min_quality_score_v2 = float(
                                hint_selective_extension_min_quality_score_v2
                            )
                        hint_selective_extension_time_stop_bars = _to_int(
                            execution_hints.get("selective_extension_time_stop_bars")
                        )
                        if (
                            hint_selective_extension_time_stop_bars is not None
                            and hint_selective_extension_time_stop_bars >= 0
                        ):
                            selective_extension_time_stop_bars = int(
                                hint_selective_extension_time_stop_bars
                            )
                        hint_selective_extension_take_profit_r = _to_float(
                            execution_hints.get("selective_extension_take_profit_r")
                        )
                        if (
                            hint_selective_extension_take_profit_r is not None
                            and hint_selective_extension_take_profit_r >= 0.0
                        ):
                            selective_extension_take_profit_r = float(
                                hint_selective_extension_take_profit_r
                            )
                        hint_selective_extension_move_stop_to_be_at_r = _to_float(
                            execution_hints.get("selective_extension_move_stop_to_be_at_r")
                        )
                        if (
                            hint_selective_extension_move_stop_to_be_at_r is not None
                            and hint_selective_extension_move_stop_to_be_at_r >= 0.0
                        ):
                            selective_extension_move_stop_to_be_at_r = float(
                                hint_selective_extension_move_stop_to_be_at_r
                            )

                    expected_edge_pct, risk_pct = _entry_reward_and_risk_pct(
                        signal_side=signal_side,
                        entry_fill=float(entry_fill),
                        sl_tp=sl_tp if isinstance(sl_tp, dict) else None,
                        execution_hints=execution_hints if isinstance(execution_hints, dict) else None,
                    )
                    roundtrip_cost_pct = (
                        (2.0 * max(float(model.fee_bps), 0.0))
                        + (2.0 * max(float(model.slippage_bps), 0.0))
                    ) / 10000.0
                    funding_buffer_pct = (
                        max(float(model.funding_bps_per_8h), 0.0) / 10000.0
                    ) * 0.25
                    min_edge_pct = (roundtrip_cost_pct + funding_buffer_pct) * max(
                        float(min_expected_edge_over_roundtrip_cost),
                        0.0,
                    )
                    if expected_edge_pct <= min_edge_pct:
                        entry_block_counter["edge_cost_block"] += 1
                        continue

                    if effective_min_reward_risk_ratio > 0.0:
                        reward_pct = float(expected_edge_pct)
                        if reward_pct <= 0.0 or risk_pct <= 0.0:
                            entry_block_counter["reward_risk_missing_block"] += 1
                            continue

                        reward_risk_ratio = reward_pct / risk_pct
                        if reward_risk_ratio < effective_min_reward_risk_ratio:
                            entry_block_counter["reward_risk_block"] += 1
                            continue

                    decision_risk_per_trade_pct = _to_float(
                        (decision or {}).get("risk_per_trade_pct")
                    )
                    if decision_risk_per_trade_pct is None:
                        decision_risk_per_trade_pct = effective_default_risk_per_trade_pct
                    decision_risk_per_trade_pct = min(
                        max(float(decision_risk_per_trade_pct), 0.0),
                        1.0,
                    )
                    decision_max_effective_leverage = _to_float(
                        (decision or {}).get("max_effective_leverage")
                    )
                    if decision_max_effective_leverage is None:
                        decision_max_effective_leverage = effective_default_max_effective_leverage
                    decision_max_effective_leverage = max(
                        float(decision_max_effective_leverage),
                        1.0,
                    )
                    decision_reverse_exit_min_r = _to_float(
                        (decision or {}).get("reverse_exit_min_r")
                    )
                    if decision_reverse_exit_min_r is None:
                        decision_reverse_exit_min_r = effective_default_reverse_exit_min_r
                    decision_reverse_exit_min_r = max(float(decision_reverse_exit_min_r), 0.0)
                    decision_regime = str((decision or {}).get("regime") or "").strip().upper()

                    margin_used = 0.0
                    max_loss_cap: float | None = None
                    effective_leverage_used: float | None = None
                    initial_risk_abs: float | None = None
                    entry_equity = float(initial_capital) + realized_pnl
                    if entry_equity <= 0:
                        continue

                    if decision_risk_per_trade_pct > 0.0:
                        if sl is None or sl <= 0 or entry_fill <= 0:
                            entry_block_counter["risk_size_missing_stop_block"] += 1
                            continue
                        risk_per_unit = abs(entry_fill - float(sl))
                        if risk_per_unit <= 0.0:
                            entry_block_counter["risk_size_invalid_stop_block"] += 1
                            continue
                        risk_budget = entry_equity * decision_risk_per_trade_pct
                        qty = risk_budget / risk_per_unit
                        max_notional = entry_equity * decision_max_effective_leverage
                        if max_notional > 0 and entry_fill > 0:
                            qty = min(qty, max_notional / entry_fill)
                        qty = max(float(qty), 0.0)
                        if qty <= 0.0:
                            entry_block_counter["risk_size_invalid_qty_block"] += 1
                            continue
                        entry_notional_est = abs(entry_fill * qty)
                        margin_used = (
                            entry_notional_est / decision_max_effective_leverage
                            if decision_max_effective_leverage > 0
                            else 0.0
                        )
                        effective_leverage_used = (
                            entry_notional_est / entry_equity if entry_equity > 0 else 0.0
                        )
                        initial_risk_abs = risk_per_unit * qty
                        max_loss_cap = initial_risk_abs
                        if effective_margin_loss_fraction > 0.0 and margin_used > 0.0:
                            max_loss_cap = min(
                                max_loss_cap,
                                margin_used * effective_margin_loss_fraction,
                            )
                    elif fixed_leverage is not None and float(fixed_leverage) > 0 and entry_fill > 0:
                        margin_ratio = max(0.01, min(float(fixed_leverage_margin_use_pct), 1.0))
                        if peak_realized_equity > 0 and entry_equity < peak_realized_equity:
                            drawdown_pct = max(
                                (float(peak_realized_equity) - float(entry_equity))
                                / float(peak_realized_equity),
                                0.0,
                            )
                            if drawdown_pct > effective_drawdown_scale_start_pct:
                                if (
                                    effective_drawdown_scale_end_pct
                                    <= effective_drawdown_scale_start_pct
                                ):
                                    margin_ratio *= effective_drawdown_margin_scale_min
                                elif drawdown_pct >= effective_drawdown_scale_end_pct:
                                    margin_ratio *= effective_drawdown_margin_scale_min
                                else:
                                    progress = (
                                        drawdown_pct - effective_drawdown_scale_start_pct
                                    ) / (
                                        effective_drawdown_scale_end_pct
                                        - effective_drawdown_scale_start_pct
                                    )
                                    scale = 1.0 - (
                                        progress * (1.0 - effective_drawdown_margin_scale_min)
                                    )
                                    margin_ratio *= max(
                                        effective_drawdown_margin_scale_min,
                                        min(scale, 1.0),
                                    )
                        margin_used = max(entry_equity * margin_ratio, 0.0)
                        qty = (margin_used * float(fixed_leverage)) / entry_fill
                        if effective_margin_loss_fraction > 0.0:
                            max_loss_cap = margin_used * effective_margin_loss_fraction
                        else:
                            max_loss_cap = margin_used
                        effective_leverage_used = float(fixed_leverage)
                        if sl is not None and sl > 0:
                            initial_risk_abs = abs(entry_fill - float(sl)) * float(qty)

                    if initial_risk_abs is None and sl is not None and sl > 0 and entry_fill > 0 and qty > 0:
                        initial_risk_abs = abs(entry_fill - float(sl)) * float(qty)

                    entry_notional = abs(entry_fill * float(qty))
                    entry_fee = model.fee(notional=entry_notional)
                    realized_pnl -= entry_fee
                    total_fees += entry_fee
                    open_trade = _OpenTrade(
                        symbol=symbol,
                        side=signal_side,
                        entry_price=float(entry_fill),
                        quantity=float(qty),
                        initial_quantity=float(qty),
                        entry_fee=float(entry_fee),
                        entry_notional=float(entry_notional),
                        margin_used=float(margin_used),
                        max_loss_cap=max_loss_cap,
                        tp=tp,
                        sl=sl,
                        initial_risk_abs=initial_risk_abs,
                        time_stop_bars=time_stop_bars,
                        progress_check_bars=progress_check_bars,
                        progress_min_mfe_r=progress_min_mfe_r,
                        progress_extend_trigger_r=progress_extend_trigger_r,
                        progress_extend_bars=progress_extend_bars,
                        reverse_exit_min_r=decision_reverse_exit_min_r,
                        allow_reverse_exit=allow_reverse_exit,
                        effective_leverage=effective_leverage_used,
                        regime=(decision_regime or None),
                        entry_tick=tick,
                        entry_time_ms=candle.open_time_ms,
                        stop_exit_cooldown_bars=stop_exit_cooldown_bars,
                        loss_streak_trigger=loss_streak_trigger_hint,
                        loss_streak_cooldown_bars=loss_streak_cooldown_bars_hint,
                        profit_exit_cooldown_bars=profit_exit_cooldown_bars,
                        alpha_id=alpha_id,
                        entry_family=entry_family,
                        regime_lost_exit_required=regime_lost_exit_required,
                        tp_partial_ratio=tp_partial_ratio,
                        tp_partial_price=tp_partial_price,
                        tp_partial_at_r=tp_partial_at_r,
                        break_even_move_r=break_even_move_r,
                        runner_exit_mode=runner_exit_mode,
                        runner_trailing_atr_mult=runner_trailing_atr_mult,
                        stalled_trend_timeout_bars=stalled_trend_timeout_bars,
                        stalled_volume_ratio_floor=stalled_volume_ratio_floor,
                        entry_quality_score_v2=entry_quality_score_v2,
                        entry_regime_strength=entry_regime_strength,
                        entry_bias_strength=entry_bias_strength,
                        quality_exit_applied=quality_exit_applied,
                        selective_extension_proof_bars=selective_extension_proof_bars,
                        selective_extension_min_mfe_r=selective_extension_min_mfe_r,
                        selective_extension_min_regime_strength=selective_extension_min_regime_strength,
                        selective_extension_min_bias_strength=selective_extension_min_bias_strength,
                        selective_extension_min_quality_score_v2=(
                            selective_extension_min_quality_score_v2
                        ),
                        selective_extension_time_stop_bars=selective_extension_time_stop_bars,
                        selective_extension_take_profit_r=selective_extension_take_profit_r,
                        selective_extension_move_stop_to_be_at_r=(
                            selective_extension_move_stop_to_be_at_r
                        ),
                        peak_price=float(entry_fill) if signal_side == "BUY" else None,
                        trough_price=float(entry_fill) if signal_side == "SELL" else None,
                    )
                    day_trade_counter[day_key] = int(day_trade_counter.get(day_key, 0)) + 1

        unrealized = 0.0
        if open_trade is not None:
            qty = max(float(open_trade.quantity), 0.0)
            if open_trade.side == "BUY":
                unrealized = (close - open_trade.entry_price) * qty
            else:
                unrealized = (open_trade.entry_price - close) * qty
        equity_curve.append(float(initial_capital) + realized_pnl + unrealized)

    if open_trade is not None and candles_15m:
        _close_trade(
            tick=len(candles_15m) - 1,
            candle=candles_15m[-1],
            exit_price=float(candles_15m[-1].close),
            reason="end_of_data",
        )
        equity_curve[-1] = float(initial_capital) + realized_pnl

    gross_profit = sum(max(0.0, float(item["pnl"])) for item in trade_events)
    gross_loss = abs(sum(min(0.0, float(item["pnl"])) for item in trade_events))
    wins = sum(1 for item in trade_events if float(item["pnl"]) > 0)
    losses = sum(1 for item in trade_events if float(item["pnl"]) < 0)
    total_trades = len(trade_events)
    final_equity = float(initial_capital) + realized_pnl
    total_return_pct = (
        ((final_equity - float(initial_capital)) / float(initial_capital)) * 100.0
        if float(initial_capital) > 0
        else 0.0
    )
    profit_factor = float("inf") if gross_loss <= 0 and gross_profit > 0 else 0.0
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    win_rate_pct = ((wins / total_trades) * 100.0) if total_trades > 0 else 0.0
    max_drawdown_pct = _calc_max_drawdown_pct(equity_curve)
    alpha_block_distribution: dict[str, Counter[str]] = {}
    for row in rows:
        decision = row.get("decision")
        if not isinstance(decision, dict):
            continue
        alpha_blocks = decision.get("alpha_blocks")
        if not isinstance(alpha_blocks, dict):
            continue
        for alpha_id, reason in alpha_blocks.items():
            alpha_name = str(alpha_id or "").strip()
            reason_name = str(reason or "").strip()
            if not alpha_name or not reason_name:
                continue
            alpha_block_distribution.setdefault(alpha_name, Counter())[reason_name] += 1
    alpha_stats = _summarize_alpha_stats(
        trade_events=trade_events,
        alpha_block_distribution=alpha_block_distribution,
        initial_capital=float(initial_capital),
    )

    return {
        "symbol": symbol,
        "candles_15m": len(candles_15m),
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate_pct, 4),
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "gross_trade_pnl": round(gross_trade_pnl_total, 6),
        "total_fees": round(total_fees, 6),
        "total_funding_pnl": round(total_funding_pnl, 6),
        "net_profit": round(realized_pnl, 6),
        "profit_factor": None if profit_factor == float("inf") else round(profit_factor, 6),
        "profit_factor_infinite": bool(profit_factor == float("inf")),
        "max_drawdown_pct": round(max_drawdown_pct, 6),
        "initial_capital": float(initial_capital),
        "final_equity": round(final_equity, 6),
        "total_return_pct": round(total_return_pct, 6),
        "entry_block_distribution": dict(entry_block_counter),
        "alpha_block_distribution": {
            alpha_id: dict(counter) for alpha_id, counter in alpha_block_distribution.items()
        },
        "alpha_stats": alpha_stats,
        "trading_lock_reason": trading_lock_reason,
        "trade_events": trade_events,
    }


def _write_local_backtest_markdown(*, report_payload: dict[str, Any], target_json: Path) -> Path:
    backtest = report_payload.get("backtest")
    summary = report_payload.get("summary")
    symbols = report_payload.get("symbols")

    if not isinstance(backtest, dict):
        backtest = {}
    if not isinstance(summary, dict):
        summary = {}
    if not isinstance(symbols, list):
        symbols = []

    def _fmt(value: Any, digits: int = 2) -> str:
        number = _to_float(value)
        if number is None:
            return "-"
        return f"{number:.{digits}f}"

    def _top_entry_blocks(
        source: Any, *, limit: int = 6
    ) -> list[tuple[str, int]]:
        if not isinstance(source, dict):
            return []
        items: list[tuple[str, int]] = []
        for key, value in source.items():
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            items.append((str(key), count))
        items.sort(key=lambda item: (-item[1], item[0]))
        return items[: max(int(limit), 1)]

    top_entry_blocks = _top_entry_blocks(summary.get("entry_block_distribution"))
    alpha_stats = summary.get("alpha_stats")
    if not isinstance(alpha_stats, dict):
        alpha_stats = {}
    window_slices = summary.get("window_slices_6m")
    if not isinstance(window_slices, list):
        window_slices = []
    research_gate = summary.get("research_gate")
    if not isinstance(research_gate, dict):
        research_gate = {}

    md_path = target_json.with_suffix(".md")
    lines: list[str] = [
        "# 로컬 백테스트 리포트",
        "",
        f"- 생성시각: {report_payload.get('generated_at', '-')}",
        f"- Initial Capital (USDT): {_fmt(backtest.get('initial_capital_usdt', summary.get('total_initial_capital')))}",
        f"- 심볼: {', '.join(str(sym) for sym in backtest.get('symbols', [])) if isinstance(backtest.get('symbols'), list) else '-'}",
        f"- 기간(년): {backtest.get('years', '-')}",
        f"- 기간 구간(UTC): {backtest.get('backtest_start_utc', '-')} ~ {backtest.get('backtest_end_utc', '-')}",
        f"- 구간 모드: {backtest.get('window_mode', '-')}",
        f"- 포지션 사이징 모드: {backtest.get('position_sizing_mode', 'fixed_leverage')}",
        f"- 고정 레버리지: {backtest.get('fixed_leverage', '-')}x",
        f"- 포지션 증거금 사용률: {round(float(backtest.get('fixed_leverage_margin_use_pct', 0.0)) * 100, 2)}%",
        f"- 일일 손실 제한: {round(float(backtest.get('daily_loss_limit_pct', 0.0)) * 100, 2)}%",
        f"- 자본 보호 하한: {round(float(backtest.get('equity_floor_pct', 0.0)) * 100, 2)}%",
        f"- 트레이드 최대 손실 캡: {round(float(backtest.get('max_trade_margin_loss_fraction', 0.0)) * 100, 2)}% of margin",
        f"- 최소 시그널 점수: {round(float(backtest.get('min_signal_score', 0.0)), 3)}",
        f"- 역신호 청산 최소 수익률: {round(float(backtest.get('reverse_exit_min_profit_pct', 0.0)) * 100, 3)}%",
        f"- 역신호 청산 최소 점수: {round(float(backtest.get('reverse_exit_min_signal_score', 0.0)), 3)}",
        f"- 드로우다운 감속 시작: {round(float(backtest.get('drawdown_scale_start_pct', 0.0)) * 100, 2)}%",
        f"- 드로우다운 감속 최대: {round(float(backtest.get('drawdown_scale_end_pct', 0.0)) * 100, 2)}%",
        f"- 감속 최소 배수: {round(float(backtest.get('drawdown_margin_scale_min', 0.0)) * 100, 2)}%",
        f"- 손절 연속 트리거: {int(backtest.get('stoploss_streak_trigger', 0))}회",
        f"- 손절 쿨다운 봉수: {int(backtest.get('stoploss_cooldown_bars', 0))}봉",
        f"- 손실 후 쿨다운 봉수: {int(backtest.get('loss_cooldown_bars', 0))}봉",
        "",
        "## 전체 요약",
        "",
        "| 항목 | 값 |",
        "| --- | ---: |",
        f"| 초기 자본 (USDT) | {_fmt(summary.get('total_initial_capital'))} |",
        f"| 최종 자산 (USDT) | {_fmt(summary.get('total_final_equity'))} |",
        f"| 순손익 (USDT) | {_fmt(summary.get('total_net_profit'))} |",
        f"| 총 거래 손익 (USDT) | {_fmt(summary.get('gross_trade_pnl'))} |",
        f"| 총 수익액 (USDT) | {_fmt(summary.get('gross_profit'))} |",
        f"| 총 손실액 (USDT) | {_fmt(summary.get('gross_loss'))} |",
        f"| 총 수수료 (USDT) | {_fmt(summary.get('total_fees'))} |",
        f"| 수수료/총수익액 (%) | {_fmt(summary.get('fee_to_gross_profit_pct'))} |",
        f"| 수수료/총거래손익 (%) | {_fmt(summary.get('fee_to_trade_gross_pct'))} |",
        f"| 총 펀딩손익 (USDT) | {_fmt(summary.get('total_funding_pnl'))} |",
        f"| 총 수익률 (%) | {_fmt(summary.get('total_return_pct'))} |",
        f"| 승률 (%) | {_fmt(summary.get('win_rate_pct'))} |",
        f"| PF | {_fmt(summary.get('profit_factor'), digits=3)} |",
        f"| 최대 낙폭 (%) | {_fmt(summary.get('max_drawdown_pct'))} |",
        f"| 총 거래 수 | {int(summary.get('total_trades', 0)) if str(summary.get('total_trades', '')).isdigit() else summary.get('total_trades', '-')} |",
    ]

    if research_gate:
        checks = research_gate.get("checks")
        if not isinstance(checks, list):
            checks = []
        lines.extend(
            [
                "",
                "## 연구 게이트",
                "",
                f"- 트랙: {research_gate.get('track', '-')}",
                f"- 최종 판정: {research_gate.get('verdict', '-')}",
                "",
                "| 체크 | 판정 | 순손익 | PF | 최대낙폭(%) | 거래수 | 수수료/총거래손익(%) | 사유 |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        if checks:
            for item in checks:
                if not isinstance(item, dict):
                    continue
                metrics = item.get("metrics")
                if not isinstance(metrics, dict):
                    metrics = {}
                reasons = item.get("reasons")
                if isinstance(reasons, list) and reasons:
                    reason_text = ", ".join(str(reason) for reason in reasons)
                else:
                    reason_text = "-"
                lines.append(
                    "| "
                    + f"{item.get('name', '-')}"
                    + " | "
                    + f"{item.get('verdict', '-')}"
                    + " | "
                    + f"{_fmt(metrics.get('net_profit'))}"
                    + " | "
                    + f"{_fmt(metrics.get('profit_factor'), digits=3)}"
                    + " | "
                    + f"{_fmt(metrics.get('max_drawdown_pct'))}"
                    + " | "
                    + f"{int(metrics.get('trades', 0))}"
                    + " | "
                    + f"{_fmt(metrics.get('fee_to_trade_gross_pct'))}"
                    + " | "
                    + f"{reason_text}"
                    + " |"
                )
        else:
            lines.append("| 없음 | - | - | - | - | - | - | - |")

    lines.extend(
        [
            "",
            "## 진입 차단 상위 사유",
            "",
            "| 사유 | 횟수 |",
            "| --- | ---: |",
        ]
    )

    if top_entry_blocks:
        for reason, count in top_entry_blocks:
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("| 없음 | 0 |")

    lines.extend(
        [
            "",
            "## 알파별 요약",
            "",
            "| 알파 | 순손익(USDT) | PF | 거래수 | 최대낙폭(%) | 상위 차단사유 |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )

    if alpha_stats:
        for alpha_id, payload in sorted(alpha_stats.items()):
            if not isinstance(payload, dict):
                continue
            block_top = payload.get("block_top")
            block_summary = "-"
            if isinstance(block_top, list) and block_top:
                rendered: list[str] = []
                for item in block_top[:3]:
                    if not isinstance(item, dict):
                        continue
                    rendered.append(
                        f"{item.get('reason', '-')}: {int(item.get('count', 0))}"
                    )
                if rendered:
                    block_summary = ", ".join(rendered)
            lines.append(
                "| "
                + f"{alpha_id}"
                + " | "
                + f"{_fmt(payload.get('net_profit'))}"
                + " | "
                + f"{_fmt(payload.get('profit_factor'), digits=3)}"
                + " | "
                + f"{int(payload.get('trades', 0))}"
                + " | "
                + f"{_fmt(payload.get('max_drawdown_pct'))}"
                + " | "
                + f"{block_summary}"
                + " |"
            )
    else:
        lines.append("| 없음 | 0.00 | - | 0 | 0.00 | - |")

    portfolio_slots = summary.get("portfolio_open_slots_usage")
    bucket_blocks = summary.get("bucket_block_distribution")
    capital_utilization = summary.get("capital_utilization")
    simultaneous_hist = summary.get("simultaneous_position_histogram")
    if any(
        isinstance(item, dict) and item
        for item in (portfolio_slots, bucket_blocks, capital_utilization, simultaneous_hist)
    ):
        lines.extend(
            [
                "",
                "## 포트폴리오 요약",
                "",
                f"- 슬롯 사용 히스토그램: {portfolio_slots if isinstance(portfolio_slots, dict) else '-'}",
                f"- 버킷 차단 분포: {bucket_blocks if isinstance(bucket_blocks, dict) else '-'}",
                f"- 동시 포지션 히스토그램: {simultaneous_hist if isinstance(simultaneous_hist, dict) else '-'}",
                f"- 자본 활용도: {capital_utilization if isinstance(capital_utilization, dict) else '-'}",
            ]
        )

    lines.extend(
        [
            "",
            "## 6개월 구간 요약",
            "",
            "| 구간 | 순손익(USDT) | PF | 거래수 | 최대낙폭(%) |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )

    if window_slices:
        for item in window_slices:
            if not isinstance(item, dict):
                continue
            lines.append(
                "| "
                + f"{item.get('label', '-')}"
                + " | "
                + f"{_fmt(item.get('net_profit'))}"
                + " | "
                + f"{_fmt(item.get('profit_factor'), digits=3)}"
                + " | "
                + f"{int(item.get('trades', 0))}"
                + " | "
                + f"{_fmt(item.get('max_drawdown_pct'))}"
                + " |"
            )
    else:
        lines.append("| 없음 | 0.00 | - | 0 | 0.00 |")

    lines.extend(
        [
            "",
            "## 심볼별 요약",
            "",
            "| 심볼 | 순손익(USDT) | 수익률(%) | 승률(%) | 거래수 | 최대낙폭(%) |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )

    for item in symbols:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| "
            + f"{item.get('symbol', '-')}"
            + " | "
            + f"{_fmt(item.get('net_profit'))}"
            + " | "
            + f"{_fmt(item.get('total_return_pct'))}"
            + " | "
            + f"{_fmt(item.get('win_rate_pct'))}"
            + " | "
            + f"{int(item.get('total_trades', 0)) if str(item.get('total_trades', '')).isdigit() else item.get('total_trades', '-')}"
            + " | "
            + f"{_fmt(item.get('max_drawdown_pct'))}"
            + " |"
        )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def _local_backtest_strategy_runtime_params(
    *,
    active_strategy_name: str,
    alpha_squeeze_percentile_max: float,
    alpha_expansion_buffer_bps: float,
    alpha_expansion_range_atr_min: float,
    alpha_expansion_body_ratio_min: float,
    alpha_expansion_close_location_min: float,
    alpha_expansion_width_expansion_min: float,
    alpha_expansion_break_distance_atr_min: float,
    alpha_expansion_breakout_efficiency_min: float,
    alpha_expansion_breakout_stability_score_min: float,
    alpha_expansion_breakout_stability_edge_score_min: float,
    alpha_expansion_quality_score_min: float,
    alpha_expansion_quality_score_v2_min: float,
    alpha_min_volume_ratio: float,
    alpha_take_profit_r: float,
    alpha_time_stop_bars: int,
    alpha_trend_adx_min_4h: float,
    alpha_trend_adx_max_4h: float,
    alpha_trend_adx_rising_lookback_4h: int,
    alpha_trend_adx_rising_min_delta_4h: float,
    alpha_expected_move_cost_mult: float,
    fb_failed_break_buffer_bps: float,
    fb_wick_ratio_min: float,
    fb_take_profit_r: float,
    fb_time_stop_bars: int,
    cbr_squeeze_percentile_max: float,
    cbr_breakout_buffer_bps: float,
    cbr_take_profit_r: float,
    cbr_time_stop_bars: int,
    cbr_trend_adx_min_4h: float,
    cbr_ema_gap_trend_min_frac_4h: float,
    cbr_breakout_min_range_atr: float,
    cbr_breakout_min_volume_ratio: float,
    sfd_reclaim_sweep_buffer_bps: float,
    sfd_reclaim_wick_ratio_min: float,
    sfd_drive_breakout_range_atr_min: float,
    sfd_take_profit_r: float,
    pfd_premium_z_min: float,
    pfd_funding_24h_min: float,
    pfd_reclaim_buffer_atr: float,
    pfd_take_profit_r: float,
) -> dict[str, Any]:
    if str(active_strategy_name).startswith("ra_2026_alpha_v2"):
        return {
            "squeeze_percentile_threshold": min(
                max(float(alpha_squeeze_percentile_max), 0.05),
                0.95,
            ),
            "expansion_buffer_bps": max(float(alpha_expansion_buffer_bps), 0.0),
            "expansion_range_atr_min": max(float(alpha_expansion_range_atr_min), 0.0),
            "expansion_body_ratio_min": min(
                max(float(alpha_expansion_body_ratio_min), 0.0),
                1.0,
            ),
            "expansion_close_location_min": min(
                max(float(alpha_expansion_close_location_min), 0.0),
                1.0,
            ),
            "expansion_width_expansion_min": max(
                float(alpha_expansion_width_expansion_min),
                0.0,
            ),
            "expansion_break_distance_atr_min": max(
                float(alpha_expansion_break_distance_atr_min),
                0.0,
            ),
            "expansion_breakout_efficiency_min": max(
                float(alpha_expansion_breakout_efficiency_min),
                0.0,
            ),
            "expansion_breakout_stability_score_min": min(
                max(float(alpha_expansion_breakout_stability_score_min), 0.0),
                1.0,
            ),
            "expansion_breakout_stability_edge_score_min": min(
                max(float(alpha_expansion_breakout_stability_edge_score_min), 0.0),
                1.0,
            ),
            "expansion_quality_score_min": min(
                max(float(alpha_expansion_quality_score_min), 0.0),
                1.0,
            ),
            "expansion_quality_score_v2_min": min(
                max(float(alpha_expansion_quality_score_v2_min), 0.0),
                1.0,
            ),
            "min_volume_ratio_15m": max(float(alpha_min_volume_ratio), 0.0),
            "take_profit_r": max(float(alpha_take_profit_r), 0.5),
            "time_stop_bars": max(int(alpha_time_stop_bars), 1),
            "trend_adx_min_4h": max(float(alpha_trend_adx_min_4h), 0.0),
            "trend_adx_max_4h": max(float(alpha_trend_adx_max_4h), 0.0),
            "trend_adx_rising_lookback_4h": max(int(alpha_trend_adx_rising_lookback_4h), 0),
            "trend_adx_rising_min_delta_4h": max(
                float(alpha_trend_adx_rising_min_delta_4h),
                0.0,
            ),
            "expected_move_cost_mult": max(float(alpha_expected_move_cost_mult), 0.1),
        }
    return {}


def _local_backtest_profile_alpha_overrides(profile_name: str) -> dict[str, Any]:
    normalized = str(profile_name).strip().lower()
    mapping: dict[str, dict[str, Any]] = {
        "ra_2026_alpha_v2_expansion": {"enabled_alphas": ["alpha_expansion"]},
        "ra_2026_alpha_v2_expansion_verified_candidate": {
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.30,
            "expansion_buffer_bps": 2.0,
            "expansion_range_atr_min": 0.7,
            "expansion_body_ratio_min": 0.25,
            "expansion_close_location_min": 0.45,
            "expansion_width_expansion_min": 0.05,
            "min_volume_ratio_15m": 1.0,
            "take_profit_r": 2.0,
            "time_stop_bars": 18,
            "trend_adx_min_4h": 14.0,
            "expected_move_cost_mult": 1.6,
        },
        "ra_2026_alpha_v2_expansion_verified_q070": {
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.30,
            "expansion_buffer_bps": 2.0,
            "expansion_range_atr_min": 0.7,
            "expansion_body_ratio_min": 0.25,
            "expansion_close_location_min": 0.45,
            "expansion_width_expansion_min": 0.05,
            "min_volume_ratio_15m": 1.0,
            "take_profit_r": 2.0,
            "time_stop_bars": 18,
            "trend_adx_min_4h": 14.0,
            "expected_move_cost_mult": 1.6,
            "expansion_quality_score_v2_min": 0.70,
        },
        "ra_2026_alpha_v2_expansion_champion_candidate": {
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.30,
            "expansion_buffer_bps": 2.0,
            "expansion_range_atr_min": 0.7,
            "expansion_body_ratio_min": 0.25,
            "expansion_close_location_min": 0.45,
            "expansion_width_expansion_min": 0.05,
            "min_volume_ratio_15m": 1.0,
            "take_profit_r": 2.0,
            "time_stop_bars": 18,
            "trend_adx_min_4h": 14.0,
            "expected_move_cost_mult": 1.6,
            "expansion_quality_score_v2_min": 0.70,
        },
        "ra_2026_alpha_v2_expansion_candidate": {
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.30,
            "expansion_buffer_bps": 2.0,
            "expansion_range_atr_min": 0.7,
            "expansion_body_ratio_min": 0.25,
            "expansion_close_location_min": 0.45,
            "expansion_width_expansion_min": 0.05,
            "min_volume_ratio_15m": 1.0,
            "take_profit_r": 2.0,
            "time_stop_bars": 18,
            "trend_adx_min_4h": 14.0,
            "expected_move_cost_mult": 1.6,
        },
        "ra_2026_alpha_v2_expansion_live_candidate": {
            "enabled_alphas": ["alpha_expansion"],
            "squeeze_percentile_threshold": 0.30,
            "expansion_buffer_bps": 2.0,
            "expansion_range_atr_min": 0.7,
            "expansion_body_ratio_min": 0.25,
            "expansion_close_location_min": 0.45,
            "expansion_width_expansion_min": 0.05,
            "min_volume_ratio_15m": 1.0,
            "take_profit_r": 2.0,
            "time_stop_bars": 18,
            "trend_adx_min_4h": 14.0,
            "expected_move_cost_mult": 1.6,
        },
    }
    return dict(mapping.get(normalized, {}))


def _run_local_backtest_symbol_replay_worker(
    *,
    symbol: str,
    profile: str,
    mode: ModeName,
    env: EnvName,
    years: int,
    start_ms: int,
    end_ms: int,
    cache_root: str,
    sqlite_path: str,
    initial_capital: float,
    execution_model: _BacktestExecutionModel,
    fixed_leverage: float,
    fixed_leverage_margin_use_pct: float,
    reverse_min_hold_bars: int,
    reverse_cooldown_bars: int,
    min_expected_edge_over_roundtrip_cost: float,
    min_reward_risk_ratio: float,
    max_trades_per_day_per_symbol: int,
    daily_loss_limit_pct: float,
    equity_floor_pct: float,
    max_trade_margin_loss_fraction: float,
    min_signal_score: float,
    reverse_exit_min_profit_pct: float,
    reverse_exit_min_signal_score: float,
    default_reverse_exit_min_r: float,
    default_risk_per_trade_pct: float,
    default_max_effective_leverage: float,
    drawdown_scale_start_pct: float,
    drawdown_scale_end_pct: float,
    drawdown_margin_scale_min: float,
    stoploss_streak_trigger: int,
    stoploss_cooldown_bars: int,
    loss_cooldown_bars: int,
    alpha_squeeze_percentile_max: float,
    alpha_expansion_buffer_bps: float,
    alpha_expansion_range_atr_min: float,
    alpha_expansion_body_ratio_min: float,
    alpha_expansion_close_location_min: float,
    alpha_expansion_width_expansion_min: float,
    alpha_expansion_break_distance_atr_min: float,
    alpha_expansion_breakout_efficiency_min: float,
    alpha_expansion_breakout_stability_score_min: float,
    alpha_expansion_breakout_stability_edge_score_min: float,
    alpha_expansion_quality_score_min: float,
    alpha_expansion_quality_score_v2_min: float,
    alpha_min_volume_ratio: float,
    alpha_take_profit_r: float,
    alpha_time_stop_bars: int,
    alpha_trend_adx_min_4h: float,
    alpha_trend_adx_max_4h: float,
    alpha_trend_adx_rising_lookback_4h: int,
    alpha_trend_adx_rising_min_delta_4h: float,
    alpha_expected_move_cost_mult: float,
    fb_failed_break_buffer_bps: float,
    fb_wick_ratio_min: float,
    fb_take_profit_r: float,
    fb_time_stop_bars: int,
    cbr_squeeze_percentile_max: float,
    cbr_breakout_buffer_bps: float,
    cbr_take_profit_r: float,
    cbr_time_stop_bars: int,
    cbr_trend_adx_min_4h: float,
    cbr_ema_gap_trend_min_frac_4h: float,
    cbr_breakout_min_range_atr: float,
    cbr_breakout_min_volume_ratio: float,
    sfd_reclaim_sweep_buffer_bps: float,
    sfd_reclaim_wick_ratio_min: float,
    sfd_drive_breakout_range_atr_min: float,
    sfd_take_profit_r: float,
    pfd_premium_z_min: float,
    pfd_funding_24h_min: float,
    pfd_reclaim_buffer_atr: float,
    pfd_take_profit_r: float,
    market_intervals: list[str],
    max_peak_drawdown_pct: float | None,
) -> dict[str, Any]:
    cfg = load_effective_config(
        profile=profile,
        mode=mode,
        env=env,
        env_file_path=".env",
        config_path=None,
    )
    cache_dir = Path(cache_root)

    def _load_interval_cached_with_fallback(interval: str) -> list[_Kline15m]:
        cache_path = _cache_file_for_klines(
            cache_root=cache_dir,
            symbol=symbol,
            interval=interval,
            years=years,
        )
        strict_rows = _load_cached_klines_for_range(
            path=cache_path,
            interval=interval,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        if strict_rows:
            return strict_rows
        raw_rows = _read_klines_csv_rows(cache_path)
        if not raw_rows:
            return []
        fallback_rows = [
            row
            for row in raw_rows
            if int(row.open_time_ms) >= int(start_ms) and int(row.open_time_ms) < int(end_ms)
        ]
        if fallback_rows:
            return fallback_rows
        return raw_rows

    resolved_intervals: list[str] = []
    seen_intervals: set[str] = set()
    for raw in market_intervals:
        interval = str(raw).strip()
        if not interval or interval in seen_intervals:
            continue
        seen_intervals.add(interval)
        resolved_intervals.append(interval)
    if "15m" not in resolved_intervals:
        resolved_intervals.insert(0, "15m")

    candles_15m = _load_interval_cached_with_fallback("15m")
    market_candles: dict[str, list[_Kline15m]] = {}
    for interval in resolved_intervals:
        if interval == "15m":
            continue
        market_candles[interval] = _load_interval_cached_with_fallback(interval)
    premium_rows_15m = _load_local_backtest_cached_premium_for_symbol(
        cache_root=cache_dir,
        symbol=symbol,
        years=years,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    funding_rows = _load_local_backtest_cached_funding_for_symbol(
        cache_root=cache_dir,
        symbol=symbol,
        years=years,
        start_ms=start_ms,
        end_ms=end_ms,
    )

    if len(candles_15m) == 0:
        return {
            "symbol": symbol,
            "skipped": True,
            "reason": "no_cached_15m_candles",
        }

    storage = RuntimeStorage(sqlite_path=sqlite_path)
    storage.ensure_schema()
    state_store = EngineStateStore(storage=storage, mode=cfg.mode)
    state_store.set(mode=cfg.mode, status="RUNNING")

    collector = _ReplayDecision()
    provider = _HistoricalSnapshotProvider(
        symbol=symbol,
        candles_15m=candles_15m,
        market_candles=market_candles,
        premium_rows_15m=premium_rows_15m,
        funding_rows=funding_rows,
        market_intervals=resolved_intervals,
    )

    kernel = build_default_kernel(
        state_store=state_store,
        behavior=cfg.behavior,
        profile=cfg.profile,
        mode=cfg.mode,
        tick=0,
        dry_run=True,
        rest_client=None,
        snapshot_provider=provider,
        overheat_fetcher=None,
        journal_logger=collector,
        max_leverage=fixed_leverage,
    )
    enabled_strategies = [
        str(entry.name).strip()
        for entry in cfg.behavior.strategies
        if bool(getattr(entry, "enabled", False))
    ]
    active_strategy_name = enabled_strategies[0] if enabled_strategies else "none"
    strategy_runtime_params = _local_backtest_strategy_runtime_params(
        active_strategy_name=active_strategy_name,
        alpha_squeeze_percentile_max=float(alpha_squeeze_percentile_max),
        alpha_expansion_buffer_bps=float(alpha_expansion_buffer_bps),
        alpha_expansion_range_atr_min=float(alpha_expansion_range_atr_min),
        alpha_expansion_body_ratio_min=float(alpha_expansion_body_ratio_min),
        alpha_expansion_close_location_min=float(alpha_expansion_close_location_min),
        alpha_expansion_width_expansion_min=float(alpha_expansion_width_expansion_min),
        alpha_expansion_break_distance_atr_min=float(alpha_expansion_break_distance_atr_min),
        alpha_expansion_breakout_efficiency_min=float(alpha_expansion_breakout_efficiency_min),
        alpha_expansion_breakout_stability_score_min=float(
            alpha_expansion_breakout_stability_score_min
        ),
        alpha_expansion_breakout_stability_edge_score_min=float(
            alpha_expansion_breakout_stability_edge_score_min
        ),
        alpha_expansion_quality_score_min=float(alpha_expansion_quality_score_min),
        alpha_expansion_quality_score_v2_min=float(alpha_expansion_quality_score_v2_min),
        alpha_min_volume_ratio=float(alpha_min_volume_ratio),
        alpha_take_profit_r=float(alpha_take_profit_r),
        alpha_time_stop_bars=int(alpha_time_stop_bars),
        alpha_trend_adx_min_4h=float(alpha_trend_adx_min_4h),
        alpha_trend_adx_max_4h=float(alpha_trend_adx_max_4h),
        alpha_trend_adx_rising_lookback_4h=int(alpha_trend_adx_rising_lookback_4h),
        alpha_trend_adx_rising_min_delta_4h=float(alpha_trend_adx_rising_min_delta_4h),
        alpha_expected_move_cost_mult=float(alpha_expected_move_cost_mult),
        fb_failed_break_buffer_bps=float(fb_failed_break_buffer_bps),
        fb_wick_ratio_min=float(fb_wick_ratio_min),
        fb_take_profit_r=float(fb_take_profit_r),
        fb_time_stop_bars=int(fb_time_stop_bars),
        cbr_squeeze_percentile_max=float(cbr_squeeze_percentile_max),
        cbr_breakout_buffer_bps=float(cbr_breakout_buffer_bps),
        cbr_take_profit_r=float(cbr_take_profit_r),
        cbr_time_stop_bars=int(cbr_time_stop_bars),
        cbr_trend_adx_min_4h=float(cbr_trend_adx_min_4h),
        cbr_ema_gap_trend_min_frac_4h=float(cbr_ema_gap_trend_min_frac_4h),
        cbr_breakout_min_range_atr=float(cbr_breakout_min_range_atr),
        cbr_breakout_min_volume_ratio=float(cbr_breakout_min_volume_ratio),
        sfd_reclaim_sweep_buffer_bps=float(sfd_reclaim_sweep_buffer_bps),
        sfd_reclaim_wick_ratio_min=float(sfd_reclaim_wick_ratio_min),
        sfd_drive_breakout_range_atr_min=float(sfd_drive_breakout_range_atr_min),
        sfd_take_profit_r=float(sfd_take_profit_r),
        pfd_premium_z_min=float(pfd_premium_z_min),
        pfd_funding_24h_min=float(pfd_funding_24h_min),
        pfd_reclaim_buffer_atr=float(pfd_reclaim_buffer_atr),
        pfd_take_profit_r=float(pfd_take_profit_r),
    )
    if str(active_strategy_name).startswith("ra_2026_alpha_v2"):
        strategy_runtime_params.update(_local_backtest_profile_alpha_overrides(profile))
    if strategy_runtime_params:
        kernel.set_strategy_runtime_params(**strategy_runtime_params)
    bracket_planner = BracketPlanner(
        cfg=BracketConfig(
            take_profit_pct=cfg.behavior.tpsl.take_profit_pct,
            stop_loss_pct=cfg.behavior.tpsl.stop_loss_pct,
        )
    )

    rows: list[dict[str, Any]] = []
    state_counter: Counter[str] = Counter()
    for _tick in range(len(provider)):
        cycle = kernel.run_once()
        decision = collector.take()
        row, state = _build_local_backtest_cycle_input(
            cycle=cycle,
            decision=decision,
            bracket_planner=bracket_planner,
        )
        rows.append(row)
        state_counter[state] += 1

    per_symbol_report = _simulate_symbol_metrics(
        symbol=symbol,
        rows=rows,
        candles_15m=candles_15m,
        initial_capital=initial_capital,
        execution_model=execution_model,
        fixed_leverage=fixed_leverage,
        fixed_leverage_margin_use_pct=fixed_leverage_margin_use_pct,
        reverse_min_hold_bars=reverse_min_hold_bars,
        reverse_cooldown_bars=reverse_cooldown_bars,
        min_expected_edge_over_roundtrip_cost=min_expected_edge_over_roundtrip_cost,
        min_reward_risk_ratio=min_reward_risk_ratio,
        max_trades_per_day_per_symbol=max_trades_per_day_per_symbol,
        daily_loss_limit_pct=daily_loss_limit_pct,
        equity_floor_pct=equity_floor_pct,
        max_trade_margin_loss_fraction=max_trade_margin_loss_fraction,
        min_signal_score=min_signal_score,
        reverse_exit_min_profit_pct=reverse_exit_min_profit_pct,
        reverse_exit_min_signal_score=reverse_exit_min_signal_score,
        default_reverse_exit_min_r=default_reverse_exit_min_r,
        default_risk_per_trade_pct=default_risk_per_trade_pct,
        default_max_effective_leverage=default_max_effective_leverage,
        drawdown_scale_start_pct=drawdown_scale_start_pct,
        drawdown_scale_end_pct=drawdown_scale_end_pct,
        drawdown_margin_scale_min=drawdown_margin_scale_min,
        stoploss_streak_trigger=stoploss_streak_trigger,
        stoploss_cooldown_bars=stoploss_cooldown_bars,
        loss_cooldown_bars=loss_cooldown_bars,
        max_peak_drawdown_pct=max_peak_drawdown_pct,
    )
    interval_counts = {interval: len(rows) for interval, rows in market_candles.items()}
    interval_counts["15m"] = len(candles_15m)
    per_symbol_report["candles_by_interval"] = interval_counts
    per_symbol_report["candles_10m"] = int(interval_counts.get("10m", 0))
    per_symbol_report["candles_30m"] = int(interval_counts.get("30m", 0))
    per_symbol_report["candles_1h"] = int(interval_counts.get("1h", 0))
    per_symbol_report["candles_4h"] = int(interval_counts.get("4h", 0))
    per_symbol_report["premium_15m"] = len(premium_rows_15m)
    per_symbol_report["funding_events"] = len(funding_rows)
    per_symbol_report["cycles"] = len(rows)
    return {
        "symbol": symbol,
        "skipped": False,
        "report": per_symbol_report,
        "state_distribution": dict(state_counter),
        "total_cycles": len(rows),
    }


def _load_local_backtest_cached_market_for_symbol(
    *,
    cache_root: Path,
    symbol: str,
    years: int,
    start_ms: int,
    end_ms: int,
    market_intervals: list[str],
) -> dict[str, list[_Kline15m]]:
    def _load_interval_cached_with_fallback(interval: str) -> list[_Kline15m]:
        cache_path = _cache_file_for_klines(
            cache_root=cache_root,
            symbol=symbol,
            interval=interval,
            years=years,
        )
        strict_rows = _load_cached_klines_for_range(
            path=cache_path,
            interval=interval,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        if strict_rows:
            return strict_rows
        raw_rows = _read_klines_csv_rows(cache_path)
        if not raw_rows:
            return []
        fallback_rows = [
            row
            for row in raw_rows
            if int(row.open_time_ms) >= int(start_ms) and int(row.open_time_ms) < int(end_ms)
        ]
        if fallback_rows:
            return fallback_rows
        return raw_rows

    resolved_intervals: list[str] = []
    seen_intervals: set[str] = set()
    for raw in market_intervals:
        interval = str(raw).strip()
        if not interval or interval in seen_intervals:
            continue
        seen_intervals.add(interval)
        resolved_intervals.append(interval)
    if "15m" not in resolved_intervals:
        resolved_intervals.insert(0, "15m")

    candles_by_interval: dict[str, list[_Kline15m]] = {}
    for interval in resolved_intervals:
        candles_by_interval[interval] = _load_interval_cached_with_fallback(interval)
    return candles_by_interval


def _load_local_backtest_cached_premium_for_symbol(
    *,
    cache_root: Path,
    symbol: str,
    years: int,
    start_ms: int,
    end_ms: int,
) -> list[_Kline15m]:
    cache_path = _cache_file_for_premium(
        cache_root=cache_root,
        symbol=symbol,
        interval="15m",
        years=years,
    )
    strict_rows = _load_cached_premium_for_range(
        path=cache_path,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    if strict_rows:
        return strict_rows
    raw_rows = _read_klines_csv_rows(cache_path)
    if not raw_rows:
        return []
    fallback_rows = [
        row
        for row in raw_rows
        if int(row.open_time_ms) >= int(start_ms) and int(row.open_time_ms) < int(end_ms)
    ]
    if fallback_rows:
        return fallback_rows
    return raw_rows


def _load_local_backtest_cached_funding_for_symbol(
    *,
    cache_root: Path,
    symbol: str,
    years: int,
    start_ms: int,
    end_ms: int,
) -> list[_FundingRateRow]:
    cache_path = _cache_file_for_funding(
        cache_root=cache_root,
        symbol=symbol,
        years=years,
    )
    strict_rows = _load_cached_funding_for_range(
        path=cache_path,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    if strict_rows:
        return strict_rows
    raw_rows = _read_funding_csv_rows(cache_path)
    if not raw_rows:
        return []
    fallback_rows = [
        row
        for row in raw_rows
        if int(row.funding_time_ms) >= int(start_ms) and int(row.funding_time_ms) < int(end_ms)
    ]
    if fallback_rows:
        return fallback_rows
    return raw_rows


def _candidate_to_payload(candidate: Candidate) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "side": candidate.side,
        "score": float(candidate.score),
        "raw_score": candidate.raw_score,
        "portfolio_score": candidate.portfolio_score,
        "portfolio_bucket": candidate.portfolio_bucket,
        "volume_quality": candidate.volume_quality,
        "edge_efficiency": candidate.edge_efficiency,
        "spread_penalty": candidate.spread_penalty,
        "correlation_penalty": candidate.correlation_penalty,
        "alpha_id": candidate.alpha_id,
        "entry_family": candidate.entry_family,
        "reason": candidate.reason,
        "entry_price": candidate.entry_price,
        "stop_price_hint": candidate.stop_price_hint,
        "stop_distance_frac": candidate.stop_distance_frac,
        "regime_hint": candidate.regime_hint,
        "regime_strength": candidate.regime_strength,
        "risk_per_trade_pct": candidate.risk_per_trade_pct,
        "max_effective_leverage": candidate.max_effective_leverage,
        "expected_move_frac": candidate.expected_move_frac,
        "required_move_frac": candidate.required_move_frac,
        "spread_pct": candidate.spread_pct,
    }


def _candidate_from_payload(payload: dict[str, Any]) -> Candidate | None:
    symbol = str(payload.get("symbol") or "").strip().upper()
    side = str(payload.get("side") or "").strip().upper()
    score = _to_float(payload.get("score"))
    if not symbol or side not in {"BUY", "SELL"} or score is None:
        return None
    return Candidate(
        symbol=symbol,
        side="BUY" if side == "BUY" else "SELL",
        score=float(score),
        raw_score=_to_float(payload.get("raw_score")),
        portfolio_score=_to_float(payload.get("portfolio_score")),
        portfolio_bucket=str(
            payload.get("portfolio_bucket") or portfolio_bucket_for_symbol(symbol)
        ).strip(),
        volume_quality=_to_float(payload.get("volume_quality")),
        edge_efficiency=_to_float(payload.get("edge_efficiency")),
        spread_penalty=_to_float(payload.get("spread_penalty")),
        correlation_penalty=_to_float(payload.get("correlation_penalty")),
        alpha_id=str(payload.get("alpha_id") or "").strip() or None,
        entry_family=str(payload.get("entry_family") or "").strip() or None,
        reason=str(payload.get("reason") or "").strip() or None,
        entry_price=_to_float(payload.get("entry_price")),
        stop_price_hint=_to_float(payload.get("stop_price_hint")),
        stop_distance_frac=_to_float(payload.get("stop_distance_frac")),
        regime_hint=str(payload.get("regime_hint") or "").strip().upper() or None,
        regime_strength=_to_float(payload.get("regime_strength")),
        risk_per_trade_pct=_to_float(payload.get("risk_per_trade_pct")),
        max_effective_leverage=_to_float(payload.get("max_effective_leverage")),
        expected_move_frac=_to_float(payload.get("expected_move_frac")),
        required_move_frac=_to_float(payload.get("required_move_frac")),
        spread_pct=_to_float(payload.get("spread_pct")),
    )


def _portfolio_history_limit(params: dict[str, Any] | None) -> int:
    source = params or {}

    def _ival(name: str, default: int) -> int:
        value = _to_int(source.get(name))
        return max(int(value if value is not None else default), 1)

    required_4h = max(_ival("ema_slow_4h", 200) + 5, _ival("adx_period_4h", 14) + 5)
    required_1h = max(_ival("ema_bias_period_1h", 34) + 5, _ival("rsi_period_1h", 14) + 5)
    bb_period_15m = _ival("bb_period_15m", 20)
    premium_lookback_3d = _ival("premium_zscore_lookback_3d_15m", 288)
    required_15m = max(
        _ival("atr_period_15m", 14) + 5,
        _ival("ema_pullback_period_15m", 20) + 5,
        _ival("donchian_period_15m", 20) + 2,
        bb_period_15m + 5,
        _ival("volume_sma_period_15m", 20) + 2,
        _ival("swing_lookback_15m", 8) + 2,
        _ival("squeeze_lookback_15m", 48) + bb_period_15m + 2,
        premium_lookback_3d + 2,
    )
    return max(required_4h, required_1h, required_15m) + 8


def _build_local_backtest_portfolio_rows(
    *,
    cfg: EffectiveConfig,
    candles_by_symbol: dict[str, dict[str, list[_Kline15m]]],
    premium_by_symbol: dict[str, list[_Kline15m]] | None = None,
    funding_by_symbol: dict[str, list[_FundingRateRow]] | None = None,
    market_intervals: list[str],
    strategy_runtime_params: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    provider = _HistoricalPortfolioSnapshotProvider(
        candles_by_symbol=candles_by_symbol,
        premium_by_symbol=premium_by_symbol,
        funding_by_symbol=funding_by_symbol,
        market_intervals=market_intervals,
        history_limit=_portfolio_history_limit(strategy_runtime_params),
    )
    strategy = _build_strategy_selector(
        behavior=cfg.behavior,
        snapshot_provider=provider,
        overheat_fetcher=None,
        journal_logger=None,
    )
    runtime_updater = getattr(strategy, "set_runtime_params", None)
    if callable(runtime_updater) and strategy_runtime_params:
        runtime_updater(**strategy_runtime_params)

    symbols = sorted(
        str(symbol).strip().upper() for symbol in candles_by_symbol.keys() if str(symbol).strip()
    )

    def _candidate_from_decision(symbol: str, decision: dict[str, Any]) -> Candidate | None:
        intent = str(decision.get("intent") or "NONE").upper()
        side = str(decision.get("side") or "NONE").upper()
        if intent not in {"LONG", "SHORT"} or side not in {"BUY", "SELL"}:
            return None

        score = _to_float(decision.get("score")) or 0.0
        if score <= 0.0:
            return None

        indicators = decision.get("indicators")
        atr_hint = None
        spread_pct = None
        if isinstance(indicators, dict):
            atr_hint = _to_float(indicators.get("atr14_15m"))
            spread_bps = _to_float(indicators.get("spread_estimate_bps"))
            if spread_bps is not None:
                spread_pct = float(spread_bps) / 100.0

        return Candidate(
            symbol=symbol,
            side="BUY" if side == "BUY" else "SELL",
            score=float(score),
            alpha_id=str(decision.get("alpha_id") or "").strip() or None,
            entry_family=str(decision.get("entry_family") or "").strip() or None,
            reason=str(decision.get("reason") or "entry_signal"),
            source=str(getattr(strategy, "name", "strategy")),
            entry_price=_to_float(decision.get("entry_price")),
            stop_price_hint=_to_float(decision.get("stop_price_hint")),
            stop_distance_frac=_to_float(decision.get("stop_distance_frac")),
            volatility_hint=atr_hint,
            regime_hint=str(decision.get("regime") or "").strip().upper() or None,
            regime_strength=_to_float(decision.get("regime_strength")),
            risk_per_trade_pct=_to_float(decision.get("risk_per_trade_pct")),
            max_effective_leverage=_to_float(decision.get("max_effective_leverage")),
            expected_move_frac=_to_float(decision.get("expected_move_frac")),
            required_move_frac=_to_float(decision.get("required_move_frac")),
            spread_pct=spread_pct,
        )

    def _no_candidate_reason(skipped: dict[str, str]) -> str:
        if not skipped:
            return "no_candidate"
        ordered = sorted((symbol, str(reason or "no_candidate")) for symbol, reason in skipped.items())
        reasons = sorted({reason for _, reason in ordered})
        if len(reasons) == 1:
            return reasons[0]
        snippet = ";".join(f"{symbol}:{reason}" for symbol, reason in ordered[:3])
        return f"no_candidate_multi:{snippet}"

    rows: list[dict[str, Any]] = []
    state_counter: Counter[str] = Counter()
    total_ticks = len(provider)
    replay_started_at = time.monotonic()
    last_progress_pct = -5

    for tick in range(total_ticks):
        snapshot = provider()
        if not snapshot:
            continue
        symbols_market = snapshot.get("symbols")
        decisions: dict[str, dict[str, Any]] = {}
        ranked: list[Candidate] = []
        skipped: dict[str, str] = {}

        for symbol in symbols:
            symbol_snapshot = dict(snapshot)
            if isinstance(symbols_market, dict):
                market = symbols_market.get(symbol)
                if isinstance(market, dict):
                    symbol_snapshot["market"] = market
            symbol_snapshot["symbol"] = symbol

            decision = strategy.decide(symbol_snapshot) if strategy is not None else {}
            if not isinstance(decision, dict):
                decision = {}
            decisions[symbol] = _ReplayDecisionBySymbol._compact_payload(decision)

            candidate = _candidate_from_decision(symbol, decision)
            if candidate is not None:
                ranked.append(candidate)
                continue
            skipped[symbol] = str(decision.get("reason") or "no_candidate")

        ranked.sort(
            key=lambda item: (
                float(item.portfolio_score if item.portfolio_score is not None else item.score),
                float(item.score),
            ),
            reverse=True,
        )
        state = "dry_run" if ranked else "no_candidate"
        reason = "would_execute" if ranked else _no_candidate_reason(skipped)
        rows.append(
            {
                "tick": tick,
                "timestamp": snapshot.get("timestamp"),
                "open_time": snapshot.get("open_time"),
                "close_time": snapshot.get("close_time"),
                "state": state,
                "reason": reason,
                "ranked_candidates": [_candidate_to_payload(item) for item in ranked],
                "decisions": decisions,
                "candles": snapshot.get("candles") or {},
            }
        )
        state_counter[state] += 1
        if total_ticks > 0:
            progress_pct = int(((tick + 1) / total_ticks) * 100.0)
            rounded_pct = min((progress_pct // 5) * 5, 100)
            if rounded_pct >= last_progress_pct + 5 or tick + 1 == total_ticks:
                elapsed_sec = int(time.monotonic() - replay_started_at)
                print(
                    "[LOCAL_BACKTEST] portfolio replay "
                    f"{rounded_pct}% ({tick + 1}/{total_ticks}) elapsed={elapsed_sec}s"
                )
                last_progress_pct = rounded_pct
    return rows, state_counter


def _simulate_portfolio_metrics(
    *,
    rows: list[dict[str, Any]],
    initial_capital: float,
    execution_model: _BacktestExecutionModel,
    fixed_leverage_margin_use_pct: float,
    max_open_positions: int,
    max_new_entries_per_tick: int,
    reverse_cooldown_bars: int,
    max_trades_per_day_per_symbol: int,
    min_expected_edge_over_roundtrip_cost: float = 0.0,
    min_reward_risk_ratio: float = 0.0,
    daily_loss_limit_pct: float = 0.0,
    equity_floor_pct: float = 0.0,
    max_trade_margin_loss_fraction: float = 1.0,
    min_signal_score: float = 0.0,
    drawdown_scale_start_pct: float,
    drawdown_scale_end_pct: float,
    drawdown_margin_scale_min: float,
) -> dict[str, Any]:
    open_trades: dict[str, _OpenTrade] = {}
    realized_pnl = 0.0
    total_fees = 0.0
    total_funding_pnl = 0.0
    gross_trade_pnl_total = 0.0
    trade_events: list[dict[str, Any]] = []
    equity_curve: list[float] = []
    entry_block_counter: Counter[str] = Counter()
    bucket_block_counter: Counter[str] = Counter()
    simultaneous_position_histogram: Counter[int] = Counter()
    portfolio_open_slots_usage: Counter[int] = Counter()
    state_distribution: Counter[str] = Counter()
    day_trade_counter: dict[tuple[int, str], int] = {}
    last_exit_tick_by_symbol: dict[str, int] = {}
    last_exit_cooldown_bars_by_symbol: dict[str, int] = {}
    capital_utilization_samples: list[float] = []
    peak_realized_equity = float(initial_capital)
    day_ms = 24 * 60 * 60 * 1000
    effective_min_expected_edge = max(float(min_expected_edge_over_roundtrip_cost), 0.0)
    effective_min_reward_risk_ratio = max(float(min_reward_risk_ratio), 0.0)
    effective_daily_loss_limit_pct = max(float(daily_loss_limit_pct), 0.0)
    effective_equity_floor_pct = max(float(equity_floor_pct), 0.0)
    effective_max_trade_margin_loss_fraction = max(
        0.0,
        min(float(max_trade_margin_loss_fraction), 1.0),
    )
    effective_min_signal_score = max(float(min_signal_score), 0.0)
    current_day_key: int | None = None
    day_start_equity = float(initial_capital)
    day_locked = False
    trading_locked = False
    trading_lock_reason: str | None = None

    def _record_reason(reason: str) -> None:
        normalized = str(reason or "").strip()
        if not normalized:
            return
        if normalized.startswith("no_candidate_multi:"):
            detail = normalized.split(":", 1)[1]
            for token in detail.split(";"):
                item = str(token).strip()
                if not item:
                    continue
                _, _, reason_part = item.partition(":")
                entry_block_counter[str(reason_part or "no_candidate").strip()] += 1
            return
        if normalized not in {"no_candidate", "would_execute"}:
            entry_block_counter[normalized] += 1

    def _drawdown_scale(entry_equity: float) -> float:
        if peak_realized_equity <= 0.0 or entry_equity >= peak_realized_equity:
            return 1.0
        dd_pct = max((peak_realized_equity - entry_equity) / peak_realized_equity, 0.0)
        if dd_pct <= float(drawdown_scale_start_pct):
            return 1.0
        if float(drawdown_scale_end_pct) <= float(drawdown_scale_start_pct):
            return float(drawdown_margin_scale_min)
        if dd_pct >= float(drawdown_scale_end_pct):
            return float(drawdown_margin_scale_min)
        progress = (dd_pct - float(drawdown_scale_start_pct)) / max(
            float(drawdown_scale_end_pct) - float(drawdown_scale_start_pct),
            1e-9,
        )
        return max(
            float(drawdown_margin_scale_min),
            1.0 - (progress * (1.0 - float(drawdown_margin_scale_min))),
        )

    def _close_trade(*, trade: _OpenTrade, candle: dict[str, float], tick: int, reason: str) -> None:
        nonlocal realized_pnl, total_fees, total_funding_pnl, gross_trade_pnl_total
        qty = max(float(trade.quantity), 0.0)
        if qty <= 0.0:
            return
        close_time_ms = int(candle.get("close_time_ms", 0.0))
        raw_exit_price = float(candle["close"])
        if reason == "stop_loss" and trade.sl is not None:
            raw_exit_price = float(trade.sl)
        elif reason == "take_profit" and trade.tp is not None:
            raw_exit_price = float(trade.tp)
        exit_fill = execution_model.filled_exit_price(side=trade.side, raw_price=raw_exit_price)
        gross_pnl = (
            (exit_fill - float(trade.entry_price)) * qty
            if trade.side == "BUY"
            else (float(trade.entry_price) - exit_fill) * qty
        )
        exit_notional = abs(exit_fill * qty)
        exit_fee = execution_model.fee(notional=exit_notional)
        funding_pnl = execution_model.funding_pnl(
            side=trade.side,
            entry_time_ms=int(trade.entry_time_ms),
            exit_time_ms=close_time_ms,
            notional=float(trade.entry_notional),
        )
        net_pnl = gross_pnl - exit_fee + funding_pnl
        if trade.max_loss_cap is not None:
            trade_floor = -float(trade.max_loss_cap) + float(trade.entry_fee)
            if net_pnl < trade_floor:
                net_pnl = trade_floor

        realized_pnl += net_pnl
        total_fees += exit_fee
        total_funding_pnl += funding_pnl
        gross_trade_pnl_total += gross_pnl
        trade_events.append(
            {
                "symbol": trade.symbol,
                "side": "LONG" if trade.side == "BUY" else "SHORT",
                "regime": trade.regime,
                "alpha_id": trade.alpha_id,
                "entry_price": trade.entry_price,
                "exit_price": exit_fill,
                "quantity": float(trade.initial_quantity),
                "gross_pnl": gross_pnl,
                "entry_fee": trade.entry_fee,
                "exit_fee": exit_fee,
                "funding_pnl": funding_pnl,
                "pnl": net_pnl,
                "entry_tick": trade.entry_tick,
                "exit_tick": tick,
                "entry_time_ms": trade.entry_time_ms,
                "exit_time_ms": close_time_ms,
                "initial_risk_abs": trade.initial_risk_abs,
                "effective_leverage": trade.effective_leverage,
                "entry_family": trade.entry_family,
                "partial_taken": False,
                "reason": reason,
            }
        )
        last_exit_tick_by_symbol[trade.symbol] = int(tick)
        last_exit_cooldown_bars_by_symbol[trade.symbol] = max(int(trade.stop_exit_cooldown_bars), 0)
        open_trades.pop(trade.symbol, None)

    def _liquidate_open_trades(*, candles: dict[str, Any], tick: int, reason: str) -> None:
        for symbol in list(open_trades.keys()):
            trade = open_trades.get(symbol)
            candle = candles.get(symbol)
            if trade is None or not isinstance(candle, dict):
                continue
            _close_trade(trade=trade, candle=candle, tick=tick, reason=reason)

    for row in rows:
        tick = int(row.get("tick", 0))
        state_distribution[str(row.get("state") or "unknown")] += 1
        candles = row.get("candles")
        if not isinstance(candles, dict):
            candles = {}
        day_key = int(float(row.get("open_time") or 0.0) // day_ms)
        if current_day_key != day_key:
            current_day_key = day_key
            day_start_equity = max(float(initial_capital) + realized_pnl, 0.0)
            day_locked = False

        for symbol in list(open_trades.keys()):
            trade = open_trades.get(symbol)
            candle = candles.get(symbol)
            if trade is None or not isinstance(candle, dict):
                continue
            high = float(candle.get("high", candle.get("close", trade.entry_price)))
            low = float(candle.get("low", candle.get("close", trade.entry_price)))
            close = float(candle.get("close", trade.entry_price))
            held_bars = int(tick) - int(trade.entry_tick)
            if trade.side == "BUY":
                if trade.sl is not None and low <= float(trade.sl):
                    _close_trade(trade=trade, candle=candle, tick=tick, reason="stop_loss")
                elif trade.tp is not None and high >= float(trade.tp):
                    _close_trade(trade=trade, candle=candle, tick=tick, reason="take_profit")
                elif trade.time_stop_bars is not None and held_bars >= int(trade.time_stop_bars):
                    _close_trade(
                        trade=trade,
                        candle={**candle, "close": close},
                        tick=tick,
                        reason="time_stop",
                    )
            else:
                if trade.sl is not None and high >= float(trade.sl):
                    _close_trade(trade=trade, candle=candle, tick=tick, reason="stop_loss")
                elif trade.tp is not None and low <= float(trade.tp):
                    _close_trade(trade=trade, candle=candle, tick=tick, reason="take_profit")
                elif trade.time_stop_bars is not None and held_bars >= int(trade.time_stop_bars):
                    _close_trade(
                        trade=trade,
                        candle={**candle, "close": close},
                        tick=tick,
                        reason="time_stop",
                    )

        entry_equity = float(initial_capital) + realized_pnl
        peak_realized_equity = max(float(peak_realized_equity), float(entry_equity))
        if (
            not day_locked
            and effective_daily_loss_limit_pct > 0.0
            and float(day_start_equity) > 0.0
            and float(entry_equity)
            <= float(day_start_equity) * (1.0 - float(effective_daily_loss_limit_pct))
        ):
            day_locked = True

        if (
            not trading_locked
            and effective_equity_floor_pct > 0.0
            and float(entry_equity) <= float(initial_capital) * float(effective_equity_floor_pct)
        ):
            _liquidate_open_trades(candles=candles, tick=tick, reason="equity_floor_stop")
            trading_locked = True
            trading_lock_reason = "equity_floor"
            entry_equity = float(initial_capital) + realized_pnl

        ranked_candidates = row.get("ranked_candidates")
        decisions = row.get("decisions")
        if not isinstance(ranked_candidates, list):
            ranked_candidates = []
        if not isinstance(decisions, dict):
            decisions = {}

        if not ranked_candidates:
            _record_reason(str(row.get("reason") or "no_candidate"))

        open_symbols = {str(symbol).strip().upper() for symbol in open_trades.keys()}
        ranked_objects: list[Candidate] = []
        for item in ranked_candidates:
            if isinstance(item, Candidate):
                ranked_objects.append(item)
                continue
            if isinstance(item, dict):
                candidate = _candidate_from_payload(item)
                if candidate is not None:
                    ranked_objects.append(candidate)
        routing = route_ranked_candidates(
            candidates=ranked_objects,
            open_symbols=open_symbols,
            allow_reentry=False,
            config=PortfolioRoutingConfig(
                max_open_positions=int(max_open_positions),
                max_new_entries_per_tick=int(max_new_entries_per_tick),
            ),
        )
        for blocked_candidate, blocked_reason in routing.blocked:
            entry_block_counter[blocked_reason] += 1
            if blocked_reason == "portfolio_bucket_cap":
                bucket_block_counter[
                    blocked_candidate.portfolio_bucket
                    or portfolio_bucket_for_symbol(blocked_candidate.symbol)
                ] += 1

        free_slots = len(routing.selected)
        for candidate in routing.selected:
            symbol = str(candidate.symbol).strip().upper()
            decision = decisions.get(symbol)
            if not isinstance(decision, dict):
                decision = {}

            cooldown_bars = int(last_exit_cooldown_bars_by_symbol.get(symbol, max(int(reverse_cooldown_bars), 0)))
            if int(tick) - int(last_exit_tick_by_symbol.get(symbol, -(10**9))) < cooldown_bars:
                entry_block_counter["reverse_cooldown_block"] += 1
                continue

            if max(int(max_trades_per_day_per_symbol), 0) > 0 and day_trade_counter.get(
                (day_key, symbol),
                0,
            ) >= int(max_trades_per_day_per_symbol):
                entry_block_counter["daily_trade_cap_block"] += 1
                continue

            if trading_locked:
                entry_block_counter[f"{trading_lock_reason or 'equity_floor'}_block"] += 1
                continue
            if day_locked:
                entry_block_counter["daily_loss_stop_block"] += 1
                continue

            signal_side = str(candidate.side or decision.get("side") or "NONE").upper()
            if signal_side not in {"BUY", "SELL"}:
                entry_block_counter["entry_side_missing_block"] += 1
                continue
            signal_score = float(candidate.portfolio_score or candidate.score)
            if signal_score < effective_min_signal_score:
                entry_block_counter["score_quality_block"] += 1
                continue

            entry_price = _to_float(candidate.entry_price)
            if entry_price is None or entry_price <= 0.0:
                entry_price = _to_float(decision.get("entry_price"))
            if entry_price is None or entry_price <= 0.0:
                candle = candles.get(symbol)
                if isinstance(candle, dict):
                    entry_price = _to_float(candle.get("close"))
            if entry_price is None or entry_price <= 0.0:
                entry_block_counter["price_unavailable"] += 1
                continue

            sl_tp = decision.get("sl_tp") if isinstance(decision.get("sl_tp"), dict) else {}
            tp = _to_float(sl_tp.get("take_profit"))
            sl = _to_float(sl_tp.get("stop_loss"))
            if sl is None or sl <= 0.0:
                entry_block_counter["risk_size_missing_stop_block"] += 1
                continue

            entry_fill = execution_model.filled_entry_price(side=signal_side, raw_price=float(entry_price))
            expected_edge_pct, risk_pct = _entry_reward_and_risk_pct(
                signal_side=signal_side,
                entry_fill=float(entry_fill),
                sl_tp=sl_tp if isinstance(sl_tp, dict) else None,
                execution_hints=decision.get("execution")
                if isinstance(decision.get("execution"), dict)
                else None,
            )
            roundtrip_cost_pct = (
                (2.0 * max(float(execution_model.fee_bps), 0.0))
                + (2.0 * max(float(execution_model.slippage_bps), 0.0))
            ) / 10000.0
            funding_buffer_pct = (
                max(float(execution_model.funding_bps_per_8h), 0.0) / 10000.0
            ) * 0.25
            min_edge_pct = (roundtrip_cost_pct + funding_buffer_pct) * effective_min_expected_edge
            if expected_edge_pct <= min_edge_pct:
                entry_block_counter["edge_cost_block"] += 1
                continue
            if effective_min_reward_risk_ratio > 0.0:
                if expected_edge_pct <= 0.0 or risk_pct <= 0.0:
                    entry_block_counter["reward_risk_missing_block"] += 1
                    continue
                reward_risk_ratio = expected_edge_pct / risk_pct
                if reward_risk_ratio < effective_min_reward_risk_ratio:
                    entry_block_counter["reward_risk_block"] += 1
                    continue
            risk_per_unit = abs(entry_fill - float(sl))
            if risk_per_unit <= 0.0:
                entry_block_counter["risk_size_invalid_stop_block"] += 1
                continue

            total_margin_used = sum(float(trade.margin_used) for trade in open_trades.values())
            available_margin = max(float(entry_equity) - float(total_margin_used), 0.0)
            if available_margin <= 0.0:
                entry_block_counter["capital_unavailable_block"] += 1
                continue

            risk_per_trade_pct = max(
                _to_float(candidate.risk_per_trade_pct)
                or _to_float(decision.get("risk_per_trade_pct"))
                or 0.0,
                0.0,
            )
            max_effective_leverage = max(
                _to_float(candidate.max_effective_leverage)
                or _to_float(decision.get("max_effective_leverage"))
                or 1.0,
                1.0,
            )
            scale = _drawdown_scale(float(entry_equity))
            risk_budget = float(entry_equity) * float(risk_per_trade_pct) * float(scale)
            qty = risk_budget / risk_per_unit if risk_budget > 0.0 else 0.0
            max_notional = float(entry_equity) * float(max_effective_leverage) * float(scale)
            if max_notional > 0.0:
                qty = min(qty, max_notional / float(entry_fill))
            qty = max(float(qty), 0.0)
            if qty <= 0.0:
                entry_block_counter["risk_size_invalid_qty_block"] += 1
                continue

            entry_notional = abs(float(entry_fill) * float(qty))
            margin_used = (
                entry_notional / float(max_effective_leverage) if float(max_effective_leverage) > 0.0 else 0.0
            )
            if margin_used > available_margin and margin_used > 0.0:
                scale_down = available_margin / margin_used
                qty *= scale_down
                entry_notional = abs(float(entry_fill) * float(qty))
                margin_used = available_margin
            if qty <= 0.0 or margin_used <= 0.0:
                entry_block_counter["capital_unavailable_block"] += 1
                continue

            entry_fee = execution_model.fee(notional=entry_notional)
            realized_pnl -= entry_fee
            total_fees += entry_fee
            max_loss_cap = float(risk_per_unit * qty)
            if effective_max_trade_margin_loss_fraction > 0.0:
                max_loss_cap = min(
                    max_loss_cap,
                    float(margin_used) * float(effective_max_trade_margin_loss_fraction),
                )
            open_trades[symbol] = _OpenTrade(
                symbol=symbol,
                side=signal_side,
                entry_price=float(entry_fill),
                quantity=float(qty),
                initial_quantity=float(qty),
                entry_fee=float(entry_fee),
                entry_notional=float(entry_notional),
                margin_used=float(margin_used),
                max_loss_cap=max_loss_cap,
                tp=tp,
                sl=sl,
                initial_risk_abs=float(risk_per_unit * qty),
                time_stop_bars=_to_int((decision.get("execution") or {}).get("time_stop_bars"))
                if isinstance(decision.get("execution"), dict)
                else None,
                effective_leverage=float(max_effective_leverage),
                regime=str(decision.get("regime") or "").strip().upper() or None,
                entry_tick=int(tick),
                entry_time_ms=int(float(row.get("open_time") or 0.0)),
                stop_exit_cooldown_bars=_to_int((decision.get("execution") or {}).get("stop_exit_cooldown_bars"))
                if isinstance(decision.get("execution"), dict)
                else max(int(reverse_cooldown_bars), 0),
                profit_exit_cooldown_bars=_to_int((decision.get("execution") or {}).get("profit_exit_cooldown_bars"))
                if isinstance(decision.get("execution"), dict)
                else 0,
                alpha_id=str(candidate.alpha_id or decision.get("alpha_id") or "").strip() or None,
                entry_family=str(candidate.entry_family or decision.get("entry_family") or "").strip()
                or None,
            )
            open_symbols.add(symbol)
            day_trade_counter[(day_key, symbol)] = int(day_trade_counter.get((day_key, symbol), 0)) + 1
            free_slots -= 1

        unrealized = 0.0
        for symbol, trade in open_trades.items():
            candle = candles.get(symbol)
            if not isinstance(candle, dict):
                continue
            close = float(candle.get("close", trade.entry_price))
            qty = max(float(trade.quantity), 0.0)
            if trade.side == "BUY":
                unrealized += (close - float(trade.entry_price)) * qty
            else:
                unrealized += (float(trade.entry_price) - close) * qty
        current_equity = float(initial_capital) + realized_pnl + unrealized
        equity_curve.append(current_equity)
        simultaneous_position_histogram[len(open_trades)] += 1
        portfolio_open_slots_usage[len(open_trades)] += 1
        if entry_equity > 0.0:
            capital_utilization_samples.append(
                sum(float(trade.margin_used) for trade in open_trades.values()) / float(entry_equity)
            )

        for decision in decisions.values():
            if not isinstance(decision, dict):
                continue
            alpha_blocks = decision.get("alpha_blocks")
            if not isinstance(alpha_blocks, dict):
                continue
            for alpha_id, reason in alpha_blocks.items():
                alpha_name = str(alpha_id or "").strip()
                reason_name = str(reason or "").strip()
                if not alpha_name or not reason_name:
                    continue
                entry_block_counter[reason_name] += 0

    if open_trades and rows:
        last_row = rows[-1]
        candles = last_row.get("candles") if isinstance(last_row.get("candles"), dict) else {}
        for symbol in list(open_trades.keys()):
            trade = open_trades.get(symbol)
            candle = candles.get(symbol)
            if trade is None or not isinstance(candle, dict):
                continue
            _close_trade(trade=trade, candle=candle, tick=len(rows) - 1, reason="end_of_data")
        if equity_curve:
            equity_curve[-1] = float(initial_capital) + realized_pnl

    alpha_block_distribution: dict[str, Counter[str]] = {}
    for row in rows:
        decisions = row.get("decisions")
        if not isinstance(decisions, dict):
            continue
        for decision in decisions.values():
            if not isinstance(decision, dict):
                continue
            alpha_blocks = decision.get("alpha_blocks")
            if not isinstance(alpha_blocks, dict):
                continue
            for alpha_id, reason in alpha_blocks.items():
                alpha_name = str(alpha_id or "").strip()
                reason_name = str(reason or "").strip()
                if not alpha_name or not reason_name:
                    continue
                alpha_block_distribution.setdefault(alpha_name, Counter())[reason_name] += 1

    gross_profit = sum(max(0.0, float(item["pnl"])) for item in trade_events)
    gross_loss = abs(sum(min(0.0, float(item["pnl"])) for item in trade_events))
    wins = sum(1 for item in trade_events if float(item["pnl"]) > 0)
    losses = sum(1 for item in trade_events if float(item["pnl"]) < 0)
    total_trades = len(trade_events)
    final_equity = float(initial_capital) + realized_pnl
    total_return_pct = (
        ((final_equity - float(initial_capital)) / float(initial_capital)) * 100.0
        if float(initial_capital) > 0.0
        else 0.0
    )
    profit_factor = float("inf") if gross_loss <= 0 and gross_profit > 0 else 0.0
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    win_rate_pct = ((wins / total_trades) * 100.0) if total_trades > 0 else 0.0
    max_drawdown_pct = _calc_max_drawdown_pct(equity_curve)
    avg_capital_utilization = (
        sum(capital_utilization_samples) / len(capital_utilization_samples)
        if capital_utilization_samples
        else 0.0
    )
    max_capital_utilization = max(capital_utilization_samples) if capital_utilization_samples else 0.0
    alpha_stats = _summarize_alpha_stats(
        trade_events=trade_events,
        alpha_block_distribution=alpha_block_distribution,
        initial_capital=float(initial_capital),
    )
    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate_pct, 4),
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "gross_trade_pnl": round(gross_trade_pnl_total, 6),
        "total_fees": round(total_fees, 6),
        "total_funding_pnl": round(total_funding_pnl, 6),
        "net_profit": round(realized_pnl, 6),
        "profit_factor": None if profit_factor == float("inf") else round(profit_factor, 6),
        "profit_factor_infinite": bool(profit_factor == float("inf")),
        "max_drawdown_pct": round(max_drawdown_pct, 6),
        "initial_capital": float(initial_capital),
        "final_equity": round(final_equity, 6),
        "total_return_pct": round(total_return_pct, 6),
        "entry_block_distribution": dict(entry_block_counter),
        "bucket_block_distribution": {str(key): int(value) for key, value in bucket_block_counter.items()},
        "simultaneous_position_histogram": {
            str(key): int(value) for key, value in simultaneous_position_histogram.items()
        },
        "portfolio_open_slots_usage": {
            str(key): int(value) for key, value in portfolio_open_slots_usage.items()
        },
        "capital_utilization": {
            "avg_pct": round(avg_capital_utilization * 100.0, 6),
            "max_pct": round(max_capital_utilization * 100.0, 6),
        },
        "state_distribution": dict(state_distribution),
        "alpha_block_distribution": {
            alpha_id: dict(counter) for alpha_id, counter in alpha_block_distribution.items()
        },
        "alpha_stats": alpha_stats,
        "trading_lock_reason": trading_lock_reason,
        "trade_events": trade_events,
    }


def _run_local_backtest_portfolio_replay(
    *,
    cfg: EffectiveConfig,
    symbols: list[str],
    active_strategy_name: str,
    years: int,
    start_ms: int,
    end_ms: int,
    cache_root: Path,
    market_intervals: list[str],
    initial_capital: float,
    execution_model: _BacktestExecutionModel,
    fixed_leverage_margin_use_pct: float,
    reverse_cooldown_bars: int,
    max_trades_per_day_per_symbol: int,
    min_expected_edge_over_roundtrip_cost: float,
    min_reward_risk_ratio: float,
    daily_loss_limit_pct: float,
    equity_floor_pct: float,
    max_trade_margin_loss_fraction: float,
    min_signal_score: float,
    drawdown_scale_start_pct: float,
    drawdown_scale_end_pct: float,
    drawdown_margin_scale_min: float,
    strategy_runtime_params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    candles_by_symbol: dict[str, dict[str, list[_Kline15m]]] = {}
    interval_counts_by_symbol: dict[str, dict[str, int]] = {}
    for symbol in symbols:
        market = _load_local_backtest_cached_market_for_symbol(
            cache_root=cache_root,
            symbol=symbol,
            years=years,
            start_ms=start_ms,
            end_ms=end_ms,
            market_intervals=market_intervals,
        )
        if len(market.get("15m", [])) == 0:
            continue
        candles_by_symbol[symbol] = market
        interval_counts_by_symbol[symbol] = {
            interval: len(rows) for interval, rows in market.items()
        }
    if not candles_by_symbol:
        raise RuntimeError("no portfolio backtest candles available")

    rows, state_counter = _build_local_backtest_portfolio_rows(
        cfg=cfg,
        candles_by_symbol=candles_by_symbol,
        premium_by_symbol=None,
        funding_by_symbol=None,
        market_intervals=market_intervals,
        strategy_runtime_params=strategy_runtime_params,
    )
    metrics = _simulate_portfolio_metrics(
        rows=rows,
        initial_capital=float(initial_capital),
        execution_model=execution_model,
        fixed_leverage_margin_use_pct=float(fixed_leverage_margin_use_pct),
        max_open_positions=max(int(getattr(cfg.behavior.engine, "max_open_positions", 1) or 1), 1),
        max_new_entries_per_tick=max(
            1,
            min(int(getattr(cfg.behavior.engine, "max_open_positions", 1) or 1), 2),
        ),
        reverse_cooldown_bars=int(reverse_cooldown_bars),
        max_trades_per_day_per_symbol=int(max_trades_per_day_per_symbol),
        min_expected_edge_over_roundtrip_cost=float(min_expected_edge_over_roundtrip_cost),
        min_reward_risk_ratio=float(min_reward_risk_ratio),
        daily_loss_limit_pct=float(daily_loss_limit_pct),
        equity_floor_pct=float(equity_floor_pct),
        max_trade_margin_loss_fraction=float(max_trade_margin_loss_fraction),
        min_signal_score=float(min_signal_score),
        drawdown_scale_start_pct=float(drawdown_scale_start_pct),
        drawdown_scale_end_pct=float(drawdown_scale_end_pct),
        drawdown_margin_scale_min=float(drawdown_margin_scale_min),
    )
    cleaned_count = _cleanup_local_backtest_artifacts(cache_root.parent)
    metrics["state_distribution"] = dict(state_counter)
    metrics["symbols"] = {
        symbol: interval_counts_by_symbol.get(symbol, {}) for symbol in sorted(candles_by_symbol.keys())
    }
    return metrics, cleaned_count


def _run_local_backtest(
    cfg: EffectiveConfig,
    *,
    symbols: list[str],
    years: int,
    initial_capital: float,
    fee_bps: float,
    slippage_bps: float,
    funding_bps_per_8h: float,
    margin_use_pct: float,
    replay_workers: int,
    reverse_min_hold_bars: int,
    reverse_cooldown_bars: int,
    min_expected_edge_multiple: float,
    min_reward_risk_ratio: float,
    max_trades_per_day: int,
    daily_loss_limit_pct: float,
    equity_floor_pct: float,
    max_trade_margin_loss_fraction: float,
    min_signal_score: float,
    reverse_exit_min_profit_pct: float,
    reverse_exit_min_signal_score: float,
    drawdown_scale_start_pct: float,
    drawdown_scale_end_pct: float,
    drawdown_margin_scale_min: float,
    stoploss_streak_trigger: int,
    stoploss_cooldown_bars: int,
    loss_cooldown_bars: int,
    alpha_squeeze_percentile_max: float,
    alpha_expansion_buffer_bps: float,
    alpha_expansion_range_atr_min: float,
    alpha_expansion_body_ratio_min: float,
    alpha_expansion_close_location_min: float,
    alpha_expansion_width_expansion_min: float,
    alpha_expansion_break_distance_atr_min: float,
    alpha_expansion_breakout_efficiency_min: float,
    alpha_expansion_breakout_stability_score_min: float,
    alpha_expansion_breakout_stability_edge_score_min: float,
    alpha_expansion_quality_score_min: float,
    alpha_expansion_quality_score_v2_min: float,
    alpha_min_volume_ratio: float,
    alpha_take_profit_r: float,
    alpha_time_stop_bars: int,
    alpha_trend_adx_min_4h: float,
    alpha_trend_adx_max_4h: float,
    alpha_trend_adx_rising_lookback_4h: int,
    alpha_trend_adx_rising_min_delta_4h: float,
    alpha_expected_move_cost_mult: float,
    fb_failed_break_buffer_bps: float,
    fb_wick_ratio_min: float,
    fb_take_profit_r: float,
    fb_time_stop_bars: int,
    cbr_squeeze_percentile_max: float,
    cbr_breakout_buffer_bps: float,
    cbr_take_profit_r: float,
    cbr_time_stop_bars: int,
    cbr_trend_adx_min_4h: float,
    cbr_ema_gap_trend_min_frac_4h: float,
    cbr_breakout_min_range_atr: float,
    cbr_breakout_min_volume_ratio: float,
    sfd_reclaim_sweep_buffer_bps: float,
    sfd_reclaim_wick_ratio_min: float,
    sfd_drive_breakout_range_atr_min: float,
    sfd_take_profit_r: float,
    pfd_premium_z_min: float,
    pfd_funding_24h_min: float,
    pfd_reclaim_buffer_atr: float,
    pfd_take_profit_r: float,
    offline: bool,
    fetch_sleep_sec: float,
    backtest_start_ms: int | None = None,
    backtest_end_ms: int | None = None,
    report_dir: str,
    report_path: str | None,
) -> int:
    requested_initial_capital = float(initial_capital)
    initial_capital = _locked_local_backtest_initial_capital(requested_initial_capital)
    enabled_strategies = [
        str(entry.name).strip()
        for entry in cfg.behavior.strategies
        if bool(getattr(entry, "enabled", False))
    ]
    active_strategy_name = enabled_strategies[0] if enabled_strategies else "none"
    if active_strategy_name not in _VOL_TARGET_STRATEGIES:
        print(
            json.dumps(
                {"error": f"unsupported_strategy:{active_strategy_name}"},
                ensure_ascii=True,
            )
        )
        return 1
    use_ra_2026_mode = _is_vol_target_backtest_strategy(active_strategy_name)
    market_intervals = _resolve_market_intervals(cfg)
    fixed_leverage = (
        max(float(cfg.behavior.risk.max_leverage), 1.0) if use_ra_2026_mode else 30.0
    )
    fixed_leverage_margin_use_pct = max(0.01, min(float(margin_use_pct) / 100.0, 1.0))
    reverse_min_hold_bars = max(int(reverse_min_hold_bars), 0)
    reverse_cooldown_bars = max(int(reverse_cooldown_bars), 0)
    min_expected_edge_over_roundtrip_cost = max(float(min_expected_edge_multiple), 0.0)
    min_reward_risk_ratio = max(float(min_reward_risk_ratio), 0.0)
    max_trades_per_day_per_symbol = max(int(max_trades_per_day), 0)
    daily_loss_limit_pct = max(float(daily_loss_limit_pct) / 100.0, 0.0)
    equity_floor_pct = max(float(equity_floor_pct) / 100.0, 0.0)
    max_trade_margin_loss_fraction = max(
        0.0,
        min(float(max_trade_margin_loss_fraction) / 100.0, 1.0),
    )
    min_signal_score = max(float(min_signal_score), 0.0)
    reverse_exit_min_profit_pct = max(float(reverse_exit_min_profit_pct) / 100.0, 0.0)
    reverse_exit_min_signal_score = max(float(reverse_exit_min_signal_score), 0.0)
    drawdown_scale_start_pct = max(float(drawdown_scale_start_pct) / 100.0, 0.0)
    drawdown_scale_end_pct = max(float(drawdown_scale_end_pct) / 100.0, 0.0)
    drawdown_margin_scale_min = max(
        0.05,
        min(float(drawdown_margin_scale_min) / 100.0, 1.0),
    )
    stoploss_streak_trigger = max(int(stoploss_streak_trigger), 0)
    stoploss_cooldown_bars = max(int(stoploss_cooldown_bars), 0)
    loss_cooldown_bars = max(int(loss_cooldown_bars), 0)
    alpha_squeeze_percentile_max = min(max(float(alpha_squeeze_percentile_max), 0.05), 0.95)
    alpha_expansion_buffer_bps = max(float(alpha_expansion_buffer_bps), 0.0)
    alpha_expansion_range_atr_min = max(float(alpha_expansion_range_atr_min), 0.0)
    alpha_expansion_body_ratio_min = min(max(float(alpha_expansion_body_ratio_min), 0.0), 1.0)
    alpha_expansion_close_location_min = min(
        max(float(alpha_expansion_close_location_min), 0.0),
        1.0,
    )
    alpha_expansion_width_expansion_min = max(float(alpha_expansion_width_expansion_min), 0.0)
    alpha_expansion_break_distance_atr_min = max(
        float(alpha_expansion_break_distance_atr_min),
        0.0,
    )
    alpha_expansion_breakout_efficiency_min = max(
        float(alpha_expansion_breakout_efficiency_min),
        0.0,
    )
    alpha_expansion_breakout_stability_score_min = min(
        max(float(alpha_expansion_breakout_stability_score_min), 0.0),
        1.0,
    )
    alpha_expansion_breakout_stability_edge_score_min = min(
        max(float(alpha_expansion_breakout_stability_edge_score_min), 0.0),
        1.0,
    )
    alpha_expansion_quality_score_min = min(
        max(float(alpha_expansion_quality_score_min), 0.0),
        1.0,
    )
    alpha_expansion_quality_score_v2_min = min(
        max(float(alpha_expansion_quality_score_v2_min), 0.0),
        1.0,
    )
    alpha_min_volume_ratio = max(float(alpha_min_volume_ratio), 0.0)
    alpha_take_profit_r = max(float(alpha_take_profit_r), 0.5)
    alpha_time_stop_bars = max(int(alpha_time_stop_bars), 1)
    alpha_trend_adx_min_4h = max(float(alpha_trend_adx_min_4h), 0.0)
    alpha_trend_adx_max_4h = max(float(alpha_trend_adx_max_4h), 0.0)
    alpha_trend_adx_rising_lookback_4h = max(int(alpha_trend_adx_rising_lookback_4h), 0)
    alpha_trend_adx_rising_min_delta_4h = max(
        float(alpha_trend_adx_rising_min_delta_4h),
        0.0,
    )
    alpha_expected_move_cost_mult = max(float(alpha_expected_move_cost_mult), 0.1)
    fb_failed_break_buffer_bps = max(float(fb_failed_break_buffer_bps), 0.0)
    fb_wick_ratio_min = max(float(fb_wick_ratio_min), 0.1)
    fb_take_profit_r = max(float(fb_take_profit_r), 0.5)
    fb_time_stop_bars = max(int(fb_time_stop_bars), 1)
    cbr_squeeze_percentile_max = min(max(float(cbr_squeeze_percentile_max), 0.05), 0.95)
    cbr_breakout_buffer_bps = max(float(cbr_breakout_buffer_bps), 0.0)
    cbr_take_profit_r = max(float(cbr_take_profit_r), 0.5)
    cbr_time_stop_bars = max(int(cbr_time_stop_bars), 1)
    cbr_trend_adx_min_4h = max(float(cbr_trend_adx_min_4h), 0.0)
    cbr_ema_gap_trend_min_frac_4h = max(float(cbr_ema_gap_trend_min_frac_4h), 0.0)
    cbr_breakout_min_range_atr = max(float(cbr_breakout_min_range_atr), 0.0)
    cbr_breakout_min_volume_ratio = max(float(cbr_breakout_min_volume_ratio), 0.0)
    sfd_reclaim_sweep_buffer_bps = max(float(sfd_reclaim_sweep_buffer_bps), 0.0)
    sfd_reclaim_wick_ratio_min = max(float(sfd_reclaim_wick_ratio_min), 0.1)
    sfd_drive_breakout_range_atr_min = max(float(sfd_drive_breakout_range_atr_min), 0.0)
    sfd_take_profit_r = max(float(sfd_take_profit_r), 0.5)
    pfd_premium_z_min = max(float(pfd_premium_z_min), 0.5)
    pfd_funding_24h_min = max(float(pfd_funding_24h_min), 0.0)
    pfd_reclaim_buffer_atr = max(float(pfd_reclaim_buffer_atr), 0.0)
    pfd_take_profit_r = max(float(pfd_take_profit_r), 0.5)
    default_reverse_exit_min_r = 1.0 if use_ra_2026_mode else 0.0
    default_risk_per_trade_pct = 0.0
    default_max_effective_leverage = max(float(cfg.behavior.risk.max_leverage), 1.0)
    max_peak_drawdown_pct = 0.20 if active_strategy_name in _VOL_TARGET_STRATEGIES else None
    strategy_runtime_params = _local_backtest_strategy_runtime_params(
        active_strategy_name=active_strategy_name,
        alpha_squeeze_percentile_max=float(alpha_squeeze_percentile_max),
        alpha_expansion_buffer_bps=float(alpha_expansion_buffer_bps),
        alpha_expansion_range_atr_min=float(alpha_expansion_range_atr_min),
        alpha_expansion_body_ratio_min=float(alpha_expansion_body_ratio_min),
        alpha_expansion_close_location_min=float(alpha_expansion_close_location_min),
        alpha_expansion_width_expansion_min=float(alpha_expansion_width_expansion_min),
        alpha_expansion_break_distance_atr_min=float(alpha_expansion_break_distance_atr_min),
        alpha_expansion_breakout_efficiency_min=float(alpha_expansion_breakout_efficiency_min),
        alpha_expansion_breakout_stability_score_min=float(
            alpha_expansion_breakout_stability_score_min
        ),
        alpha_expansion_breakout_stability_edge_score_min=float(
            alpha_expansion_breakout_stability_edge_score_min
        ),
        alpha_expansion_quality_score_min=float(alpha_expansion_quality_score_min),
        alpha_expansion_quality_score_v2_min=float(alpha_expansion_quality_score_v2_min),
        alpha_min_volume_ratio=float(alpha_min_volume_ratio),
        alpha_take_profit_r=float(alpha_take_profit_r),
        alpha_time_stop_bars=int(alpha_time_stop_bars),
        alpha_trend_adx_min_4h=float(alpha_trend_adx_min_4h),
        alpha_trend_adx_max_4h=float(alpha_trend_adx_max_4h),
        alpha_trend_adx_rising_lookback_4h=int(alpha_trend_adx_rising_lookback_4h),
        alpha_trend_adx_rising_min_delta_4h=float(alpha_trend_adx_rising_min_delta_4h),
        alpha_expected_move_cost_mult=float(alpha_expected_move_cost_mult),
        fb_failed_break_buffer_bps=float(fb_failed_break_buffer_bps),
        fb_wick_ratio_min=float(fb_wick_ratio_min),
        fb_take_profit_r=float(fb_take_profit_r),
        fb_time_stop_bars=int(fb_time_stop_bars),
        cbr_squeeze_percentile_max=float(cbr_squeeze_percentile_max),
        cbr_breakout_buffer_bps=float(cbr_breakout_buffer_bps),
        cbr_take_profit_r=float(cbr_take_profit_r),
        cbr_time_stop_bars=int(cbr_time_stop_bars),
        cbr_trend_adx_min_4h=float(cbr_trend_adx_min_4h),
        cbr_ema_gap_trend_min_frac_4h=float(cbr_ema_gap_trend_min_frac_4h),
        cbr_breakout_min_range_atr=float(cbr_breakout_min_range_atr),
        cbr_breakout_min_volume_ratio=float(cbr_breakout_min_volume_ratio),
        sfd_reclaim_sweep_buffer_bps=float(sfd_reclaim_sweep_buffer_bps),
        sfd_reclaim_wick_ratio_min=float(sfd_reclaim_wick_ratio_min),
        sfd_drive_breakout_range_atr_min=float(sfd_drive_breakout_range_atr_min),
        sfd_take_profit_r=float(sfd_take_profit_r),
        pfd_premium_z_min=float(pfd_premium_z_min),
        pfd_funding_24h_min=float(pfd_funding_24h_min),
        pfd_reclaim_buffer_atr=float(pfd_reclaim_buffer_atr),
        pfd_take_profit_r=float(pfd_take_profit_r),
    )
    if str(active_strategy_name).startswith("ra_2026_alpha_v2"):
        strategy_runtime_params.update(_local_backtest_profile_alpha_overrides(cfg.profile))

    if years <= 0:
        print(json.dumps({"error": "backtest years must be > 0"}, ensure_ascii=True))
        return 1
    if initial_capital <= 0:
        print(json.dumps({"error": "backtest initial capital must be > 0"}, ensure_ascii=True))
        return 1
    if not symbols:
        print(json.dumps({"error": "no backtest symbols"}, ensure_ascii=True))
        return 1

    if (backtest_start_ms is not None) != (backtest_end_ms is not None):
        print(
            json.dumps(
                {
                    "error": "backtest-start-utc and backtest-end-utc must be used together",
                },
                ensure_ascii=True,
            )
        )
        return 1

    now = datetime.now(timezone.utc)
    if backtest_start_ms is None and backtest_end_ms is None:
        start = now.replace(microsecond=0) - timedelta(days=int(years) * 365)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        backtest_window_label = "rolling"
    else:
        start_ms = int(backtest_start_ms)
        end_ms = int(backtest_end_ms)
        if start_ms >= end_ms:
            print(
                json.dumps(
                    {
                        "error": "backtest-start-utc must be earlier than backtest-end-utc",
                    },
                    ensure_ascii=True,
                )
            )
            return 1
        backtest_window_label = "fixed"

    rest: BinanceRESTClient | None = None
    if not offline:
        rest = BinanceRESTClient(
            env=cfg.env,
            api_key=None,
            api_secret=None,
            recv_window_ms=cfg.behavior.exchange.recv_window_ms,
            time_sync_enabled=False,
            rate_limit_per_sec=cfg.behavior.exchange.request_rate_limit_per_sec,
            backoff_policy=BackoffPolicy(
                base_seconds=cfg.behavior.exchange.backoff_base_seconds,
                cap_seconds=cfg.behavior.exchange.backoff_cap_seconds,
            ),
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    output_root = Path(report_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    cache_root = output_root / "_cache"
    cache_root.mkdir(parents=True, exist_ok=True)

    symbol_reports: list[dict[str, Any]] = []
    combined_state_counter: Counter[str] = Counter()
    total_cycles = 0
    downloaded_symbols: list[dict[str, Any]] = []
    total_symbols = len(symbols)
    per_symbol_initial_capital = float(initial_capital) / max(int(total_symbols), 1)
    execution_model = _BacktestExecutionModel(
        fee_bps=max(float(fee_bps), 0.0),
        slippage_bps=max(float(slippage_bps), 0.0),
        funding_bps_per_8h=max(float(funding_bps_per_8h), 0.0),
    )

    print(
        f"[LOCAL_BACKTEST] start symbols={','.join(symbols)} years={years} window={backtest_window_label} "
        f"start={_format_utc_iso(start_ms)} end={_format_utc_iso(end_ms)} initial_capital={initial_capital:.2f} "
        f"per_symbol={per_symbol_initial_capital:.2f} strategy={active_strategy_name} sizing_mode={'vol_target' if use_ra_2026_mode else 'fixed_leverage'} "
        f"leverage_cap={fixed_leverage:.1f}x intervals={','.join(market_intervals)} offline={offline}"
    )
    if abs(requested_initial_capital - initial_capital) > 1e-9:
        print(
            f"[LOCAL_BACKTEST] initial_capital_locked requested={requested_initial_capital:.2f} enforced={initial_capital:.2f}"
        )

    cleaned_count = 0
    try:
        with asyncio.Runner() as runner:
            try:
                for symbol_index, symbol in enumerate(symbols, start=1):
                    print(
                        f"[LOCAL_BACKTEST] [{symbol_index}/{total_symbols}] {symbol} download started"
                    )

                    last_download_pct = -1

                    def _on_download_progress(
                        percent: int,
                        *,
                        _symbol_index: int = symbol_index,
                        _symbol: str = symbol,
                        _total_symbols: int = total_symbols,
                    ) -> None:
                        nonlocal last_download_pct
                        if percent <= last_download_pct:
                            return
                        last_download_pct = percent
                        print(
                            f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} download {percent}%"
                        )

                    def _load_interval_with_cache(
                        *,
                        interval: str,
                        on_progress: Callable[[int], None] | None = None,
                        _symbol: str = symbol,
                        _symbol_index: int = symbol_index,
                        _total_symbols: int = total_symbols,
                    ) -> list[_Kline15m]:
                        def _fetch_range(
                            *, fetch_start_ms: int, fetch_end_ms: int
                        ) -> list[_Kline15m]:
                            if rest is None:
                                raise RuntimeError("offline cache-only backtest")
                            if interval == "15m":
                                return runner.run(
                                    _fetch_klines_15m(
                                        rest_client=rest,
                                        symbol=_symbol,
                                        start_ms=fetch_start_ms,
                                        end_ms=fetch_end_ms,
                                        on_progress=on_progress,
                                        sleep_sec=fetch_sleep_sec,
                                    )
                                )
                            return runner.run(
                                _fetch_klines_interval(
                                    rest_client=rest,
                                    symbol=_symbol,
                                    interval=interval,
                                    start_ms=fetch_start_ms,
                                    end_ms=fetch_end_ms,
                                    sleep_sec=fetch_sleep_sec,
                                )
                            )

                        cache_file = _cache_file_for_klines(
                            cache_root=cache_root,
                            symbol=_symbol,
                            interval=interval,
                            years=years,
                        )
                        cache_has_volume = _klines_csv_has_volume_column(cache_file)
                        cached_rows: list[_Kline15m] = []
                        if cache_has_volume:
                            cached_rows = _load_cached_klines_for_range(
                                path=cache_file,
                                interval=interval,
                                start_ms=start_ms,
                                end_ms=end_ms,
                            )
                        if cached_rows:
                            if on_progress is not None:
                                on_progress(100)
                            print(
                                f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache hit interval={interval} candles={len(cached_rows)}"
                            )
                            return cached_rows

                        raw_cached_rows = _read_klines_csv_rows(cache_file)
                        if raw_cached_rows and not cache_has_volume and not offline:
                            print(
                                f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache stale interval={interval} reason=missing_volume recaching"
                            )
                            raw_cached_rows = []
                        interval_ms = _interval_to_ms(interval)
                        if raw_cached_rows:
                            filtered_cached_rows = [
                                row
                                for row in raw_cached_rows
                                if int(row.open_time_ms) >= int(start_ms)
                                and int(row.open_time_ms) < int(end_ms)
                            ]
                            if offline:
                                if filtered_cached_rows:
                                    print(
                                        f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache_only interval={interval} candles={len(filtered_cached_rows)}"
                                    )
                                    return filtered_cached_rows
                                print(
                                    f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache_only_missing interval={interval} no_cached_file={cache_file}"
                                )
                                return []
                            if filtered_cached_rows:
                                expected = max(
                                    int((int(end_ms) - int(start_ms)) // max(interval_ms, 1)),
                                    1,
                                )
                                head_ok = (
                                    int(filtered_cached_rows[0].open_time_ms)
                                    <= int(start_ms) + interval_ms
                                )
                                density_ok = len(filtered_cached_rows) >= int(expected * 0.90)
                                tail_ok = int(filtered_cached_rows[-1].open_time_ms) >= int(
                                    end_ms
                                ) - (interval_ms * 2)

                                if head_ok and density_ok and not tail_ok:
                                    delta_start_ms = (
                                        int(filtered_cached_rows[-1].open_time_ms) + interval_ms
                                    )
                                    if delta_start_ms < int(end_ms):
                                        if offline:
                                            print(
                                                f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache stale interval={interval} offline=1 skip_extend"
                                            )
                                            return filtered_cached_rows
                                        print(
                                            f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache stale interval={interval} extending"
                                        )
                                        try:
                                            delta_rows = _fetch_range(
                                                fetch_start_ms=delta_start_ms,
                                                fetch_end_ms=int(end_ms),
                                            )
                                        except Exception as exc:  # noqa: BLE001
                                            print(
                                                f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache extend failed interval={interval} error={type(exc).__name__}:{exc} fallback=stale_cache"
                                            )
                                            return filtered_cached_rows
                                        if delta_rows:
                                            merged: dict[int, _Kline15m] = {
                                                int(row.open_time_ms): row
                                                for row in raw_cached_rows
                                            }
                                            for row in delta_rows:
                                                merged[int(row.open_time_ms)] = row
                                            merged_rows = [
                                                merged[key] for key in sorted(merged.keys())
                                            ]
                                            _write_klines_csv(
                                                path=cache_file,
                                                symbol=_symbol,
                                                rows=merged_rows,
                                            )
                                            recached_rows = _load_cached_klines_for_range(
                                                path=cache_file,
                                                interval=interval,
                                                start_ms=start_ms,
                                                end_ms=end_ms,
                                            )
                                            if recached_rows:
                                                print(
                                                    f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache extend hit interval={interval} candles={len(recached_rows)}"
                                                )
                                                return recached_rows

                        if offline:
                            print(
                                f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache_only miss interval={interval} no_cached_klines"
                            )
                            return []

                        print(
                            f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} cache miss interval={interval} downloading"
                        )
                        try:
                            fetched_rows = _fetch_range(
                                fetch_start_ms=int(start_ms),
                                fetch_end_ms=int(end_ms),
                            )
                        except Exception as exc:  # noqa: BLE001
                            print(
                                f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} fetch failed interval={interval} error={type(exc).__name__}:{exc}"
                            )
                            if raw_cached_rows:
                                print(
                                    f"[LOCAL_BACKTEST] [{_symbol_index}/{_total_symbols}] {_symbol} fallback interval={interval} raw_cached_rows={len(raw_cached_rows)}"
                                )
                                return raw_cached_rows
                            raise
                        if fetched_rows:
                            _write_klines_csv(path=cache_file, symbol=_symbol, rows=fetched_rows)
                        return fetched_rows

                    candles_15m = _load_interval_with_cache(
                        interval="15m",
                        on_progress=_on_download_progress,
                    )
                    if len(candles_15m) == 0:
                        print(f"[LOCAL_BACKTEST] [{symbol_index}/{total_symbols}] {symbol} no_data")
                        continue

                    print(
                        f"[LOCAL_BACKTEST] [{symbol_index}/{total_symbols}] {symbol} download complete candles={len(candles_15m)}"
                    )

                    interval_counts: dict[str, int] = {"15m": len(candles_15m)}
                    aux_intervals = [interval for interval in market_intervals if interval != "15m"]
                    if aux_intervals:
                        print(
                            f"[LOCAL_BACKTEST] [{symbol_index}/{total_symbols}] {symbol} aux timeframe download started ({','.join(aux_intervals)})"
                        )
                    for interval in aux_intervals:
                        candles_aux = _load_interval_with_cache(interval=interval)
                        interval_counts[interval] = len(candles_aux)
                    if aux_intervals:
                        aux_summary = " ".join(
                            f"{interval}={interval_counts.get(interval, 0)}"
                            for interval in aux_intervals
                        )
                        print(
                            f"[LOCAL_BACKTEST] [{symbol_index}/{total_symbols}] {symbol} aux complete {aux_summary}"
                        )

                    downloaded_symbols.append(
                        {
                            "symbol": symbol,
                            "candles_by_interval": interval_counts,
                        }
                    )
            finally:
                if rest is not None:
                    runner.run(rest.aclose())

        if len(downloaded_symbols) == 0:
            print(json.dumps({"error": "no historical klines downloaded"}, ensure_ascii=True))
            return 1

        if active_strategy_name in _LEGACY_PORTFOLIO_BACKTEST_STRATEGIES:
            portfolio_metrics, cleaned_count = _run_local_backtest_portfolio_replay(
                cfg=cfg,
                symbols=symbols,
                active_strategy_name=active_strategy_name,
                years=years,
                start_ms=start_ms,
                end_ms=end_ms,
                cache_root=cache_root,
                market_intervals=market_intervals,
                initial_capital=float(initial_capital),
                execution_model=execution_model,
                fixed_leverage_margin_use_pct=float(fixed_leverage_margin_use_pct),
                reverse_cooldown_bars=int(reverse_cooldown_bars),
                max_trades_per_day_per_symbol=int(max_trades_per_day_per_symbol),
                min_expected_edge_over_roundtrip_cost=float(min_expected_edge_over_roundtrip_cost),
                min_reward_risk_ratio=float(min_reward_risk_ratio),
                daily_loss_limit_pct=float(daily_loss_limit_pct),
                equity_floor_pct=float(equity_floor_pct),
                max_trade_margin_loss_fraction=float(max_trade_margin_loss_fraction),
                min_signal_score=float(min_signal_score),
                drawdown_scale_start_pct=float(drawdown_scale_start_pct),
                drawdown_scale_end_pct=float(drawdown_scale_end_pct),
                drawdown_margin_scale_min=float(drawdown_margin_scale_min),
                strategy_runtime_params=strategy_runtime_params,
            )
            symbol_reports = []
            raw_symbol_counts = portfolio_metrics.get("symbols")
            if isinstance(raw_symbol_counts, dict):
                for symbol, interval_counts in sorted(raw_symbol_counts.items()):
                    if not isinstance(interval_counts, dict):
                        continue
                    normalized_counts: dict[str, int] = {}
                    for key, value in interval_counts.items():
                        try:
                            normalized_counts[str(key)] = int(value)
                        except (TypeError, ValueError):
                            continue
                    symbol_reports.append(
                        {
                            "symbol": symbol,
                            "candles_by_interval": normalized_counts,
                            "candles_10m": int(normalized_counts.get("10m", 0)),
                            "candles_30m": int(normalized_counts.get("30m", 0)),
                            "candles_1h": int(normalized_counts.get("1h", 0)),
                            "candles_4h": int(normalized_counts.get("4h", 0)),
                        }
                    )

            window_slices_6m = _build_half_year_window_summaries(
                symbol_reports=[{"trade_events": portfolio_metrics.get("trade_events", [])}],
                start_ms=int(start_ms),
                end_ms=int(end_ms),
                total_initial_capital=float(initial_capital),
            )
            state_distribution = portfolio_metrics.get("state_distribution")
            total_cycles = (
                sum(int(value) for value in state_distribution.values())
                if isinstance(state_distribution, dict)
                else 0
            )
            gross_profit = float(portfolio_metrics.get("gross_profit", 0.0))
            gross_trade_pnl = float(portfolio_metrics.get("gross_trade_pnl", 0.0))
            total_fees = float(portfolio_metrics.get("total_fees", 0.0))
            fee_to_gross_profit_pct = (
                round((total_fees / gross_profit) * 100.0, 6) if gross_profit > 0.0 else None
            )
            fee_to_trade_gross_pct = (
                round((total_fees / gross_trade_pnl) * 100.0, 6)
                if gross_trade_pnl > 0.0
                else None
            )
            research_gate = _portfolio_research_gate(
                years=int(years),
                total_net_profit=float(portfolio_metrics.get("net_profit", 0.0)),
                profit_factor=_to_float(portfolio_metrics.get("profit_factor")),
                max_drawdown_pct=float(portfolio_metrics.get("max_drawdown_pct", 0.0)),
                fee_to_trade_gross_pct=fee_to_trade_gross_pct,
            )
            report_payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "backtest": {
                    "years": years,
                    "symbols": symbols,
                    "start_ms": int(start_ms),
                    "end_ms": int(end_ms),
                    "env": cfg.env,
                    "profile": cfg.profile,
                    "mode": cfg.mode,
                    "strategy_name": active_strategy_name,
                    "window_mode": backtest_window_label,
                    "backtest_start_utc": _format_utc_iso(start_ms),
                    "backtest_end_utc": _format_utc_iso(end_ms),
                    "timeframe_base": "15m",
                    "timeframe_context": [interval for interval in market_intervals if interval != "15m"],
                    "initial_capital_usdt": round(float(initial_capital), 6),
                    "initial_capital_locked": True,
                    "position_sizing_mode": "portfolio_risk_budget",
                    "fixed_leverage": round(float(fixed_leverage), 6),
                    "fixed_leverage_margin_use_pct": round(float(fixed_leverage_margin_use_pct), 6),
                    "reverse_min_hold_bars": int(reverse_min_hold_bars),
                    "reverse_cooldown_bars": int(reverse_cooldown_bars),
                    "min_expected_edge_over_roundtrip_cost": round(
                        float(min_expected_edge_over_roundtrip_cost),
                        6,
                    ),
                    "min_reward_risk_ratio": round(float(min_reward_risk_ratio), 6),
                    "max_trades_per_day_per_symbol": int(max_trades_per_day_per_symbol),
                    "daily_loss_limit_pct": round(float(daily_loss_limit_pct), 6),
                    "equity_floor_pct": round(float(equity_floor_pct), 6),
                    "max_trade_margin_loss_fraction": round(
                        float(max_trade_margin_loss_fraction),
                        6,
                    ),
                    "min_signal_score": round(float(min_signal_score), 6),
                    "reverse_exit_min_profit_pct": round(float(reverse_exit_min_profit_pct), 6),
                    "reverse_exit_min_signal_score": round(float(reverse_exit_min_signal_score), 6),
                    "pfd_premium_z_min": round(float(pfd_premium_z_min), 6),
                    "pfd_funding_24h_min": round(float(pfd_funding_24h_min), 6),
                    "pfd_reclaim_buffer_atr": round(float(pfd_reclaim_buffer_atr), 6),
                    "pfd_take_profit_r": round(float(pfd_take_profit_r), 6),
                    "drawdown_scale_start_pct": round(float(drawdown_scale_start_pct), 6),
                    "drawdown_scale_end_pct": round(float(drawdown_scale_end_pct), 6),
                    "drawdown_margin_scale_min": round(float(drawdown_margin_scale_min), 6),
                    "portfolio_engine": {
                        "max_open_positions": max(
                            int(getattr(cfg.behavior.engine, "max_open_positions", 1) or 1),
                            1,
                        ),
                        "max_new_entries_per_tick": max(
                            1,
                            min(int(getattr(cfg.behavior.engine, "max_open_positions", 1) or 1), 2),
                        ),
                    },
                    "execution_model": {
                        "fee_bps": round(float(execution_model.fee_bps), 6),
                        "slippage_bps": round(float(execution_model.slippage_bps), 6),
                        "funding_bps_per_8h": round(float(execution_model.funding_bps_per_8h), 6),
                    },
                },
                "summary": {
                    "symbols_ran": len(symbol_reports),
                    "total_cycles": int(total_cycles),
                    "state_distribution": state_distribution if isinstance(state_distribution, dict) else {},
                    "entry_block_distribution": portfolio_metrics.get("entry_block_distribution", {}),
                    "bucket_block_distribution": portfolio_metrics.get("bucket_block_distribution", {}),
                    "portfolio_open_slots_usage": portfolio_metrics.get("portfolio_open_slots_usage", {}),
                    "capital_utilization": portfolio_metrics.get("capital_utilization", {}),
                    "simultaneous_position_histogram": portfolio_metrics.get(
                        "simultaneous_position_histogram",
                        {},
                    ),
                    "total_initial_capital": round(float(initial_capital), 6),
                    "total_final_equity": round(
                        float(portfolio_metrics.get("final_equity", initial_capital)),
                        6,
                    ),
                    "total_net_profit": round(float(portfolio_metrics.get("net_profit", 0.0)), 6),
                    "total_return_pct": round(float(portfolio_metrics.get("total_return_pct", 0.0)), 6),
                    "total_trades": int(portfolio_metrics.get("total_trades", 0)),
                    "wins": int(portfolio_metrics.get("wins", 0)),
                    "losses": int(portfolio_metrics.get("losses", 0)),
                    "win_rate_pct": round(float(portfolio_metrics.get("win_rate_pct", 0.0)), 6),
                    "gross_profit": round(float(portfolio_metrics.get("gross_profit", 0.0)), 6),
                    "gross_loss": round(float(portfolio_metrics.get("gross_loss", 0.0)), 6),
                    "gross_trade_pnl": round(float(portfolio_metrics.get("gross_trade_pnl", 0.0)), 6),
                    "total_fees": round(float(portfolio_metrics.get("total_fees", 0.0)), 6),
                    "fee_to_gross_profit_pct": fee_to_gross_profit_pct,
                    "fee_to_trade_gross_pct": fee_to_trade_gross_pct,
                    "total_funding_pnl": round(float(portfolio_metrics.get("total_funding_pnl", 0.0)), 6),
                    "profit_factor": portfolio_metrics.get("profit_factor"),
                    "max_drawdown_pct": round(float(portfolio_metrics.get("max_drawdown_pct", 0.0)), 6),
                    "alpha_stats": portfolio_metrics.get("alpha_stats", {}),
                    "window_slices_6m": window_slices_6m,
                    "cleaned_artifacts": int(cleaned_count),
                    "research_gate": research_gate,
                },
                "symbols": symbol_reports,
                "portfolio": {
                    "trade_events": portfolio_metrics.get("trade_events", []),
                    "alpha_block_distribution": portfolio_metrics.get("alpha_block_distribution", {}),
                },
            }

            if report_path is not None:
                target = Path(report_path)
                target.parent.mkdir(parents=True, exist_ok=True)
            else:
                target = output_root / f"local_backtest_{stamp}.json"
            target.write_text(json.dumps(report_payload, indent=2, ensure_ascii=True), encoding="utf-8")
            markdown_target = _write_local_backtest_markdown(
                report_payload=report_payload,
                target_json=target,
            )

            print("LOCAL BACKTEST SUMMARY")
            print(
                " | ".join(
                    [
                        f"strategy={active_strategy_name}",
                        "sizing=portfolio",
                        f"net={float(portfolio_metrics.get('net_profit', 0.0)):.2f} USDT",
                        f"return={float(portfolio_metrics.get('total_return_pct', 0.0)):.2f}%",
                        f"win_rate={float(portfolio_metrics.get('win_rate_pct', 0.0)):.2f}%",
                        f"pf={float(portfolio_metrics.get('profit_factor') or 0.0):.3f}",
                        f"max_dd={float(portfolio_metrics.get('max_drawdown_pct', 0.0)):.2f}%",
                        f"slots={max(int(getattr(cfg.behavior.engine, 'max_open_positions', 1) or 1), 1)}",
                    ]
                )
            )
            print(f"REPORT_JSON={target}")
            print(f"REPORT_MD={markdown_target}")
            return 0

        requested_workers = int(replay_workers)
        if requested_workers > 0:
            replay_worker_count = max(1, min(int(len(downloaded_symbols)), requested_workers))
        else:
            cpu_workers = max(int(os.cpu_count() or 1), 1)
            replay_worker_count = max(1, min(int(len(downloaded_symbols)), cpu_workers))
        print(
            f"[LOCAL_BACKTEST] replay parallel started symbols={len(downloaded_symbols)} workers={replay_worker_count}"
        )
        def _worker_kwargs(item: dict[str, Any]) -> dict[str, Any]:
            symbol = str(item["symbol"])
            sqlite_path = output_root / f"backtest_{symbol.lower()}_{stamp}.sqlite3"
            return {
                "symbol": symbol,
                "profile": cfg.profile,
                "mode": cfg.mode,
                "env": cfg.env,
                "years": years,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "cache_root": str(cache_root),
                "sqlite_path": str(sqlite_path),
                "initial_capital": per_symbol_initial_capital,
                "execution_model": execution_model,
                "fixed_leverage": fixed_leverage,
                "fixed_leverage_margin_use_pct": fixed_leverage_margin_use_pct,
                "reverse_min_hold_bars": reverse_min_hold_bars,
                "reverse_cooldown_bars": reverse_cooldown_bars,
                "min_expected_edge_over_roundtrip_cost": min_expected_edge_over_roundtrip_cost,
                "min_reward_risk_ratio": min_reward_risk_ratio,
                "max_trades_per_day_per_symbol": max_trades_per_day_per_symbol,
                "daily_loss_limit_pct": daily_loss_limit_pct,
                "equity_floor_pct": equity_floor_pct,
                "max_trade_margin_loss_fraction": max_trade_margin_loss_fraction,
                "min_signal_score": min_signal_score,
                "reverse_exit_min_profit_pct": reverse_exit_min_profit_pct,
                "reverse_exit_min_signal_score": reverse_exit_min_signal_score,
                "default_reverse_exit_min_r": default_reverse_exit_min_r,
                "default_risk_per_trade_pct": default_risk_per_trade_pct,
                "default_max_effective_leverage": default_max_effective_leverage,
                "drawdown_scale_start_pct": drawdown_scale_start_pct,
                "drawdown_scale_end_pct": drawdown_scale_end_pct,
                "drawdown_margin_scale_min": drawdown_margin_scale_min,
                "stoploss_streak_trigger": stoploss_streak_trigger,
                "stoploss_cooldown_bars": stoploss_cooldown_bars,
                "loss_cooldown_bars": loss_cooldown_bars,
                "alpha_squeeze_percentile_max": alpha_squeeze_percentile_max,
                "alpha_expansion_buffer_bps": alpha_expansion_buffer_bps,
                "alpha_expansion_range_atr_min": alpha_expansion_range_atr_min,
                "alpha_expansion_body_ratio_min": alpha_expansion_body_ratio_min,
                "alpha_expansion_close_location_min": alpha_expansion_close_location_min,
                "alpha_expansion_width_expansion_min": alpha_expansion_width_expansion_min,
                "alpha_expansion_break_distance_atr_min": alpha_expansion_break_distance_atr_min,
                "alpha_expansion_breakout_efficiency_min": alpha_expansion_breakout_efficiency_min,
                "alpha_expansion_breakout_stability_score_min": alpha_expansion_breakout_stability_score_min,
                "alpha_expansion_breakout_stability_edge_score_min": alpha_expansion_breakout_stability_edge_score_min,
                "alpha_expansion_quality_score_min": alpha_expansion_quality_score_min,
                "alpha_expansion_quality_score_v2_min": alpha_expansion_quality_score_v2_min,
                "alpha_min_volume_ratio": alpha_min_volume_ratio,
                "alpha_take_profit_r": alpha_take_profit_r,
                "alpha_time_stop_bars": alpha_time_stop_bars,
                "alpha_trend_adx_min_4h": alpha_trend_adx_min_4h,
                "alpha_trend_adx_max_4h": alpha_trend_adx_max_4h,
                "alpha_trend_adx_rising_lookback_4h": alpha_trend_adx_rising_lookback_4h,
                "alpha_trend_adx_rising_min_delta_4h": alpha_trend_adx_rising_min_delta_4h,
                "alpha_expected_move_cost_mult": alpha_expected_move_cost_mult,
                "fb_failed_break_buffer_bps": fb_failed_break_buffer_bps,
                "fb_wick_ratio_min": fb_wick_ratio_min,
                "fb_take_profit_r": fb_take_profit_r,
                "fb_time_stop_bars": fb_time_stop_bars,
                "cbr_squeeze_percentile_max": cbr_squeeze_percentile_max,
                "cbr_breakout_buffer_bps": cbr_breakout_buffer_bps,
                "cbr_take_profit_r": cbr_take_profit_r,
                "cbr_time_stop_bars": cbr_time_stop_bars,
                "cbr_trend_adx_min_4h": cbr_trend_adx_min_4h,
                "cbr_ema_gap_trend_min_frac_4h": cbr_ema_gap_trend_min_frac_4h,
                "cbr_breakout_min_range_atr": cbr_breakout_min_range_atr,
                "cbr_breakout_min_volume_ratio": cbr_breakout_min_volume_ratio,
                "sfd_reclaim_sweep_buffer_bps": sfd_reclaim_sweep_buffer_bps,
                "sfd_reclaim_wick_ratio_min": sfd_reclaim_wick_ratio_min,
                "sfd_drive_breakout_range_atr_min": sfd_drive_breakout_range_atr_min,
                "sfd_take_profit_r": sfd_take_profit_r,
                "pfd_premium_z_min": pfd_premium_z_min,
                "pfd_funding_24h_min": pfd_funding_24h_min,
                "pfd_reclaim_buffer_atr": pfd_reclaim_buffer_atr,
                "pfd_take_profit_r": pfd_take_profit_r,
                "market_intervals": list(market_intervals),
                "max_peak_drawdown_pct": max_peak_drawdown_pct,
            }

        def _consume_result(*, item: dict[str, Any], result: dict[str, Any], done_count: int) -> None:
            nonlocal total_cycles
            symbol = str(item.get("symbol", "UNKNOWN"))
            if bool(result.get("skipped")):
                print(
                    f"[LOCAL_BACKTEST] replay skipped symbol={symbol} reason={result.get('reason', 'unknown')}"
                )
                return

            report_raw = result.get("report")
            if not isinstance(report_raw, dict):
                print(f"[LOCAL_BACKTEST] replay invalid report symbol={symbol}")
                return

            per_symbol_report = dict(report_raw)
            interval_counts = item.get("candles_by_interval")
            if isinstance(interval_counts, dict):
                normalized_counts: dict[str, int] = {}
                for key, value in interval_counts.items():
                    try:
                        normalized_counts[str(key)] = int(value)
                    except (TypeError, ValueError):
                        continue
                per_symbol_report["candles_by_interval"] = normalized_counts
                per_symbol_report["candles_10m"] = int(normalized_counts.get("10m", 0))
                per_symbol_report["candles_30m"] = int(normalized_counts.get("30m", 0))
                per_symbol_report["candles_1h"] = int(normalized_counts.get("1h", 0))
                per_symbol_report["candles_4h"] = int(normalized_counts.get("4h", 0))

            symbol_cycles = 0
            try:
                symbol_cycles = int(result.get("total_cycles", 0))
            except (TypeError, ValueError):
                symbol_cycles = 0
            per_symbol_report["cycles"] = symbol_cycles
            total_cycles += symbol_cycles

            state_distribution = result.get("state_distribution")
            if isinstance(state_distribution, dict):
                for key, value in state_distribution.items():
                    try:
                        combined_state_counter[str(key)] += int(value)
                    except (TypeError, ValueError):
                        continue

            symbol_reports.append(per_symbol_report)
            print(
                f"[LOCAL_BACKTEST] [{done_count}/{len(downloaded_symbols)}] {symbol} done net={float(per_symbol_report['net_profit']):.2f} return={float(per_symbol_report['total_return_pct']):.2f}% win={float(per_symbol_report['win_rate_pct']):.2f}% max_dd={float(per_symbol_report['max_drawdown_pct']):.2f}%"
            )

        def _run_replay_sequential(items: list[dict[str, Any]]) -> None:
            print("[LOCAL_BACKTEST] replay mode=sequential")
            for idx, item in enumerate(items, start=1):
                symbol = str(item.get("symbol", "UNKNOWN"))
                kwargs = _worker_kwargs(item)
                try:
                    result = _run_local_backtest_symbol_replay_worker(**kwargs)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[LOCAL_BACKTEST] replay failed symbol={symbol} error={type(exc).__name__}:{exc}"
                    )
                    continue
                _consume_result(item=item, result=result, done_count=idx)

        if replay_worker_count <= 1:
            _run_replay_sequential(downloaded_symbols)
        else:
            try:
                with ProcessPoolExecutor(max_workers=replay_worker_count) as pool:
                    future_to_symbol: dict[Any, dict[str, Any]] = {}
                    for item in downloaded_symbols:
                        symbol = str(item["symbol"])
                        future = pool.submit(
                            _run_local_backtest_symbol_replay_worker,
                            **_worker_kwargs(item),
                        )
                        future_to_symbol[future] = item
                        print(f"[LOCAL_BACKTEST] replay queued symbol={symbol}")

                    done_count = 0
                    pending = set(future_to_symbol.keys())
                    replay_started_at = time.monotonic()
                    heartbeat_interval_sec = 20.0

                    while pending:
                        done_now, pending = wait(
                            pending,
                            timeout=heartbeat_interval_sec,
                            return_when=FIRST_COMPLETED,
                        )

                        if not done_now:
                            elapsed_sec = int(time.monotonic() - replay_started_at)
                            pending_symbols = [
                                str(future_to_symbol[f].get("symbol", "UNKNOWN"))
                                for f in pending
                                if f in future_to_symbol
                            ]
                            pending_symbols.sort()
                            pending_label = ",".join(pending_symbols[:6])
                            if len(pending_symbols) > 6:
                                pending_label = pending_label + ",..."
                            print(
                                f"[LOCAL_BACKTEST] replay running done={done_count}/{len(downloaded_symbols)} pending={len(pending)} elapsed={elapsed_sec}s symbols={pending_label}"
                            )
                            continue

                        for future in done_now:
                            done_count += 1
                            item = future_to_symbol[future]
                            symbol = str(item.get("symbol", "UNKNOWN"))
                            try:
                                result = future.result()
                            except Exception as exc:
                                print(
                                    f"[LOCAL_BACKTEST] replay failed symbol={symbol} error={type(exc).__name__}:{exc}"
                                )
                                continue
                            _consume_result(item=item, result=result, done_count=done_count)
            except PermissionError as exc:
                print(
                    f"[LOCAL_BACKTEST] replay parallel unavailable error={type(exc).__name__}:{exc} fallback=sequential"
                )
                _run_replay_sequential(downloaded_symbols)

        if len(symbol_reports) == 0:
            print(json.dumps({"error": "no local backtest results"}, ensure_ascii=True))
            return 1

        total_initial = sum(float(item.get("initial_capital", 0.0)) for item in symbol_reports)
        total_final = sum(float(item["final_equity"]) for item in symbol_reports)
        total_net = sum(float(item["net_profit"]) for item in symbol_reports)
        total_gross_profit = sum(float(item["gross_profit"]) for item in symbol_reports)
        total_gross_loss = sum(float(item["gross_loss"]) for item in symbol_reports)
        total_trade_gross_pnl = sum(
            float(item.get("gross_trade_pnl", 0.0)) for item in symbol_reports
        )
        total_fees = sum(float(item.get("total_fees", 0.0)) for item in symbol_reports)
        total_funding_pnl = sum(
            float(item.get("total_funding_pnl", 0.0)) for item in symbol_reports
        )
        entry_block_counter = Counter()
        for item in symbol_reports:
            blocks = item.get("entry_block_distribution")
            if isinstance(blocks, dict):
                for key, value in blocks.items():
                    try:
                        entry_block_counter[str(key)] += int(value)
                    except (TypeError, ValueError):
                        continue
        alpha_block_distribution: dict[str, Counter[str]] = {}
        combined_trade_events: list[dict[str, Any]] = []
        for item in symbol_reports:
            alpha_blocks = item.get("alpha_block_distribution")
            if isinstance(alpha_blocks, dict):
                for alpha_id, source in alpha_blocks.items():
                    if not isinstance(source, dict):
                        continue
                    counter = alpha_block_distribution.setdefault(str(alpha_id), Counter())
                    for reason, value in source.items():
                        try:
                            counter[str(reason)] += int(value)
                        except (TypeError, ValueError):
                            continue
            trade_events = item.get("trade_events")
            if isinstance(trade_events, list):
                for trade in trade_events:
                    if isinstance(trade, dict):
                        combined_trade_events.append(dict(trade))
        alpha_stats = _summarize_alpha_stats(
            trade_events=combined_trade_events,
            alpha_block_distribution=alpha_block_distribution,
            initial_capital=float(total_initial),
        )
        total_trades = sum(int(item["total_trades"]) for item in symbol_reports)
        total_wins = sum(int(item["wins"]) for item in symbol_reports)
        total_losses = sum(int(item["losses"]) for item in symbol_reports)
        win_rate_pct = (
            (float(total_wins) / float(total_trades) * 100.0) if total_trades > 0 else 0.0
        )
        profit_factor = (total_gross_profit / total_gross_loss) if total_gross_loss > 0 else None
        max_drawdown_pct = max(float(item["max_drawdown_pct"]) for item in symbol_reports)
        total_return_pct = (
            ((total_final - total_initial) / total_initial * 100.0) if total_initial > 0 else 0.0
        )
        fee_to_gross_profit_pct = (
            (total_fees / total_gross_profit * 100.0) if total_gross_profit > 0 else None
        )
        fee_to_trade_gross_pct = (
            (total_fees / total_trade_gross_pnl * 100.0) if total_trade_gross_pnl > 0 else None
        )
        window_slices_6m = _build_half_year_window_summaries(
            symbol_reports=symbol_reports,
            start_ms=int(start_ms),
            end_ms=int(end_ms),
            total_initial_capital=float(total_initial),
        )

        cleaned_count = _cleanup_local_backtest_artifacts(output_root)
        if cleaned_count > 0:
            print(f"[LOCAL_BACKTEST] cleanup removed artifacts={cleaned_count}")

        cycle_counter = Counter(combined_state_counter)
        report_payload: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "backtest": {
                "years": years,
                "symbols": symbols,
                "start_ms": int(start_ms),
                "end_ms": int(end_ms),
                "env": cfg.env,
                "profile": cfg.profile,
                "mode": cfg.mode,
                "strategy_name": active_strategy_name,
                "window_mode": backtest_window_label,
                "backtest_start_utc": _format_utc_iso(start_ms),
                "backtest_end_utc": _format_utc_iso(end_ms),
                "timeframe_base": "15m",
                "timeframe_context": [interval for interval in market_intervals if interval != "15m"],
                "initial_capital_usdt": round(float(initial_capital), 6),
                "initial_capital_locked": True,
                "position_sizing_mode": (
                    "volatility_targeted" if use_ra_2026_mode else "fixed_leverage"
                ),
                "fixed_leverage": round(float(fixed_leverage), 6),
                "fixed_leverage_margin_use_pct": round(float(fixed_leverage_margin_use_pct), 6),
                "reverse_min_hold_bars": int(reverse_min_hold_bars),
                "reverse_cooldown_bars": int(reverse_cooldown_bars),
                "min_expected_edge_over_roundtrip_cost": round(
                    float(min_expected_edge_over_roundtrip_cost),
                    6,
                ),
                "min_reward_risk_ratio": round(float(min_reward_risk_ratio), 6),
                "max_trades_per_day_per_symbol": int(max_trades_per_day_per_symbol),
                "daily_loss_limit_pct": round(float(daily_loss_limit_pct), 6),
                "equity_floor_pct": round(float(equity_floor_pct), 6),
                "max_trade_margin_loss_fraction": round(
                    float(max_trade_margin_loss_fraction),
                    6,
                ),
                "min_signal_score": round(float(min_signal_score), 6),
                "reverse_exit_min_profit_pct": round(float(reverse_exit_min_profit_pct), 6),
                "reverse_exit_min_signal_score": round(float(reverse_exit_min_signal_score), 6),
                "alpha_squeeze_percentile_max": round(float(alpha_squeeze_percentile_max), 6),
                "alpha_expansion_buffer_bps": round(float(alpha_expansion_buffer_bps), 6),
                "alpha_expansion_range_atr_min": round(float(alpha_expansion_range_atr_min), 6),
                "alpha_expansion_body_ratio_min": round(float(alpha_expansion_body_ratio_min), 6),
                "alpha_expansion_close_location_min": round(
                    float(alpha_expansion_close_location_min),
                    6,
                ),
                "alpha_expansion_width_expansion_min": round(
                    float(alpha_expansion_width_expansion_min),
                    6,
                ),
                "alpha_expansion_break_distance_atr_min": round(
                    float(alpha_expansion_break_distance_atr_min),
                    6,
                ),
                "alpha_expansion_breakout_efficiency_min": round(
                    float(alpha_expansion_breakout_efficiency_min),
                    6,
                ),
                "alpha_expansion_breakout_stability_score_min": round(
                    float(alpha_expansion_breakout_stability_score_min),
                    6,
                ),
                "alpha_expansion_breakout_stability_edge_score_min": round(
                    float(alpha_expansion_breakout_stability_edge_score_min),
                    6,
                ),
                "alpha_expansion_quality_score_min": round(
                    float(alpha_expansion_quality_score_min),
                    6,
                ),
                "alpha_expansion_quality_score_v2_min": round(
                    float(alpha_expansion_quality_score_v2_min),
                    6,
                ),
                "alpha_min_volume_ratio": round(float(alpha_min_volume_ratio), 6),
                "alpha_take_profit_r": round(float(alpha_take_profit_r), 6),
                "alpha_time_stop_bars": int(alpha_time_stop_bars),
                "alpha_trend_adx_min_4h": round(float(alpha_trend_adx_min_4h), 6),
                "alpha_trend_adx_max_4h": round(float(alpha_trend_adx_max_4h), 6),
                "alpha_trend_adx_rising_lookback_4h": int(alpha_trend_adx_rising_lookback_4h),
                "alpha_trend_adx_rising_min_delta_4h": round(
                    float(alpha_trend_adx_rising_min_delta_4h),
                    6,
                ),
                "alpha_expected_move_cost_mult": round(
                    float(alpha_expected_move_cost_mult),
                    6,
                ),
                "fb_failed_break_buffer_bps": round(float(fb_failed_break_buffer_bps), 6),
                "fb_wick_ratio_min": round(float(fb_wick_ratio_min), 6),
                "fb_take_profit_r": round(float(fb_take_profit_r), 6),
                "fb_time_stop_bars": int(fb_time_stop_bars),
                "cbr_squeeze_percentile_max": round(float(cbr_squeeze_percentile_max), 6),
                "cbr_breakout_buffer_bps": round(float(cbr_breakout_buffer_bps), 6),
                "cbr_take_profit_r": round(float(cbr_take_profit_r), 6),
                "cbr_time_stop_bars": int(cbr_time_stop_bars),
                "cbr_trend_adx_min_4h": round(float(cbr_trend_adx_min_4h), 6),
                "cbr_ema_gap_trend_min_frac_4h": round(
                    float(cbr_ema_gap_trend_min_frac_4h),
                    6,
                ),
                "cbr_breakout_min_range_atr": round(float(cbr_breakout_min_range_atr), 6),
                "cbr_breakout_min_volume_ratio": round(float(cbr_breakout_min_volume_ratio), 6),
                "sfd_reclaim_sweep_buffer_bps": round(float(sfd_reclaim_sweep_buffer_bps), 6),
                "sfd_reclaim_wick_ratio_min": round(float(sfd_reclaim_wick_ratio_min), 6),
                "sfd_drive_breakout_range_atr_min": round(
                    float(sfd_drive_breakout_range_atr_min),
                    6,
                ),
                "sfd_take_profit_r": round(float(sfd_take_profit_r), 6),
                "pfd_premium_z_min": round(float(pfd_premium_z_min), 6),
                "pfd_funding_24h_min": round(float(pfd_funding_24h_min), 6),
                "pfd_reclaim_buffer_atr": round(float(pfd_reclaim_buffer_atr), 6),
                "pfd_take_profit_r": round(float(pfd_take_profit_r), 6),
                "default_reverse_exit_min_r": round(float(default_reverse_exit_min_r), 6),
                "default_risk_per_trade_pct": round(float(default_risk_per_trade_pct), 6),
                "default_max_effective_leverage": round(
                    float(default_max_effective_leverage),
                    6,
                ),
                "drawdown_scale_start_pct": round(float(drawdown_scale_start_pct), 6),
                "drawdown_scale_end_pct": round(float(drawdown_scale_end_pct), 6),
                "drawdown_margin_scale_min": round(float(drawdown_margin_scale_min), 6),
                "stoploss_streak_trigger": int(stoploss_streak_trigger),
                "stoploss_cooldown_bars": int(stoploss_cooldown_bars),
                "loss_cooldown_bars": int(loss_cooldown_bars),
                "max_peak_drawdown_pct": (
                    None
                    if max_peak_drawdown_pct is None
                    else round(float(max_peak_drawdown_pct), 6)
                ),
                "execution_model": {
                    "fee_bps": round(float(execution_model.fee_bps), 6),
                    "slippage_bps": round(float(execution_model.slippage_bps), 6),
                    "funding_bps_per_8h": round(float(execution_model.funding_bps_per_8h), 6),
                },
            },
            "summary": {
                "symbols_ran": len(symbol_reports),
                "total_cycles": int(total_cycles),
                "state_distribution": dict(cycle_counter),
                "entry_block_distribution": dict(entry_block_counter),
                "total_initial_capital": round(total_initial, 6),
                "total_final_equity": round(total_final, 6),
                "total_net_profit": round(total_net, 6),
                "total_return_pct": round(total_return_pct, 6),
                "total_trades": total_trades,
                "wins": total_wins,
                "losses": total_losses,
                "win_rate_pct": round(win_rate_pct, 6),
                "gross_profit": round(total_gross_profit, 6),
                "gross_loss": round(total_gross_loss, 6),
                "gross_trade_pnl": round(total_trade_gross_pnl, 6),
                "total_fees": round(total_fees, 6),
                "fee_to_gross_profit_pct": (
                    None if fee_to_gross_profit_pct is None else round(fee_to_gross_profit_pct, 6)
                ),
                "fee_to_trade_gross_pct": (
                    None if fee_to_trade_gross_pct is None else round(fee_to_trade_gross_pct, 6)
                ),
                "total_funding_pnl": round(total_funding_pnl, 6),
                "profit_factor": None if profit_factor is None else round(profit_factor, 6),
                "max_drawdown_pct": round(max_drawdown_pct, 6),
                "alpha_stats": alpha_stats,
                "window_slices_6m": window_slices_6m,
                "cleaned_artifacts": cleaned_count,
            },
            "symbols": symbol_reports,
        }
        if report_path is not None:
            target = Path(report_path)
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            target = output_root / f"local_backtest_{stamp}.json"
        target.write_text(json.dumps(report_payload, indent=2, ensure_ascii=True), encoding="utf-8")
        markdown_target = _write_local_backtest_markdown(
            report_payload=report_payload, target_json=target
        )

        print("LOCAL BACKTEST SUMMARY")
        print(
            " | ".join(
                [
                    f"strategy={active_strategy_name}",
                    f"sizing={'vol_target' if use_ra_2026_mode else 'fixed'}",
                    f"net={total_net:.2f} USDT",
                    f"return={total_return_pct:.2f}%",
                    f"win_rate={win_rate_pct:.2f}%",
                    f"pf={(profit_factor if profit_factor is not None else 0.0):.3f}",
                    f"lev={fixed_leverage:.0f}x",
                    f"margin_use={fixed_leverage_margin_use_pct * 100:.0f}%",
                    f"daily_stop={daily_loss_limit_pct * 100:.1f}%",
                    f"equity_floor={equity_floor_pct * 100:.1f}%",
                    f"score_min={min_signal_score:.2f}",
                    f"rr_min={min_reward_risk_ratio:.2f}",
                    f"rev_profit_min={reverse_exit_min_profit_pct * 100:.2f}%",
                    f"rev_score_min={reverse_exit_min_signal_score:.2f}",
                    f"dd_scale={drawdown_scale_start_pct * 100:.0f}->{drawdown_scale_end_pct * 100:.0f}%/{drawdown_margin_scale_min * 100:.0f}%",
                    f"sl_streak={stoploss_streak_trigger}",
                    f"sl_cd={stoploss_cooldown_bars}",
                    f"loss_cd={loss_cooldown_bars}",
                    f"fees={total_fees:.2f}",
                    f"funding={total_funding_pnl:.2f}",
                    f"max_dd={max_drawdown_pct:.2f}%",
                    f"trades={total_trades}",
                    f"cleaned={cleaned_count}",
                ]
            )
        )
        top_entry_block_items: list[tuple[str, int]] = []
        for reason, count in entry_block_counter.items():
            try:
                top_entry_block_items.append((str(reason), int(count)))
            except (TypeError, ValueError):
                continue
        top_entry_block_items.sort(key=lambda item: (-item[1], item[0]))
        if top_entry_block_items:
            top_entry_preview = ", ".join(
                f"{reason}:{count}" for reason, count in top_entry_block_items[:8]
            )
            print(f"ENTRY_BLOCK_TOP={top_entry_preview}")

        suggest_signal = float(min_signal_score)
        suggest_rr = float(min_reward_risk_ratio)
        suggest_cd = int(reverse_cooldown_bars)
        suggest_edge = float(min_expected_edge_over_roundtrip_cost)
        suggest_trades = max(1, int(max_trades_per_day_per_symbol))
        suggest_hold = max(0, int(reverse_min_hold_bars) - 2)
        suggest_reason = ""

        if top_entry_block_items:
            suggest_reason = top_entry_block_items[0][0]
            top_count = int(top_entry_block_items[0][1])
            if top_count > 0:
                if suggest_reason == "score_quality_block":
                    suggest_signal = max(0.10, suggest_signal - 0.15)
                elif suggest_reason == "edge_cost_block":
                    suggest_edge = max(1.0, suggest_edge - 0.60)
                elif suggest_reason in {
                    "reward_risk_block",
                    "reward_risk_missing_block",
                }:
                    suggest_rr = max(0.40, suggest_rr - 0.80)
                elif suggest_reason == "reverse_cooldown_block":
                    suggest_cd = max(6, int(suggest_cd * 0.6))
                elif suggest_reason == "reverse_hold_block":
                    suggest_hold = max(0, suggest_hold - 2)
                elif suggest_reason in {
                    "stoploss_cooldown_block",
                    "loss_streak_cooldown_block",
                }:
                    suggest_cd = max(6, int(suggest_cd * 0.7))
                    suggest_trades = max(1, suggest_trades - 1)

        weak_sample = int(total_trades) < 12 or (
            profit_factor is not None and float(profit_factor) < 1.0
        )
        if weak_sample:
            symbol_csv = ",".join(symbols)
            suggest_signal = max(0.10, suggest_signal - 0.05)
            suggest_rr = max(0.40, suggest_rr - 0.10)
            suggest_cd = max(6, int(suggest_cd * 0.9))
            suggest_trades = max(1, suggest_trades)
            suggest_signal = min(suggest_signal, float(min_signal_score))
            suggest_env = [
                f"BACKTEST_OFFLINE={1 if offline else 0}",
                f"BACKTEST_SYMBOLS={symbol_csv}",
                f"MIN_SIGNAL_SCORE={suggest_signal:.2f}",
                f"MIN_REWARD_RISK_RATIO={suggest_rr:.2f}",
                f"MIN_EXPECTED_EDGE_MULTIPLE={suggest_edge:.2f}",
                f"REVERSE_COOLDOWN_BARS={suggest_cd}",
                f"REVERSE_MIN_HOLD_BARS={suggest_hold}",
                f"MAX_TRADES_PER_DAY={suggest_trades}",
            ]
            if backtest_window_label == "fixed":
                suggest_env.extend(
                    [
                        f"BACKTEST_START_UTC={_format_utc_iso(start_ms)}",
                        f"BACKTEST_END_UTC={_format_utc_iso(end_ms)}",
                    ]
                )
            print(f"SUGGEST={' '.join(suggest_env)}")
            suggest_cmd = f"{' '.join(suggest_env)} bash local_backtest/run_local_backtest.sh {symbol_csv} {years} {initial_capital:g}"
            print(
                f"SUGGEST_CMD={suggest_cmd}",
            )
            if suggest_reason:
                print(f"SUGGEST_REASON=top_entry_block={suggest_reason}")

        print(f"REPORT_JSON={target}")
        print(f"REPORT_MD={markdown_target}")

        print(
            json.dumps(
                {
                    "backtest": {
                        "status": "completed",
                        "report": str(target),
                        "report_markdown": str(markdown_target),
                        "symbols": symbols,
                        "years": years,
                        "cleaned_artifacts": cleaned_count,
                    }
                },
                ensure_ascii=True,
            )
        )
        return 0
    finally:
        leftovers = _cleanup_local_backtest_artifacts(output_root)
        if leftovers > 0:
            print(f"[LOCAL_BACKTEST] cleanup removed leftovers={leftovers}")


def _boot(cfg: EffectiveConfig, *, loop_enabled: bool = False, max_cycles: int = 0) -> None:
    _configure_runtime_logging(component="runtime_boot")
    storage: RuntimeStorage | None = None
    state_store: EngineStateStore | None = None
    runtime_state_provider = lambda: {  # noqa: E731
        **_runtime_identity_payload(cfg),
        "engine_state": state_store.get().status if state_store is not None else None,
        "last_reconcile_at": state_store.get().last_reconcile_at if state_store is not None else None,
    }
    with _live_runtime_lock(enabled=cfg.mode == "live"):
        with _dirty_runtime_marker(
            enabled=cfg.mode == "live",
            cfg=cfg,
            state_provider=runtime_state_provider,
        ) as dirty_restart_detected:
            logger.info(
                "runtime_boot",
                extra={
                    "event": "runtime_boot",
                    "mode": cfg.mode,
                    "profile": cfg.profile,
                    "loop_enabled": bool(loop_enabled),
                    "dirty_restart_detected": bool(dirty_restart_detected),
                },
            )
            event_bus = EventBus()
            storage, state_store, ops, adapter, rest_client = _build_runtime(cfg)
            state_store.set(mode=cfg.mode, status="RUNNING")

            if cfg.mode == "live" and dirty_restart_detected:
                ops.safe_mode()

            if cfg.behavior.ops.pause_on_start:
                ops.pause()

            risk = RiskManager(config=cfg.behavior.risk)
            _ = risk.validate_leverage(cfg.behavior.risk.max_leverage)
            kill_switch = KillSwitch()
            if kill_switch.tripped and cfg.behavior.ops.flatten_on_kill:
                _ = asyncio.run(ops.flatten(symbol=cfg.behavior.exchange.default_symbol))

            _ = adapter.ping()

            brackets = BracketPlanner(
                cfg=BracketConfig(
                    take_profit_pct=cfg.behavior.tpsl.take_profit_pct,
                    stop_loss_pct=cfg.behavior.tpsl.stop_loss_pct,
                )
            )
            _ = brackets.levels(entry_price=100.0)

            notifier = Notifier(
                enabled=cfg.behavior.notify.enabled,
                provider=cfg.behavior.notify.provider,
                webhook_url=cfg.secrets.notify_webhook_url,
            )
            notifier.send("v2 boot completed")

            journal_logger = None
            enabled_strategies = [
                str(entry.name).strip()
                for entry in cfg.behavior.strategies
                if bool(getattr(entry, "enabled", False))
            ]
            active_strategy_name = enabled_strategies[0] if enabled_strategies else "none"

            if cfg.mode == "shadow":

                def _journal_logger(payload: dict[str, Any]) -> None:
                    print(
                        json.dumps(
                            {
                                "strategy": active_strategy_name,
                                "regime": payload.get("regime"),
                                "allowed_side": payload.get("allowed_side"),
                                "signals": payload.get("signals"),
                                "blocks": payload.get("blocks"),
                                "decision": payload.get("decision", {}).get("intent")
                                if isinstance(payload.get("decision"), dict)
                                else None,
                                "reasons": payload.get("decision", {}).get("reason")
                                if isinstance(payload.get("decision"), dict)
                                else None,
                            },
                            ensure_ascii=True,
                        )
                    )

                journal_logger = _journal_logger

            order_manager = OrderManager(event_bus=event_bus)

            kernel = build_default_kernel(
                state_store=state_store,
                behavior=cfg.behavior,
                profile=cfg.profile,
                mode=cfg.mode,
                tick=0,
                dry_run=cfg.mode == "shadow",
                rest_client=rest_client,
                journal_logger=journal_logger,
            )
            scheduler = Scheduler(
                tick_seconds=cfg.behavior.scheduler.tick_seconds,
                event_bus=event_bus,
            )

            def _on_scheduler_tick(event: Event) -> None:
                _ = storage.append_journal(
                    event_type=event.topic,
                    payload_json=render_effective_config(cfg),
                )

            event_bus.subscribe(
                "scheduler.tick",
                _on_scheduler_tick,
            )
            stop_requested = False

            def _request_stop(_sig: int, _frame: object | None) -> None:
                nonlocal stop_requested
                stop_requested = True

            for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
                if sig is None:
                    continue
                try:
                    signal.signal(sig, _request_stop)
                except (OSError, RuntimeError, ValueError):
                    continue

            cycles = 0
            while True:
                if hasattr(kernel, "set_tick"):
                    kernel.set_tick(cycles)
                cycle = kernel.run_once()
                event_bus.publish(
                    Event(
                        topic="kernel.cycle",
                        payload={
                            "state": cycle.state,
                            "reason": cycle.reason,
                            "symbol": cycle.candidate.symbol if cycle.candidate is not None else None,
                        },
                    )
                )
                if cycle.state in {"executed", "execution_failed"}:
                    notifier.send(f"v2 cycle {cycle.state}: {cycle.reason}")

                scheduler.run_once()

                if ops.can_open_new_entries():
                    order_manager.submit({"symbol": cfg.behavior.exchange.default_symbol, "mode": cfg.mode})
                else:
                    event_bus.publish(
                        Event(
                            topic="order.entry_blocked",
                            payload={"symbol": cfg.behavior.exchange.default_symbol, "paused": True},
                        )
                    )
                cycles += 1
                if not loop_enabled:
                    break
                if max_cycles > 0 and cycles >= max_cycles:
                    break
                if stop_requested:
                    break
                asyncio.run(asyncio.sleep(max(0.2, float(cfg.behavior.scheduler.tick_seconds))))

            event_bus.publish(Event(topic="v2.started", payload={"profile": cfg.profile, "env": cfg.env}))


def _run_ops_action(cfg: EffectiveConfig, *, action: str, symbol: str | None) -> int:
    _storage, _state_store, ops, _adapter, _rest = _build_runtime(cfg)
    symbol_v = symbol or cfg.behavior.exchange.default_symbol

    if action == "pause":
        ops.pause()
        print(json.dumps({"action": "pause", "paused": True}, ensure_ascii=True))
        return 0
    if action == "resume":
        ops.resume()
        print(
            json.dumps({"action": "resume", "paused": False, "safe_mode": False}, ensure_ascii=True)
        )
        return 0
    if action == "safe_mode":
        ops.safe_mode()
        print(
            json.dumps(
                {"action": "safe_mode", "paused": True, "safe_mode": True}, ensure_ascii=True
            )
        )
        return 0
    if action == "flatten":
        result = asyncio.run(ops.flatten(symbol=symbol_v))
        print(
            json.dumps(
                {
                    "action": "flatten",
                    "symbol": result.symbol,
                    "paused": result.paused,
                    "safe_mode": result.safe_mode,
                    "open_regular_orders": result.open_regular_orders,
                    "open_algo_orders": result.open_algo_orders,
                    "position_amt": result.position_amt,
                },
                ensure_ascii=True,
            )
        )
        return 0
    return 0


def _serve_ops_http(cfg: EffectiveConfig, *, host: str, port: int) -> int:
    import uvicorn

    _storage, _state_store, ops, _adapter, _rest = _build_runtime(cfg)
    app = create_ops_http_app(ops=ops)
    uvicorn.run(app, host=host, port=port)
    return 0


def _serve_control_http(cfg: EffectiveConfig, *, host: str, port: int) -> int:
    import uvicorn

    try:
        _configure_runtime_logging(component="control_runtime")
        state_store: EngineStateStore | None = None
        controller = None
        runtime_state_provider = lambda: {  # noqa: E731
            **_runtime_identity_payload(cfg, host=host, port=port),
            "engine_state": state_store.get().status if state_store is not None else None,
            "last_reconcile_at": state_store.get().last_reconcile_at if state_store is not None else None,
            "readyz": controller._readyz_snapshot() if controller is not None else None,
        }
        with _live_runtime_lock(enabled=cfg.mode == "live"):
            with _dirty_runtime_marker(
                enabled=cfg.mode == "live",
                cfg=cfg,
                state_provider=runtime_state_provider,
            ) as dirty_restart_detected:
                logger.info(
                    "runtime_boot",
                    extra={
                        "event": "runtime_boot",
                        "mode": cfg.mode,
                        "profile": cfg.profile,
                        "host": host,
                        "port": port,
                        "dirty_restart_detected": bool(dirty_restart_detected),
                    },
                )
                event_bus = EventBus()
                storage, state_store, ops, adapter, rest_client = _build_runtime(cfg)
                state_store.set(mode=cfg.mode, status="STOPPED")

                notifier = Notifier(
                    enabled=cfg.behavior.notify.enabled,
                    provider=cfg.behavior.notify.provider,
                    webhook_url=cfg.secrets.notify_webhook_url,
                )
                market_data_state: dict[str, Any] = {
                    "last_market_data_at": None,
                    "last_market_symbol_count": 0,
                    "last_market_data_source_ok_at": None,
                    "last_market_data_source_fail_at": None,
                    "last_market_data_source_error": None,
                }

                def _on_market_data(snapshot: dict[str, Any]) -> None:
                    market_data_state["last_market_data_at"] = datetime.now(timezone.utc).isoformat()
                    market_data_state["last_market_data_source_ok_at"] = market_data_state["last_market_data_at"]
                    market_data_state["last_market_data_source_fail_at"] = None
                    market_data_state["last_market_data_source_error"] = None
                    symbols_payload = snapshot.get("symbols")
                    market_data_state["last_market_symbol_count"] = (
                        len(symbols_payload) if isinstance(symbols_payload, dict) else 0
                    )

                kernel = build_default_kernel(
                    state_store=state_store,
                    behavior=cfg.behavior,
                    profile=cfg.profile,
                    mode=cfg.mode,
                    tick=0,
                    dry_run=cfg.mode == "shadow",
                    rest_client=rest_client,
                    market_data_observer=_on_market_data,
                    journal_logger=None,
                )
                _ = storage
                user_stream_manager = None
                if cfg.mode == "live":
                    user_stream_manager = adapter.create_user_stream_manager(cfg=cfg)
                scheduler = Scheduler(
                    tick_seconds=cfg.behavior.scheduler.tick_seconds,
                    event_bus=event_bus,
                )
                balance_rest_client = _build_control_balance_rest_client(
                    cfg=cfg, runtime_rest_client=rest_client
                )
                controller = build_runtime_controller(
                    cfg=cfg,
                    state_store=state_store,
                    ops=ops,
                    kernel=kernel,
                    scheduler=scheduler,
                    event_bus=event_bus,
                    notifier=notifier,
                    rest_client=balance_rest_client,
                    user_stream_manager=user_stream_manager,
                    market_data_state=market_data_state,
                    runtime_lock_active=True,
                    dirty_restart_detected=bool(dirty_restart_detected),
                )
                app = create_control_http_app(controller=controller)
                uvicorn.run(app, host=host, port=port)
    except RuntimeError as exc:
        if str(exc).startswith("runtime_lock_held:"):
            print(json.dumps({"error": str(exc)}, ensure_ascii=True))
            return 1
        raise
    return 0


def _build_control_balance_rest_client(
    *, cfg: EffectiveConfig, runtime_rest_client: Any | None
) -> Any | None:
    if runtime_rest_client is not None:
        return runtime_rest_client
    if not cfg.secrets.binance_api_key or not cfg.secrets.binance_api_secret:
        return None
    return BinanceRESTClient(
        env=cfg.env,
        api_key=cfg.secrets.binance_api_key,
        api_secret=cfg.secrets.binance_api_secret,
        recv_window_ms=cfg.behavior.exchange.recv_window_ms,
        time_sync_enabled=True,
        rate_limit_per_sec=cfg.behavior.exchange.request_rate_limit_per_sec,
        backoff_policy=BackoffPolicy(
            base_seconds=cfg.behavior.exchange.backoff_base_seconds,
            cap_seconds=cfg.behavior.exchange.backoff_cap_seconds,
        ),
    )


def _evaluate_runtime_preflight(
    *,
    controller: Any,
    cfg: EffectiveConfig,
    host: str,
    port: int,
) -> dict[str, Any]:
    readyz = controller._readyz_snapshot()
    readiness = controller._live_readiness_snapshot()
    identity = _runtime_identity_payload(cfg, host=host, port=port)
    checks = {
        "identity": {
            "ok": True,
            "detail": {
                "profile": cfg.profile,
                "mode": cfg.mode,
                "env": cfg.env,
                "live_trading_enabled": _live_trading_enabled(cfg),
            },
        },
        "bind_localhost": {
            "ok": host == "127.0.0.1",
            "detail": {"host": host, "port": int(port)},
        },
        "readyz": {
            "ok": bool(readyz.get("ready")),
            "detail": readyz,
        },
        "readiness": {
            "ok": str(readiness.get("overall")) != "blocked",
            "detail": readiness,
        },
        "recovery_clean": {
            "ok": not bool(readyz.get("recovery_required")),
            "detail": {
                "recovery_required": bool(readyz.get("recovery_required")),
                "recovery_reason": readyz.get("recovery_reason"),
            },
        },
        "uncertainty_clean": {
            "ok": not bool(readyz.get("state_uncertain")),
            "detail": {
                "state_uncertain": bool(readyz.get("state_uncertain")),
                "reason": readyz.get("state_uncertain_reason"),
            },
        },
        "freshness_clean": {
            "ok": (not bool(readyz.get("user_ws_stale"))) and (not bool(readyz.get("market_data_stale"))),
            "detail": {
                "user_ws_stale": bool(readyz.get("user_ws_stale")),
                "market_data_stale": bool(readyz.get("market_data_stale")),
                "market_data_source_stale": bool(readyz.get("market_data_source_stale")),
                "market_data_source_error": readyz.get("market_data_source_error"),
            },
        },
        "private_auth": {
            "ok": bool(readyz.get("private_auth_ok", True)),
            "detail": {"private_auth_ok": bool(readyz.get("private_auth_ok", True))},
        },
        "paper_live_separation": {
            "ok": not (_live_trading_enabled(cfg) and str(cfg.env) != "prod"),
            "detail": {
                "mode": cfg.mode,
                "env": cfg.env,
                "live_trading_enabled": _live_trading_enabled(cfg),
            },
        },
    }
    ok = all(bool(item["ok"]) for item in checks.values())
    summary_lines = [
        f"프로필={cfg.profile} / 모드={cfg.mode} / 환경={cfg.env}",
        f"실거래활성={'예' if _live_trading_enabled(cfg) else '아니오'} / 바인드={host}:{port}",
        (
            "배포 게이트 통과"
            if ok
            else f"배포 게이트 차단: {[name for name, item in checks.items() if not bool(item['ok'])]}"
        ),
    ]
    return {
        "ok": ok,
        "identity": identity,
        "checks": checks,
        "readyz": readyz,
        "readiness": readiness,
        "summary_lines": summary_lines,
    }


def _run_runtime_preflight(cfg: EffectiveConfig, *, host: str, port: int) -> int:
    _configure_runtime_logging(component="runtime_preflight")
    event_bus = EventBus()
    storage: RuntimeStorage | None = None
    state_store: EngineStateStore | None = None
    controller = None
    payload: dict[str, Any] | None = None
    runtime_state_provider = lambda: {  # noqa: E731
        **_runtime_identity_payload(cfg, host=host, port=port),
        "engine_state": state_store.get().status if state_store is not None else None,
        "last_reconcile_at": state_store.get().last_reconcile_at if state_store is not None else None,
        "readyz": controller._readyz_snapshot() if controller is not None else None,
    }
    try:
        with _live_runtime_lock(enabled=cfg.mode == "live"):
            with _dirty_runtime_marker(
                enabled=False,
                cfg=cfg,
                state_provider=runtime_state_provider,
            ):
                storage, state_store, ops, adapter, rest_client = _build_runtime(cfg)
            notifier = Notifier(
                enabled=cfg.behavior.notify.enabled,
                provider=cfg.behavior.notify.provider,
                webhook_url=cfg.secrets.notify_webhook_url,
            )
            market_data_state: dict[str, Any] = {
                "last_market_data_at": None,
                "last_market_symbol_count": 0,
                "last_market_data_source_ok_at": None,
                "last_market_data_source_fail_at": None,
                "last_market_data_source_error": None,
            }

            def _on_market_data(snapshot: dict[str, Any]) -> None:
                market_data_state["last_market_data_at"] = datetime.now(timezone.utc).isoformat()
                market_data_state["last_market_data_source_ok_at"] = market_data_state["last_market_data_at"]
                market_data_state["last_market_data_source_fail_at"] = None
                market_data_state["last_market_data_source_error"] = None
                symbols_payload = snapshot.get("symbols")
                market_data_state["last_market_symbol_count"] = (
                    len(symbols_payload) if isinstance(symbols_payload, dict) else 0
                )

            kernel = build_default_kernel(
                state_store=state_store,
                behavior=cfg.behavior,
                profile=cfg.profile,
                mode=cfg.mode,
                tick=0,
                dry_run=cfg.mode == "shadow",
                rest_client=rest_client,
                market_data_observer=_on_market_data,
                journal_logger=None,
            )
            user_stream_manager = adapter.create_user_stream_manager(cfg=cfg) if cfg.mode == "live" else None
            scheduler = Scheduler(
                tick_seconds=cfg.behavior.scheduler.tick_seconds,
                event_bus=event_bus,
            )
            balance_rest_client = _build_control_balance_rest_client(
                cfg=cfg, runtime_rest_client=rest_client
            )
            controller = build_runtime_controller(
                cfg=cfg,
                state_store=state_store,
                ops=ops,
                kernel=kernel,
                scheduler=scheduler,
                event_bus=event_bus,
                notifier=notifier,
                rest_client=balance_rest_client,
                user_stream_manager=user_stream_manager,
                market_data_state=market_data_state,
                runtime_lock_active=True,
                dirty_restart_detected=False,
            )
            if cfg.mode == "live":
                asyncio.run(controller.start_live_services())
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline:
                    controller._maybe_probe_market_data()
                    readyz = controller._readyz_snapshot()
                    if bool(readyz.get("private_auth_ok")) and not bool(readyz.get("market_data_stale")):
                        if controller._user_stream_started:
                            if not bool(readyz.get("user_ws_stale")):
                                break
                        else:
                            break
                    time.sleep(0.1)
            else:
                controller._maybe_probe_market_data()

            try:
                payload = _evaluate_runtime_preflight(
                    controller=controller,
                    cfg=cfg,
                    host=host,
                    port=port,
                )
                for line in payload["summary_lines"]:
                    print(f"[runtime-preflight] {line}")
                print(json.dumps(payload, ensure_ascii=True))
                return 0 if bool(payload["ok"]) else 1
            finally:
                if cfg.mode == "live":
                    asyncio.run(controller.stop_live_services())
                if storage is not None and payload is not None:
                    storage.save_runtime_marker(marker_key="runtime_preflight", payload=payload)
    except RuntimeError as exc:
        if str(exc).startswith("runtime_lock_held:"):
            print(json.dumps({"error": str(exc)}, ensure_ascii=True))
            return 1
        raise


def _run_deploy_prep(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "v2" / "scripts" / "deploy_prep.sh"
    cmd = [
        "bash",
        str(script),
        "--profile",
        str(args.profile),
        "--mode",
        str(args.mode),
        "--env",
        str(args.env),
        "--config",
        str(args.config if args.config else "config/config.yaml"),
        "--report-dir",
        str(args.report_dir),
        "--test-scope",
        str(args.test_scope),
    ]
    if args.keep_reports is not None:
        cmd.extend(["--keep-reports", str(max(int(args.keep_reports), 0))])
    done = subprocess.run(cmd, cwd=str(repo_root), check=False)
    return int(done.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.deploy_prep:
        return _run_deploy_prep(args)

    effective = load_effective_config(
        profile=args.profile,
        mode=args.mode,
        env=args.env,
        config_path=args.config,
        env_file_path=args.env_file,
    )

    print("[v2] effective config")
    print(render_effective_config(effective))
    if args.control_http:
        _print_runtime_banner(
            effective,
            host=args.control_http_host,
            port=int(args.control_http_port),
        )
    elif args.ops_http:
        _print_runtime_banner(
            effective,
            host=args.ops_http_host,
            port=int(args.ops_http_port),
        )
    else:
        _print_runtime_banner(effective)

    if args.ops_http:
        return _serve_ops_http(effective, host=args.ops_http_host, port=args.ops_http_port)

    if args.control_http:
        return _serve_control_http(
            effective, host=args.control_http_host, port=args.control_http_port
        )

    if args.runtime_preflight:
        return _run_runtime_preflight(
            effective,
            host=args.control_http_host,
            port=int(args.control_http_port),
        )

    if args.local_backtest:
        parsed_backtest_start_ms = _parse_utc_datetime_ms(args.backtest_start_utc)
        parsed_backtest_end_ms = _parse_utc_datetime_ms(args.backtest_end_utc)

        if args.backtest_start_utc is not None and parsed_backtest_start_ms is None:
            print(
                json.dumps(
                    {
                        "error": "invalid --backtest-start-utc format. use ISO-8601 UTC (e.g. 2024-01-01T00:00:00Z)",
                    },
                    ensure_ascii=True,
                )
            )
            return 1

        if args.backtest_end_utc is not None and parsed_backtest_end_ms is None:
            print(
                json.dumps(
                    {
                        "error": "invalid --backtest-end-utc format. use ISO-8601 UTC (e.g. 2024-12-31T23:59:59Z)",
                    },
                    ensure_ascii=True,
                )
            )
            return 1

        return _run_local_backtest(
            effective,
            symbols=_parse_symbols(args.backtest_symbols),
            years=max(int(args.backtest_years), 0),
            initial_capital=float(args.backtest_initial_capital),
            fee_bps=float(args.backtest_fee_bps),
            slippage_bps=float(args.backtest_slippage_bps),
            funding_bps_per_8h=float(args.backtest_funding_bps_8h),
            margin_use_pct=float(args.backtest_margin_use_pct),
            replay_workers=int(args.backtest_replay_workers),
            reverse_min_hold_bars=int(args.backtest_reverse_min_hold_bars),
            reverse_cooldown_bars=int(args.backtest_reverse_cooldown_bars),
            min_expected_edge_multiple=float(args.backtest_min_expected_edge_multiple),
            min_reward_risk_ratio=float(args.backtest_min_reward_risk_ratio),
            max_trades_per_day=int(args.backtest_max_trades_per_day),
            daily_loss_limit_pct=float(args.backtest_daily_loss_limit_pct),
            equity_floor_pct=float(args.backtest_equity_floor_pct),
            max_trade_margin_loss_fraction=float(args.backtest_max_trade_margin_loss_fraction),
            min_signal_score=float(args.backtest_min_signal_score),
            reverse_exit_min_profit_pct=float(args.backtest_reverse_exit_min_profit_pct),
            reverse_exit_min_signal_score=float(args.backtest_reverse_exit_min_signal_score),
            drawdown_scale_start_pct=float(args.backtest_drawdown_scale_start_pct),
            drawdown_scale_end_pct=float(args.backtest_drawdown_scale_end_pct),
            drawdown_margin_scale_min=float(args.backtest_drawdown_margin_scale_min),
            stoploss_streak_trigger=int(args.backtest_stoploss_streak_trigger),
            stoploss_cooldown_bars=int(args.backtest_stoploss_cooldown_bars),
            loss_cooldown_bars=int(args.backtest_loss_cooldown_bars),
            alpha_squeeze_percentile_max=float(args.backtest_alpha_squeeze_percentile_max),
            alpha_expansion_buffer_bps=float(args.backtest_alpha_expansion_buffer_bps),
            alpha_expansion_range_atr_min=float(args.backtest_alpha_expansion_range_atr_min),
            alpha_expansion_body_ratio_min=float(args.backtest_alpha_expansion_body_ratio_min),
            alpha_expansion_close_location_min=float(
                args.backtest_alpha_expansion_close_location_min
            ),
            alpha_expansion_width_expansion_min=float(
                args.backtest_alpha_expansion_width_expansion_min
            ),
            alpha_expansion_break_distance_atr_min=float(
                args.backtest_alpha_expansion_break_distance_atr_min
            ),
            alpha_expansion_breakout_efficiency_min=float(
                args.backtest_alpha_expansion_breakout_efficiency_min
            ),
            alpha_expansion_breakout_stability_score_min=float(
                args.backtest_alpha_expansion_breakout_stability_score_min
            ),
            alpha_expansion_breakout_stability_edge_score_min=float(
                args.backtest_alpha_expansion_breakout_stability_edge_score_min
            ),
            alpha_expansion_quality_score_min=float(
                args.backtest_alpha_expansion_quality_score_min
            ),
            alpha_expansion_quality_score_v2_min=float(
                args.backtest_alpha_expansion_quality_score_v2_min
            ),
            alpha_min_volume_ratio=float(args.backtest_alpha_min_volume_ratio),
            alpha_take_profit_r=float(args.backtest_alpha_take_profit_r),
            alpha_time_stop_bars=int(args.backtest_alpha_time_stop_bars),
            alpha_trend_adx_min_4h=float(args.backtest_alpha_trend_adx_min_4h),
            alpha_trend_adx_max_4h=float(args.backtest_alpha_trend_adx_max_4h),
            alpha_trend_adx_rising_lookback_4h=int(
                args.backtest_alpha_trend_adx_rising_lookback_4h
            ),
            alpha_trend_adx_rising_min_delta_4h=float(
                args.backtest_alpha_trend_adx_rising_min_delta_4h
            ),
            alpha_expected_move_cost_mult=float(args.backtest_alpha_expected_move_cost_mult),
            fb_failed_break_buffer_bps=float(args.backtest_fb_failed_break_buffer_bps),
            fb_wick_ratio_min=float(args.backtest_fb_wick_ratio_min),
            fb_take_profit_r=float(args.backtest_fb_take_profit_r),
            fb_time_stop_bars=int(args.backtest_fb_time_stop_bars),
            cbr_squeeze_percentile_max=float(args.backtest_cbr_squeeze_percentile_max),
            cbr_breakout_buffer_bps=float(args.backtest_cbr_breakout_buffer_bps),
            cbr_take_profit_r=float(args.backtest_cbr_take_profit_r),
            cbr_time_stop_bars=int(args.backtest_cbr_time_stop_bars),
            cbr_trend_adx_min_4h=float(args.backtest_cbr_trend_adx_min_4h),
            cbr_ema_gap_trend_min_frac_4h=float(args.backtest_cbr_ema_gap_trend_min_frac_4h),
            cbr_breakout_min_range_atr=float(args.backtest_cbr_breakout_min_range_atr),
            cbr_breakout_min_volume_ratio=float(args.backtest_cbr_breakout_min_volume_ratio),
            sfd_reclaim_sweep_buffer_bps=float(args.backtest_sfd_reclaim_sweep_buffer_bps),
            sfd_reclaim_wick_ratio_min=float(args.backtest_sfd_reclaim_wick_ratio_min),
            sfd_drive_breakout_range_atr_min=float(
                args.backtest_sfd_drive_breakout_range_atr_min
            ),
            sfd_take_profit_r=float(args.backtest_sfd_take_profit_r),
            pfd_premium_z_min=float(args.backtest_pfd_premium_z_min),
            pfd_funding_24h_min=float(args.backtest_pfd_funding_24h_min),
            pfd_reclaim_buffer_atr=float(args.backtest_pfd_reclaim_buffer_atr),
            pfd_take_profit_r=float(args.backtest_pfd_take_profit_r),
            offline=bool(args.backtest_offline),
            fetch_sleep_sec=float(args.backtest_fetch_sleep_sec),
            backtest_start_ms=parsed_backtest_start_ms,
            backtest_end_ms=parsed_backtest_end_ms,
            report_dir=args.report_dir,
            report_path=args.report_path,
        )

    if args.replay is not None:
        return _run_replay(
            effective,
            replay_path=args.replay,
            report_dir=args.report_dir,
            report_path=args.report_path,
        )

    if args.ops_action != "none":
        return _run_ops_action(effective, action=args.ops_action, symbol=args.ops_symbol)

    try:
        _boot(effective, loop_enabled=args.loop, max_cycles=max(int(args.max_cycles), 0))
    except RuntimeError as exc:
        if str(exc).startswith("runtime_lock_held:"):
            print(json.dumps({"error": str(exc)}, ensure_ascii=True))
            return 1
        raise
    print("[v2] started")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
