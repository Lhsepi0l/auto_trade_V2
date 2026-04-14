from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from v2.common.async_bridge import run_async_blocking
from v2.kernel.contracts import KernelCycleResult

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _normalize_pct(value: Any, default: float = 0.0) -> float:
    parsed = abs(_to_float(value, default=default))
    if parsed > 1.0:
        parsed = parsed / 100.0
    return max(parsed, 0.0)


def effective_budget_leverage(
    *,
    symbols: list[str],
    max_leverage: float,
    symbol_leverage_map: dict[str, float],
) -> float:
    base = max(1.0, float(max_leverage))
    if not symbols:
        return base

    resolved: list[float] = []
    for sym in symbols:
        candidate = symbol_leverage_map.get(sym, base)
        lev = max(1.0, _to_float(candidate, default=base))
        resolved.append(min(lev, base))

    if not resolved:
        return base
    return max(1.0, min(resolved))


def runtime_symbols(controller: Any) -> list[str]:
    symbols_raw = controller._risk.get("universe_symbols")
    if isinstance(symbols_raw, list):
        symbols = [str(sym).strip().upper() for sym in symbols_raw if str(sym).strip()]
    else:
        symbols = [controller.cfg.behavior.exchange.default_symbol]
    if not symbols:
        symbols = [controller.cfg.behavior.exchange.default_symbol]
    return symbols


def runtime_symbol_leverage_map(controller: Any) -> dict[str, float]:
    mapping_raw = controller._risk.get("symbol_leverage_map")
    mapping: dict[str, float] = {}
    if isinstance(mapping_raw, dict):
        for sym, lev in mapping_raw.items():
            sym_u = str(sym).strip().upper()
            if not sym_u:
                continue
            lev_f = _to_float(lev, default=0.0)
            if lev_f > 0:
                mapping[sym_u] = lev_f
    return mapping


def runtime_target_margin(controller: Any) -> float:
    capital_mode = str(controller._risk.get("capital_mode") or "").upper()
    margin_use_pct = max(0.0, _to_float(controller._risk.get("margin_use_pct"), default=1.0))
    margin_budget = _to_float(controller._risk.get("margin_budget_usdt"), default=100.0)
    fixed_budget = _to_float(controller._risk.get("capital_usdt"), default=100.0)
    base_margin = fixed_budget if capital_mode == "FIXED_USDT" else margin_budget
    target_margin = float(base_margin) * float(margin_use_pct)
    if target_margin <= 0:
        target_margin = 10.0
    return target_margin


def runtime_budget_context(controller: Any) -> tuple[list[str], dict[str, float], float, float, float]:
    symbols = runtime_symbols(controller)
    mapping = runtime_symbol_leverage_map(controller)
    target_margin = runtime_target_margin(controller)
    max_leverage = max(1.0, _to_float(controller._risk.get("max_leverage"), default=1.0))
    leverage = effective_budget_leverage(
        symbols=symbols,
        max_leverage=max_leverage,
        symbol_leverage_map=mapping,
    )
    expected_notional = float(target_margin) * float(leverage)
    max_position_notional = _to_float(
        controller._risk.get("max_position_notional_usdt"),
        default=0.0,
    )
    if max_position_notional > 0:
        expected_notional = min(expected_notional, float(max_position_notional))
    effective_margin = expected_notional / float(leverage) if leverage > 0 else target_margin
    return symbols, mapping, effective_margin, leverage, expected_notional


def record_recent_block(controller: Any, reason: str) -> None:
    raw_reason = str(reason or "unknown").strip() or "unknown"
    normalized = raw_reason.split(":", 1)[0]
    blocks_raw = controller._risk.get("recent_blocks")
    blocks = dict(blocks_raw) if isinstance(blocks_raw, dict) else {}
    blocks[normalized] = int(_to_float(blocks.get(normalized), default=0.0)) + 1
    ranked = sorted(blocks.items(), key=lambda item: (-int(item[1]), item[0]))[:10]
    controller._risk["recent_blocks"] = {key: count for key, count in ranked}
    controller._risk["last_block_reason"] = raw_reason


def refresh_runtime_risk_context(controller: Any) -> None:
    state = controller.state_store.get()
    today = datetime.now(timezone.utc).date().isoformat()
    if str(controller._risk.get("risk_day") or "") != today:
        controller._risk["daily_trade_entry_counts"] = {}
    _symbols, _mapping, effective_margin, _leverage, _expected_notional = runtime_budget_context(controller)
    capital_base = max(float(effective_margin), 1e-9)
    live_equity_basis_ok = controller.cfg.mode != "live"
    cached_balance = controller._cached_live_balance(max_age_sec=300.0)
    if cached_balance is not None:
        _available, wallet = cached_balance
        if wallet is not None and float(wallet) > 0.0:
            capital_base = max(float(wallet), capital_base)
            live_equity_basis_ok = True
    elif controller.cfg.mode == "live":
        _available, wallet, source = controller._fetch_live_usdt_balance()
        if source in {"exchange", "exchange_cached"} and wallet is not None and float(wallet) > 0.0:
            capital_base = max(float(wallet), capital_base)
            live_equity_basis_ok = True

    daily_realized = 0.0
    lose_streak = 0
    last_loss_time_ms: int | None = None

    for fill in state.last_fills:
        pnl = fill.realized_pnl if fill.realized_pnl is not None else 0.0
        fill_time_ms = int(fill.fill_time_ms or 0)
        if fill_time_ms > 0:
            fill_day = datetime.fromtimestamp(fill_time_ms / 1000.0, tz=timezone.utc).date().isoformat()
            if fill_day == today:
                daily_realized += float(pnl)

    for fill in reversed(state.last_fills):
        pnl = fill.realized_pnl
        if pnl is None:
            continue
        if float(pnl) < 0.0:
            lose_streak += 1
            if last_loss_time_ms is None and fill.fill_time_ms is not None:
                last_loss_time_ms = int(fill.fill_time_ms)
            continue
        break

    unrealized = 0.0
    for position in state.current_position.values():
        unrealized += float(position.unrealized_pnl or 0.0)

    equity_now = capital_base + unrealized
    equity_peak = max(
        _to_float(controller._risk.get("runtime_equity_peak_usdt"), default=0.0),
        float(equity_now),
    )
    if equity_peak <= 0.0:
        equity_peak = capital_base

    daily_realized_pct = float(daily_realized) / capital_base if capital_base > 0.0 else 0.0
    daily_loss_used_pct = max(0.0, -daily_realized_pct)
    if controller.cfg.mode == "live" and not live_equity_basis_ok:
        dd_used_pct = 0.0
    else:
        dd_used_pct = max(0.0, (equity_peak - equity_now) / equity_peak) if equity_peak > 0.0 else 0.0

    daily_limit = _normalize_pct(controller._risk.get("daily_loss_limit_pct"), default=0.02)
    dd_limit = _normalize_pct(controller._risk.get("dd_limit_pct"), default=0.15)
    daily_lock = daily_limit > 0.0 and daily_loss_used_pct >= daily_limit
    dd_lock = dd_limit > 0.0 and dd_used_pct >= dd_limit

    cooldown_until_raw = controller._risk.get("cooldown_until")
    cooldown_until = float(cooldown_until_raw) if cooldown_until_raw is not None else 0.0
    lose_streak_trigger = max(int(_to_float(controller._risk.get("lose_streak_n"), default=0.0)), 0)
    cooldown_hours = max(0.0, _to_float(controller._risk.get("cooldown_hours"), default=0.0))
    if (
        lose_streak_trigger > 0
        and lose_streak >= lose_streak_trigger
        and last_loss_time_ms is not None
        and cooldown_hours > 0.0
    ):
        cooldown_until = max(
            cooldown_until,
            (float(last_loss_time_ms) / 1000.0) + (cooldown_hours * 3600.0),
        )
    cooldown_until_value: float | None = None if cooldown_until <= time.time() else cooldown_until

    controller._risk["risk_day"] = today
    controller._risk["daily_realized_pnl"] = float(daily_realized)
    controller._risk["daily_realized_pct"] = float(daily_realized_pct)
    controller._risk["daily_loss_used_pct"] = float(daily_loss_used_pct)
    controller._risk["dd_used_pct"] = float(dd_used_pct)
    controller._risk["lose_streak"] = int(lose_streak)
    controller._risk["cooldown_until"] = cooldown_until_value
    controller._risk["daily_lock"] = bool(daily_lock)
    controller._risk["dd_lock"] = bool(dd_lock)
    controller._risk["runtime_equity_now_usdt"] = float(equity_now)
    controller._risk["runtime_equity_peak_usdt"] = float(equity_peak)
    if not isinstance(controller._risk.get("recent_blocks"), dict):
        controller._risk["recent_blocks"] = {}
    if not isinstance(controller._risk.get("daily_trade_entry_counts"), dict):
        controller._risk["daily_trade_entry_counts"] = {}


def record_daily_entry(controller: Any, *, symbol: str) -> None:
    symbol_u = str(symbol or "").strip().upper()
    if not symbol_u:
        return
    today = datetime.now(timezone.utc).date().isoformat()
    if str(controller._risk.get("risk_day") or "") != today:
        controller._risk["risk_day"] = today
        controller._risk["daily_trade_entry_counts"] = {}
    counts_raw = controller._risk.get("daily_trade_entry_counts")
    counts = dict(counts_raw) if isinstance(counts_raw, dict) else {}
    counts[symbol_u] = int(_to_float(counts.get(symbol_u), default=0.0)) + 1
    controller._risk["daily_trade_entry_counts"] = counts


def maybe_apply_auto_risk_circuit(controller: Any, cycle: KernelCycleResult) -> None:
    if not _to_bool(controller._risk.get("auto_risk_enabled"), default=True):
        return
    if cycle.reason not in {"daily_loss_limit", "drawdown_limit"}:
        return

    controller._risk["last_auto_risk_reason"] = cycle.reason
    controller._risk["last_auto_risk_at"] = _utcnow_iso()
    controller._log_event("risk_trip", reason=cycle.reason)
    if _to_bool(controller._risk.get("auto_safe_mode_on_risk"), default=True):
        controller.ops.safe_mode()
    elif _to_bool(controller._risk.get("auto_pause_on_risk"), default=True):
        controller.ops.pause()

    if controller.cfg.mode != "live" or not _to_bool(controller._risk.get("auto_flatten_on_risk"), default=True):
        return

    symbols = set(runtime_symbols(controller))
    for symbol in controller.state_store.get().current_position.keys():
        symbols.add(str(symbol).strip().upper())

    for symbol in sorted(sym for sym in symbols if sym):
        try:
            _ = run_async_blocking(
                lambda s=symbol: controller.close_position(
                    symbol=s,
                    notify_reason="auto_risk_close",
                ),
                timeout_sec=30.0,
            )
        except Exception:  # noqa: BLE001
            logger.exception("auto_risk_flatten_failed symbol=%s reason=%s", symbol, cycle.reason)
