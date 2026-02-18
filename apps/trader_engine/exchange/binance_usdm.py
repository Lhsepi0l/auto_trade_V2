from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests

from apps.trader_engine.exchange.time_sync import TimeSync
from shared.utils.retry import retry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BinanceCredentials:
    api_key: str
    api_secret: str


class BinanceError(Exception):
    pass


class BinanceAuthError(BinanceError):
    pass


class BinanceHTTPError(BinanceError):
    def __init__(
        self,
        *,
        status_code: int,
        path: str,
        code: Optional[int] = None,
        msg: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        self.path = path
        self.code = code
        self.msg = msg
        super().__init__(f"binance_http_error status={status_code} code={code} path={path} msg={msg}")


class BinanceRetryableError(BinanceError):
    pass


def _as_float(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def _as_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


def _dec(x: Any) -> Decimal:
    # Decimal wrapper for safe rounding operations.
    return Decimal(str(x))


def _floor_to_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return qty
    return (qty // step) * step


class BinanceUSDMClient:
    """Binance USD-M Futures REST client (조회 전용).

    금지: 주문/청산/레버리지 변경/마진 타입 변경 등 트레이딩 관련 기능 구현.
    """

    def __init__(
        self,
        creds: BinanceCredentials,
        *,
        base_url: str = "https://fapi.binance.com",
        time_sync: Optional[TimeSync] = None,
        timeout_sec: float = 8.0,
        retry_count: int = 3,
        retry_backoff: float = 0.25,
        recv_window_ms: int = 5000,
    ) -> None:
        self._creds = creds
        self._base_url = base_url.rstrip("/")
        self._time_sync = time_sync or TimeSync()
        self._timeout_sec = timeout_sec
        self._retry_count = retry_count
        self._retry_backoff = retry_backoff
        self._recv_window_ms = recv_window_ms

        self._session = requests.Session()
        self._exchange_info_cache: Optional[Mapping[str, Any]] = None
        self._exchange_info_cached_at_ms: int = 0
        self._exchange_info_ttl_ms: int = 60_000

    @property
    def time_sync(self) -> TimeSync:
        return self._time_sync

    def close(self) -> None:
        try:
            self._session.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("binance_session_close_failed", extra={"err": type(e).__name__}, exc_info=True)

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _auth_headers(self) -> Dict[str, str]:
        if not self._creds.api_key:
            return {}
        return {"X-MBX-APIKEY": self._creds.api_key}

    def _sign_params(self, params: Mapping[str, Any]) -> str:
        if not self._creds.api_secret:
            raise BinanceAuthError("BINANCE_API_SECRET is missing")
        qs = urllib.parse.urlencode(params, doseq=True)
        sig = hmac.new(self._creds.api_secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def _parse_error_payload(self, payload: Any) -> Tuple[Optional[int], Optional[str]]:
        if isinstance(payload, dict):
            code = payload.get("code")
            msg = payload.get("msg")
            try:
                return (int(code) if code is not None else None, str(msg) if msg is not None else None)
            except Exception:
                return (None, None)
        return (None, None)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        signed: bool = False,
    ) -> Any:
        url = f"{self._base_url}{path}"
        params = dict(params or {})

        headers: Dict[str, str] = {}
        if signed:
            if not self._creds.api_key:
                raise BinanceAuthError("BINANCE_API_KEY is missing")
            headers.update(self._auth_headers())
            params.setdefault("recvWindow", self._recv_window_ms)
            params["timestamp"] = self._time_sync.apply(self._now_ms())

        def _do_once() -> Any:
            req_url = url
            req_params: Optional[Mapping[str, Any]] = params
            if signed:
                # Put signed params in URL; do not log this string (contains signature).
                req_url = f"{url}?{self._sign_params(params)}"
                req_params = None

            try:
                resp = self._session.request(
                    method=method,
                    url=req_url,
                    params=req_params,
                    headers=headers,
                    timeout=self._timeout_sec,
                )
            except requests.RequestException as e:
                raise BinanceRetryableError(f"network_error path={path} err={type(e).__name__}") from e

            # Binance uses JSON for both success and errors.
            try:
                payload = resp.json()
            except Exception:
                payload = None

            if resp.status_code >= 400:
                code, msg = self._parse_error_payload(payload)
                # Retry only for rate limits / bans / transient server errors.
                if resp.status_code in (418, 429) or resp.status_code >= 500:
                    raise BinanceRetryableError(
                        f"retryable_http_error status={resp.status_code} code={code} path={path}"
                    )
                raise BinanceHTTPError(status_code=resp.status_code, path=path, code=code, msg=msg)

            return payload

        def _do_with_time_resync() -> Any:
            try:
                return _do_once()
            except BinanceHTTPError as e:
                # -1021: timestamp out of sync; refresh offset once then retry.
                if e.code == -1021 and signed:
                    try:
                        self.refresh_time_offset()
                    except Exception as refresh_err:  # noqa: BLE001
                        logger.warning(
                            "binance_time_offset_refresh_failed",
                            extra={"err": type(refresh_err).__name__},
                            exc_info=True,
                        )
                    return _do_once()
                raise

        return retry(_do_with_time_resync, attempts=self._retry_count, base_delay_sec=self._retry_backoff)

    def _request_api_key_only_json(self, method: str, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        """Endpoints that require API key header but no signature/timestamp."""
        if not self._creds.api_key:
            raise BinanceAuthError("BINANCE_API_KEY is missing")
        url = f"{self._base_url}{path}"
        headers = self._auth_headers()
        params = dict(params or {})
        try:
            resp = self._session.request(
                method=method,
                url=url,
                params=params if params else None,
                headers=headers,
                timeout=self._timeout_sec,
            )
        except requests.RequestException as e:
            raise BinanceRetryableError(f"network_error path={path} err={type(e).__name__}") from e

        try:
            payload = resp.json()
        except Exception:
            payload = None

        if resp.status_code >= 400:
            code, msg = self._parse_error_payload(payload)
            raise BinanceHTTPError(status_code=resp.status_code, path=path, code=code, msg=msg)
        return payload

    # Public endpoints
    def get_server_time(self) -> Mapping[str, Any]:
        payload = self._request_json("GET", "/fapi/v1/time")
        assert isinstance(payload, dict)
        return payload

    def get_server_time_ms(self) -> int:
        payload = self.get_server_time()
        return int(payload["serverTime"])

    def refresh_time_offset(self) -> int:
        server_ms = self.get_server_time_ms()
        self._time_sync.measure(server_time_ms=server_ms)
        return self._time_sync.offset_ms

    def get_exchange_info(self) -> Mapping[str, Any]:
        payload = self._request_json("GET", "/fapi/v1/exchangeInfo")
        assert isinstance(payload, dict)
        return payload

    def get_exchange_info_cached(self) -> Mapping[str, Any]:
        now = self._now_ms()
        if self._exchange_info_cache and (now - self._exchange_info_cached_at_ms) <= self._exchange_info_ttl_ms:
            return self._exchange_info_cache
        info = self.get_exchange_info()
        self._exchange_info_cache = info
        self._exchange_info_cached_at_ms = now
        return info

    def validate_symbols(self, allowed_list: Sequence[str]) -> Tuple[List[str], List[Dict[str, str]]]:
        info = self.get_exchange_info()
        symbols = info.get("symbols", [])
        symbol_map: Dict[str, Mapping[str, Any]] = {}
        if isinstance(symbols, list):
            for s in symbols:
                if isinstance(s, dict) and "symbol" in s:
                    symbol_map[str(s["symbol"]).upper()] = s

        enabled: List[str] = []
        disabled: List[Dict[str, str]] = []
        for raw in allowed_list:
            sym = str(raw).strip().upper()
            if not sym:
                continue
            rec = symbol_map.get(sym)
            if not rec:
                disabled.append({"symbol": sym, "reason": "not_found_in_exchangeInfo"})
                continue
            status = str(rec.get("status", "")).upper()
            if status != "TRADING":
                disabled.append({"symbol": sym, "reason": f"status_{status or 'UNKNOWN'}"})
                continue
            enabled.append(sym)
        return enabled, disabled

    def get_book_ticker(self, symbol: str) -> Mapping[str, Any]:
        payload = self._request_json("GET", "/fapi/v1/ticker/bookTicker", params={"symbol": symbol})
        assert isinstance(payload, dict)
        return payload

    def get_mark_price(self, symbol: str) -> Mapping[str, Any]:
        payload = self._request_json("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol})
        assert isinstance(payload, dict)
        return payload

    # --- User stream (listenKey) ---
    def start_user_stream(self) -> str:
        payload = self._request_api_key_only_json("POST", "/fapi/v1/listenKey")
        if not isinstance(payload, dict) or not payload.get("listenKey"):
            raise BinanceHTTPError(status_code=500, path="/fapi/v1/listenKey", msg="listenKey_missing")
        return str(payload["listenKey"])

    def keepalive_user_stream(self, *, listen_key: str) -> None:
        _ = self._request_api_key_only_json("PUT", "/fapi/v1/listenKey", params={"listenKey": listen_key})

    def close_user_stream(self, *, listen_key: str) -> None:
        _ = self._request_api_key_only_json("DELETE", "/fapi/v1/listenKey", params={"listenKey": listen_key})

    def get_klines(self, *, symbol: str, interval: str, limit: int = 200) -> List[List[Any]]:
        """Fetch futures klines (candlesticks) for a symbol.

        Endpoint: GET /fapi/v1/klines (public).
        Returns raw list rows (Binance schema).
        """
        lim = int(limit)
        if lim <= 0:
            lim = 200
        # Binance max is typically 1500 for klines.
        lim = min(lim, 1500)
        payload = self._request_json(
            "GET",
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": lim},
            signed=False,
        )
        if isinstance(payload, list):
            # Each row is a list; keep as-is for downstream parsing.
            return [r for r in payload if isinstance(r, list)]
        return []

    # --- Execution endpoints (USDT-M Futures only) ---
    def place_order_market(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
        new_client_order_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if new_client_order_id:
            params["newClientOrderId"] = str(new_client_order_id)
        payload = self._request_json("POST", "/fapi/v1/order", params=params, signed=True)
        assert isinstance(payload, dict)
        return payload

    def place_order_limit(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        post_only: bool = False,
        reduce_only: bool = False,
        new_client_order_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        # Futures post-only uses timeInForce=GTX.
        tif = "GTX" if post_only else "GTC"
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": tif,
            "quantity": quantity,
            "price": price,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if new_client_order_id:
            params["newClientOrderId"] = str(new_client_order_id)
        payload = self._request_json("POST", "/fapi/v1/order", params=params, signed=True)
        assert isinstance(payload, dict)
        return payload

    def cancel_all_open_orders(self, *, symbol: str) -> List[Mapping[str, Any]]:
        payload = self._request_json("DELETE", "/fapi/v1/allOpenOrders", params={"symbol": symbol}, signed=True)
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)]
        # Binance sometimes returns {"code":..., "msg":...} on error; success is list.
        return []

    def get_order(self, *, symbol: str, order_id: int) -> Mapping[str, Any]:
        payload = self._request_json("GET", "/fapi/v1/order", params={"symbol": symbol, "orderId": order_id}, signed=True)
        assert isinstance(payload, dict)
        return payload

    def get_order_by_client_order_id(self, *, symbol: str, client_order_id: str) -> Mapping[str, Any]:
        payload = self._request_json(
            "GET",
            "/fapi/v1/order",
            params={"symbol": symbol, "origClientOrderId": client_order_id},
            signed=True,
        )
        assert isinstance(payload, dict)
        return payload

    def set_leverage(self, *, symbol: str, leverage: int) -> Mapping[str, Any]:
        params = {"symbol": symbol, "leverage": leverage}
        payload = self._request_json("POST", "/fapi/v1/leverage", params=params, signed=True)
        assert isinstance(payload, dict)
        return payload

    def get_position_mode_one_way(self) -> bool:
        """Return True if account is in One-way mode (hedge mode off)."""
        payload = self._request_json("GET", "/fapi/v1/positionSide/dual", signed=True)
        if isinstance(payload, dict):
            # dualSidePosition=True => hedge mode ON.
            dual = payload.get("dualSidePosition")
            return bool(dual is False)
        return False

    def get_symbol_filters(self, *, symbol: str) -> Mapping[str, Any]:
        """Parse exchangeInfo filters needed for sizing/rounding.

        Returns dict with:
          - step_size, min_qty (from LOT_SIZE / MARKET_LOT_SIZE)
          - tick_size (from PRICE_FILTER)
          - min_notional (from MIN_NOTIONAL), if present
        """
        info = self.get_exchange_info_cached()
        symbols = info.get("symbols", [])
        rec: Optional[Mapping[str, Any]] = None
        if isinstance(symbols, list):
            for s in symbols:
                if isinstance(s, dict) and str(s.get("symbol", "")).upper() == symbol.upper():
                    rec = s
                    break
        if not rec:
            return {}

        filters = rec.get("filters", [])
        lot: Optional[Mapping[str, Any]] = None
        mlot: Optional[Mapping[str, Any]] = None
        price_f: Optional[Mapping[str, Any]] = None
        min_notional_f: Optional[Mapping[str, Any]] = None

        if isinstance(filters, list):
            for f in filters:
                if not isinstance(f, dict):
                    continue
                ft = str(f.get("filterType", "")).upper()
                if ft == "LOT_SIZE":
                    lot = f
                elif ft == "MARKET_LOT_SIZE":
                    mlot = f
                elif ft == "PRICE_FILTER":
                    price_f = f
                elif ft == "MIN_NOTIONAL":
                    min_notional_f = f

        def _pick_step(src: Optional[Mapping[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
            if not src:
                return (None, None)
            return (_as_float(src.get("stepSize")), _as_float(src.get("minQty")))

        step_size, min_qty = _pick_step(mlot or lot)
        tick_size = _as_float(price_f.get("tickSize")) if price_f else None

        min_notional: Optional[float] = None
        if min_notional_f:
            # Futures sometimes uses "notional" field.
            if "notional" in min_notional_f:
                min_notional = _as_float(min_notional_f.get("notional"))
            else:
                min_notional = _as_float(min_notional_f.get("minNotional"))

        return {
            "symbol": symbol.upper(),
            "step_size": step_size,
            "min_qty": min_qty,
            "tick_size": tick_size,
            "min_notional": min_notional,
        }

    # Signed (read-only) endpoints
    def get_account_balance_usdtm(self) -> Dict[str, float]:
        payload = self._request_json("GET", "/fapi/v2/balance", signed=True)
        if not isinstance(payload, list):
            return {"wallet": 0.0, "available": 0.0}

        for row in payload:
            if not isinstance(row, dict):
                continue
            if str(row.get("asset", "")).upper() != "USDT":
                continue
            # Fields vary slightly by endpoint/version; pick best-effort.
            wallet = _as_float(row.get("balance"))
            available = _as_float(row.get("availableBalance", row.get("withdrawAvailable")))
            return {"wallet": wallet, "available": available}

        return {"wallet": 0.0, "available": 0.0}

    def get_positions_usdtm(self, symbols: Sequence[str]) -> Dict[str, Dict[str, float]]:
        payload = self._request_json("GET", "/fapi/v2/positionRisk", signed=True)
        if not isinstance(payload, list):
            return {}

        wanted = {s.upper() for s in symbols}
        out: Dict[str, Dict[str, float]] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol", "")).upper()
            if sym not in wanted:
                continue
            out[sym] = {
                "position_amt": _as_float(row.get("positionAmt")),
                "entry_price": _as_float(row.get("entryPrice")),
                "unrealized_pnl": _as_float(row.get("unRealizedProfit", row.get("unrealizedProfit"))),
                "leverage": _as_float(row.get("leverage")),
            }
        return out


    def get_symbol_leverage_usdtm(self, symbols: Sequence[str]) -> Dict[str, float]:
        """Return leverage settings for symbols from Binance positionRisk endpoint."""
        payload = self._request_json("GET", "/fapi/v2/positionRisk", signed=True)
        if not isinstance(payload, list):
            return {}

        wanted = [str(s).upper().strip() for s in symbols if str(s).strip()]
        if not wanted:
            return {}

        wanted_set = set(wanted)
        out: Dict[str, float] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol", "")).upper()
            if sym not in wanted_set:
                continue
            out[sym] = _as_float(row.get("leverage"))
        return out


    def get_open_orders_usdtm(self, symbols: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for sym in symbols:
            payload = self._request_json("GET", "/fapi/v1/openOrders", params={"symbol": sym}, signed=True)
            if not isinstance(payload, list):
                out[sym] = []
                continue
            # Keep a safe, compact subset of fields.
            orders: List[Dict[str, Any]] = []
            for row in payload:
                if not isinstance(row, dict):
                    continue
                orders.append(
                    {
                        "symbol": str(row.get("symbol", "")),
                        "order_id": row.get("orderId"),
                        "client_order_id": row.get("clientOrderId"),
                        "side": row.get("side"),
                        "type": row.get("type"),
                        "status": row.get("status"),
                        "price": row.get("price"),
                        "orig_qty": row.get("origQty"),
                        "executed_qty": row.get("executedQty"),
                        "time": row.get("time"),
                    }
                )
            out[sym] = orders
        return out

    def get_open_positions_any(self) -> Dict[str, Dict[str, float]]:
        """Return all non-zero positions across the account (USDT-M)."""
        payload = self._request_json("GET", "/fapi/v2/positionRisk", signed=True)
        if not isinstance(payload, list):
            return {}
        out: Dict[str, Dict[str, float]] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            amt = _as_float(row.get("positionAmt"))
            if abs(amt) <= 0:
                continue
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            out[sym] = {
                "position_amt": amt,
                "entry_price": _as_float(row.get("entryPrice")),
                "unrealized_pnl": _as_float(row.get("unRealizedProfit", row.get("unrealizedProfit"))),
                "leverage": _as_float(row.get("leverage")),
            }
        return out
