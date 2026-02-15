from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from apps.trader_engine.domain.models import PnLState
from apps.trader_engine.storage.repositories import PnLStateRepo

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _utc_day_str(ts: Optional[datetime] = None) -> str:
    t = ts or _utcnow()
    return t.date().isoformat()


@dataclass(frozen=True)
class PnLMetrics:
    equity_usdt: float
    daily_pnl_pct: float
    drawdown_pct: float


class PnLService:
    """Minimal PnL tracking to support policy guards.

    Notes:
    - "daily_realized_pnl" is tracked as a running sum for the current UTC day.
    - Realized PnL is measured by wallet balance deltas around close actions (best-effort).
    - equity_peak is tracked to compute simple drawdown (equity vs peak).
    """

    def __init__(self, *, repo: PnLStateRepo) -> None:
        self._repo = repo

    def get_or_bootstrap(self) -> PnLState:
        st = self._repo.get()
        now = _utcnow()
        today = _utc_day_str(now)
        if st is None:
            st = PnLState(
                day=today,
                daily_realized_pnl=0.0,
                equity_peak=0.0,
                lose_streak=0,
                cooldown_until=None,
                last_entry_symbol=None,
                last_entry_at=None,
                last_block_reason=None,
                updated_at=now,
            )
            self._repo.upsert(st)
            return st

        # Day rollover: reset only daily realized PnL; keep streak/cooldown as risk state.
        if st.day != today:
            st = st.model_copy(
                update={
                    "day": today,
                    "daily_realized_pnl": 0.0,
                    "updated_at": now,
                }
            )
            self._repo.upsert(st)
        return st

    def set_last_block_reason(self, reason: Optional[str]) -> None:
        st = self.get_or_bootstrap()
        now = _utcnow()
        st = st.model_copy(update={"last_block_reason": reason, "updated_at": now})
        self._repo.upsert(st)

    def set_last_entry(self, *, symbol: Optional[str], at: Optional[datetime]) -> None:
        st = self.get_or_bootstrap()
        now = _utcnow()
        st = st.model_copy(update={"last_entry_symbol": symbol, "last_entry_at": at, "updated_at": now})
        self._repo.upsert(st)

    def set_last_fill(
        self,
        *,
        symbol: Optional[str],
        side: Optional[str],
        qty: Optional[float],
        price: Optional[float],
        realized_pnl: Optional[float],
        at: Optional[datetime],
    ) -> None:
        st = self.get_or_bootstrap()
        now = _utcnow()
        st = st.model_copy(
            update={
                "last_fill_symbol": symbol,
                "last_fill_side": side,
                "last_fill_qty": qty,
                "last_fill_price": price,
                "last_fill_realized_pnl": realized_pnl,
                "last_fill_time": at,
                "updated_at": now,
            }
        )
        self._repo.upsert(st)

    def update_equity_peak(self, *, equity_usdt: float) -> PnLState:
        st = self.get_or_bootstrap()
        now = _utcnow()
        peak = float(st.equity_peak or 0.0)
        if peak <= 0.0 or equity_usdt > peak:
            st = st.model_copy(update={"equity_peak": float(equity_usdt), "updated_at": now})
            self._repo.upsert(st)
        return st

    def apply_realized_pnl_delta(self, *, realized_delta_usdt: float, equity_usdt: float) -> PnLState:
        st = self.get_or_bootstrap()
        now = _utcnow()

        daily = float(st.daily_realized_pnl or 0.0) + float(realized_delta_usdt)
        # Lose streak is based on realized PnL sign at close.
        if realized_delta_usdt < 0:
            lose_streak = int(st.lose_streak or 0) + 1
        else:
            lose_streak = 0

        peak = float(st.equity_peak or 0.0)
        if peak <= 0.0 or equity_usdt > peak:
            peak = float(equity_usdt)

        st = st.model_copy(
            update={
                "daily_realized_pnl": daily,
                "lose_streak": lose_streak,
                "equity_peak": peak,
                "updated_at": now,
            }
        )
        self._repo.upsert(st)
        logger.info(
            "pnl_state_updated",
            extra={
                "day": st.day,
                "daily_realized_pnl": st.daily_realized_pnl,
                "lose_streak": st.lose_streak,
                "equity_peak": st.equity_peak,
            },
        )
        return st

    def set_cooldown_until(self, *, cooldown_until: Optional[datetime]) -> PnLState:
        st = self.get_or_bootstrap()
        now = _utcnow()
        st = st.model_copy(update={"cooldown_until": cooldown_until, "updated_at": now})
        self._repo.upsert(st)
        return st

    def clear_risk_guards(self) -> PnLState:
        """Clear cooldown/streak related guards for manual operator override."""
        st = self.get_or_bootstrap()
        now = _utcnow()
        st = st.model_copy(
            update={
                "lose_streak": 0,
                "cooldown_until": None,
                "last_block_reason": None,
                "updated_at": now,
            }
        )
        self._repo.upsert(st)
        return st

    def compute_metrics(self, *, st: PnLState, equity_usdt: float) -> PnLMetrics:
        equity = float(equity_usdt or 0.0)
        if equity <= 0.0:
            return PnLMetrics(equity_usdt=equity, daily_pnl_pct=0.0, drawdown_pct=0.0)

        daily_realized = float(st.daily_realized_pnl or 0.0)
        daily_pnl_pct = (daily_realized / equity) * 100.0

        peak = float(st.equity_peak or 0.0)
        if peak <= 0.0:
            drawdown_pct = 0.0
        else:
            drawdown_pct = ((equity - peak) / peak) * 100.0

        return PnLMetrics(equity_usdt=equity, daily_pnl_pct=daily_pnl_pct, drawdown_pct=drawdown_pct)
