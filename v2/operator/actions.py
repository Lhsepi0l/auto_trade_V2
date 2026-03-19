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


def _action_status(action: str, raw_result: dict[str, Any]) -> str:
    error = str(raw_result.get("error") or "").strip()
    if error == "tick_busy":
        return "busy"

    snapshot = raw_result.get("snapshot", {})
    if isinstance(snapshot, dict):
        reason = str(snapshot.get("last_decision_reason") or "").strip()
        if reason == "tick_busy":
            return "busy"
        if reason in {"market_data_stale", "user_ws_stale", "state_uncertain"}:
            return "stale"

    if action == "reconcile" and bool(raw_result.get("state_uncertain")):
        return "blocked"

    if not _infer_ok(raw_result):
        return "failed"
    return "success"


def _action_label(action: str) -> str:
    return {
        "start_resume": "시작/재개",
        "pause": "일시정지",
        "panic": "패닉",
        "tick_now": "즉시 판단",
        "symbol_leverage": "심볼 레버리지 변경",
        "reconcile": "수동 reconcile",
        "cooldown_clear": "쿨다운 해제",
        "close_position": "포지션 종료",
        "close_all": "전체 종료",
        "scheduler_interval": "판단 주기 변경",
        "exec_mode": "실행 모드 변경",
        "margin_budget": "증거금 설정",
        "risk_basic": "리스크 기본 설정",
        "risk_advanced": "리스크 고급 설정",
        "notify_interval": "상태 알림 주기 변경",
    }.get(action, action)


def _action_summary(action: str, raw_result: dict[str, Any], context: dict[str, Any]) -> str:
    if action == "symbol_leverage":
        symbol = str(context.get("symbol") or raw_result.get("symbol") or "-")
        leverage_map = raw_result.get("symbol_leverage_map", {})
        if isinstance(leverage_map, dict):
            value = leverage_map.get(symbol)
            if value is not None:
                return f"{symbol} 레버리지 {float(value):g}x 적용"
    if action == "close_position":
        symbol = str(context.get("symbol") or raw_result.get("symbol") or "-")
        return f"{symbol} 포지션 종료 요청 완료"
    if action == "close_all":
        detail = raw_result.get("detail", {})
        if isinstance(detail, dict):
            results = detail.get("results", [])
            if isinstance(results, list):
                return f"전체 종료 요청 완료: {len(results)}개 심볼 처리"
    if action == "scheduler_interval":
        tick_sec = raw_result.get("tick_sec", context.get("tick_sec"))
        if tick_sec is not None:
            return f"판단 주기 {int(float(tick_sec))}초로 변경"
    if action == "exec_mode":
        mode = str(context.get("exec_mode") or raw_result.get("applied_value") or "-").upper()
        return f"실행 모드 {mode} 적용"
    if action == "margin_budget":
        amount = context.get("amount_usdt")
        leverage = context.get("leverage")
        if amount is not None and leverage is not None:
            return f"증거금 {float(amount):.4f} USDT / {float(leverage):g}x 적용"
        if amount is not None:
            return f"증거금 {float(amount):.4f} USDT 적용"
    if action == "risk_basic":
        return "리스크 기본 설정 적용"
    if action == "risk_advanced":
        return "리스크 고급 설정 적용"
    if action == "notify_interval":
        seconds = context.get("notify_interval_sec")
        if seconds is not None:
            return f"상태 알림 주기 {int(float(seconds))}초 적용"
    if action == "cooldown_clear":
        return "쿨다운 및 리스크 잠금 상태 해제"
    if action == "reconcile":
        if bool(raw_result.get("state_uncertain")):
            reason = str(raw_result.get("state_uncertain_reason") or "-")
            return f"reconcile 후 상태 불확실 유지: {humanize_reason_token(reason)}"
        return "reconcile 완료"
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


def wrap_operator_action(
    *,
    action: str,
    raw_result: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ctx = context if isinstance(context, dict) else {}
    return {
        "ok": _infer_ok(raw_result),
        "status": _action_status(action, raw_result),
        "action": action,
        "action_label": _action_label(action),
        "summary": _action_summary(action, raw_result, ctx),
        "context": ctx,
        "result": raw_result,
    }
