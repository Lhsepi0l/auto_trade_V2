from __future__ import annotations

import pytest

from apps.trader_engine.services.scoring_service import _regime_4h


@pytest.mark.unit
def test_regime_bull() -> None:
    assert _regime_4h(ema_fast=110, ema_slow=100, rsi_v=60) == "BULL"


@pytest.mark.unit
def test_regime_bear() -> None:
    assert _regime_4h(ema_fast=90, ema_slow=100, rsi_v=40) == "BEAR"


@pytest.mark.unit
def test_regime_choppy() -> None:
    assert _regime_4h(ema_fast=100, ema_slow=100, rsi_v=50) == "CHOPPY"

