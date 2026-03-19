from __future__ import annotations

from typing import Any

from v2.common.operator_labels import humanize_reason_token


def _infer_ok(raw_result: dict[str, Any]) -> bool:
    if isinstance(raw_result.get("ok"), bool):
        return bool(raw_result["ok"])
    if raw_result.get("error"):
        return False
    if raw_result.get("state") in {"RUNNING", "PAUSED", "KILLED", "STOPPED"}:
        return True
    if raw_result.get("symbol") is not None:
        return True
    if raw_result.get("tick_sec") is not None:
        return True
    if raw_result.get("snapshot") is not None:
        return True
    return True


def _action_label(action: str) -> str:
    return {
        "start_resume": "시작/재개",
        "pause": "일시정지",
        "panic": "패닉",
        "tick_now": "즉시 판단",
        "symbol_leverage": "심볼 레버리지 변경",
    }.get(action, action)


def _action_summary(action: str, raw_result: dict[str, Any]) -> str:
    if action == "symbol_leverage":
        symbol = str(raw_result.get("symbol") or "-")
        leverage_map = raw_result.get("symbol_leverage_map", {})
        if isinstance(leverage_map, dict):
            value = leverage_map.get(symbol)
            if value is not None:
                return f"{symbol} 레버리지 {float(value):g}x 적용"
    if action == "tick_now":
        snapshot = raw_result.get("snapshot", {})
        if isinstance(snapshot, dict):
            last_action = str(snapshot.get("last_action") or "-")
            reason = str(snapshot.get("last_decision_reason") or "-")
            return f"{_action_label(action)} 완료: {last_action} / {humanize_reason_token(reason)}"
    if raw_result.get("state") is not None:
        return f"{_action_label(action)} 완료: 상태={raw_result['state']}"
    if raw_result.get("kind") == "STATE":
        engine_state = raw_result.get("engine_state", {})
        if isinstance(engine_state, dict) and engine_state.get("state") is not None:
            return f"{_action_label(action)} 완료: 상태={engine_state['state']}"
    error = raw_result.get("error")
    if error:
        return f"{_action_label(action)} 실패: {error}"
    return f"{_action_label(action)} 완료"


def wrap_operator_action(*, action: str, raw_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": _infer_ok(raw_result),
        "action": action,
        "action_label": _action_label(action),
        "summary": _action_summary(action, raw_result),
        "result": raw_result,
    }
