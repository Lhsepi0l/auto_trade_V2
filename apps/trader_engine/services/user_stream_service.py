from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

import websockets

from apps.trader_engine.exchange.binance_usdm import BinanceHTTPError
from apps.trader_engine.exchange.binance_usdm import BinanceUSDMClient
from apps.trader_engine.services.engine_service import EngineService
from apps.trader_engine.services.execution_service import ExecutionService
from apps.trader_engine.services.notifier_service import Notifier
from apps.trader_engine.services.pnl_service import PnLService
from apps.trader_engine.services.reconcile_service import ReconcileService
from apps.trader_engine.services.snapshot_service import SnapshotService
from apps.trader_engine.storage.repositories import OrderRecordRepo

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _f(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def _to_order_record_status(raw: str) -> str:
    s = str(raw or "").upper()
    mapping = {
        "NEW": "ACK",
        "PARTIALLY_FILLED": "PARTIAL",
        "FILLED": "FILLED",
        "CANCELED": "CANCELED",
        "EXPIRED": "EXPIRED",
        "REJECTED": "ERROR",
    }
    return mapping.get(s, "SENT")


class UserStreamService:
    """Binance Futures user stream WS service.

    - Maintains listenKey (keepalive every 30~50 minutes; default 45 minutes).
    - Reconnects WS with exponential backoff.
    - Updates ws status + pnl based on fill/account events.
    """

    def __init__(
        self,
        *,
        client: BinanceUSDMClient,
        engine: EngineService,
        pnl: PnLService,
        execution: ExecutionService,
        notifier: Optional[Notifier] = None,
        snapshot: Optional[SnapshotService] = None,
        order_records: Optional[OrderRecordRepo] = None,
        reconcile: Optional[ReconcileService] = None,
        ws_base_url: str = "wss://fstream.binance.com/ws",
        keepalive_interval_sec: int = 45 * 60,
        reconnect_backoff_min_sec: float = 1.0,
        reconnect_backoff_max_sec: float = 60.0,
        safe_mode_after_sec: int = 90,
        stale_msg_after_sec: int | None = None,
        stale_event_after_sec: int | None = None,
        exposure_probe_interval_sec: float = 5.0,
        tracked_symbols: Optional[Sequence[str]] = None,
    ) -> None:
        self._client = client
        self._engine = engine
        self._pnl = pnl
        self._execution = execution
        self._notifier = notifier
        self._snapshot = snapshot
        self._order_records = order_records
        self._reconcile = reconcile
        self._ws_base_url = ws_base_url.rstrip("/")
        self._keepalive_interval_sec = int(keepalive_interval_sec)
        self._reconnect_backoff_min_sec = float(reconnect_backoff_min_sec)
        self._reconnect_backoff_max_sec = float(reconnect_backoff_max_sec)
        self._safe_mode_after_sec = max(int(safe_mode_after_sec), 1)
        self._stale_msg_after_sec = max(
            int(stale_msg_after_sec if stale_msg_after_sec is not None else (self._safe_mode_after_sec * 3)),
            self._safe_mode_after_sec,
        )
        # Event-level staleness can be noisy with passive positions because Binance user stream
        # may not emit account/order events for long periods even when transport is healthy.
        # Keep transport staleness check enabled by default and only enable event staleness when
        # explicitly configured.
        self._stale_event_after_sec = (
            max(int(stale_event_after_sec), 1) if stale_event_after_sec is not None else 0
        )
        self._exposure_probe_interval_sec = max(float(exposure_probe_interval_sec), 0.5)
        self._tracked_symbols = [str(s).strip().upper() for s in (tracked_symbols or []) if str(s).strip()]

        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._listen_key: Optional[str] = None
        self._keepalive_task: Optional[asyncio.Task[None]] = None
        self._health_task: Optional[asyncio.Task[None]] = None
        self._active_ws: Optional[Any] = None
        self._listen_key_last_keepalive_ts: Optional[datetime] = None
        self._last_ws_msg_ts: Optional[datetime] = None
        self._last_ws_event_ts: Optional[datetime] = None
        self._last_ws_connect_ts: Optional[datetime] = None
        self._disconnected_since: Optional[float] = None
        self._last_exposure_probe_ts: float = 0.0
        self._last_has_active_exposure: bool = False
        self._safe_mode = False
        self._safe_mode_alerted = False
        self._reconnect_requested = False

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        if not self._health_task or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_guard_loop(), name="binance_user_stream_health")
        self._task = asyncio.create_task(self.run_forever(), name="binance_user_stream")

    async def stop(self) -> None:
        self._stop.set()
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning("user_stream_health_task_stop_failed", extra={"err": type(e).__name__}, exc_info=True)
            self._health_task = None
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning("user_stream_keepalive_task_stop_failed", extra={"err": type(e).__name__}, exc_info=True)
            self._keepalive_task = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning("user_stream_main_task_stop_failed", extra={"err": type(e).__name__}, exc_info=True)
            self._task = None
        self._active_ws = None
        if self._listen_key:
            try:
                await asyncio.to_thread(self._client.close_user_stream, listen_key=self._listen_key)
            except Exception as e:  # noqa: BLE001
                logger.warning("user_stream_close_listen_key_failed", extra={"err": type(e).__name__}, exc_info=True)
            self._listen_key = None
        self._set_safe_mode(False)
        self._engine.set_ws_status(connected=False)

    async def run_forever(self) -> None:
        backoff = max(self._reconnect_backoff_min_sec, 0.1)
        while not self._stop.is_set():
            try:
                lk = await asyncio.to_thread(self._client.start_user_stream)
                self._listen_key = lk
                self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="binance_user_stream_keepalive")
                ws_url = f"{self._ws_base_url}/{lk}"
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                    self._active_ws = ws
                    logger.info("user_stream_connected")
                    now = _utcnow()
                    self._last_ws_connect_ts = now
                    self._last_ws_msg_ts = now
                    self._engine.set_ws_status(connected=True, last_event_time=now)
                    self._set_safe_mode(False)
                    self._disconnected_since = None
                    self._reconnect_requested = False
                    if self._reconcile:
                        try:
                            await asyncio.to_thread(self._reconcile.startup_reconcile)
                        except Exception:
                            logger.exception("ws_reconcile_failed")
                    backoff = max(self._reconnect_backoff_min_sec, 0.1)
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        self._last_ws_msg_ts = _utcnow()
                        await self._on_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("user_stream_reconnect", extra={"err": type(e).__name__, "backoff_sec": backoff})
            finally:
                self._engine.set_ws_status(connected=False)
                self._active_ws = None
                if self._disconnected_since is None:
                    self._disconnected_since = time.time()
                self._reconnect_requested = False
                if self._keepalive_task:
                    self._keepalive_task.cancel()
                    try:
                        await self._keepalive_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:  # noqa: BLE001
                        logger.warning("user_stream_keepalive_task_join_failed", extra={"err": type(e).__name__}, exc_info=True)
                    self._keepalive_task = None

            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, max(self._reconnect_backoff_max_sec, self._reconnect_backoff_min_sec))

    async def _keepalive_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(max(self._keepalive_interval_sec, 1))
            if self._stop.is_set():
                return
            lk = self._listen_key
            if not lk:
                return
            try:
                await asyncio.to_thread(self._client.keepalive_user_stream, listen_key=lk)
                self._listen_key_last_keepalive_ts = _utcnow()
                logger.info("user_stream_keepalive_ok")
            except BinanceHTTPError as e:
                if int(getattr(e, "code", 0) or 0) == -1125:
                    logger.warning("user_stream_keepalive_invalid_listen_key", extra={"code": e.code})
                    self._listen_key = None
                    await self._request_ws_reconnect()
                    return
                logger.exception("user_stream_keepalive_failed")
                return
            except Exception as e:
                if "invalid_listen_key" in str(e).lower():
                    self._listen_key = None
                    await self._request_ws_reconnect()
                    return
                logger.exception("user_stream_keepalive_failed")
                return

    async def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        now = _utcnow()
        self._engine.set_ws_status(connected=True, last_event_time=now)
        self._set_safe_mode(False)
        self._disconnected_since = None

        et = str(msg.get("e") or "")
        if et == "listenKeyExpired":
            logger.warning("listen_key_expired")
            self._listen_key = None
            raise RuntimeError("listen_key_expired")
        if et == "ORDER_TRADE_UPDATE":
            self._last_ws_event_ts = now
            await self._handle_order_trade_update(msg)
            return
        if et == "ACCOUNT_UPDATE":
            self._last_ws_event_ts = now
            await self._handle_account_update(msg)
            return

    async def _handle_order_trade_update(self, msg: Mapping[str, Any]) -> None:
        o = msg.get("o")
        if not isinstance(o, Mapping):
            return

        symbol = str(o.get("s") or "")
        client_order_id = str(o.get("c") or o.get("clientOrderId") or "")
        status_raw = str(o.get("X") or "")
        order_id_raw = o.get("i") or o.get("orderId")
        if self._order_records and client_order_id:
            try:
                self._order_records.mark_status(
                    client_order_id=client_order_id,
                    status=_to_order_record_status(status_raw),
                    exchange_order_id=(str(order_id_raw) if order_id_raw is not None else None),
                    last_error=None,
                )
            except Exception:
                logger.exception("order_record_ws_update_failed")

        execution_type = str(o.get("x") or "")
        if execution_type != "TRADE":
            return

        side = str(o.get("S") or "")
        last_qty = _f(o.get("l"))
        last_price = _f(o.get("L"))
        realized = _f(o.get("rp"))
        reduce_only = bool(o.get("R"))
        order_status = str(o.get("X") or "")

        fill_ts_ms = o.get("T") or msg.get("E")
        fill_at = _utcnow()
        try:
            if fill_ts_ms is not None:
                fill_at = datetime.fromtimestamp(int(fill_ts_ms) / 1000.0, tz=timezone.utc)
        except Exception as e:  # noqa: BLE001
            logger.warning("user_stream_fill_timestamp_parse_failed", extra={"err": type(e).__name__}, exc_info=True)

        self._pnl.set_last_fill(
            symbol=symbol or None,
            side=side or None,
            qty=last_qty if last_qty > 0 else None,
            price=last_price if last_price > 0 else None,
            realized_pnl=realized,
            at=fill_at,
        )
        if self._snapshot:
            try:
                self._snapshot.capture_snapshot(reason="FILL", preferred_symbol=symbol or None)
            except Exception:
                logger.exception("snapshot_capture_failed_on_fill", extra={"symbol": symbol})

        if self._notifier:
            try:
                await self._notifier.send_event(
                    {
                        "kind": "FILL",
                        "symbol": symbol,
                        "detail": {
                            "side": side,
                            "qty": last_qty,
                            "price_ref": last_price,
                            "realized_pnl": realized,
                            "reduce_only": reduce_only,
                            "order_status": order_status,
                        },
                    }
                )
            except Exception:
                logger.exception("fill_notify_failed")

        # Realized PnL / lose_streak update: judge on full close confirmation.
        if reduce_only:
            try:
                open_pos = await asyncio.to_thread(self._client.get_open_positions_any)
                closed = symbol.upper() not in {s.upper() for s in open_pos.keys()}
                if closed:
                    bal = await asyncio.to_thread(self._client.get_account_balance_usdtm)
                    wallet = float(bal.get("wallet") or 0.0)
                    upnl = sum(float(r.get("unrealized_pnl") or 0.0) for r in open_pos.values())
                    equity = wallet + upnl
                    self._pnl.apply_realized_pnl_delta(realized_delta_usdt=realized, equity_usdt=equity)
            except Exception:
                logger.exception("pnl_update_failed_on_ws_fill")

    async def _handle_account_update(self, msg: Mapping[str, Any]) -> None:
        a = msg.get("a")
        if not isinstance(a, Mapping):
            return
        p = a.get("P")
        b = a.get("B")
        if self._notifier and ((isinstance(p, list) and p) or (isinstance(b, list) and b)):
            try:
                await self._notifier.send_event(
                    {
                        "kind": "ACCOUNT_UPDATE",
                        "detail": {
                            "positions_count": len(p) if isinstance(p, list) else 0,
                            "balances_count": len(b) if isinstance(b, list) else 0,
                            "event_time": int(msg.get("E") or int(time.time() * 1000)),
                        },
                    }
                )
            except Exception:
                logger.exception("account_update_notify_failed")

    async def _request_ws_reconnect(self) -> None:
        if self._reconnect_requested:
            return
        self._reconnect_requested = True
        ws = self._active_ws
        if ws is None:
            return
        try:
            await ws.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("user_stream_ws_close_failed", extra={"err": type(e).__name__}, exc_info=True)

    async def _has_active_exposure(self, *, now_ts: float) -> bool:
        if (now_ts - self._last_exposure_probe_ts) < self._exposure_probe_interval_sec:
            return bool(self._last_has_active_exposure)

        has_pos = False
        has_open_orders = False
        try:
            pos = await asyncio.to_thread(self._client.get_open_positions_any)
            if isinstance(pos, Mapping):
                for row in pos.values():
                    if not isinstance(row, Mapping):
                        continue
                    if abs(float(row.get("position_amt") or 0.0)) > 0.0:
                        has_pos = True
                        break
        except Exception:
            logger.exception("user_stream_exposure_probe_positions_failed")

        symbols = list(self._tracked_symbols)
        if not symbols:
            self._last_has_active_exposure = bool(has_pos)
            self._last_exposure_probe_ts = now_ts
            return bool(has_pos)

        try:
            oo = await asyncio.to_thread(self._client.get_open_orders_usdtm, symbols)
            if isinstance(oo, Mapping):
                for rows in oo.values():
                    if not isinstance(rows, list):
                        continue
                    for r in rows:
                        if not isinstance(r, Mapping):
                            continue
                        st = str(r.get("status") or "").upper()
                        if st in {"NEW", "PARTIALLY_FILLED"}:
                            has_open_orders = True
                            break
                    if has_open_orders:
                        break
        except Exception:
            logger.exception("user_stream_exposure_probe_open_orders_failed")

        out = bool(has_pos or has_open_orders)
        self._last_has_active_exposure = out
        self._last_exposure_probe_ts = now_ts
        return out

    def _stale_reason(self, *, now_ts: float, has_active_exposure: bool) -> Optional[str]:
        if not has_active_exposure:
            return None

        msg_ref = self._last_ws_msg_ts or self._last_ws_connect_ts
        if msg_ref is not None and (now_ts - msg_ref.timestamp()) > float(self._stale_msg_after_sec):
            return "user_stream_stale_transport"

        if self._stale_event_after_sec > 0:
            evt_ref = self._last_ws_event_ts or self._last_ws_connect_ts
            if evt_ref is not None and (now_ts - evt_ref.timestamp()) > float(self._stale_event_after_sec):
                return "user_stream_stale_event_with_exposure"
        return None

    async def _health_guard_loop(self) -> None:
        while not self._stop.is_set():
            try:
                st = self._engine.get_state()
                if bool(getattr(st, "ws_connected", False)):
                    self._disconnected_since = None
                    now_ts = time.time()
                    has_exposure = await self._has_active_exposure(now_ts=now_ts)
                    stale_reason = self._stale_reason(now_ts=now_ts, has_active_exposure=has_exposure)
                    if stale_reason is not None:
                        self._set_safe_mode(True, reason=stale_reason)
                        await self._request_ws_reconnect()
                    else:
                        self._set_safe_mode(False)
                else:
                    if self._disconnected_since is None:
                        self._disconnected_since = time.time()
                    if (time.time() - self._disconnected_since) >= float(self._safe_mode_after_sec):
                        self._set_safe_mode(True, reason="user_stream_disconnected")
            except Exception:
                logger.exception("user_stream_health_guard_failed")
            await asyncio.sleep(1.0)

    def _set_safe_mode(self, active: bool, *, reason: str = "user_stream_disconnected") -> None:
        next_state = bool(active)
        if self._safe_mode == next_state:
            return
        self._safe_mode = next_state
        try:
            self._engine.set_ws_safe_mode(next_state)
        except Exception:
            logger.exception("engine_set_ws_safe_mode_failed")
        if next_state and not self._safe_mode_alerted and self._notifier:
            self._safe_mode_alerted = True
            try:
                self._notifier.notify({"kind": "WS_DOWN_SAFE_MODE", "reason": str(reason)})
            except Exception:
                logger.exception("ws_down_safe_mode_notify_failed")
        if not next_state:
            self._safe_mode_alerted = False

    @property
    def listen_key_last_keepalive_ts(self) -> Optional[datetime]:
        return self._listen_key_last_keepalive_ts

    @property
    def last_ws_event_ts(self) -> Optional[datetime]:
        return self._last_ws_event_ts

    @property
    def last_ws_msg_ts(self) -> Optional[datetime]:
        return self._last_ws_msg_ts

    @property
    def safe_mode(self) -> bool:
        return bool(self._safe_mode)
