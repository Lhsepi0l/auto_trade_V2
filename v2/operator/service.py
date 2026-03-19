from __future__ import annotations

from typing import TYPE_CHECKING, Any

from v2.operator.actions import wrap_operator_action
from v2.operator.read_models import build_operator_console_payload

if TYPE_CHECKING:
    from v2.control.api import RuntimeController


class OperatorService:
    def __init__(self, *, controller: RuntimeController) -> None:
        self._controller = controller

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
        return wrap_operator_action(action="symbol_leverage", raw_result=result)
