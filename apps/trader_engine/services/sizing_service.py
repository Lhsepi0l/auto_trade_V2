from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import logging
import time
from typing import Any, Dict, Mapping, Optional

from apps.trader_engine.domain.models import RiskConfig
from apps.trader_engine.exchange.binance_usdm import BinanceUSDMClient

logger = logging.getLogger(__name__)


def _dec(x: Any) -> Decimal:
    return Decimal(str(x))


def _floor_to_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return qty
    return (qty // step) * step


@dataclass(frozen=True)
class SizingResult:
    available_usdt: float
    budget_usdt: float
    used_margin: float
    leverage: float
    notional_usdt: float
    mark_price: float
    qty: float
    blocked: bool
    block_reason: Optional[str] = None
    filters_ts: Optional[float] = None
    # Backward-compat fields for existing scheduler/legacy tests.
    stop_distance_pct: float = 0.0
    capped_by: Optional[str] = None

    @property
    def target_notional_usdt(self) -> float:
        return float(self.notional_usdt)

    @property
    def target_qty(self) -> float:
        return float(self.qty)


class SizingService:
    """Sizing helper for both legacy risk-based sizing and production capital sizing."""

    def __init__(self, *, client: BinanceUSDMClient) -> None:
        self._client = client
        self._filters_cache: Dict[str, Mapping[str, Any]] = {}
        self._filters_last_refresh_time: Optional[float] = None
        self.refresh_filters()

    @property
    def filters_last_refresh_time(self) -> Optional[float]:
        return self._filters_last_refresh_time

    def refresh_filters(self) -> None:
        now = time.time()
        try:
            info = self._client.get_exchange_info_cached()
            symbols = info.get("symbols", []) if isinstance(info, dict) else []
            cache: Dict[str, Mapping[str, Any]] = {}
            if isinstance(symbols, list):
                for rec in symbols:
                    if not isinstance(rec, dict):
                        continue
                    sym = str(rec.get("symbol", "")).upper()
                    if not sym:
                        continue
                    f = self._client.get_symbol_filters(symbol=sym)
                    if isinstance(f, dict):
                        cache[sym] = f
            if cache:
                self._filters_cache = cache
                self._filters_last_refresh_time = now
                return
        except Exception:
            # Best-effort pre-warm only.
            logger.exception("sizing_refresh_filters_failed")
        if self._filters_last_refresh_time is None:
            self._filters_last_refresh_time = now

    def _get_symbol_filters(self, symbol: str) -> Mapping[str, Any]:
        sym = symbol.strip().upper()
        cached = self._filters_cache.get(sym)
        if cached:
            return cached
        try:
            f = self._client.get_symbol_filters(symbol=sym)
            if isinstance(f, dict) and f:
                self._filters_cache[sym] = f
                self._filters_last_refresh_time = time.time()
                return f
        except Exception as e:  # noqa: BLE001
            logger.warning("sizing_symbol_filters_fetch_failed", extra={"symbol": sym, "err": type(e).__name__}, exc_info=True)
        return {}

    def _fetch_available_usdt(self) -> float:
        # Preferred: USDT-M balance endpoint.
        try:
            bal = self._client.get_account_balance_usdtm()
            if isinstance(bal, dict):
                avail = float(bal.get("available") or 0.0)
                wallet = float(bal.get("wallet") or 0.0)
                if avail > 0.0:
                    return avail
                if wallet > 0.0:
                    return wallet
        except Exception as e:  # noqa: BLE001
            logger.warning("sizing_fetch_balance_failed", extra={"err": type(e).__name__}, exc_info=True)

        # Fallback: account-like payload if the client provides it.
        try:
            get_account = getattr(self._client, "get_account", None)
            if callable(get_account):
                payload = get_account()
                if isinstance(payload, dict):
                    assets = payload.get("assets")
                    if isinstance(assets, list):
                        for row in assets:
                            if not isinstance(row, dict):
                                continue
                            if str(row.get("asset", "")).upper() != "USDT":
                                continue
                            avail = float(
                                row.get("availableBalance")
                                or row.get("available")
                                or row.get("withdrawAvailable")
                                or 0.0
                            )
                            wallet = float(row.get("walletBalance") or row.get("balance") or 0.0)
                            if avail > 0.0:
                                return avail
                            if wallet > 0.0:
                                return wallet
        except Exception as e:  # noqa: BLE001
            logger.warning("sizing_fetch_balance_fallback_failed", extra={"err": type(e).__name__}, exc_info=True)

        return 0.0

    def _fetch_mark_price(self, symbol: str) -> float:
        try:
            mp = self._client.get_mark_price(symbol)
            if isinstance(mp, dict):
                return float(mp.get("markPrice") or 0.0)
        except Exception:
            return 0.0
        return 0.0

    def compute_live(
        self,
        *,
        symbol: str,
        risk: RiskConfig,
        leverage: Optional[float] = None,
    ) -> SizingResult:
        sym = symbol.strip().upper()
        lev = float(leverage if leverage is not None else risk.max_leverage)
        lev = max(1.0, lev)

        available = max(float(self._fetch_available_usdt()), 0.0)
        mark_price = max(float(self._fetch_mark_price(sym)), 0.0)
        filters = self._get_symbol_filters(sym)
        filters_ts = self._filters_last_refresh_time

        if available <= 0.0:
            return SizingResult(
                available_usdt=available,
                budget_usdt=0.0,
                used_margin=0.0,
                leverage=lev,
                notional_usdt=0.0,
                mark_price=mark_price,
                qty=0.0,
                blocked=True,
                block_reason="BALANCE_UNAVAILABLE",
                filters_ts=filters_ts,
            )
        if mark_price <= 0.0:
            return SizingResult(
                available_usdt=available,
                budget_usdt=0.0,
                used_margin=0.0,
                leverage=lev,
                notional_usdt=0.0,
                mark_price=mark_price,
                qty=0.0,
                blocked=True,
                block_reason="MARK_PRICE_UNAVAILABLE",
                filters_ts=filters_ts,
            )

        available_net = max(available * (1.0 - float(risk.fee_buffer_pct)), 0.0)
        mode = str(risk.capital_mode.value if hasattr(risk.capital_mode, "value") else risk.capital_mode).upper()
        if mode == "PCT_AVAILABLE":
            budget = available_net * float(risk.capital_pct)
            used_margin = max(budget * float(risk.margin_use_pct), 0.0)
        elif mode == "MARGIN_BUDGET_USDT":
            budget = min(available_net, float(risk.margin_budget_usdt))
            # In margin-budget mode, requested value is direct margin capital.
            used_margin = max(budget, 0.0)
        else:
            budget = min(available_net, float(risk.capital_usdt))
            used_margin = max(budget * float(risk.margin_use_pct), 0.0)

        if risk.max_exposure_pct is not None:
            budget = min(budget, available_net * float(risk.max_exposure_pct))
            used_margin = min(used_margin, budget)

        budget = max(budget, 0.0)
        used_margin = max(min(used_margin, budget), 0.0)
        notional = used_margin * lev

        if risk.max_position_notional_usdt is not None:
            notional = min(notional, float(risk.max_position_notional_usdt))

        notional = min(notional, available_net * lev)
        notional = max(notional, 0.0)

        step = _dec(filters.get("step_size") or "0")
        min_qty = _dec(filters.get("min_qty") or "0")
        min_notional_raw = filters.get("min_notional")
        min_notional = float(min_notional_raw) if min_notional_raw is not None else 0.0

        if min_notional > 0.0 and notional < min_notional:
            return SizingResult(
                available_usdt=available,
                budget_usdt=budget,
                used_margin=used_margin,
                leverage=lev,
                notional_usdt=notional,
                mark_price=mark_price,
                qty=0.0,
                blocked=True,
                block_reason="BUDGET_TOO_SMALL_FOR_MIN_NOTIONAL",
                filters_ts=filters_ts,
            )

        qty_raw = (notional / mark_price) if mark_price > 0.0 else 0.0
        qd = _dec(qty_raw)
        qd = _floor_to_step(qd, step) if step > 0 else qd
        qty = float(qd)

        if min_qty > 0 and qd < min_qty:
            return SizingResult(
                available_usdt=available,
                budget_usdt=budget,
                used_margin=used_margin,
                leverage=lev,
                notional_usdt=notional,
                mark_price=mark_price,
                qty=qty,
                blocked=True,
                block_reason="BUDGET_TOO_SMALL_FOR_MIN_QTY",
                filters_ts=filters_ts,
            )

        notional_adj = max(qty * mark_price, 0.0)
        if min_notional > 0.0 and notional_adj < min_notional:
            return SizingResult(
                available_usdt=available,
                budget_usdt=budget,
                used_margin=used_margin,
                leverage=lev,
                notional_usdt=notional_adj,
                mark_price=mark_price,
                qty=qty,
                blocked=True,
                block_reason="BUDGET_TOO_SMALL_FOR_MIN_NOTIONAL",
                filters_ts=filters_ts,
            )

        return SizingResult(
            available_usdt=available,
            budget_usdt=budget,
            used_margin=used_margin,
            leverage=lev,
            notional_usdt=notional_adj,
            mark_price=mark_price,
            qty=qty,
            blocked=False,
            block_reason=None,
            filters_ts=filters_ts,
        )

    def compute(
        self,
        *,
        symbol: str,
        risk: RiskConfig,
        equity_usdt: float,
        available_usdt: float,
        price: float,
        stop_distance_pct: float,
        existing_exposure_notional_usdt: float = 0.0,
    ) -> SizingResult:
        sym = symbol.strip().upper()
        eq = float(equity_usdt or 0.0)
        avail = float(available_usdt or 0.0)
        px = float(price or 0.0)
        sd = float(stop_distance_pct or 0.0)

        if eq <= 0.0 or px <= 0.0:
            return SizingResult(
                available_usdt=avail,
                budget_usdt=0.0,
                used_margin=0.0,
                leverage=float(risk.max_leverage),
                notional_usdt=0.0,
                mark_price=px,
                qty=0.0,
                blocked=True,
                block_reason="INSUFFICIENT_INPUT",
                filters_ts=self._filters_last_refresh_time,
                stop_distance_pct=max(sd, 0.0),
            )

        # Guard against unrealistic tiny stop distance (would oversize).
        sd = max(sd, 0.25)  # percent

        risk_budget_usdt = eq * (float(risk.per_trade_risk_pct) / 100.0)
        # notional = risk_budget / stop_distance
        notional = risk_budget_usdt / (sd / 100.0)

        capped_by: Optional[str] = None

        # Exposure cap uses available (operationally safer for futures wallet).
        me = float(risk.max_exposure_pct or 0.0)
        max_exposure = avail * me
        projected = float(existing_exposure_notional_usdt or 0.0) + float(notional)
        if max_exposure > 0 and projected > max_exposure:
            notional = max(0.0, max_exposure - float(existing_exposure_notional_usdt or 0.0))
            capped_by = "max_exposure_pct"

        # Notional cap uses equity.
        max_notional = eq * (float(risk.max_notional_pct) / 100.0)
        if max_notional > 0 and notional > max_notional:
            notional = float(max_notional)
            capped_by = "max_notional_pct"

        if notional <= 0.0:
            return SizingResult(
                available_usdt=avail,
                budget_usdt=risk_budget_usdt,
                used_margin=0.0,
                leverage=float(risk.max_leverage),
                notional_usdt=0.0,
                mark_price=px,
                qty=0.0,
                blocked=True,
                block_reason=None,
                filters_ts=self._filters_last_refresh_time,
                stop_distance_pct=sd,
                capped_by=capped_by,
            )

        qty = notional / px

        f = self._get_symbol_filters(sym)
        step = _dec(f.get("step_size") or "0")
        min_qty = _dec(f.get("min_qty") or "0")
        min_notional = f.get("min_notional")

        qd = _dec(qty)
        qd = _floor_to_step(qd, step) if step > 0 else qd
        if min_qty > 0 and qd < min_qty:
            return SizingResult(
                available_usdt=avail,
                budget_usdt=risk_budget_usdt,
                used_margin=0.0,
                leverage=float(risk.max_leverage),
                notional_usdt=0.0,
                mark_price=px,
                qty=0.0,
                blocked=True,
                block_reason="BUDGET_TOO_SMALL_FOR_MIN_QTY",
                filters_ts=self._filters_last_refresh_time,
                stop_distance_pct=sd,
                capped_by="min_qty",
            )

        notional2 = float(qd) * px
        if min_notional is not None:
            try:
                if float(min_notional) > 0 and notional2 < float(min_notional):
                    return SizingResult(
                        available_usdt=avail,
                        budget_usdt=risk_budget_usdt,
                        used_margin=0.0,
                        leverage=float(risk.max_leverage),
                        notional_usdt=0.0,
                        mark_price=px,
                        qty=0.0,
                        blocked=True,
                        block_reason="BUDGET_TOO_SMALL_FOR_MIN_NOTIONAL",
                        filters_ts=self._filters_last_refresh_time,
                        stop_distance_pct=sd,
                        capped_by="min_notional",
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("sizing_min_notional_check_failed", extra={"symbol": symbol, "err": type(e).__name__}, exc_info=True)

        return SizingResult(
            available_usdt=avail,
            budget_usdt=risk_budget_usdt,
            used_margin=notional2 / max(float(risk.max_leverage), 1.0),
            leverage=float(risk.max_leverage),
            notional_usdt=float(notional2),
            mark_price=px,
            qty=float(qd),
            blocked=False,
            block_reason=None,
            filters_ts=self._filters_last_refresh_time,
            stop_distance_pct=sd,
            capped_by=capped_by,
        )
