from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Mapping, Optional, Tuple

from apps.trader_engine.domain.enums import EngineState
from apps.trader_engine.exchange.binance_usdm import BinanceUSDMClient
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.execution_service import ExecutionRejected, ExecutionService
from apps.trader_engine.services.indicators import atr_pct, clamp
from apps.trader_engine.services.market_data_service import MarketDataService
from apps.trader_engine.services.notifier_service import Notifier
from apps.trader_engine.services.oplog import OperationalLogger
from apps.trader_engine.services.reconcile_service import ReconcileService
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.storage.repositories import OrderRecordRepo, TrailingStateRepo

logger = logging.getLogger(__name__)


@dataclass
class WatchdogMetrics:
    symbol: Optional[str] = None
    last_mark_price: Optional[float] = None
    last_1m_return: Optional[float] = None
    spread_pct: Optional[float] = None
    market_blocked_by_spread: bool = False
    last_shock_reason: Optional[str] = None
    last_trailing_reason: Optional[str] = None
    last_peak_pnl_pct: Optional[float] = None
    last_trailing_distance_pct: Optional[float] = None
    last_checked_at: Optional[str] = None


@dataclass
class TrailingPositionState:
    symbol: str
    side: str
    entry_price: float
    entry_ts: float
    peak_pnl_pct: float
    armed: bool
    close_state: str
    last_close_attempt_ts: float
    attempt_count: int


@dataclass(frozen=True)
class AtrTrailCacheEntry:
    atr_pct: float
    fetched_ts: float


class WatchdogService:
    """Risk watchdog loop (defense only).

    - Runs on short interval (default 10s; from risk_config.watchdog_interval_sec).
    - Never enters new positions.
    - If shock trigger is hit, closes existing position immediately.
    """

    def __init__(
        self,
        *,
        client: BinanceUSDMClient,
        engine: EngineService,
        risk: RiskConfigService,
        execution: ExecutionService,
        notifier: Optional[Notifier] = None,
        oplog: Optional[OperationalLogger] = None,
        market_data: Optional[MarketDataService] = None,
        reconcile: Optional[ReconcileService] = None,
        order_records: Optional[OrderRecordRepo] = None,
        trailing_state_repo: Optional[TrailingStateRepo] = None,
        trailing_retry_base_sec: float = 3.0,
        trailing_retry_max_sec: float = 10.0,
    ) -> None:
        self._client = client
        self._engine = engine
        self._risk = risk
        self._execution = execution
        self._notifier = notifier
        self._oplog = oplog
        self._market_data = market_data
        self._reconcile = reconcile
        self._order_records = order_records
        self._trailing_state_repo = trailing_state_repo
        self._trailing_retry_base_sec = max(float(trailing_retry_base_sec), 0.5)
        self._trailing_retry_max_sec = max(float(trailing_retry_max_sec), self._trailing_retry_base_sec)

        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._hist: Dict[str, Deque[Tuple[float, float]]] = {}
        self._spread_alerted: Dict[str, bool] = {}
        self._metrics = WatchdogMetrics()
        self._trailing: Optional[TrailingPositionState] = None
        self._atr_cache: Dict[Tuple[str, str], AtrTrailCacheEntry] = {}

    @property
    def metrics(self) -> WatchdogMetrics:
        return self._metrics

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="risk_watchdog")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            cfg = self._risk.get_config()
            interval = max(int(cfg.watchdog_interval_sec), 1)
            try:
                if bool(cfg.enable_watchdog):
                    await self.tick_once()
            except Exception:
                logger.exception("watchdog_tick_failed")
            await self._sleep(interval)

    async def _sleep(self, sec: int) -> None:
        end = time.monotonic() + max(float(sec), 1.0)
        while time.monotonic() < end:
            if self._stop.is_set():
                return
            await asyncio.sleep(min(0.5, end - time.monotonic()))

    async def tick_once(self) -> None:
        st = self._engine.get_state().state
        # Periodic fallback reconcile when WS health is bad.
        try:
            est = self._engine.get_state()
            ws_bad = (not bool(est.ws_connected)) or (
                est.last_ws_event_time is not None and (time.time() - est.last_ws_event_time.timestamp()) > 180.0
            )
            if self._reconcile:
                _ = self._reconcile.maybe_periodic_reconcile(ws_bad=ws_bad, min_interval_sec=600)
        except Exception:
            logger.exception("watchdog_reconcile_fallback_failed")
        pos = await asyncio.to_thread(self._client.get_open_positions_any)

        # No action when no position exists (metrics still keep last seen values).
        if not pos:
            self._trailing = None
            self._clear_trailing_state()
            self._metrics.last_peak_pnl_pct = None
            self._metrics.last_trailing_distance_pct = None
            self._metrics.last_checked_at = _iso_now()
            return

        # Single-asset invariant should hold; if not, choose the largest notional proxy by abs(position_amt).
        symbol, row = max(
            pos.items(),
            key=lambda kv: abs(float((kv[1] or {}).get("position_amt") or 0.0)),
        )
        symbol = str(symbol).upper()
        entry_price = float((row or {}).get("entry_price") or 0.0)
        position_amt = float((row or {}).get("position_amt") or 0.0)
        if abs(position_amt) <= 0.0:
            self._trailing = None
            self._clear_trailing_state()
            self._metrics.last_peak_pnl_pct = None
            self._metrics.last_trailing_distance_pct = None
            self._metrics.last_checked_at = _iso_now()
            return
        side = "LONG" if position_amt > 0.0 else "SHORT"
        now_ts = time.time()
        self._sync_trailing_state(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            now_ts=now_ts,
        )

        # PANIC is already emergency-locked; avoid duplicate close actions.
        if st == EngineState.PANIC:
            self._metrics.symbol = symbol
            self._metrics.last_checked_at = _iso_now()
            return

        mp = await asyncio.to_thread(self._client.get_mark_price, symbol)
        mark = float((mp or {}).get("markPrice") or 0.0)
        if mark <= 0.0:
            self._metrics.symbol = symbol
            self._metrics.last_checked_at = _iso_now()
            return

        # 1m return tracking from a 10s loop via deque history.
        r1m = self._update_and_get_1m_return(symbol=symbol, mark=mark)

        bt = await asyncio.to_thread(self._client.get_book_ticker, symbol)
        bid = float((bt or {}).get("bidPrice") or 0.0)
        ask = float((bt or {}).get("askPrice") or 0.0)
        spread_pct = _spread_pct(bid=bid, ask=ask)
        cfg = self._risk.get_config()
        spread_max_ratio = float(cfg.spread_max_pct)
        if spread_max_ratio > 0.1:
            spread_max_ratio = spread_max_ratio / 100.0
        spread_wide = spread_pct is not None and (spread_pct / 100.0) >= spread_max_ratio

        self._metrics.symbol = symbol
        self._metrics.last_mark_price = mark
        self._metrics.last_1m_return = r1m
        self._metrics.spread_pct = spread_pct
        self._metrics.market_blocked_by_spread = bool(spread_wide and not bool(cfg.allow_market_when_wide_spread))
        self._metrics.last_checked_at = _iso_now()

        # Spread alert only (no close by default).
        if spread_wide and not bool(cfg.allow_market_when_wide_spread):
            prev = self._spread_alerted.get(symbol, False)
            if not prev:
                self._spread_alerted[symbol] = True
                await self._notify(
                    {
                        "kind": "BLOCK",
                        "symbol": symbol,
                        "reason": f"spread_too_wide_market_disabled:{spread_pct:.4f}%",
                    }
                )
        else:
            self._spread_alerted[symbol] = False

        # Shock A: 1m mark return.
        if r1m is not None and r1m <= -abs(float(cfg.shock_1m_pct)):
            reason = f"WATCHDOG_SHOCK_1M:{r1m:.6f}"
            await self._trigger_close(symbol=symbol, reason=reason)
            return

        # Shock B: from entry.
        if entry_price > 0.0:
            from_entry = (mark - entry_price) / entry_price
            if from_entry <= -abs(float(cfg.shock_from_entry_pct)):
                reason = f"WATCHDOG_SHOCK_FROM_ENTRY:{from_entry:.6f}"
                await self._trigger_close(symbol=symbol, reason=reason)
                return

        # Trailing stop (after shock checks; shock has priority).
        await self._maybe_trigger_trailing(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            mark=mark,
            now_ts=now_ts,
            cfg=cfg,
        )

    def _update_and_get_1m_return(self, *, symbol: str, mark: float) -> Optional[float]:
        now = time.time()
        dq = self._hist.setdefault(symbol, deque())
        dq.append((now, mark))
        # Keep enough history for 60s window with margin.
        cutoff_keep = now - 120.0
        while dq and dq[0][0] < cutoff_keep:
            dq.popleft()

        cutoff_1m = now - 60.0
        base = None
        for ts, px in dq:
            if ts >= cutoff_1m:
                base = px
                break
        if base is None or base <= 0:
            return None
        return (mark - base) / base

    async def _trigger_close(self, *, symbol: str, reason: str) -> None:
        self._metrics.last_shock_reason = reason
        await self._notify({"kind": "WATCHDOG_SHOCK", "symbol": symbol, "reason": reason})
        if self._oplog:
            try:
                self._oplog.log_event("WATCHDOG_SHOCK", {"symbol": symbol, "reason": reason})
            except Exception:
                logger.exception("oplog_watchdog_shock_failed")
        try:
            await self._execution.close_position(symbol, reason="WATCHDOG_SHOCK")
        except ExecutionRejected as e:
            await self._notify({"kind": "FAIL", "symbol": symbol, "error": e.message})
        except Exception as e:  # noqa: BLE001
            await self._notify({"kind": "FAIL", "symbol": symbol, "error": f"{type(e).__name__}: {e}"})

    async def _maybe_trigger_trailing(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        mark: float,
        now_ts: float,
        cfg: object,
    ) -> None:
        tr = self._trailing
        if tr is None or tr.symbol != symbol:
            return
        if not bool(getattr(cfg, "trailing_enabled", True)):
            return
        if str(tr.close_state).upper() in {"SENT", "DONE"}:
            return
        if entry_price <= 0.0 or mark <= 0.0:
            return

        grace_minutes = max(int(getattr(cfg, "trail_grace_minutes", 30) or 0), 0)
        if now_ts < (tr.entry_ts + grace_minutes * 60):
            return

        pnl_pct = _position_pnl_pct(side=side, entry_price=entry_price, mark=mark)
        self._metrics.last_peak_pnl_pct = float(tr.peak_pnl_pct)
        if pnl_pct > tr.peak_pnl_pct:
            tr.peak_pnl_pct = pnl_pct
            self._metrics.last_peak_pnl_pct = float(tr.peak_pnl_pct)
            self._persist_trailing_state()

        arm_pct = float(getattr(cfg, "trail_arm_pnl_pct", 1.2) or 0.0)
        if not tr.armed and pnl_pct >= arm_pct:
            tr.armed = True
            tr.peak_pnl_pct = max(tr.peak_pnl_pct, pnl_pct)
            self._persist_trailing_state()
            if self._oplog:
                try:
                    self._oplog.log_event(
                        "TRAILING_ARMED",
                        {
                            "symbol": symbol,
                            "side": side,
                            "entry_ts": tr.entry_ts,
                            "peak_pnl_pct": tr.peak_pnl_pct,
                        },
                    )
                except Exception:
                    logger.exception("oplog_trailing_armed_failed")

        if not tr.armed:
            return

        mode = str(getattr(cfg, "trailing_mode", "PCT")).upper()
        dist_pct = self._resolve_trailing_distance_pct(
            symbol=symbol,
            mode=mode,
            cfg=cfg,
            fallback_dist_pct=float(getattr(cfg, "trail_distance_pnl_pct", 0.8) or 0.0),
        )
        if dist_pct is None:
            return
        self._metrics.last_trailing_distance_pct = float(dist_pct)
        trigger_level = tr.peak_pnl_pct - dist_pct
        if pnl_pct > trigger_level:
            return

        cooldown_sec = min(self._trailing_retry_base_sec * (2 ** max(tr.attempt_count, 0)), self._trailing_retry_max_sec)
        if tr.last_close_attempt_ts > 0.0 and (now_ts - tr.last_close_attempt_ts) < cooldown_sec:
            return
        tr.last_close_attempt_ts = float(now_ts)
        tr.attempt_count = int(tr.attempt_count) + 1
        self._persist_trailing_state()

        kind = "TRAILING_ATR" if mode == "ATR" else "TRAILING_PCT"
        reason = f"{kind}:pnl_pct={pnl_pct:.6f},peak_pnl_pct={tr.peak_pnl_pct:.6f},distance_pct={dist_pct:.6f}"
        self._metrics.last_trailing_reason = reason
        await self._notify({"kind": kind, "symbol": symbol, "reason": reason})
        if self._oplog:
            try:
                self._oplog.log_event(
                    kind,
                    {"symbol": symbol, "side": side, "reason": reason, "distance_pct": dist_pct, "mode": mode},
                )
            except Exception:
                logger.exception("oplog_trailing_trigger_failed")
        try:
            await self._execution.close_position(symbol, reason=kind)
            tr.close_state = "SENT"
            self._persist_trailing_state()
        except ExecutionRejected as e:
            tr.close_state = "IDLE"
            self._persist_trailing_state()
            await self._notify({"kind": "FAIL", "symbol": symbol, "error": e.message})
        except Exception as e:  # noqa: BLE001
            tr.close_state = "IDLE"
            self._persist_trailing_state()
            await self._notify({"kind": "FAIL", "symbol": symbol, "error": f"{type(e).__name__}: {e}"})

    def _resolve_trailing_distance_pct(
        self,
        *,
        symbol: str,
        mode: str,
        cfg: object,
        fallback_dist_pct: float,
    ) -> Optional[float]:
        if mode != "ATR":
            return float(fallback_dist_pct)
        tf = str(getattr(cfg, "atr_trail_timeframe", "1h") or "1h").lower()
        k = float(getattr(cfg, "atr_trail_k", 2.0) or 2.0)
        lo = float(getattr(cfg, "atr_trail_min_pct", 0.6) or 0.6)
        hi = float(getattr(cfg, "atr_trail_max_pct", 1.8) or 1.8)
        atr_val = self._get_atr_pct_cached(symbol=symbol, timeframe=tf)
        if atr_val is None:
            return None
        return _atr_trail_distance_pct(atr_pct_value=atr_val, k=k, min_pct=lo, max_pct=hi)

    def _get_atr_pct_cached(self, *, symbol: str, timeframe: str) -> Optional[float]:
        if not self._market_data:
            return None
        key = (str(symbol).upper(), str(timeframe).lower())
        now = time.time()
        cached = self._atr_cache.get(key)
        if cached and (now - cached.fetched_ts) <= 30.0:
            return float(cached.atr_pct)
        try:
            candles = self._market_data.get_klines(symbol=key[0], interval=key[1], limit=120)
            v = atr_pct(candles, period=14)
            if v is None:
                return None
            out = float(v)
            self._atr_cache[key] = AtrTrailCacheEntry(atr_pct=out, fetched_ts=now)
            return out
        except Exception:
            logger.exception("atr_trail_fetch_failed", extra={"symbol": key[0], "timeframe": key[1]})
            return None

    def _sync_trailing_state(self, *, symbol: str, side: str, entry_price: float, now_ts: float) -> None:
        tr = self._trailing
        if tr is None:
            self._trailing = self._load_or_bootstrap_trailing_state(
                symbol=symbol,
                side=side,
                entry_price=float(entry_price),
                now_ts=float(now_ts),
            )
            self._persist_trailing_state()
            self._cleanup_trailing_state(symbol=symbol)
            return
        changed = (
            tr.symbol != symbol
            or tr.side != side
            or abs(float(tr.entry_price) - float(entry_price)) > 1e-12
        )
        if changed:
            self._trailing = self._load_or_bootstrap_trailing_state(
                symbol=symbol,
                side=side,
                entry_price=float(entry_price),
                now_ts=float(now_ts),
            )
            self._persist_trailing_state()
            self._cleanup_trailing_state(symbol=symbol)

    def _infer_entry_ts(self, *, symbol: str, side: str, now_ts: float) -> float:
        if not self._order_records:
            return float(now_ts)
        try:
            entry_side = "BUY" if str(side).upper() == "LONG" else "SELL"
            ts = self._order_records.get_latest_entry_created_ts(symbol=symbol, side=entry_side)
            if ts is not None and ts > 0.0:
                return float(ts)
        except Exception:
            logger.exception("trailing_infer_entry_ts_failed", extra={"symbol": symbol, "side": side})
        return float(now_ts)

    def _load_or_bootstrap_trailing_state(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        now_ts: float,
    ) -> TrailingPositionState:
        if self._trailing_state_repo:
            try:
                row = self._trailing_state_repo.get(symbol=symbol)
                if row is not None:
                    rs = str(row.get("position_side") or "").upper()
                    rp = float(row.get("entry_price") or 0.0)
                    if rs == str(side).upper() and abs(rp - float(entry_price)) <= 1e-12:
                        return TrailingPositionState(
                            symbol=symbol,
                            side=side,
                            entry_price=float(entry_price),
                            entry_ts=float(row.get("entry_ts") or now_ts),
                            peak_pnl_pct=float(row.get("peak_pnl_pct") or 0.0),
                            armed=bool(row.get("armed")),
                            close_state=str(row.get("close_state") or "IDLE").upper(),
                            last_close_attempt_ts=float(row.get("last_close_attempt_ts") or 0.0),
                            attempt_count=int(row.get("attempt_count") or 0),
                        )
            except Exception:
                logger.exception("trailing_state_load_failed", extra={"symbol": symbol})

        return TrailingPositionState(
            symbol=symbol,
            side=side,
            entry_price=float(entry_price),
            entry_ts=self._infer_entry_ts(symbol=symbol, side=side, now_ts=now_ts),
            peak_pnl_pct=0.0,
            armed=False,
            close_state="IDLE",
            last_close_attempt_ts=0.0,
            attempt_count=0,
        )

    def _persist_trailing_state(self) -> None:
        if not self._trailing_state_repo or not self._trailing:
            return
        tr = self._trailing
        try:
            self._trailing_state_repo.upsert(
                symbol=tr.symbol,
                position_side=tr.side,
                entry_price=float(tr.entry_price),
                entry_ts=float(tr.entry_ts),
                peak_pnl_pct=float(tr.peak_pnl_pct),
                armed=bool(tr.armed),
                close_state=str(tr.close_state or "IDLE").upper(),
                last_close_attempt_ts=(float(tr.last_close_attempt_ts) if tr.last_close_attempt_ts > 0.0 else None),
                attempt_count=int(tr.attempt_count),
            )
        except Exception:
            logger.exception("trailing_state_persist_failed", extra={"symbol": tr.symbol})

    def _cleanup_trailing_state(self, *, symbol: str) -> None:
        if not self._trailing_state_repo:
            return
        try:
            self._trailing_state_repo.delete_all_except(symbols=[symbol])
        except Exception:
            logger.exception("trailing_state_cleanup_failed", extra={"symbol": symbol})

    def _clear_trailing_state(self) -> None:
        if not self._trailing_state_repo:
            return
        try:
            self._trailing_state_repo.clear()
        except Exception:
            logger.exception("trailing_state_clear_failed")

    async def _notify(self, event: Mapping[str, str]) -> None:
        if not self._notifier:
            return
        try:
            await self._notifier.send_event(dict(event))
        except Exception:
            logger.exception("watchdog_notify_failed")


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _spread_pct(*, bid: float, ask: float) -> Optional[float]:
    if bid <= 0.0 or ask <= 0.0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0.0:
        return None
    return ((ask - bid) / mid) * 100.0


def _position_pnl_pct(*, side: str, entry_price: float, mark: float) -> float:
    if entry_price <= 0.0 or mark <= 0.0:
        return 0.0
    if str(side).upper() == "SHORT":
        return ((entry_price - mark) / entry_price) * 100.0
    return ((mark - entry_price) / entry_price) * 100.0


def _atr_trail_distance_pct(*, atr_pct_value: float, k: float, min_pct: float, max_pct: float) -> float:
    lo = float(min_pct)
    hi = float(max_pct)
    if hi < lo:
        hi = lo
    return float(clamp(float(k) * float(atr_pct_value), lo, hi))
