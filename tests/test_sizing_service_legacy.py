from __future__ import annotations

from dataclasses import dataclass

from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.sizing_service import SizingService


@dataclass
class _FakeClient:
    step_size: float = 0.001
    min_qty: float = 0.0
    min_notional: float | None = None
    available: float = 1000.0
    mark_price: float = 100.0

    def get_symbol_filters(self, *, symbol: str):
        return {
            "symbol": symbol,
            "step_size": self.step_size,
            "min_qty": self.min_qty,
            "min_notional": self.min_notional,
        }

    def get_exchange_info_cached(self):
        return {"symbols": [{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}]}

    def get_account_balance_usdtm(self):
        return {"wallet": self.available, "available": self.available}

    def get_mark_price(self, symbol: str):
        return {"symbol": symbol, "markPrice": str(self.mark_price)}


def test_sizing_computes_notional_from_risk_and_stop_distance():
    risk = RiskConfig(
        per_trade_risk_pct=1.0,
        max_exposure_pct=1.0,
        max_notional_pct=100.0,
        max_leverage=10.0,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6.0,
        notify_interval_sec=120,
    )
    svc = SizingService(client=_FakeClient())  # type: ignore[arg-type]
    # equity 1000, risk 1% => $10 risk budget. stop distance 2% => $500 notional.
    res = svc.compute(
        symbol="BTCUSDT",
        risk=risk,
        equity_usdt=1000.0,
        available_usdt=1000.0,
        price=100.0,
        stop_distance_pct=2.0,
    )
    assert abs(res.target_notional_usdt - 500.0) < 1e-6
    assert res.target_qty > 0


def test_sizing_respects_max_notional_pct_cap():
    risk = RiskConfig(
        per_trade_risk_pct=5.0,
        max_exposure_pct=1.0,
        max_notional_pct=10.0,
        max_leverage=10.0,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6.0,
        notify_interval_sec=120,
    )
    svc = SizingService(client=_FakeClient())  # type: ignore[arg-type]
    res = svc.compute(
        symbol="BTCUSDT",
        risk=risk,
        equity_usdt=1000.0,
        available_usdt=1000.0,
        price=100.0,
        stop_distance_pct=1.0,
    )
    assert res.target_notional_usdt <= 100.0 + 1e-6
    assert res.capped_by in ("max_notional_pct", None)


def test_live_sizing_pct_available_mode():
    risk = RiskConfig(
        per_trade_risk_pct=1.0,
        max_exposure_pct=0.5,
        max_notional_pct=100.0,
        max_leverage=5.0,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6.0,
        notify_interval_sec=120,
        capital_mode="PCT_AVAILABLE",
        capital_pct=0.2,
        capital_usdt=100.0,
        margin_use_pct=0.9,
        max_position_notional_usdt=None,
        fee_buffer_pct=0.002,
    )
    svc = SizingService(client=_FakeClient())  # type: ignore[arg-type]
    out = svc.compute_live(symbol="BTCUSDT", risk=risk, leverage=5.0)
    assert out.blocked is False
    assert out.available_usdt == 1000.0
    assert out.budget_usdt > 0.0
    assert out.qty > 0.0


def test_live_sizing_blocks_for_min_notional():
    risk = RiskConfig(
        per_trade_risk_pct=1.0,
        max_exposure_pct=1.0,
        max_notional_pct=100.0,
        max_leverage=2.0,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6.0,
        notify_interval_sec=120,
        capital_mode="FIXED_USDT",
        capital_pct=0.2,
        capital_usdt=5.0,
        margin_use_pct=0.1,
        max_position_notional_usdt=None,
        fee_buffer_pct=0.0,
    )
    svc = SizingService(client=_FakeClient(min_notional=100.0))  # type: ignore[arg-type]
    out = svc.compute_live(symbol="BTCUSDT", risk=risk, leverage=1.0)
    assert out.blocked is True
    assert out.block_reason == "BUDGET_TOO_SMALL_FOR_MIN_NOTIONAL"
