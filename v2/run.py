from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import subprocess
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Sequence

from v2.backtest.analytics import (  # noqa: F401
    _BacktestExecutionModel,
    _calc_max_drawdown_pct,
    _entry_reward_and_risk_pct,
    _OpenTrade,
    _summarize_alpha_stats,
)
from v2.backtest.cache_loader import (  # noqa: F401
    _klines_csv_has_volume_column,
    _load_cached_funding_for_range,
    _load_cached_klines_for_range,
    _load_cached_premium_for_range,
    _read_funding_csv_rows,
    _read_klines_csv_rows,
    _write_funding_csv,
    _write_klines_csv,
)
from v2.backtest.cache_paths import (  # noqa: F401
    _cache_file_for_funding,
    _cache_file_for_klines,
    _cache_file_for_premium,
    _interval_to_ms,
)
from v2.backtest.common import (  # noqa: F401
    _candidate_from_payload,
    _candidate_to_payload,
    _to_float,
    _to_int,
)
from v2.backtest.decision_types import _ReplayDecision, _ReplayDecisionBySymbol  # noqa: F401
from v2.backtest.downloader import (  # noqa: F401
    _aggregate_klines_to_interval,
    _fetch_funding_rate_history,
    _fetch_klines_15m,
    _fetch_klines_interval,
    _fetch_premium_index_klines_15m,
)
from v2.backtest.policy import (  # noqa: F401
    _LEGACY_PORTFOLIO_BACKTEST_STRATEGIES,
    _VOL_TARGET_STRATEGIES,
    LOCAL_BACKTEST_INITIAL_CAPITAL_USDT,
    _is_vol_target_backtest_strategy,
    _locked_local_backtest_initial_capital,
    _resolve_market_intervals,
)
from v2.backtest.providers import (  # noqa: F401
    _HistoricalPortfolioSnapshotProvider,
    _HistoricalSnapshotProvider,
    _ReplaySnapshotProvider,
    _sum_recent_funding,
    _zscore_latest,
)
from v2.backtest.research_policy import _portfolio_research_gate  # noqa: F401
from v2.backtest.snapshots import _FundingRateRow, _Kline15m, _ReplayFrame  # noqa: F401
from v2.backtest.summaries import _build_half_year_window_summaries, _format_utc_iso  # noqa: F401
from v2.clean_room.kernel import _build_strategy_selector  # noqa: F401
from v2.common.logging_setup import LoggingConfig, setup_logging
from v2.config.loader import (
    EffectiveConfig,
    load_effective_config,
    render_effective_config,
)
from v2.engine import EngineStateStore
from v2.exchange import BinanceAdapter
from v2.ops import OpsController
from v2.storage import RuntimeStorage
from v2.tpsl import BracketPlanner  # noqa: F401

logger = logging.getLogger(__name__)


def _lazy_impl(module_path: str, attr_name: str) -> Any:
    return getattr(import_module(module_path), attr_name)


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
    from v2.cli.parser import build_parser

    return build_parser()


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

def _safe_json_loads(raw: Any) -> Any:
    from v2.backtest.row_loader import _safe_json_loads as _impl

    return _impl(raw)


def _extract_meta(payload: dict[str, Any]) -> dict[str, Any]:
    from v2.backtest.row_loader import _extract_meta as _impl

    return _impl(payload)


def _extract_snapshot_time(meta: dict[str, Any]) -> Any:
    from v2.backtest.row_loader import _extract_snapshot_time as _impl

    return _impl(meta)


def _normalize_snapshot(*args, **kwargs):
    return _lazy_impl("v2.backtest.row_loader", "_normalize_snapshot")(*args, **kwargs)


def _normalize_replay_rows(*args, **kwargs):
    return _lazy_impl("v2.backtest.row_loader", "_normalize_replay_rows")(*args, **kwargs)


def _load_replay_rows_json(*args, **kwargs):
    return _lazy_impl("v2.backtest.row_loader", "_load_replay_rows_json")(*args, **kwargs)


def _load_replay_rows_csv(*args, **kwargs):
    return _lazy_impl("v2.backtest.row_loader", "_load_replay_rows_csv")(*args, **kwargs)


def _load_replay_frames(*args, **kwargs):
    return _lazy_impl("v2.backtest.row_loader", "_load_replay_frames")(*args, **kwargs)


def _build_replay_cycle_record(*args, **kwargs):
    return _lazy_impl("v2.backtest.replay", "_build_replay_cycle_record")(*args, **kwargs)


def _build_local_backtest_cycle_input(*args, **kwargs):
    return _lazy_impl("v2.backtest.orchestration", "_build_local_backtest_cycle_input")(
        *args,
        **kwargs,
    )


def _write_replay_report(*args, **kwargs):
    return _lazy_impl("v2.backtest.reporting", "_write_replay_report")(*args, **kwargs)


def _run_replay(*args, **kwargs):
    return _lazy_impl("v2.backtest.replay", "_run_replay")(*args, **kwargs)


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


def _cleanup_local_backtest_artifacts(*args, **kwargs):
    return _lazy_impl("v2.backtest.reporting", "_cleanup_local_backtest_artifacts")(
        *args,
        **kwargs,
    )


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


def _simulate_symbol_metrics(*args, **kwargs):
    return _lazy_impl("v2.backtest.metrics", "_simulate_symbol_metrics")(*args, **kwargs)


def _write_local_backtest_markdown(*args, **kwargs):
    from v2.backtest.reporting import _write_local_backtest_markdown as _impl

    return _impl(*args, **kwargs)


def _local_backtest_strategy_runtime_params(*args, **kwargs):
    from v2.backtest.local_runner import _local_backtest_strategy_runtime_params as _impl

    return _impl(*args, **kwargs)


def _local_backtest_profile_alpha_overrides(*args, **kwargs):
    from v2.backtest.local_runner import _local_backtest_profile_alpha_overrides as _impl

    return _impl(*args, **kwargs)


def _merge_local_backtest_profile_alpha_overrides(*args, **kwargs):
    from v2.backtest.local_runner import _merge_local_backtest_profile_alpha_overrides as _impl

    return _impl(*args, **kwargs)


def _run_local_backtest_symbol_replay_worker(*args, **kwargs):
    from v2.backtest.local_runner import _run_local_backtest_symbol_replay_worker as _impl

    return _impl(*args, **kwargs)


def _load_local_backtest_cached_market_for_symbol(*args, **kwargs):
    from v2.backtest.local_runner import _load_local_backtest_cached_market_for_symbol as _impl

    return _impl(*args, **kwargs)


def _load_local_backtest_cached_premium_for_symbol(*args, **kwargs):
    from v2.backtest.local_runner import _load_local_backtest_cached_premium_for_symbol as _impl

    return _impl(*args, **kwargs)


def _load_local_backtest_cached_funding_for_symbol(*args, **kwargs):
    from v2.backtest.local_runner import _load_local_backtest_cached_funding_for_symbol as _impl

    return _impl(*args, **kwargs)


def _build_local_backtest_portfolio_rows(*args, **kwargs):
    return _lazy_impl("v2.backtest.orchestration", "_build_local_backtest_portfolio_rows")(
        *args,
        **kwargs,
    )


def _simulate_portfolio_metrics(*args, **kwargs):
    return _lazy_impl("v2.backtest.metrics", "_simulate_portfolio_metrics")(*args, **kwargs)


def _run_local_backtest_portfolio_replay(*args, **kwargs):
    return _lazy_impl("v2.backtest.local_runner", "_run_local_backtest_portfolio_replay")(
        *args,
        **kwargs,
    )


def _run_local_backtest(*args, **kwargs):
    return _lazy_impl("v2.backtest.local_runner", "_run_local_backtest")(*args, **kwargs)


def _boot(cfg: EffectiveConfig, *, loop_enabled: bool = False, max_cycles: int = 0) -> None:
    from v2.runtime.boot import boot_runtime

    boot_runtime(cfg, loop_enabled=loop_enabled, max_cycles=max_cycles)


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
    from v2.runtime.serve import serve_ops_http

    return serve_ops_http(cfg, host=host, port=port)


def _serve_control_http(
    cfg: EffectiveConfig,
    *,
    host: str,
    port: int,
    enable_operator_web: bool = False,
) -> int:
    from v2.runtime.serve import serve_control_http

    return serve_control_http(
        cfg,
        host=host,
        port=port,
        enable_operator_web=enable_operator_web,
    )


def _build_control_balance_rest_client(
    *, cfg: EffectiveConfig, runtime_rest_client: Any | None
) -> Any | None:
    from v2.runtime.boot import build_control_balance_rest_client

    return build_control_balance_rest_client(
        cfg=cfg,
        runtime_rest_client=runtime_rest_client,
    )


def _evaluate_runtime_preflight(
    *,
    controller: Any,
    cfg: EffectiveConfig,
    host: str,
    port: int,
) -> dict[str, Any]:
    from v2.runtime.boot import evaluate_runtime_preflight

    return evaluate_runtime_preflight(
        controller=controller,
        cfg=cfg,
        host=host,
        port=port,
    )


def _run_runtime_preflight(cfg: EffectiveConfig, *, host: str, port: int) -> int:
    from v2.runtime.boot import run_runtime_preflight

    return run_runtime_preflight(cfg, host=host, port=port)


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


def _validate_runtime_entry_path(args: argparse.Namespace, cfg: EffectiveConfig) -> str | None:
    from v2.runtime.entry_guard import validate_runtime_entry_path

    return validate_runtime_entry_path(args, cfg)


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

    path_error = _validate_runtime_entry_path(args, effective)
    if path_error is not None:
        print(json.dumps({"error": path_error}, ensure_ascii=True))
        return 1

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
            effective,
            host=args.control_http_host,
            port=args.control_http_port,
            enable_operator_web=bool(args.operator_web),
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
