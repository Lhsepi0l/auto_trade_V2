from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from apps.trader_engine.domain.enums import Direction, EngineState, ExecHint
from apps.trader_engine.services.ai_service import AiService, AiSignal
from apps.trader_engine.services.binance_service import BinanceService
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.execution_service import ExecutionRejected, ExecutionService
from apps.trader_engine.services.market_data_service import MarketDataService
from apps.trader_engine.services.notifier_service import Notifier
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.risk_config_service import RiskConfigService
from apps.trader_engine.services.scoring_service import Candidate, ScoringService, SymbolScore
from apps.trader_engine.services.sizing_service import SizingService
from apps.trader_engine.services.strategy_service import PositionState, StrategyDecision, StrategyService
from apps.trader_engine.services.oplog import OperationalLogger
from apps.trader_engine.services.snapshot_service import SnapshotService

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


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


class TraderScheduler:
    """30m tick strategy loop.

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
        tick_sec: float = 1800.0,
        reverse_threshold: float = 0.55,
        oplog: Optional[OperationalLogger] = None,
        snapshot: Optional[SnapshotService] = None,
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
        self._oplog = oplog
        self._snapshot = snapshot

        self._tick_sec = float(tick_sec)
        self._reverse_threshold = float(reverse_threshold)
        self._last_status_notify_ts = 0.0

        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self.snapshot: Optional[SchedulerSnapshot] = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="trader_scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None

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

    async def _sleep_until_next(self) -> None:
        total = max(self._tick_sec, 1.0)
        end = time.time() + total
        while time.time() < end:
            if self._stop.is_set():
                return
            await asyncio.sleep(min(1.0, end - time.time()))

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
        if isinstance(positions, dict):
            for sym, row in positions.items():
                if not isinstance(row, dict):
                    continue
                upnl_total += float(row.get("unrealized_pnl") or 0.0)
                amt = float(row.get("position_amt") or 0.0)
                if abs(amt) > 0:
                    open_pos_symbol = str(sym).upper()
                    open_pos_amt = amt
                    open_pos_upnl = float(row.get("unrealized_pnl") or 0.0)
        equity = wallet + upnl_total

        # Update equity peak tracking (also helps /status).
        await asyncio.to_thread(self._pnl.update_equity_peak, equity_usdt=equity)

        # Collect market data (multi TF) for universe.
        candles_by_symbol_interval: Dict[str, Dict[str, Any]] = {}
        for sym in enabled:
            by_itv: Dict[str, Any] = {}
            for itv in ("30m", "1h", "4h"):
                cs = await asyncio.to_thread(self._market_data.get_klines, symbol=sym, interval=itv, limit=260)
                by_itv[itv] = cs
            candles_by_symbol_interval[sym] = by_itv

        scores: Dict[str, SymbolScore] = await asyncio.to_thread(
            self._scoring.score_universe,
            cfg=cfg,
            candles_by_symbol_interval=candles_by_symbol_interval,
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
                self._snapshot.maybe_capture_periodic(cycle_id=str(snap.tick_started_at), interval_sec=1800)
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
            try:
                out = await self._execution.close_position(sym, reason=close_reason)
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
        if not self._notifier:
            return
        interval = max(int(cfg_notify_interval_sec), 10)
        now_mono = time.monotonic()
        if (now_mono - self._last_status_notify_ts) < interval:
            return

        st_pnl = await asyncio.to_thread(self._pnl.get_or_bootstrap)
        m = await asyncio.to_thread(self._pnl.compute_metrics, st=st_pnl, equity_usdt=equity)
        payload = {
            "engine_state": st.value,
            "position_symbol": open_pos_symbol,
            "position_amt": float(open_pos_amt),
            "upnl": float(upnl_total),
            "daily_pnl_pct": float(m.daily_pnl_pct),
            "drawdown_pct": float(m.drawdown_pct),
            "candidate_symbol": candidate.symbol if candidate else None,
            "regime": candidate.regime_4h if candidate else None,
            "last_decision_reason": snap.last_decision_reason,
            "last_action": snap.last_action,
            "last_error": snap.last_error,
        }
        try:
            await self._notifier.send_status_snapshot(payload)
            self._last_status_notify_ts = now_mono
        except Exception:
            logger.exception("status_notify_failed")
