from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from v2.control.mutating_core_helpers import capture_public_risk_config
from v2.control.mutating_responses import build_clear_cooldown_response, build_set_value_response
from v2.control.presentation import build_risk_response
from v2.control.profile_policy import (
    normalize_runtime_risk_config as _normalize_runtime_risk_config,
)

logger = logging.getLogger(__name__)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def load_persisted_risk_config(controller: Any) -> None:
    try:
        persisted = controller.state_store.load_runtime_risk_config()
    except Exception:  # noqa: BLE001
        logger.exception("runtime_risk_config_load_failed")
        return
    normalized = _normalize_runtime_risk_config(persisted)
    if not normalized:
        return
    normalized, stripped_changed = controller._strip_persisted_strategy_runtime_overrides(normalized)
    normalized, changed = controller._migrate_legacy_strategy_runtime_defaults(normalized)
    normalized, scheduler_changed = controller._migrate_persisted_scheduler_runtime_defaults(normalized)
    for key, value in normalized.items():
        controller._risk[key] = value
    if changed or stripped_changed or scheduler_changed:
        persist_risk_config(controller)


def persist_risk_config(controller: Any) -> None:
    try:
        controller.state_store.save_runtime_risk_config(
            config=controller._persistent_risk_config()
        )
    except Exception:  # noqa: BLE001
        logger.exception("runtime_risk_config_save_failed")


def get_risk_response(controller: Any) -> dict[str, Any]:
    controller._refresh_runtime_risk_context()
    controller._sync_kernel_runtime_overrides()
    return build_risk_response(capture_public_risk_config(controller))


def set_runtime_value(
    controller: Any,
    *,
    normalized_key: str,
    parsed: Any,
    requested_value: str,
) -> dict[str, Any]:
    if normalized_key == "universe_symbols":
        if isinstance(parsed, str):
            parsed = [item.strip().upper() for item in parsed.split(",") if item.strip()]
        elif isinstance(parsed, list):
            parsed = [str(item).strip().upper() for item in parsed if str(item).strip()]
        else:
            parsed = [controller.cfg.behavior.exchange.default_symbol]
    if normalized_key in {"notify_interval_sec", "scheduler_tick_sec"}:
        parsed = max(1, int(_to_float(parsed, default=1.0)))

    controller._risk[normalized_key] = parsed
    if normalized_key == "scheduler_tick_sec":
        controller._apply_scheduler_tick_change(sec=int(_to_float(parsed, default=1.0)))
    else:
        controller._risk[normalized_key] = parsed
    controller._refresh_runtime_risk_context()
    controller._sync_kernel_runtime_overrides()
    persist_risk_config(controller)
    if normalized_key == "notify_interval_sec":
        controller._emit_status_update(force=True)
    return build_set_value_response(
        key=normalized_key,
        requested_value=requested_value,
        applied_value=controller._risk.get(normalized_key),
        risk_config=capture_public_risk_config(controller),
    )


def set_runtime_symbol_leverage(
    controller: Any,
    *,
    symbol: str,
    leverage: float,
) -> dict[str, Any]:
    symbol_u = symbol.strip().upper()
    mapping = controller._risk.get("symbol_leverage_map")
    if not isinstance(mapping, dict):
        mapping = {}
    if leverage <= 0:
        mapping.pop(symbol_u, None)
    else:
        leverage_f = float(leverage)
        mapping[symbol_u] = leverage_f
        current_max = max(
            1.0,
            _to_float(
                controller._risk.get("max_leverage"),
                default=float(controller.cfg.behavior.risk.max_leverage),
            ),
        )
        if leverage_f > current_max:
            controller._risk["max_leverage"] = leverage_f
    controller._risk["symbol_leverage_map"] = mapping
    controller._refresh_runtime_risk_context()
    controller._sync_kernel_runtime_overrides()
    persist_risk_config(controller)
    return build_risk_response(capture_public_risk_config(controller))


def preset_runtime_risk_profile(controller: Any, name: str) -> dict[str, Any]:
    profile = str(name).strip().lower()
    if profile == "conservative":
        controller._risk["max_leverage"] = 5.0
        controller._risk["risk_per_trade_pct"] = 5.0
    elif profile == "normal":
        controller._risk["max_leverage"] = 10.0
        controller._risk["risk_per_trade_pct"] = 10.0
    elif profile == "aggressive":
        controller._risk["max_leverage"] = 20.0
        controller._risk["risk_per_trade_pct"] = 20.0
    controller._sync_kernel_runtime_overrides()
    persist_risk_config(controller)
    return build_risk_response(capture_public_risk_config(controller))


def clear_cooldown_state(controller: Any) -> dict[str, Any]:
    controller._risk["daily_realized_pnl"] = 0.0
    controller._risk["daily_realized_pct"] = 0.0
    controller._risk["daily_loss_used_pct"] = 0.0
    controller._risk["dd_used_pct"] = 0.0
    controller._risk["lose_streak"] = 0
    controller._risk["cooldown_until"] = None
    controller._risk["daily_lock"] = False
    controller._risk["dd_lock"] = False
    controller._risk["runtime_equity_peak_usdt"] = float(
        controller._risk.get("runtime_equity_now_usdt") or 0.0
    )
    controller._risk["last_auto_risk_reason"] = None
    controller._risk["last_auto_risk_at"] = None
    controller._risk["last_block_reason"] = None
    controller._risk["last_strategy_block_reason"] = None
    controller._risk["last_alpha_id"] = None
    controller._risk["last_entry_family"] = None
    controller._risk["last_regime"] = None
    controller._risk["last_alpha_blocks"] = {}
    controller._risk["last_alpha_reject_focus"] = None
    controller._risk["last_alpha_reject_metrics"] = {}
    controller._risk["overheat_state"] = {"blocked": False, "reason": None}
    persist_risk_config(controller)
    controller._sync_kernel_runtime_overrides()
    return build_clear_cooldown_response(
        day=datetime.now(timezone.utc).date().isoformat(),
        risk_config=controller._risk,
    )
