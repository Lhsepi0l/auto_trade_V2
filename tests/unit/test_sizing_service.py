from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

import pytest

from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.services.sizing_service import SizingService


@dataclass
class _FakeClient:
    step_size: float = 0.001
    min_qty: float = 0.001
    min_notional: float | None = 5.0
    available: float = 1000.0
    mark_price: float = 100.0

    def get_exchange_info_cached(self) -> Dict[str, Any]:
        return {"symbols": [{"symbol": "BTCUSDT"}]}

    def get_symbol_filters(self, *, symbol: str) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "step_size": self.step_size,
            "min_qty": self.min_qty,
            "min_notional": self.min_notional,
        }

    def get_account_balance_usdtm(self) -> Dict[str, float]:
        return {"wallet": self.available, "available": self.available}

    def get_mark_price(self, symbol: str) -> Dict[str, str]:
        return {"symbol": symbol, "markPrice": str(self.mark_price)}


def _cfg(**kwargs: object) -> RiskConfig:
    base = RiskConfig(
        per_trade_risk_pct=1.0,
        max_exposure_pct=1.0,
        max_notional_pct=100.0,
        max_leverage=5.0,
        daily_loss_limit_pct=-0.02,
        dd_limit_pct=-0.15,
        lose_streak_n=3,
        cooldown_hours=6.0,
        notify_interval_sec=120,
        capital_mode="PCT_AVAILABLE",
        capital_pct=0.20,
        capital_usdt=100.0,
        margin_budget_usdt=100.0,
        margin_use_pct=0.90,
        max_position_notional_usdt=None,
        fee_buffer_pct=0.002,
    )
    return base.model_copy(update=dict(kwargs))


@pytest.mark.unit
def test_pct_mode_budget() -> None:
    svc = SizingService(client=_FakeClient())  # type: ignore[arg-type]
    out = svc.compute_live(symbol="BTCUSDT", risk=_cfg(capital_mode="PCT_AVAILABLE", capital_pct=0.2), leverage=5)
    # available_net=1000*(1-0.002)=998, budget=199.6
    assert out.blocked is False
    assert out.budget_usdt == pytest.approx(199.6, rel=1e-6)


@pytest.mark.unit
def test_fixed_mode_budget() -> None:
    svc = SizingService(client=_FakeClient())  # type: ignore[arg-type]
    out = svc.compute_live(symbol="BTCUSDT", risk=_cfg(capital_mode="FIXED_USDT", capital_usdt=150), leverage=5)
    assert out.blocked is False
    assert out.budget_usdt == pytest.approx(150.0, rel=1e-6)


@pytest.mark.unit
def test_margin_budget_mode_uses_direct_margin_cap() -> None:
    svc = SizingService(client=_FakeClient())  # type: ignore[arg-type]
    out = svc.compute_live(
        symbol="BTCUSDT",
        risk=_cfg(capital_mode="MARGIN_BUDGET_USDT", margin_budget_usdt=120, margin_use_pct=0.1),
        leverage=5,
    )
    assert out.blocked is False
    assert out.budget_usdt == pytest.approx(120.0, rel=1e-6)
    assert out.used_margin == pytest.approx(120.0, rel=1e-6)
    assert out.notional_usdt == pytest.approx(600.0, rel=1e-6)


@pytest.mark.unit
def test_fee_buffer_pct_effect() -> None:
    svc = SizingService(client=_FakeClient())  # type: ignore[arg-type]
    no_fee = svc.compute_live(symbol="BTCUSDT", risk=_cfg(fee_buffer_pct=0.0, capital_pct=0.2), leverage=5)
    with_fee = svc.compute_live(symbol="BTCUSDT", risk=_cfg(fee_buffer_pct=0.01, capital_pct=0.2), leverage=5)
    assert no_fee.budget_usdt > with_fee.budget_usdt


@pytest.mark.unit
def test_max_exposure_pct_clamp() -> None:
    svc = SizingService(client=_FakeClient())  # type: ignore[arg-type]
    out = svc.compute_live(
        symbol="BTCUSDT",
        risk=_cfg(capital_mode="FIXED_USDT", capital_usdt=500, max_exposure_pct=0.1),
        leverage=5,
    )
    # max exposure = available_net * 0.1 = 99.8
    assert out.budget_usdt == pytest.approx(99.8, rel=1e-6)


@pytest.mark.unit
def test_max_position_notional_usdt_clamp() -> None:
    svc = SizingService(client=_FakeClient())  # type: ignore[arg-type]
    out = svc.compute_live(
        symbol="BTCUSDT",
        risk=_cfg(capital_mode="FIXED_USDT", capital_usdt=300, margin_use_pct=1.0, max_position_notional_usdt=120),
        leverage=5,
    )
    assert out.notional_usdt == pytest.approx(120.0, rel=1e-6)


@pytest.mark.unit
def test_block_reason_min_notional() -> None:
    svc = SizingService(client=_FakeClient(min_notional=200.0))  # type: ignore[arg-type]
    out = svc.compute_live(
        symbol="BTCUSDT",
        risk=_cfg(capital_mode="FIXED_USDT", capital_usdt=10, margin_use_pct=0.1),
        leverage=1,
    )
    assert out.blocked is True
    assert out.block_reason == "BUDGET_TOO_SMALL_FOR_MIN_NOTIONAL"


@pytest.mark.unit
def test_block_reason_min_qty() -> None:
    svc = SizingService(client=_FakeClient(min_qty=0.5, min_notional=0.0))  # type: ignore[arg-type]
    out = svc.compute_live(
        symbol="BTCUSDT",
        risk=_cfg(capital_mode="FIXED_USDT", capital_usdt=20, margin_use_pct=0.1),
        leverage=1,
    )
    assert out.blocked is True
    assert out.block_reason == "BUDGET_TOO_SMALL_FOR_MIN_QTY"
