from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from v2.control.runtime_utils import FutureTimeoutError, age_seconds, run_async_blocking, to_float
from v2.exchange import BinanceRESTError

if TYPE_CHECKING:
    from v2.control.api import RuntimeController


logger = logging.getLogger("v2.control.api")


def build_freshness_snapshot(controller: RuntimeController) -> dict[str, Any]:
    user_ws_stale_sec = max(
        10.0,
        to_float(
            controller._risk.get("user_ws_stale_sec"),
            default=max(float(controller.scheduler.tick_seconds) * 4.0, 60.0),
        ),
    )
    market_data_stale_sec = max(
        5.0,
        to_float(
            controller._risk.get("market_data_stale_sec"),
            default=max(float(controller.scheduler.tick_seconds) * 2.0, 30.0),
        ),
    )
    reconcile_max_age_sec = max(
        30.0,
        to_float(controller._risk.get("reconcile_max_age_sec"), default=300.0),
    )
    last_user_ws_event_at = controller._user_stream_last_event_at
    last_private_stream_ok_at = controller._last_private_stream_ok_at
    last_market_data_at = controller._market_data_state.get("last_market_data_at")
    last_market_data_source_ok_at = controller._market_data_state.get("last_market_data_source_ok_at")
    last_market_data_source_fail_at = controller._market_data_state.get(
        "last_market_data_source_fail_at"
    )
    last_market_data_source_error = controller._market_data_state.get("last_market_data_source_error")
    last_reconcile_at = controller.state_store.get().last_reconcile_at
    user_ws_age_sec = age_seconds(last_private_stream_ok_at)
    market_data_age_sec = age_seconds(last_market_data_at)
    market_data_source_age_sec = age_seconds(last_market_data_source_ok_at)
    reconcile_age_sec = age_seconds(last_reconcile_at)

    if user_ws_age_sec is None and controller._user_stream_started:
        user_ws_age_sec = age_seconds(controller._user_stream_started_at)

    user_ws_stale = (
        controller.cfg.mode == "live"
        and controller._user_stream_started
        and user_ws_age_sec is not None
        and user_ws_age_sec > user_ws_stale_sec
    )
    market_data_observer_stale = (
        controller.cfg.mode == "live"
        and market_data_age_sec is not None
        and market_data_age_sec > market_data_stale_sec
    )
    market_data_source_stale = (
        controller.cfg.mode == "live"
        and market_data_source_age_sec is not None
        and market_data_source_age_sec > market_data_stale_sec
    )
    market_data_stale = market_data_observer_stale or market_data_source_stale
    return {
        "last_user_ws_event_at": last_user_ws_event_at,
        "last_private_stream_ok_at": last_private_stream_ok_at,
        "last_market_data_at": last_market_data_at,
        "last_market_data_source_ok_at": last_market_data_source_ok_at,
        "last_market_data_source_fail_at": last_market_data_source_fail_at,
        "last_market_data_source_error": last_market_data_source_error,
        "last_reconcile_at": last_reconcile_at,
        "user_ws_age_sec": user_ws_age_sec,
        "market_data_age_sec": market_data_age_sec,
        "market_data_source_age_sec": market_data_source_age_sec,
        "reconcile_age_sec": reconcile_age_sec,
        "user_ws_stale_sec": user_ws_stale_sec,
        "market_data_stale_sec": market_data_stale_sec,
        "reconcile_max_age_sec": reconcile_max_age_sec,
        "user_ws_stale": user_ws_stale,
        "market_data_stale": market_data_stale,
        "market_data_observer_stale": market_data_observer_stale,
        "market_data_source_stale": market_data_source_stale,
        "market_data_seen": last_market_data_at is not None,
        "private_stream_seen": last_private_stream_ok_at is not None,
    }


def get_cached_live_balance(
    controller: RuntimeController,
    *,
    max_age_sec: float,
) -> tuple[float | None, float | None] | None:
    fetched_at = controller._last_balance_fetched_mono
    if fetched_at is None:
        return None
    if (time.monotonic() - fetched_at) > max(0.0, float(max_age_sec)):
        return None
    if (
        controller._last_balance_available_usdt is None
        or controller._last_balance_wallet_usdt is None
    ):
        return None
    return controller._last_balance_available_usdt, controller._last_balance_wallet_usdt


def get_cached_or_fallback_balance(
    controller: RuntimeController,
    *,
    preserve_private_error: bool = False,
) -> tuple[float | None, float | None, str]:
    cached = get_cached_live_balance(controller, max_age_sec=1800.0)
    if cached is None:
        return None, None, "fallback"
    available, wallet = cached
    if not preserve_private_error:
        controller._last_balance_error = None
        controller._last_balance_error_detail = "served_from_recent_cache"
    return available, wallet, "exchange_cached"


def fetch_live_usdt_balance(
    controller: RuntimeController,
) -> tuple[float | None, float | None, str]:
    fresh_cache = get_cached_live_balance(controller, max_age_sec=20.0)
    if fresh_cache is not None:
        controller._last_balance_error = None
        controller._last_balance_error_detail = None
        available, wallet = fresh_cache
        return available, wallet, "exchange"

    if controller.rest_client is None:
        controller._last_balance_error = "rest_client_unavailable"
        controller._last_balance_error_detail = "balance_rest_client_not_configured"
        return get_cached_or_fallback_balance(controller, preserve_private_error=True)

    rest_client: Any = controller.rest_client
    payload: Any = None
    fetch_exc: Exception | None = None
    for attempt in range(2):
        try:
            payload = run_async_blocking(lambda: rest_client.get_balances(), timeout_sec=8.0)
            fetch_exc = None
            break
        except Exception as exc:  # noqa: BLE001
            fetch_exc = exc
            if attempt == 0:
                time.sleep(0.35)
                continue

    try:
        if fetch_exc is not None:
            raise fetch_exc
    except FutureTimeoutError:
        logger.warning("live_balance_fetch_timed_out")
        controller._last_balance_error = "balance_fetch_timeout"
        controller._last_balance_error_detail = "fetch_timeout_over_8s"
        return get_cached_or_fallback_balance(controller, preserve_private_error=True)
    except BinanceRESTError as exc:
        logger.warning(
            "live_balance_fetch_rest_error",
            extra={
                "status_code": exc.status_code,
                "code": exc.code,
                "path": exc.path,
            },
        )
        if exc.code in {-2014, -2015} or exc.status_code in {401, 403}:
            controller._last_balance_error = "balance_auth_failed"
        elif exc.status_code == 429 or exc.code in {-1003}:
            controller._last_balance_error = "balance_rate_limited"
        else:
            controller._last_balance_error = "balance_fetch_failed"
        controller._last_balance_error_detail = (
            f"status={exc.status_code} code={exc.code} path={exc.path} msg={exc.message}"
        )
        return get_cached_or_fallback_balance(controller, preserve_private_error=True)
    except Exception:  # noqa: BLE001
        logger.exception("live_balance_fetch_failed")
        controller._last_balance_error = "balance_fetch_failed"
        controller._last_balance_error_detail = "unexpected_exception"
        return get_cached_or_fallback_balance(controller, preserve_private_error=True)

    if not isinstance(payload, list):
        controller._last_balance_error = "balance_payload_invalid"
        controller._last_balance_error_detail = f"payload_type={type(payload).__name__}"
        return get_cached_or_fallback_balance(controller, preserve_private_error=True)

    target: dict[str, Any] | None = None
    for item in payload:
        if not isinstance(item, dict):
            continue
        asset = str(item.get("asset") or item.get("coin") or "").upper()
        if asset == "USDT":
            target = item
            break

    if target is None:
        controller._last_balance_error = "usdt_asset_missing"
        controller._last_balance_error_detail = "asset_usdt_not_found"
        return get_cached_or_fallback_balance(controller, preserve_private_error=True)

    available = to_float(
        target.get("availableBalance") or target.get("withdrawAvailable") or target.get("balance"),
        default=0.0,
    )
    wallet = to_float(
        target.get("walletBalance")
        or target.get("crossWalletBalance")
        or target.get("balance"),
        default=0.0,
    )
    controller._last_balance_available_usdt = available
    controller._last_balance_wallet_usdt = wallet
    controller._last_balance_fetched_mono = time.monotonic()
    controller._last_balance_error = None
    controller._last_balance_error_detail = None
    return available, wallet, "exchange"
