from __future__ import annotations

from typing import Any, Literal, cast

from v2.control.runtime_utils import (
    FutureTimeoutError,
    clamp,
    run_async_blocking,
    to_bool,
    to_float,
)
from v2.tpsl import BracketConfig, BracketPlanner, BracketService


def replace_management_bracket(
    controller: Any,
    *,
    symbol: str,
    entry_side: Literal["BUY", "SELL"],
    position_side: str,
    quantity: float,
    take_profit_price: float,
    stop_loss_price: float,
    reason: str,
) -> bool:
    if controller.cfg.mode != "live" or controller.rest_client is None or quantity <= 0.0:
        return False
    if take_profit_price <= 0.0 or stop_loss_price <= 0.0:
        return False
    runtime_bracket_service = BracketService(
        planner=BracketPlanner(
            cfg=BracketConfig(
                take_profit_pct=float(controller.cfg.behavior.tpsl.take_profit_pct),
                stop_loss_pct=float(controller.cfg.behavior.tpsl.stop_loss_pct),
            )
        ),
        storage=controller.state_store.runtime_storage(),
        rest_client=controller.rest_client,
        mode=controller.cfg.mode,
    )
    try:
        _ = run_async_blocking(
            lambda: runtime_bracket_service.replace_with_prices(
                symbol=symbol,
                entry_side=entry_side,
                position_side=cast(Literal["BOTH", "LONG", "SHORT"], position_side),
                quantity=float(quantity),
                take_profit_price=float(take_profit_price),
                stop_loss_price=float(stop_loss_price),
            ),
            timeout_sec=15.0,
        )
        controller._log_event(
            "position_management_update",
            symbol=symbol,
            reason=reason,
            take_profit_price=round(float(take_profit_price), 6),
            stop_loss_price=round(float(stop_loss_price), 6),
            quantity=round(float(quantity), 8),
        )
        return True
    except Exception:  # noqa: BLE001
        controller_logger = getattr(controller, "logger", None)
        if controller_logger is not None:
            controller_logger.exception(
                "position_management_bracket_replace_failed symbol=%s reason=%s",
                symbol,
                reason,
            )
        else:
            import logging

            logging.getLogger("v2.control.api").exception(
                "position_management_bracket_replace_failed symbol=%s reason=%s",
                symbol,
                reason,
            )
        return False


def resolve_bracket_config_for_cycle(
    controller: Any,
    *,
    cycle: Any,
    entry_price: float,
) -> tuple[BracketConfig, float | None, dict[str, Any]]:
    base_tp = max(
        0.0,
        to_float(
            controller._risk.get("tpsl_base_take_profit_pct"),
            default=float(controller.cfg.behavior.tpsl.take_profit_pct),
        ),
    )
    base_sl = max(
        0.0,
        to_float(
            controller._risk.get("tpsl_base_stop_loss_pct"),
            default=float(controller.cfg.behavior.tpsl.stop_loss_pct),
        ),
    )
    policy = str(controller._risk.get("tpsl_policy") or "adaptive_regime").strip().lower()
    method_raw = str(controller._risk.get("tpsl_method") or "percent").strip().lower()
    method: Literal["percent", "atr"] = "atr" if method_raw == "atr" else "percent"

    regime = str(getattr(cycle.candidate, "regime_hint", "") or "").strip().upper()
    bull_mult = to_float(controller._risk.get("tpsl_regime_mult_bull"), default=1.15)
    bear_mult = to_float(controller._risk.get("tpsl_regime_mult_bear"), default=1.15)
    sideways_mult = to_float(controller._risk.get("tpsl_regime_mult_sideways"), default=0.9)
    unknown_mult = to_float(controller._risk.get("tpsl_regime_mult_unknown"), default=1.0)
    regime_mult = unknown_mult
    if policy == "adaptive_regime":
        if regime == "BULL":
            regime_mult = bull_mult
        elif regime == "BEAR":
            regime_mult = bear_mult
        elif regime == "SIDEWAYS":
            regime_mult = sideways_mult

    tp_pct = base_tp * regime_mult
    sl_pct = base_sl * regime_mult

    volatility_mult = 1.0
    atr_hint = to_float(getattr(cycle.candidate, "volatility_hint", 0.0), default=0.0)
    if to_bool(controller._risk.get("tpsl_volatility_norm_enabled"), default=False):
        if atr_hint > 0.0 and entry_price > 0.0:
            atr_pct = atr_hint / max(entry_price, 1e-9)
            atr_pct_ref = max(1e-6, to_float(controller._risk.get("tpsl_atr_pct_ref"), default=0.01))
            raw_mult = (atr_pct / atr_pct_ref) ** 0.5
            vol_min = max(0.1, to_float(controller._risk.get("tpsl_vol_mult_min"), default=0.85))
            vol_max = max(vol_min, to_float(controller._risk.get("tpsl_vol_mult_max"), default=1.2))
            volatility_mult = clamp(raw_mult, vol_min, vol_max)
            tp_pct *= volatility_mult
            sl_pct *= volatility_mult

    tp_min = max(0.0, to_float(controller._risk.get("tpsl_tp_min_pct"), default=0.0025))
    tp_max = max(tp_min, to_float(controller._risk.get("tpsl_tp_max_pct"), default=0.06))
    sl_min = max(0.0, to_float(controller._risk.get("tpsl_sl_min_pct"), default=0.0025))
    sl_max = max(sl_min, to_float(controller._risk.get("tpsl_sl_max_pct"), default=0.03))
    tp_pct = clamp(tp_pct, tp_min, tp_max)
    sl_pct = clamp(sl_pct, sl_min, sl_max)

    rr_min = max(0.1, to_float(controller._risk.get("tpsl_rr_min"), default=0.8))
    rr_max = max(rr_min, to_float(controller._risk.get("tpsl_rr_max"), default=3.0))
    if sl_pct > 0.0:
        rr_now = tp_pct / sl_pct
        if rr_now < rr_min:
            tp_pct = clamp(sl_pct * rr_min, tp_min, tp_max)
        elif rr_now > rr_max:
            tp_pct = clamp(sl_pct * rr_max, tp_min, tp_max)

    atr_for_bracket: float | None = None
    if method == "atr":
        if atr_hint > 0.0:
            atr_for_bracket = atr_hint
        else:
            method = "percent"

    cfg = BracketConfig(
        method=method,
        take_profit_pct=tp_pct,
        stop_loss_pct=sl_pct,
        tp_atr=max(0.1, to_float(controller._risk.get("tpsl_tp_atr"), default=2.0)),
        sl_atr=max(0.1, to_float(controller._risk.get("tpsl_sl_atr"), default=1.0)),
        working_type="MARK_PRICE",
        price_protect=True,
    )
    meta = {
        "policy": policy,
        "regime": regime or "UNKNOWN",
        "regime_mult": regime_mult,
        "volatility_mult": volatility_mult,
        "method": cfg.method,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
    }
    return cfg, atr_for_bracket, meta


def place_brackets_for_cycle(controller: Any, *, cycle: Any) -> None:
    controller._last_cycle["bracket"] = None
    if cycle.state != "executed":
        return
    if cycle.candidate is None or cycle.size is None:
        return

    symbol = str(cycle.candidate.symbol or "").strip().upper()
    side = str(cycle.candidate.side or "").strip().upper()
    qty = to_float(cycle.size.qty, default=0.0)
    entry_price = to_float(cycle.candidate.entry_price, default=0.0)
    if not symbol or side not in {"BUY", "SELL"} or qty <= 0.0 or entry_price <= 0.0:
        controller._last_cycle["bracket"] = {"state": "skipped", "reason": "invalid_bracket_inputs"}
        return

    bracket_cfg, atr_for_bracket, bracket_meta = resolve_bracket_config_for_cycle(
        controller,
        cycle=cycle,
        entry_price=entry_price,
    )
    tracked_runtime = next(
        (
            row
            for row in controller._list_tracked_brackets()
            if str(row.get("symbol") or "").strip().upper() == symbol
            and str(row.get("state") or "").strip().upper() in {"CREATED", "PLACED", "ACTIVE"}
        ),
        None,
    )
    if tracked_runtime is not None:
        tracked_ids = {
            str(tracked_runtime.get("tp_order_client_id") or "").strip(),
            str(tracked_runtime.get("sl_order_client_id") or "").strip(),
        }
        tracked_ids = {cid for cid in tracked_ids if cid}
        active_tracked_ids = set(tracked_ids)
        if controller.cfg.mode == "live" and controller.rest_client is not None and tracked_ids:
            rest_client_any: Any = controller.rest_client
            try:
                open_orders = run_async_blocking(
                    lambda s=symbol: rest_client_any.get_open_algo_orders(symbol=s),
                    timeout_sec=8.0,
                )
                open_ids = {
                    str(item.get("clientAlgoId") or item.get("clientOrderId") or "").strip()
                    for item in (open_orders or [])
                    if isinstance(item, dict)
                }
                active_tracked_ids = {cid for cid in tracked_ids if cid in open_ids}
            except FutureTimeoutError:
                controller._log_event("bracket_place_warning", notify=False, symbol=symbol, reason="existing_bracket_fetch_timeout")
            except Exception:  # noqa: BLE001
                controller._log_event("bracket_place_warning", notify=False, symbol=symbol, reason="existing_bracket_fetch_failed")
        if active_tracked_ids:
            controller._last_cycle["bracket"] = {
                "state": "active",
                "symbol": symbol,
                "policy": bracket_meta,
                "reused": True,
            }
            return

    runtime_bracket_service = BracketService(
        planner=BracketPlanner(cfg=bracket_cfg),
        storage=controller.state_store.runtime_storage(),
        rest_client=controller.rest_client,
        mode=controller.cfg.mode,
    )

    try:
        def _create_bracket(entry_side: Literal["BUY", "SELL"]):
            return runtime_bracket_service.create_and_place(
                symbol=symbol,
                entry_side=entry_side,
                entry_price=entry_price,
                quantity=qty,
                atr=atr_for_bracket,
            )

        out = run_async_blocking(
            (lambda: _create_bracket("BUY")) if side == "BUY" else (lambda: _create_bracket("SELL")),
            timeout_sec=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).strip()
        err = f"bracket_failed:{type(exc).__name__}"
        if detail:
            err = f"{err}:{detail}"
        controller._last_cycle["bracket"] = {"state": "failed", "error": err}
        controller._last_cycle["last_error"] = err
        return

    planned = out.get("planned") if isinstance(out, dict) else None
    if isinstance(planned, dict):
        controller._last_cycle["bracket"] = {
            "state": "active",
            "symbol": symbol,
            "take_profit": to_float(planned.get("take_profit_price"), default=0.0),
            "stop_loss": to_float(planned.get("stop_loss_price"), default=0.0),
            "policy": bracket_meta,
        }
        return
    controller._last_cycle["bracket"] = {"state": "active", "symbol": symbol, "policy": bracket_meta}
