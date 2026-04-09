from __future__ import annotations

from typing import Any

from v2.control.live_balance_helpers import (
    fetch_live_usdt_balance,
    get_cached_live_balance,
    get_cached_or_fallback_balance,
)
from v2.control.presentation import build_scheduler_response
from v2.control.profile_policy import build_live_readiness_snapshot
from v2.control.status_payloads import (
    build_healthz_snapshot,
    build_readyz_snapshot,
    build_status_snapshot,
)


def live_readiness_snapshot(
    controller: Any,
    *,
    live_balance_source: str | None = None,
    private_error: str | None = None,
) -> dict[str, Any]:
    return build_live_readiness_snapshot(
        controller,
        live_balance_source=live_balance_source,
        private_error=private_error,
    )


def status_snapshot(controller: Any) -> dict[str, Any]:
    return build_status_snapshot(controller)


def healthz_snapshot(controller: Any) -> dict[str, Any]:
    return build_healthz_snapshot(controller)


def readyz_snapshot(controller: Any) -> dict[str, Any]:
    return build_readyz_snapshot(controller)


def cached_live_balance(
    controller: Any,
    *,
    max_age_sec: float,
) -> tuple[float | None, float | None] | None:
    return get_cached_live_balance(controller, max_age_sec=max_age_sec)


def cached_or_fallback_balance(
    controller: Any,
    *,
    preserve_private_error: bool = False,
) -> tuple[float | None, float | None, str]:
    return get_cached_or_fallback_balance(
        controller,
        preserve_private_error=preserve_private_error,
    )


def live_usdt_balance(controller: Any) -> tuple[float | None, float | None, str]:
    return fetch_live_usdt_balance(controller)


def scheduler_response(controller: Any) -> dict[str, Any]:
    return build_scheduler_response(
        tick_sec=float(controller.scheduler.tick_seconds),
        running=bool(controller._running),
    )
