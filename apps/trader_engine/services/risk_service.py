from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Optional

from apps.trader_engine.domain.enums import EngineState, ExecHint
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.risk_config_service import RiskConfigService

logger = logging.getLogger(__name__)


DecisionKind = Literal["ALLOW", "BLOCK", "PANIC"]


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: Optional[str] = None
    until: Optional[datetime] = None


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _pct_str(x: float) -> str:
    try:
        return f"{float(x):.4f}"
    except Exception:
        return str(x)


class RiskService:
    """Policy guard (risk engine) that decides whether an intent may place a real order.

    IMPORTANT: This service is expected to be called immediately before execution.
    If the decision is BLOCK or PANIC, execution must not send an order.
    """

    def __init__(
        self,
        *,
        risk: RiskConfigService,
        engine: EngineService,
        pnl: PnLService,
        stop_on_daily_loss: bool = False,
    ) -> None:
        self._risk = risk
        self._engine = engine
        self._pnl = pnl
        self._stop_on_daily_loss = bool(stop_on_daily_loss)

    def evaluate_pre_trade(
        self,
        intent: Mapping[str, Any],
        account_state: Mapping[str, Any],
        pnl_state: Mapping[str, Any],
        market_state: Mapping[str, Any],
    ) -> Decision:
        """Main entrypoint for pre-trade policy evaluation."""
        now = _utcnow()

        # If the engine is COOLDOWN but there's no active cooldown marker, recover to RUNNING.
        # This avoids a "stuck COOLDOWN" state if pnl_state was reset/cleared.
        cur_state = self._engine.get_state().state

        # Cooldown: active cooldown blocks entries.
        cd_until = pnl_state.get("cooldown_until")
        if isinstance(cd_until, datetime) and cd_until.tzinfo is None:
            cd_until = cd_until.replace(tzinfo=timezone.utc)
        if isinstance(cd_until, datetime) and now < cd_until:
            self._engine.set_state(EngineState.COOLDOWN)
            return Decision(kind="BLOCK", reason="cooldown_active", until=cd_until)

        # If cooldown has expired, allow engine to resume RUNNING automatically.
        if cur_state == EngineState.COOLDOWN and (cd_until is None or (isinstance(cd_until, datetime) and now >= cd_until)):
            self._engine.set_state(EngineState.RUNNING)
            # Persist cooldown cleared (best-effort).
            try:
                self._pnl.set_cooldown_until(cooldown_until=None)
            except Exception:
                pass

        if isinstance(cd_until, datetime) and now >= cd_until:
            if self._engine.get_state().state == EngineState.COOLDOWN:
                self._engine.set_state(EngineState.RUNNING)
            # Persist cooldown cleared (best-effort).
            try:
                self._pnl.set_cooldown_until(cooldown_until=None)
            except Exception:
                pass

        # Daily loss / drawdown checks (percent units).
        daily_pnl_pct = float(pnl_state.get("daily_pnl_pct") or 0.0)
        dd_pct = float(pnl_state.get("drawdown_pct") or 0.0)

        cfg = self._risk.get_config()

        # Compare ratios (e.g. -0.02 for -2%) against metrics (percent) converted to ratios.
        daily_pnl_ratio = daily_pnl_pct / 100.0
        dd_ratio = dd_pct / 100.0

        if daily_pnl_ratio <= float(cfg.daily_loss_limit_pct):
            if self._stop_on_daily_loss:
                self._engine.set_state(EngineState.STOPPED)
            return Decision(
                kind="BLOCK",
                reason=f"daily_loss_limit_reached:{_pct_str(daily_pnl_ratio)}<= {cfg.daily_loss_limit_pct}",
                until=None,
            )

        # dd_limit_pct is negative ratio; breach triggers PANIC.
        if dd_ratio <= float(cfg.dd_limit_pct):
            self._engine.panic()
            return Decision(kind="PANIC", reason=f"dd_limit_reached:{_pct_str(dd_ratio)}<= {cfg.dd_limit_pct}", until=None)

        # Lose streak based cooldown (if not already active).
        lose_streak = int(pnl_state.get("lose_streak") or 0)
        if lose_streak >= int(cfg.lose_streak_n):
            until = now + timedelta(hours=float(cfg.cooldown_hours))
            try:
                self._pnl.set_cooldown_until(cooldown_until=until)
            except Exception:
                pass
            self._engine.set_state(EngineState.COOLDOWN)
            return Decision(kind="BLOCK", reason="lose_streak_cooldown", until=until)

        # Spread guard (top-of-book based).
        spread_dec = self.spread_guard(intent=intent, market_state=market_state)
        if spread_dec.kind != "ALLOW":
            return spread_dec

        # Hard constraints (leverage / notional / exposure / 1-asset rule).
        cons = self.enforce_constraints(intent=intent, account_state=account_state)
        if cons.kind != "ALLOW":
            return cons

        return Decision(kind="ALLOW")

    def enforce_constraints(self, *, intent: Mapping[str, Any], account_state: Mapping[str, Any]) -> Decision:
        cfg = self._risk.get_config()

        equity_usdt = float(account_state.get("equity_usdt") or 0.0)
        if equity_usdt <= 0.0:
            return Decision(kind="BLOCK", reason="equity_unavailable")

        # Leverage: enforce user cfg but also hard cap <= 50.
        lev_in = intent.get("leverage")
        lev = float(lev_in) if lev_in is not None else 1.0
        max_lev = min(float(cfg.max_leverage), 50.0)
        if lev > max_lev:
            return Decision(kind="BLOCK", reason="leverage_above_max_leverage")

        # Single-asset safety: block if other symbols are already open.
        symbol = str(intent.get("symbol", "")).upper()
        open_symbols = account_state.get("open_symbols") or []
        if isinstance(open_symbols, (list, tuple)):
            others = [str(s).upper() for s in open_symbols if str(s).upper() and str(s).upper() != symbol]
            if others:
                return Decision(kind="BLOCK", reason="single_asset_rule_violation")

        notional = float(intent.get("notional_usdt_est") or intent.get("notional_usdt") or 0.0)
        if notional <= 0.0:
            return Decision(kind="BLOCK", reason="notional_unavailable")

        # Exposure cap: projected total exposure must be within max_exposure_pct of equity.
        existing_exposure = float(account_state.get("total_exposure_notional_usdt") or 0.0)
        projected = existing_exposure + notional
        max_exposure_ratio = float(cfg.max_exposure_pct or 0.0)
        if max_exposure_ratio > 0.0:
            max_exposure = equity_usdt * max_exposure_ratio
            if projected > max_exposure:
                return Decision(kind="BLOCK", reason="exposure_above_max_exposure")

        # Notional cap (direct) within max_notional_pct of equity.
        max_notional = equity_usdt * (float(cfg.max_notional_pct) / 100.0)
        if notional > max_notional:
            return Decision(kind="BLOCK", reason="notional_above_max_notional")

        # per_trade_risk_pct: treat as margin budget; scale by leverage to a notional ceiling.
        # This is intentionally conservative and simple for MVP.
        max_margin = equity_usdt * (float(cfg.per_trade_risk_pct) / 100.0)
        max_notional_from_risk = max_margin * lev
        if notional > max_notional_from_risk:
            return Decision(kind="BLOCK", reason="per_trade_risk_exceeded")

        return Decision(kind="ALLOW")

    def spread_guard(self, *, intent: Mapping[str, Any], market_state: Mapping[str, Any]) -> Decision:
        cfg = self._risk.get_config()
        exec_hint = intent.get("exec_hint")
        try:
            hint = ExecHint(str(getattr(exec_hint, "value", exec_hint)).upper())
        except Exception:
            hint = ExecHint.MARKET

        bid = float(market_state.get("bid") or 0.0)
        ask = float(market_state.get("ask") or 0.0)
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            # If book is missing, treat as unsafe for MARKET.
            if hint == ExecHint.MARKET:
                return Decision(kind="BLOCK", reason="book_unavailable_market_disabled")
            return Decision(kind="ALLOW")

        mid = (ask + bid) / 2.0
        if mid <= 0.0:
            if hint == ExecHint.MARKET:
                return Decision(kind="BLOCK", reason="book_unavailable_market_disabled")
            return Decision(kind="ALLOW")

        spread_ratio = (ask - bid) / mid
        # Defensive: if misconfigured with percent-like values (> 0.1), assume it's percent and convert.
        max_ratio = float(cfg.spread_max_pct)
        if max_ratio > 0.1:
            max_ratio = max_ratio / 100.0
        if spread_ratio < max_ratio:
            return Decision(kind="ALLOW")

        if hint == ExecHint.MARKET and not bool(cfg.allow_market_when_wide_spread):
            return Decision(kind="BLOCK", reason=f"spread_too_wide_market_disabled:{_pct_str(spread_ratio)}")

        return Decision(kind="ALLOW")
