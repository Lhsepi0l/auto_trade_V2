from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from apps.trader_engine.exchange.binance_usdm import (
    BinanceAuthError,
    BinanceHTTPError,
    BinanceUSDMClient,
)

logger = logging.getLogger(__name__)

FIXED_TARGET_SYMBOLS = {"BTCUSDT", "ETHUSDT", "XAUUSDT"}


@dataclass
class BinanceStartupState:
    enabled_symbols: List[str]
    disabled_symbols: List[Dict[str, str]]
    time_offset_ms: int
    time_measured_at_ms: int
    server_time_ms: int
    ok: bool
    error: Optional[str] = None


class BinanceService:
    def __init__(
        self,
        *,
        client: BinanceUSDMClient,
        allowed_symbols: Sequence[str],
        spread_wide_pct: float = 0.005,
    ) -> None:
        self._client = client
        requested = [s.strip().upper() for s in allowed_symbols if s and s.strip()]
        if not requested:
            requested = sorted(FIXED_TARGET_SYMBOLS)
        self._ignored_symbols = [s for s in requested if s not in FIXED_TARGET_SYMBOLS]
        self._allowed_symbols = [s for s in requested if s in FIXED_TARGET_SYMBOLS]
        # Stored as ratio (e.g. 0.0015 == 0.15%). If given a percent-like value (> 0.1),
        # assume it's percent and convert.
        x = float(spread_wide_pct)
        self._spread_wide_ratio = (x / 100.0) if x > 0.1 else x

        self._startup: Optional[BinanceStartupState] = None

    def close(self) -> None:
        self._client.close()

    @property
    def enabled_symbols(self) -> List[str]:
        if not self._startup:
            return []
        return list(self._startup.enabled_symbols)

    @property
    def disabled_symbols(self) -> List[Dict[str, str]]:
        if not self._startup:
            return []
        return list(self._startup.disabled_symbols)

    def startup(self) -> BinanceStartupState:
        """Validate symbols + measure time offset. Never logs secrets."""
        try:
            server_time_ms = self._client.get_server_time_ms()
            offset = self._client.time_sync.measure(server_time_ms=server_time_ms)
            enabled, disabled = self._client.validate_symbols(self._allowed_symbols)
            if self._ignored_symbols:
                disabled = [{"symbol": s, "reason": "not_in_fixed_target_list"} for s in self._ignored_symbols] + list(
                    disabled
                )
            st = BinanceStartupState(
                enabled_symbols=enabled,
                disabled_symbols=disabled,
                time_offset_ms=offset.offset_ms,
                time_measured_at_ms=offset.measured_at_ms,
                server_time_ms=server_time_ms,
                ok=True,
                error=None,
            )
            self._startup = st
            return st
        except Exception as e:  # noqa: BLE001
            # Do not prevent API boot; surface error in /status.
            msg = f"{type(e).__name__}: {e}"
            st = BinanceStartupState(
                enabled_symbols=[],
                disabled_symbols=[{"symbol": s, "reason": "startup_failed"} for s in self._allowed_symbols],
                time_offset_ms=0,
                time_measured_at_ms=0,
                server_time_ms=0,
                ok=False,
                error=msg,
            )
            self._startup = st
            logger.warning("binance_startup_failed", extra={"err": type(e).__name__})
            return st

    def set_allowed_symbols(self, allowed_symbols: Sequence[str]) -> list[str]:
        requested = [s.strip().upper() for s in allowed_symbols if s and s.strip()]
        if not requested:
            requested = sorted(FIXED_TARGET_SYMBOLS)
        self._ignored_symbols = [s for s in requested if s not in FIXED_TARGET_SYMBOLS]
        self._allowed_symbols = [s for s in requested if s in FIXED_TARGET_SYMBOLS]
        # Re-run startup flow to refresh enabled/disabled symbols against exchange status.
        st = self.startup()
        return list(st.enabled_symbols)

    def get_status(self) -> Mapping[str, Any]:
        if not self._startup:
            self.startup()
        assert self._startup is not None

        enabled = self._startup.enabled_symbols
        disabled = self._startup.disabled_symbols

        # Public (no key required): book ticker for spread.
        spreads: Dict[str, Any] = {}
        for sym in enabled:
            try:
                bt = self._client.get_book_ticker(sym)
                bid = float(bt.get("bidPrice", 0) or 0)
                ask = float(bt.get("askPrice", 0) or 0)
                spread = max(ask - bid, 0.0)
                mid = (ask + bid) / 2.0 if (ask and bid) else 0.0
                spread_ratio = (spread / mid) if mid else 0.0
                spread_pct = spread_ratio * 100.0
                spreads[sym] = {
                    "bid": bid,
                    "ask": ask,
                    "spread": spread,
                    "spread_pct": spread_pct,
                    "is_wide": bool(spread_ratio >= self._spread_wide_ratio),
                }
            except Exception as e:  # noqa: BLE001
                spreads[sym] = {"error": f"{type(e).__name__}: {e}"}

        # Private (API key/secret required): balance/positions/open orders.
        private_ok = True
        private_error: Optional[str] = None
        usdt_balance: Optional[Dict[str, Any]] = None
        positions: Optional[Dict[str, Any]] = None
        open_orders: Optional[Dict[str, Any]] = None

        try:
            usdt_balance = self._client.get_account_balance_usdtm()
            positions = self._client.get_positions_usdtm(enabled)
            open_orders = self._client.get_open_orders_usdtm(enabled)
        except BinanceAuthError as e:
            private_ok = False
            private_error = str(e)
        except BinanceHTTPError as e:
            private_ok = False
            private_error = f"http_{e.status_code} code={e.code} msg={e.msg}"
        except Exception as e:  # noqa: BLE001
            private_ok = False
            private_error = f"{type(e).__name__}: {e}"

        return {
            "startup_ok": self._startup.ok,
            "startup_error": self._startup.error,
            "enabled_symbols": enabled,
            "disabled_symbols": disabled,
            "server_time_ms": self._startup.server_time_ms,
            "time_offset_ms": self._startup.time_offset_ms,
            "time_measured_at_ms": self._startup.time_measured_at_ms,
            "private_ok": private_ok,
            "private_error": private_error,
            "usdt_balance": usdt_balance,
            "positions": positions,
            "open_orders": open_orders,
            "spreads": spreads,
        }

    def get_symbol_leverage(self, *, symbols: Sequence[str]) -> Dict[str, float]:
        # Return leverage per symbol from Binance. Symbols not returned by payload are omitted.
        return self._client.get_symbol_leverage_usdtm(symbols=symbols)

    def set_leverage(self, *, symbol: str, leverage: float) -> Mapping[str, Any]:
        """Set leverage per symbol.

        Returns raw exchange response; errors are propagated to caller for retry/logging.
        """
        leverage_int = int(max(1, round(float(leverage))))
        return self._client.set_leverage(symbol=symbol, leverage=leverage_int)
