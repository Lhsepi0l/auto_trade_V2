from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from apps.trader_engine.domain.enums import CapitalMode, Direction, EngineState, ExecHint
from apps.trader_engine.services.ai_service import AiService, AiSignal
from apps.trader_engine.services.binance_service import BinanceService
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.execution_service import ExecutionRejected, ExecutionService
from apps.trader_engine.services.market_data_service import MarketDataService
from apps.trader_engine.services.notifier_service import LoggingNotifier, Notifier
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.services.scoring_service import Candidate, ScoringService, SymbolScore
from apps.trader_engine.services.sizing_service import SizingService
from apps.trader_engine.storage.repositories import OrderRecordRepo, RiskBlockRepo
from apps.trader_engine.services.strategy_service import PositionState, StrategyDecision, StrategyService
from apps.trader_engine.services.oplog import OperationalLogger
from apps.trader_engine.services.snapshot_service import SnapshotService

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


@dataclass
class SchedulerSnapshot:
    tick_started_at: str
    tick_finished_at: Optional[str]
    tick_sec: float
    engine_state: str
    enabled_symbols: List[str]

    # Backward-compatible fields
    candidate: Optional[Dict[str, Any]]
    scores: Dict[str, Any]
    ai_signal: Optional[Dict[str, Any]]

    # FINAL-1 fields
    last_scores: Dict[str, Any]
    last_candidate: Optional[Dict[str, Any]]
    last_decision_reason: Optional[str]

    last_action: Optional[str] = None
    last_error: Optional[str] = None
    active_scoring_timeframes: List[str] = field(default_factory=list)
    candidate_score_by_timeframe: Dict[str, float] = field(default_factory=dict)
    scoring_weights: Dict[str, float] = field(default_factory=dict)
    min_bars_factor: float = 0.6
    symbol_leverage: Dict[str, float] = field(default_factory=dict)
    leverage_sync_target: Optional[float] = None
    leverage_sync_updated_at: Optional[str] = None
    leverage_sync_error: Optional[str] = None


class TraderScheduler:
    """Trading decision loop.

    - Always computes scores/snapshots.
    - Only executes when engine state is RUNNING.
    """

    def __init__(
        self,
        *,
        engine: EngineService,
        risk: RiskConfigService,
        pnl: PnLService,
        binance: BinanceService,
        market_data: MarketDataService,
        scoring: ScoringService,
        strategy: StrategyService,
        ai: AiService,
        sizing: SizingService,
        execution: ExecutionService,
        notifier: Optional[Notifier] = None,
        report_notifier: Optional[Notifier] = None,
        tick_sec: float = 600.0,
        reverse_threshold: float = 0.55,
        oplog: Optional[OperationalLogger] = None,
        snapshot: Optional[SnapshotService] = None,
        order_records: Optional[OrderRecordRepo] = None,
        risk_blocks: Optional[RiskBlockRepo] = None,
    ) -> None:
        self._engine = engine
        self._risk = risk
        self._pnl = pnl
        self._binance = binance
        self._market_data = market_data
        self._scoring = scoring
        self._strategy = strategy
        self._ai = ai
        self._sizing = sizing
        self._execution = execution
        self._notifier = notifier
        self._report_notifier = report_notifier or notifier
        self._oplog = oplog
        self._snapshot = snapshot
        self._order_records = order_records
        self._risk_blocks = risk_blocks

        self._reverse_threshold = float(reverse_threshold)
        self._last_status_notify_ts = 0.0
        self._last_daily_report_date: Optional[str] = None
        self._tick_change_event = asyncio.Event()
        self._tick_min_sec = 30.0
        self._tick_sec = max(float(tick_sec), self._tick_min_sec)

        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self.snapshot: Optional[SchedulerSnapshot] = None
        self._start_requested = False

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="trader_scheduler")
        self._start_requested = True

    async def stop(self) -> None:
        self._stop.set()
        self._tick_change_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None
        self._start_requested = False

    async def tick_once(self) -> SchedulerSnapshot:
        """Run exactly one decision tick (test/debug helper)."""
        started = _utcnow().isoformat()
        st = self._engine.get_state().state
        enabled = list(self._binance.enabled_symbols)
        snap = SchedulerSnapshot(
            tick_started_at=started,
            tick_finished_at=None,
            tick_sec=self._tick_sec,
            engine_state=st.value,
            enabled_symbols=enabled,
            candidate=None,
            scores={},
            ai_signal=None,
            last_scores={},
            last_candidate=None,
            last_decision_reason=None,
            active_scoring_timeframes=[],
            candidate_score_by_timeframe={},
            scoring_weights={},
            min_bars_factor=0.6,
            last_action=None,
            last_error=None,
        )
        self.snapshot = snap
        try:
            await self._tick(snap)
        except Exception as e:  # noqa: BLE001
            logger.exception("scheduler_tick_once_failed", extra={"err": type(e).__name__})
            snap.last_error = f"{type(e).__name__}: {e}"
        finally:
            snap.tick_finished_at = _utcnow().isoformat()
            self.snapshot = snap
        return snap

    async def send_daily_report(self, *, report_day: Optional[str] = None) -> Dict[str, Any]:
        day = report_day or datetime.now(tz=timezone.utc).date().isoformat()
        payload = self._build_daily_report_payload(day=day)
        notifier_sent, notifier_error = await self._send_daily_report_payload(
            payload=payload,
            require_discord_webhook=True,
        )
        payload["notifier_sent"] = notifier_sent
        payload["notifier_error"] = notifier_error
        return payload

    @property
    def tick_sec(self) -> float:
        return self._tick_sec

    def is_running(self) -> bool:
        return bool(self._start_requested and self._task and not self._task.done())

    def set_tick_sec(self, tick_sec: float) -> float:
        tick_sec_f = float(tick_sec)
        if tick_sec_f < self._tick_min_sec:
            raise ValueError(f"tick_sec must be >= {self._tick_min_sec:g}")
        self._tick_sec = tick_sec_f
        self._tick_change_event.set()
        logger.info(
            "scheduler_tick_interval_updated",
            extra={"tick_sec": tick_sec_f, "running": self.is_running()},
        )
        return self._tick_sec

    async def _run(self) -> None:
        while not self._stop.is_set():
            started = _utcnow().isoformat()
            st = self._engine.get_state().state

            enabled = list(self._binance.enabled_symbols)
            snap = SchedulerSnapshot(
                tick_started_at=started,
                tick_finished_at=None,
                tick_sec=self._tick_sec,
                engine_state=st.value,
                enabled_symbols=enabled,
                candidate=None,
                scores={},
                ai_signal=None,
                last_scores={},
                last_candidate=None,
                last_decision_reason=None,
                active_scoring_timeframes=[],
                candidate_score_by_timeframe={},
                scoring_weights={},
                min_bars_factor=0.6,
                last_action=None,
                last_error=None,
            )
            self.snapshot = snap

            try:
                await self._tick(snap)
            except Exception as e:  # noqa: BLE001
                logger.exception("scheduler_tick_failed", extra={"err": type(e).__name__})
                snap.last_error = f"{type(e).__name__}: {e}"
            finally:
                snap.tick_finished_at = _utcnow().isoformat()
                self.snapshot = snap

            await self._sleep_until_next()

    @staticmethod
    def _resolve_scoring_setup(cfg) -> tuple[list[str], Dict[str, float]]:
        weights: Dict[str, float] = {
            "10m": float(getattr(cfg, "tf_weight_10m", 0.25)),
            "15m": float(getattr(cfg, "tf_weight_15m", 0.0)),
            "30m": float(getattr(cfg, "tf_weight_30m", 0.25)),
            "1h": float(getattr(cfg, "tf_weight_1h", 0.25)),
            "4h": float(getattr(cfg, "tf_weight_4h", 0.25)),
        }

        if not bool(getattr(cfg, "score_tf_15m_enabled", False)):
            weights.pop("15m", None)

        cleaned: Dict[str, float] = {}
        for itv, wt in weights.items():
            if float(wt) > 0:
                cleaned[itv] = float(wt)

        if not cleaned:
            return ["10m", "30m", "1h", "4h"], {"10m": 0.25, "30m": 0.25, "1h": 0.25, "4h": 0.25}

        total = sum(cleaned.values()) or 1.0
        normalized = {itv: float(wt) / float(total) for itv, wt in cleaned.items() if float(wt) > 0}
        order = ["10m", "15m", "30m", "1h", "4h"]
        return [itv for itv in order if itv in normalized], normalized

    @staticmethod
    def _resolve_min_bars_factor(*, tick_sec: float, scoring_timeframes: List[str]) -> float:
        # For faster tick intervals, loosen minimum bar requirements slightly
        # to avoid blocking decisions at startup after interval changes.
        if tick_sec <= 600:
            return 0.42
        if tick_sec <= 900:
            return 0.48
        if tick_sec <= 1800:
            return 0.55
        return 0.6

    async def _sleep_until_next(self) -> None:
        total = max(self._tick_sec, 1.0)
        end = time.time() + total
        while time.time() < end:
            if self._stop.is_set():
                return
            wait_sec = min(1.0, max(0.0, end - time.time()))
            try:
                await asyncio.wait_for(self._tick_change_event.wait(), timeout=wait_sec)
            except asyncio.TimeoutError:
                continue
            self._tick_change_event.clear()
            return

    async def _tick(self, snap: SchedulerSnapshot) -> None:
        st = self._engine.get_state().state
        cfg = self._risk.get_config()
        enabled = list(self._binance.enabled_symbols)

        # Fetch Binance status in a thread to avoid blocking the event loop.
        b: Mapping[str, Any] = await asyncio.to_thread(self._binance.get_status)

        # Compute equity and open position summary.
        bal = (b.get("usdt_balance") or {}) if isinstance(b, dict) else {}
        positions = (b.get("positions") or {}) if isinstance(b, dict) else {}
        wallet = float(bal.get("wallet") or 0.0)
        available = float(bal.get("available") or 0.0)

        open_pos_symbol = None
        open_pos_amt = 0.0
        open_pos_upnl = 0.0
        upnl_total = 0.0

        target_lev = float(cfg.max_leverage)
        snap.leverage_sync_target = target_lev if target_lev > 0 else None
        symbol_target_map: dict[str, float] = {}
        for key, raw in (cfg.symbol_leverage_map or {}).items():
            sym = str(key or "").strip().upper()
            if not sym:
                continue
            try:
                lev = float(raw)
            except Exception:
                continue
            if lev <= 0:
                continue
            if lev > 50.0:
                lev = 50.0
            symbol_target_map[sym] = lev

        snap.symbol_leverage = {}
        snap.leverage_sync_error = None
        snap.leverage_sync_updated_at = None

        if isinstance(positions, dict):
            for sym, row in positions.items():
                if not isinstance(row, dict):
                    continue
                lev = float(row.get("leverage") or 0.0)
                if lev > 0:
                    snap.symbol_leverage.setdefault(str(sym).upper(), lev)
                upnl_total += float(row.get("unrealized_pnl") or 0.0)
                amt = float(row.get("position_amt") or 0.0)
                if abs(amt) > 0:
                    open_pos_symbol = str(sym).upper()
                    open_pos_amt = amt
                    open_pos_upnl = float(row.get("unrealized_pnl") or 0.0)

        # 1) Query current leverage from Binance for each enabled symbol.
        # If this query fails, keep execution continuing but mark sync error so we don't
        # blindly sync based on stale/missing leverage data.
        leverage_query_error: Optional[str] = None
        if enabled:
            try:
                lev_map = await asyncio.to_thread(self._binance.get_symbol_leverage, symbols=enabled)
                if not isinstance(lev_map, dict):
                    lev_map = {}
                for sym, lev in lev_map.items():
                    if lev is None:
                        continue
                    sym_u = str(sym).upper()
                    try:
                        snap.symbol_leverage[sym_u] = float(lev)
                    except Exception:
                        continue
            except Exception as e:  # noqa: BLE001
                leverage_query_error = f"{type(e).__name__}: {e}"
                snap.leverage_sync_error = leverage_query_error
                logger.warning(
                    "leverage_query_failed",
                    extra={"error": leverage_query_error, "symbols": enabled},
                )
                if self._oplog:
                    try:
                        self._oplog.log_event(
                            "LEVERAGE_SYNC",
                            {
                                "action": "query",
                                "symbols": enabled,
                                "result": "failed",
                                "error": leverage_query_error,
                            },
                        )
                    except Exception:
                        logger.exception("oplog_leverage_sync_query_failed")

        sync_error_items: List[str] = []
        if not leverage_query_error:
            for sym in enabled:
                sym = str(sym).upper()
                target = symbol_target_map.get(sym, target_lev)
                target = float(target) if target is not None else target_lev
                cur = snap.symbol_leverage.get(sym)
                if cur is None:
                    sync_error_items.append(f"{sym}: leverage_unavailable")
                    continue
                if abs(cur - target) < 1e-9:
                    continue
                try:
                    out = await asyncio.to_thread(self._binance.set_leverage, symbol=sym, leverage=target)
                    logger.info(
                        "leverage_sync_ok",
                        extra={
                            "symbol": sym,
                            "target_leverage": target,
                            "current_leverage": cur,
                            "response": out,
                        },
                    )
                    if self._oplog:
                        try:
                            self._oplog.log_event(
                                "LEVERAGE_SYNC",
                                {
                                    "action": "set",
                                    "symbol": sym,
                            "target": target,
                            "current": cur,
                            "result": "ok",
                                },
                            )
                        except Exception:
                            logger.exception("oplog_leverage_sync_failed")
                    snap.symbol_leverage[sym] = target
                except Exception as e:  # noqa: BLE001
                    msg = f"{sym}: {type(e).__name__}: {e}"
                    sync_error_items.append(msg)
                    logger.warning(
                        "leverage_sync_failed",
                        extra={
                            "symbol": sym,
                            "target_leverage": target,
                            "current_leverage": cur,
                        },
                    )
                    if self._oplog:
                        try:
                            self._oplog.log_event(
                                "LEVERAGE_SYNC",
                                {
                                    "action": "set",
                                    "symbol": sym,
                            "target": target,
                            "current": cur,
                                    "result": "failed",
                                    "error": msg,
                                },
                            )
                        except Exception:
                            logger.exception("oplog_leverage_sync_failed")
        else:
            if target_lev > 0 and leverage_query_error:
                sync_error_items.append(f"query_failed: {leverage_query_error}")

        if not leverage_query_error:
            snap.leverage_sync_updated_at = _utcnow().isoformat()
        if sync_error_items:
            snap.leverage_sync_error = "; ".join(sync_error_items)

        equity = wallet + upnl_total

        # Update equity peak tracking (also helps /status).
        await asyncio.to_thread(self._pnl.update_equity_peak, equity_usdt=equity)

        # Collect market data (multi TF) for universe.
        scoring_tfs, scoring_weights = self._resolve_scoring_setup(cfg=cfg)
        snap.active_scoring_timeframes = list(scoring_tfs)
        snap.scoring_weights = dict(scoring_weights)
        snap.min_bars_factor = self._resolve_min_bars_factor(
            tick_sec=self._tick_sec,
            scoring_timeframes=scoring_tfs,
        )

        candles_by_symbol_interval: Dict[str, Dict[str, Any]] = {}
        for sym in enabled:
            by_itv: Dict[str, Any] = {}
            for itv in scoring_tfs:
                cs = await asyncio.to_thread(self._market_data.get_klines, symbol=sym, interval=itv, limit=260)
                by_itv[itv] = cs
            candles_by_symbol_interval[sym] = by_itv

        scores: Dict[str, SymbolScore] = await asyncio.to_thread(
            self._scoring.score_universe,
            cfg=cfg,
            candles_by_symbol_interval=candles_by_symbol_interval,
            min_bars_factor=snap.min_bars_factor,
        )
        candidate: Optional[Candidate] = self._scoring.pick_candidate(scores=scores)

        # Snapshot: expose "last_scores / last_candidate" and keep older fields populated.
        snap.last_scores = {k: v.as_dict() for k, v in scores.items()}
        snap.last_candidate = candidate.as_dict() if candidate else None
        snap.scores = dict(snap.last_scores)
        if candidate:
            ss = scores.get(candidate.symbol)
            vol_tag = "VOL_SHOCK" if (ss and ss.vol_shock) else "NORMAL"
            comp = float(ss.composite) if ss else 0.0
            snap.candidate_score_by_timeframe = dict(candidate.score_by_timeframe or {})
            snap.candidate = {
                "symbol": candidate.symbol,
                "direction": candidate.direction,
                "strength": float(candidate.strength),
                "composite": comp,
                "vol_tag": vol_tag,
                "confidence": float(candidate.confidence),
                "regime_4h": candidate.regime_4h,
            }
        else:
            snap.candidate = None

        # AI signal is advisory only; keep it for status/exec_hint default.
        st_pnl = await asyncio.to_thread(self._pnl.get_or_bootstrap)
        ai_ctx = {
            "candidate": snap.candidate,
            "scores": snap.scores,
            "engine_state": st.value,
            "position": {
                "symbol": open_pos_symbol,
                "amt": open_pos_amt,
                "upnl": open_pos_upnl,
            },
            "pnl": {
                "cooldown_until": getattr(st_pnl, "cooldown_until", None),
                "lose_streak": getattr(st_pnl, "lose_streak", 0),
            },
            "spreads": b.get("spreads") if isinstance(b, dict) else {},
        }
        ai_sig: AiSignal = self._ai.get_signal(ai_ctx)
        snap.ai_signal = ai_sig.as_dict()

        # Strategy decision.
        pos_state = PositionState(
            symbol=open_pos_symbol,
            position_amt=open_pos_amt,
            unrealized_pnl=open_pos_upnl,
            last_entry_symbol=getattr(st_pnl, "last_entry_symbol", None),
            last_entry_at=getattr(st_pnl, "last_entry_at", None),
        )
        dec: StrategyDecision = self._strategy.decide_next_action(
            cfg=cfg,
            now=_utcnow(),
            candidate=candidate,
            scores=scores,
            position=pos_state,
        )
        snap.last_decision_reason = dec.reason
        if self._oplog:
            try:
                sym = candidate.symbol if candidate else "-"
                direction = candidate.direction if candidate else "NONE"
                conf = float(candidate.confidence) if candidate and candidate.confidence is not None else None
                regime = candidate.regime_4h if candidate else None
                score_payload = snap.last_scores.get(sym, {}) if candidate else {}
                self._oplog.log_decision(
                    cycle_id=str(snap.tick_started_at),
                    symbol=str(sym),
                    direction=str(direction),
                    confidence=conf,
                    regime_4h=regime,
                    scores_json=score_payload,
                    reason=dec.reason,
                )
            except Exception:
                logger.exception("oplog_decision_failed")

        logger.info(
            "strategy_tick",
            extra={
                "engine_state": st.value,
                "enabled_symbols": enabled,
                "candidate": snap.last_candidate,
                "decision": dec.as_dict(),
                "open_pos_symbol": open_pos_symbol,
            },
        )

        # Periodic status notify (default 30m via risk_config.notify_interval_sec).
        if self._snapshot:
            try:
                self._snapshot.maybe_capture_periodic(
                    cycle_id=str(snap.tick_started_at),
                    interval_sec=max(int(cfg.notify_interval_sec), 10),
                )
            except Exception:
                logger.exception("snapshot_periodic_failed")
        await self._maybe_send_status(
            cfg_notify_interval_sec=int(cfg.notify_interval_sec),
            st=st,
            equity=equity,
            upnl_total=upnl_total,
            open_pos_symbol=open_pos_symbol,
            open_pos_amt=open_pos_amt,
            candidate=candidate,
            snap=snap,
        )

        # Execution gating: RUNNING only.
        if st != EngineState.RUNNING:
            return

        # Apply decision.
        if dec.kind == "HOLD":
            snap.last_action = "hold"
            return

        if dec.kind == "CLOSE":
            sym = str(dec.close_symbol or "").upper()
            if not sym:
                snap.last_error = "close_symbol_missing"
                return
            close_reason = "TAKE_PROFIT" if float(open_pos_upnl) >= 0 else "STOP_LOSS"
            close_trigger_price = None
            if isinstance(b, dict):
                spread = (b.get("spreads") or {}).get(sym)
                if isinstance(spread, dict):
                    bid = _safe_float(spread.get("bid"))
                    ask = _safe_float(spread.get("ask"))
                    if bid is not None and ask is not None:
                        close_trigger_price = (bid + ask) / 2.0
            try:
                close_kwargs: Dict[str, Any] = {}
                if close_reason == "TAKE_PROFIT" and close_trigger_price is not None:
                    close_kwargs["take_profit_price"] = close_trigger_price
                elif close_reason == "STOP_LOSS" and close_trigger_price is not None:
                    close_kwargs["stop_loss_price"] = close_trigger_price
                if close_trigger_price is not None:
                    close_kwargs["trigger_price"] = close_trigger_price
                out = await self._execution.close_position(sym, reason=close_reason, **close_kwargs)
                snap.last_action = f"close:{sym}"
                snap.last_error = None
                logger.info("strategy_close", extra={"symbol": sym, "detail": out})
            except ExecutionRejected as e:
                snap.last_action = f"close:{sym}"
                snap.last_error = str(e)
            return

        # ENTER/REBALANCE both require sizing a target notional.
        if open_pos_symbol:
            # Startup/restart safety: never auto-enter a new position while one is already open.
            snap.last_action = "hold_with_open_position"
            return
        target_symbol = str(dec.enter_symbol or "").upper()
        if not target_symbol:
            snap.last_error = "enter_symbol_missing"
            return
        dir_s = str(dec.enter_direction or "").upper()
        direction = Direction.LONG if dir_s == "LONG" else Direction.SHORT

        capital_mode = str(cfg.capital_mode.value if hasattr(cfg.capital_mode, "value") else cfg.capital_mode).upper()
        use_margin_budget_mode = capital_mode == CapitalMode.MARGIN_BUDGET_USDT.value
        if use_margin_budget_mode:
            size = await asyncio.to_thread(
                self._sizing.compute_live,
                symbol=target_symbol,
                risk=cfg,
                leverage=self._risk.get_leverage_for_symbol(symbol=target_symbol),
            )
        else:
            # Compute sizing: use 30m ATR% as stop distance proxy (fallback 1%).
            ss = scores.get(target_symbol)
            atr_pct = 1.0
            if ss and "30m" in ss.timeframes:
                atr_pct = float(ss.timeframes["30m"].atr_pct or 1.0)
            stop_distance_pct = max(float(atr_pct), 0.5)

            # Price reference: prefer book mid from spreads, else last close.
            bt = (b.get("spreads") or {}).get(target_symbol) if isinstance(b, dict) else None
            bid = float((bt or {}).get("bid") or 0.0) if isinstance(bt, dict) else 0.0
            ask = float((bt or {}).get("ask") or 0.0) if isinstance(bt, dict) else 0.0
            price = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0.0
            if price <= 0.0:
                last = await asyncio.to_thread(self._market_data.get_last_close, symbol=target_symbol, interval="30m", limit=2)
                price = float(last or 0.0)
            if price <= 0.0:
                snap.last_error = "price_unavailable"
                return

            size = await asyncio.to_thread(
                self._sizing.compute,
                symbol=target_symbol,
                risk=cfg,
                equity_usdt=equity,
                available_usdt=available,
                price=price,
                stop_distance_pct=stop_distance_pct,
                existing_exposure_notional_usdt=0.0,
            )
        if size.target_notional_usdt <= 0 or size.target_qty <= 0:
            snap.last_error = f"sizing_blocked:{size.capped_by or 'unknown'}"
            return

        # Exec hint: take AI's suggestion (advisory) but default to LIMIT.
        hint_raw = str(ai_sig.exec_hint or "LIMIT").upper()
        exec_hint = ExecHint.LIMIT
        if hint_raw in ("MARKET", "LIMIT", "SPLIT"):
            exec_hint = ExecHint(hint_raw)

        intent = {
            "op": dec.kind,
            "symbol": target_symbol,
            "direction": direction,
            "exec_hint": exec_hint,
            "notional_usdt": float(size.target_notional_usdt),
        }

        if dec.kind == "REBALANCE":
            close_sym = str(dec.close_symbol or "").upper()
            if close_sym:
                try:
                    out = await self._execution.rebalance(close_symbol=close_sym, enter_intent=intent)
                    if bool(out.get("blocked")):
                        reason = str(out.get("block_reason") or "entry_blocked")
                        snap.last_action = f"{dec.kind.lower()}_blocked:{target_symbol}:{direction.value}"
                        snap.last_error = reason
                    else:
                        snap.last_action = f"{dec.kind.lower()}_enter:{target_symbol}:{direction.value}"
                        snap.last_error = None
                    logger.info("strategy_rebalance", extra={"symbol": target_symbol, "detail": out})
                except ExecutionRejected as e:
                    snap.last_action = f"{dec.kind.lower()}_enter:{target_symbol}:{direction.value}"
                    snap.last_error = str(e)
                except Exception as e:  # noqa: BLE001
                    snap.last_action = f"{dec.kind.lower()}_enter:{target_symbol}:{direction.value}"
                    snap.last_error = f"{type(e).__name__}: {e}"
                return

        try:
            out = await self._execution.enter_position(intent)
            if bool(out.get("blocked")):
                reason = str(out.get("block_reason") or "entry_blocked")
                snap.last_action = f"{dec.kind.lower()}_blocked:{target_symbol}:{direction.value}"
                snap.last_error = reason
            else:
                snap.last_action = f"{dec.kind.lower()}_enter:{target_symbol}:{direction.value}"
                snap.last_error = None
            logger.info("strategy_enter", extra={"symbol": target_symbol, "direction": direction.value, "detail": out})
        except ExecutionRejected as e:
            snap.last_action = f"{dec.kind.lower()}_enter:{target_symbol}:{direction.value}"
            snap.last_error = str(e)
        except Exception as e:  # noqa: BLE001
            snap.last_action = f"{dec.kind.lower()}_enter:{target_symbol}:{direction.value}"
            snap.last_error = f"{type(e).__name__}: {e}"

    async def _maybe_send_status(
        self,
        *,
        cfg_notify_interval_sec: int,
        st: EngineState,
        equity: float,
        upnl_total: float,
        open_pos_symbol: Optional[str],
        open_pos_amt: float,
        candidate: Optional[Candidate],
        snap: SchedulerSnapshot,
    ) -> None:
        await self._maybe_send_daily_report(st=st)

        if not self._notifier:
            return
        interval = max(int(cfg_notify_interval_sec), 10)
        now_mono = time.monotonic()
        if (now_mono - self._last_status_notify_ts) < interval:
            return

        st_pnl = await asyncio.to_thread(self._pnl.get_or_bootstrap)
        m = await asyncio.to_thread(self._pnl.compute_metrics, st=st_pnl, equity_usdt=equity)
        candidate_score_by_tf: Dict[str, Any] = {}
        candidate_active_tfs: list[str] = []
        if candidate:
            candidate_score_by_tf = dict(candidate.score_by_timeframe or snap.candidate_score_by_timeframe or {})
            candidate_active_tfs = list(candidate.active_timeframes or [])

        scoring_weights = dict(snap.scoring_weights)
        if not scoring_weights:
            scoring_weights = {
                "10m": 0.25,
                "30m": 0.25,
                "1h": 0.25,
                "4h": 0.25,
            }

        candidate_score_by_tf = dict(candidate_score_by_tf)
        if not candidate_score_by_tf and snap.candidate is not None:
            candidate_score_by_tf = dict((snap.candidate or {}).get("score_by_timeframe", {}))

        payload = {
            "engine_state": st.value,
            "position_symbol": open_pos_symbol,
            "position_amt": float(open_pos_amt),
            "position_side": "LONG" if open_pos_amt > 0 else "SHORT" if open_pos_amt < 0 else None,
            "upnl": float(upnl_total),
            "daily_pnl_pct": float(m.daily_pnl_pct),
            "drawdown_pct": float(m.drawdown_pct),
            "candidate_symbol": candidate.symbol if candidate else None,
            "candidate_active_timeframes": candidate_active_tfs,
            "candidate_score_by_timeframe": candidate_score_by_tf,
            "regime": candidate.regime_4h if candidate else None,
            "last_decision_reason": snap.last_decision_reason,
            "last_action": snap.last_action,
            "last_error": snap.last_error,
            "active_scoring_timeframes": [k for k in snap.active_scoring_timeframes if k in scoring_weights],
            "scoring_weights": scoring_weights,
            "min_bars_factor": float(snap.min_bars_factor),
            "tick_scan_seconds": float(self._tick_sec),
        }
        try:
            await self._notifier.send_status_snapshot(payload)
            self._last_status_notify_ts = now_mono
        except Exception:
            logger.exception("status_notify_failed")

    def _build_daily_report_payload(self, *, day: str, st: Optional[EngineState] = None) -> Dict[str, Any]:
        details: Dict[str, Any] = {
            "orders": [],
            "entries": 0,
            "closes": 0,
            "errors": 0,
            "canceled": 0,
            "total_records": 0,
            "blocks": 0,
        }

        if self._order_records is not None:
            try:
                details.update(self._order_records.get_daily_fill_stats(day=day))
                raw_orders = self._order_records.get_daily_order_events(day=day, limit=30)
                orders: list[Dict[str, Any]] = []
                for o in raw_orders:
                    status = str(o.get("status") or "").upper()
                    reduce_only = bool(int(o.get("reduce_only") or 0))
                    side = str(o.get("side") or "-").upper()
                    if status == "FILLED":
                        action = "CLOSE" if reduce_only else "ENTRY"
                    elif status == "ERROR":
                        action = "ERROR"
                    elif status == "CANCELED":
                        action = "CANCELED"
                    else:
                        action = status or "-"
                    o2 = {
                        "ts_created": str(o.get("ts_created") or ""),
                        "ts_updated": str(o.get("ts_updated") or ""),
                        "status": status,
                        "action": action,
                        "symbol": str(o.get("symbol") or "-").upper(),
                        "side": side,
                        "reduce_only": reduce_only,
                        "qty": float(o["qty"]) if o.get("qty") is not None else None,
                        "price": float(o["price"]) if o.get("price") is not None else None,
                        "intent_id": str(o.get("intent_id")) if o.get("intent_id") is not None else None,
                        "cycle_id": str(o.get("cycle_id")) if o.get("cycle_id") is not None else None,
                        "run_id": str(o.get("run_id")) if o.get("run_id") is not None else None,
                        "order_type": str(o.get("order_type")) if o.get("order_type") is not None else None,
                        "exchange_order_id": str(o.get("exchange_order_id")) if o.get("exchange_order_id") is not None else None,
                        "last_error": str(o.get("last_error")) if o.get("last_error") is not None else None,
                    }
                    if status and not action:
                        action = status
                    orders.append(o2)
                details["orders"] = orders
            except Exception:
                logger.exception("daily_report_order_stats_failed", extra={"day": day})

        if self._risk_blocks is not None:
            try:
                details["blocks"] = int(self._risk_blocks.get_daily_block_count(day=day))
            except Exception:
                logger.exception("daily_report_block_stats_failed", extra={"day": day})

        engine_state = st or self._engine.get_state().state
        engine_state_value = engine_state.value if isinstance(engine_state, EngineState) else str(engine_state)

        return {
            "kind": "DAILY_REPORT",
            "day": day,
            "engine_state": engine_state_value,
            "reported_at": datetime.now(tz=timezone.utc).isoformat(),
            "detail": details,
        }

    async def _send_daily_report_payload(
        self,
        *,
        payload: Mapping[str, Any],
        require_discord_webhook: bool = False,
    ) -> tuple[bool, Optional[str]]:
        if self._report_notifier is None:
            return False, "notifier_missing"
        if require_discord_webhook and isinstance(self._report_notifier, LoggingNotifier):
            return False, "discord_webhook_not_configured"
        try:
            await self._report_notifier.send_event(payload)
            return True, None
        except Exception as e:  # noqa: BLE001
            logger.exception("daily_report_send_failed", extra={"day": payload.get("day")})
            return False, f"{type(e).__name__}: {e}"

    async def _maybe_send_daily_report(self, *, st: EngineState) -> None:
        if self._order_records is None and self._risk_blocks is None:
            return

        if not self._report_notifier:
            if self._last_daily_report_date is None:
                self._last_daily_report_date = datetime.now(tz=timezone.utc).date().isoformat()
            else:
                self._last_daily_report_date = datetime.now(tz=timezone.utc).date().isoformat()
            return

        now_day = datetime.now(tz=timezone.utc).date().isoformat()
        if self._last_daily_report_date is None:
            self._last_daily_report_date = now_day
            return

        if self._last_daily_report_date == now_day:
            return

        report_day = self._last_daily_report_date
        self._last_daily_report_date = now_day

        payload = self._build_daily_report_payload(day=report_day, st=st)
        try:
            await self._send_daily_report_payload(payload=payload, require_discord_webhook=False)
        except Exception:
            logger.exception("daily_report_send_failed", extra={"day": report_day})

