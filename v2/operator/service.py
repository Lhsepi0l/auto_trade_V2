from __future__ import annotations

from typing import TYPE_CHECKING, Any

from v2.operator.actions import wrap_operator_action
from v2.operator.debug_bundle import export_runtime_debug_bundle
from v2.operator.guidance import build_operator_guidance
from v2.operator.presets import PRESETS, PROFILE_KEYS, build_profile_payload
from v2.operator.read_models import build_operator_console_payload
from v2.operator.universe_scoring import parse_universe_symbols, validate_scoring_weights

if TYPE_CHECKING:
    from v2.control.api import RuntimeController


class OperatorService:
    def __init__(self, *, controller: RuntimeController) -> None:
        self._controller = controller

    @staticmethod
    def _stringify_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _apply_values(self, pairs: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs.items():
            result = self._controller.set_value(key=key, value=self._stringify_value(value))
        return result

    def _current_risk_config(self) -> dict[str, Any]:
        return self._controller.get_risk()

    def console_payload(self) -> dict[str, Any]:
        return build_operator_console_payload(
            self._controller._status_snapshot(),
            guidance=build_operator_guidance(),
        )

    def start_or_resume(self) -> dict[str, Any]:
        return wrap_operator_action(action="start_resume", raw_result=self._controller.start())

    def pause(self) -> dict[str, Any]:
        return wrap_operator_action(action="pause", raw_result=self._controller.stop())

    async def panic(self) -> dict[str, Any]:
        result = await self._controller.panic()
        return wrap_operator_action(action="panic", raw_result=result)

    def tick_now(self) -> dict[str, Any]:
        return wrap_operator_action(action="tick_now", raw_result=self._controller.tick_scheduler_now())

    def set_symbol_leverage(self, *, symbol: str, leverage: float) -> dict[str, Any]:
        result = self._controller.set_symbol_leverage(symbol=symbol, leverage=leverage)
        return wrap_operator_action(
            action="symbol_leverage",
            raw_result=result,
            context={"symbol": str(symbol).strip().upper(), "leverage": float(leverage)},
        )

    async def reconcile(self) -> dict[str, Any]:
        result = await self._controller.reconcile_now()
        return wrap_operator_action(action="reconcile", raw_result=result)

    def clear_cooldown(self) -> dict[str, Any]:
        return wrap_operator_action(action="cooldown_clear", raw_result=self._controller.clear_cooldown())

    async def close_position(self, *, symbol: str) -> dict[str, Any]:
        result = await self._controller.close_position(symbol=str(symbol).strip().upper())
        return wrap_operator_action(
            action="close_position",
            raw_result=result,
            context={"symbol": str(symbol).strip().upper()},
        )

    async def close_all(self) -> dict[str, Any]:
        result = await self._controller.close_all()
        return wrap_operator_action(action="close_all", raw_result=result)

    def set_scheduler_interval(self, *, tick_sec: float) -> dict[str, Any]:
        result = self._controller.set_scheduler_interval(float(tick_sec))
        return wrap_operator_action(
            action="scheduler_interval",
            raw_result=result,
            context={"tick_sec": float(tick_sec)},
        )

    def set_exec_mode(self, *, exec_mode: str) -> dict[str, Any]:
        mode = str(exec_mode).strip().upper()
        if mode not in {"LIMIT", "MARKET", "SPLIT"}:
            raise ValueError("exec_mode must be LIMIT, MARKET, or SPLIT")
        result = self._controller.set_value(key="exec_mode_default", value=mode)
        return wrap_operator_action(
            action="exec_mode",
            raw_result=result,
            context={"exec_mode": mode},
        )

    def set_margin_budget(self, *, amount_usdt: float, leverage: float | None = None) -> dict[str, Any]:
        risk_config = self._current_risk_config()
        margin_use_pct = float(risk_config.get("margin_use_pct") or 1.0)
        if margin_use_pct <= 0.0:
            margin_use_pct = 1.0
        base_budget = float(amount_usdt) / margin_use_pct
        payload: dict[str, Any] = {
            "capital_mode": "MARGIN_BUDGET_USDT",
            "margin_budget_usdt": base_budget,
        }
        if leverage is not None:
            payload["max_leverage"] = float(leverage)
        result = self._apply_values(payload)
        return wrap_operator_action(
            action="margin_budget",
            raw_result=result,
            context={"amount_usdt": float(amount_usdt), "leverage": leverage},
        )

    def set_risk_basic(
        self,
        *,
        max_leverage: float,
        max_exposure_pct: float,
        max_notional_pct: float,
        per_trade_risk_pct: float,
    ) -> dict[str, Any]:
        result = self._apply_values(
            {
                "max_leverage": float(max_leverage),
                "max_exposure_pct": float(max_exposure_pct),
                "max_notional_pct": float(max_notional_pct),
                "per_trade_risk_pct": float(per_trade_risk_pct),
            }
        )
        return wrap_operator_action(action="risk_basic", raw_result=result)

    def set_risk_advanced(
        self,
        *,
        daily_loss_limit_pct: float,
        dd_limit_pct: float,
        min_hold_minutes: int,
        score_conf_threshold: float,
    ) -> dict[str, Any]:
        result = self._apply_values(
            {
                "daily_loss_limit_pct": float(daily_loss_limit_pct),
                "dd_limit_pct": float(dd_limit_pct),
                "min_hold_minutes": int(min_hold_minutes),
                "score_conf_threshold": float(score_conf_threshold),
            }
        )
        return wrap_operator_action(action="risk_advanced", raw_result=result)

    def set_notify_interval(self, *, notify_interval_sec: int) -> dict[str, Any]:
        sec = max(1, int(notify_interval_sec))
        _ = self._controller.set_scheduler_interval(float(sec))
        result = self._controller.set_value(
            key="notify_interval_sec",
            value=self._stringify_value(sec),
        )
        return wrap_operator_action(
            action="notify_interval",
            raw_result=result,
            context={
                "notify_interval_sec": sec,
                "scheduler_tick_sec": sec,
            },
        )

    def apply_preset(self, *, name: str) -> dict[str, Any]:
        preset_name = str(name).strip().lower()
        if preset_name not in PRESETS:
            raise ValueError(f"preset must be one of: {', '.join(PRESETS)}")
        result = self._controller.preset(preset_name)
        return wrap_operator_action(
            action="preset",
            raw_result=result,
            context={"name": preset_name},
        )

    def apply_profile_template(self, *, name: str, budget_usdt: float | None = None) -> dict[str, Any]:
        profile_name = str(name).strip()
        if profile_name not in PROFILE_KEYS:
            raise ValueError(f"profile must be one of: {', '.join(PROFILE_KEYS)}")
        payload = build_profile_payload(profile_name, budget_usdt)
        self._apply_values(payload)
        result = self._current_risk_config()
        return wrap_operator_action(
            action="profile_template",
            raw_result=result,
            context={"name": profile_name, "budget_usdt": budget_usdt},
        )

    def set_trailing_config(
        self,
        *,
        trailing_enabled: bool,
        trailing_mode: str,
        trail_arm_pnl_pct: float,
        trail_grace_minutes: int,
        trail_distance_pnl_pct: float | None = None,
        atr_trail_timeframe: str | None = None,
        atr_trail_k: float | None = None,
        atr_trail_min_pct: float | None = None,
        atr_trail_max_pct: float | None = None,
    ) -> dict[str, Any]:
        mode = str(trailing_mode).strip().upper()
        if mode not in {"PCT", "ATR"}:
            raise ValueError("trailing_mode must be PCT or ATR")

        payload: dict[str, Any] = {
            "trailing_enabled": bool(trailing_enabled),
            "trailing_mode": mode,
            "trail_arm_pnl_pct": float(trail_arm_pnl_pct),
            "trail_grace_minutes": int(trail_grace_minutes),
        }

        if mode == "PCT":
            if trail_distance_pnl_pct is None:
                raise ValueError("trail_distance_pnl_pct is required for PCT mode")
            payload["trail_distance_pnl_pct"] = float(trail_distance_pnl_pct)
        else:
            timeframe = str(atr_trail_timeframe or "").strip().lower()
            if timeframe not in {"15m", "1h", "4h"}:
                raise ValueError("atr_trail_timeframe must be 15m, 1h, or 4h")
            if atr_trail_k is None or atr_trail_min_pct is None or atr_trail_max_pct is None:
                raise ValueError("ATR mode requires atr_trail_k, atr_trail_min_pct, atr_trail_max_pct")
            if float(atr_trail_max_pct) < float(atr_trail_min_pct):
                raise ValueError("atr_trail_max_pct must be >= atr_trail_min_pct")
            payload.update(
                {
                    "atr_trail_timeframe": timeframe,
                    "atr_trail_k": float(atr_trail_k),
                    "atr_trail_min_pct": float(atr_trail_min_pct),
                    "atr_trail_max_pct": float(atr_trail_max_pct),
                }
            )

        result = self._apply_values(payload)
        return wrap_operator_action(
            action="trailing_config",
            raw_result=result,
            context={
                "trailing_enabled": bool(trailing_enabled),
                "trailing_mode": mode,
            },
        )

    def set_universe_symbols(self, *, symbols_text: str) -> dict[str, Any]:
        symbols = parse_universe_symbols(symbols_text)
        result = self._controller.set_value(key="universe_symbols", value=",".join(symbols))
        return wrap_operator_action(
            action="universe_set",
            raw_result=result,
            context={"symbols": symbols},
        )

    def remove_universe_symbol(self, *, symbol: str) -> dict[str, Any]:
        target = str(symbol).strip().upper()
        risk_config = self._current_risk_config()
        current_raw = risk_config.get("universe_symbols") or []
        current = [str(item).strip().upper() for item in current_raw if str(item).strip()]
        filtered = [item for item in current if item != target]
        if target not in current:
            raise ValueError(f"{target} is not in current universe")
        if not filtered:
            raise ValueError("universe must keep at least one symbol")
        result = self._controller.set_value(key="universe_symbols", value=",".join(filtered))
        return wrap_operator_action(
            action="universe_remove",
            raw_result=result,
            context={"symbol": target, "symbols": filtered},
        )

    def set_scoring_config(
        self,
        *,
        tf_weight_10m: float,
        tf_weight_15m: float,
        tf_weight_30m: float,
        tf_weight_1h: float,
        tf_weight_4h: float,
        score_conf_threshold: float,
        score_gap_threshold: float,
        donchian_momentum_filter: bool,
        donchian_fast_ema_period: int,
        donchian_slow_ema_period: int,
    ) -> dict[str, Any]:
        weights = validate_scoring_weights(
            {
                "10m": tf_weight_10m,
                "15m": tf_weight_15m,
                "30m": tf_weight_30m,
                "1h": tf_weight_1h,
                "4h": tf_weight_4h,
            }
        )
        fast = int(donchian_fast_ema_period)
        slow = int(donchian_slow_ema_period)
        if slow <= fast:
            slow = fast + 1
        payload = {
            "tf_weight_10m": weights["10m"],
            "tf_weight_15m": weights["15m"],
            "tf_weight_30m": weights["30m"],
            "tf_weight_1h": weights["1h"],
            "tf_weight_4h": weights["4h"],
            "score_tf_15m_enabled": bool(weights["15m"] > 0.0),
            "score_conf_threshold": float(score_conf_threshold),
            "score_gap_threshold": float(score_gap_threshold),
            "donchian_momentum_filter": bool(donchian_momentum_filter),
            "donchian_fast_ema_period": fast,
            "donchian_slow_ema_period": slow,
        }
        result = self._apply_values(payload)
        return wrap_operator_action(
            action="scoring_config",
            raw_result=result,
            context={"active_15m": bool(weights["15m"] > 0.0)},
        )

    def trigger_report(self) -> dict[str, Any]:
        result = self._controller.send_daily_report()
        return wrap_operator_action(action="report", raw_result=result)

    def export_debug_bundle(self, *, base_url: str) -> dict[str, Any]:
        result = export_runtime_debug_bundle(label="operator_logs", base_url=base_url)
        if bool(result.get("ok")):
            self._controller._log_event(
                "debug_bundle_exported",
                bundle_dir=result.get("bundle_dir"),
                summary_path=result.get("summary_path"),
                archive_path=result.get("archive_path"),
                download_url=result.get("download_url"),
            )
        return wrap_operator_action(
            action="debug_bundle",
            raw_result=result,
            context={"base_url": base_url},
        )

    def list_operator_events(
        self,
        *,
        limit: int = 200,
        category: str | None = None,
        query: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        storage = self._controller.state_store.runtime_storage()
        return storage.list_operator_events(
            limit=limit,
            category=category,
            query=query,
            offset=offset,
        )

    def count_operator_events(
        self,
        *,
        category: str | None = None,
        query: str | None = None,
    ) -> int:
        return self._controller.state_store.runtime_storage().count_operator_events(
            category=category,
            query=query,
        )
