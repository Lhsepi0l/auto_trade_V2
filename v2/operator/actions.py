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
        "preset": "프리셋 적용",
        "profile_template": "프로파일 템플릿 적용",
        "trailing_config": "트레일링 설정",
        "universe_set": "운영 심볼 설정",
        "universe_remove": "운영 심볼 해제",
        "scoring_config": "판단식 설정",
        "report": "리포트 전송",
        "push_subscribe": "푸시 구독",
        "push_unsubscribe": "푸시 구독 해제",
        "push_test": "푸시 테스트",
        "client_log": "클라이언트 로그",
        "debug_bundle": "로그 추출",
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
    if action == "preset":
        name = str(context.get("name") or raw_result.get("preset") or "-")
        return f"프리셋 {name} 적용"
    if action == "profile_template":
        name = str(context.get("name") or "-")
        budget = context.get("budget_usdt")
        if budget is not None:
            return f"프로파일 템플릿 {name} 적용 (예산 {float(budget):.4f} USDT)"
        return f"프로파일 템플릿 {name} 적용"
    if action == "trailing_config":
        enabled = bool(context.get("trailing_enabled"))
        mode = str(context.get("trailing_mode") or "-").upper()
        if not enabled:
            return "트레일링 비활성화 적용"
        return f"트레일링 {mode} 모드 적용"
    if action == "universe_set":
        symbols = context.get("symbols") or []
        if isinstance(symbols, list):
            return f"운영 심볼 {len(symbols)}개 적용"
    if action == "universe_remove":
        symbol = str(context.get("symbol") or "-")
        return f"{symbol} 운영 심볼 해제"
    if action == "scoring_config":
        active_15m = bool(context.get("active_15m"))
        return f"판단식 설정 적용 (15m {'사용' if active_15m else '미사용'})"
    if action == "report":
        sent = bool(raw_result.get("notifier_sent"))
        if sent:
            return "일일 리포트 전송 완료"
        error = str(raw_result.get("notifier_error") or "").strip()
        if error == "disabled":
            return "리포트 생성 완료 (notifier 비활성)"
        if error:
            return f"리포트 생성 완료, 전송 실패: {error}"
        return "리포트 생성 완료"
    if action == "debug_bundle":
        mode_label = "전체" if bool(raw_result.get("full_export")) else "빠른"
        download_url = str(raw_result.get("download_url") or "").strip()
        if download_url:
            return f"{mode_label} 로그 번들 생성 완료, 다운로드 시작: {download_url}"
        summary_path = str(raw_result.get("summary_path") or "").strip()
        if summary_path:
            return f"{mode_label} 로그 번들 추출 완료: {summary_path}"
        bundle_dir = str(raw_result.get("bundle_dir") or "").strip()
        if bundle_dir:
            return f"{mode_label} 로그 번들 추출 완료: {bundle_dir}"
        error = str(raw_result.get("error") or "").strip()
        if error:
            return f"{mode_label} 로그 번들 추출 실패: {error}"
        return f"{mode_label} 로그 번들 추출 완료"
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
