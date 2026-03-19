from __future__ import annotations

from typing import TYPE_CHECKING, Any

from v2.operator.actions import wrap_operator_action
from v2.operator.read_models import build_operator_console_payload

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
        return build_operator_console_payload(self._controller._status_snapshot())

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
        result = self._controller.set_value(
            key="notify_interval_sec",
            value=self._stringify_value(max(1, int(notify_interval_sec))),
        )
        return wrap_operator_action(
            action="notify_interval",
            raw_result=result,
            context={"notify_interval_sec": max(1, int(notify_interval_sec))},
        )
