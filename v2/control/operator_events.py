from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from v2.common.operator_labels import humanize_action_token, humanize_reason_token


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_bool_text(value: Any) -> str:
    return "예" if bool(value) else "아니오"


def _stale_target_label(stale_type: Any) -> str:
    normalized = str(stale_type or "").strip().lower()
    if normalized == "user_ws":
        return "프라이빗 스트림"
    if normalized == "market_data":
        return "시장 데이터"
    return normalized or "상태"


def build_operator_event_payload(*, event: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    raw_event = str(event or "").strip()
    if not raw_event:
        return None

    event_time = str(fields.get("event_time") or _utcnow_iso())
    reason = fields.get("reason")
    action = fields.get("action")
    symbol = str(fields.get("symbol") or "").strip().upper() or None

    category = "status"
    title = raw_event
    main_text = raw_event
    sub_text: str | None = None

    if raw_event == "runtime_start":
        category = "action"
        title = "엔진 시작"
        main_text = "엔진이 실행 상태로 전환되었습니다."
    elif raw_event == "controller_initialized":
        category = "status"
        title = "컨트롤러 초기화 완료"
        if bool(fields.get("recovery_required")):
            main_text = "운영 컨트롤러가 초기화됐지만 복구 확인이 필요합니다."
        else:
            main_text = "운영 컨트롤러 초기화가 완료되었습니다."
        if bool(fields.get("dirty_restart_detected")):
            sub_text = "이전 런타임 종료 흔적 감지"
    elif raw_event == "runtime_stop":
        category = "action"
        title = "엔진 일시정지"
        main_text = "엔진이 일시정지 상태로 전환되었습니다."
    elif raw_event == "panic_triggered":
        category = "action"
        title = "패닉 트리거"
        main_text = "패닉 정리 절차가 시작되었습니다."
    elif raw_event == "flatten_requested":
        category = "position"
        if action == "close_all":
            title = "전체 포지션 종료"
            main_text = "전체 포지션 종료 요청"
        elif action == "close_position":
            title = f"{symbol or '-'} 포지션 종료"
            main_text = f"{symbol or '-'} 종료 요청"
        else:
            title = "포지션 정리"
            main_text = f"{symbol or '-'} 정리 요청"
    elif raw_event == "reconcile_success":
        category = "action"
        title = "Reconcile 완료"
        main_text = "수동/자동 reconcile 성공"
        sub_text = str(reason or "").strip() or None
    elif raw_event == "recovery_transition":
        category = "status"
        if bool(fields.get("recovery_required")):
            title = "복구 필요"
            main_text = humanize_reason_token(str(reason or "recovery_required"))
        else:
            title = "복구 해제"
            main_text = "복구 필요 상태가 해제되었습니다."
    elif raw_event == "uncertainty_transition":
        category = "status"
        title = "상태 불확실"
        main_text = "상태 불확실 플래그 변경"
        sub_text = f"state_uncertain={_as_bool_text(fields.get('state_uncertain'))}"
    elif raw_event == "ready_transition":
        category = "status"
        ready = bool(fields.get("ready"))
        title = "운영 준비도 전환"
        if ready:
            main_text = "운영 준비 완료"
        else:
            main_text = "운영 준비 미완료"
            blockers: list[str] = []
            if bool(fields.get("recovery_required")):
                blockers.append("복구 필요")
            if not bool(fields.get("submission_recovery_ok", True)):
                blockers.append("주문 복구 확인 필요")
            if bool(fields.get("user_ws_stale")):
                blockers.append("프라이빗 스트림 stale")
            if bool(fields.get("market_data_stale")):
                blockers.append("시장 데이터 stale")
            sub_text = ", ".join(blockers) if blockers else "준비도 조건 재확인 필요"
    elif raw_event == "stale_transition":
        category = "risk"
        stale_type = str(fields.get("stale_type") or "unknown")
        stale = bool(fields.get("stale"))
        title = f"{_stale_target_label(stale_type)} 상태"
        main_text = "stale 감지" if stale else "정상 복귀"
        age_sec = fields.get("age_sec")
        sub_text = None if age_sec is None else f"age={age_sec}초"
    elif raw_event == "risk_trip":
        category = "risk"
        title = "자동 리스크 트립"
        main_text = humanize_reason_token(str(reason or "risk_trip"))
    elif raw_event == "user_stream_disconnect":
        category = "status"
        title = "프라이빗 스트림 끊김"
        main_text = humanize_reason_token(str(reason or "user_stream_disconnect"))
    elif raw_event == "user_stream_resync":
        category = "status"
        title = "프라이빗 스트림 재동기화"
        ok = bool(fields.get("ok"))
        main_text = "resync 성공" if ok else "resync 실패"
        sub_text = None if ok else str(reason or "").strip() or None
    elif raw_event == "user_stream_private_ok":
        category = "status"
        title = "프라이빗 스트림 정상"
        main_text = str(fields.get("source") or "private_ok")
    elif raw_event == "cycle_result":
        cycle_action = humanize_action_token(str(action or "-"))
        cycle_reason = humanize_reason_token(str(reason or "-"))
        title = "최근 판단"
        if str(action or "") in {"blocked", "risk_rejected"}:
            category = "blocked"
        elif str(action or "") in {"execution_failed", "error"}:
            category = "risk"
        else:
            category = "decision"
        main_text = f"{cycle_action} / {cycle_reason}"
        candidate_symbol = str(fields.get("candidate_symbol") or "").strip().upper()
        candidate_side = str(fields.get("candidate_side") or "").strip().upper()
        parts = [part for part in [candidate_symbol, candidate_side] if part]
        sub_text = " / ".join(parts) if parts else None
    elif raw_event == "report_sent":
        category = "report"
        title = "리포트 생성"
        main_text = str(fields.get("status") or "report")
        sub_text = str(fields.get("notifier_error") or "").strip() or None
    elif raw_event == "debug_bundle_exported":
        category = "action"
        title = "로그 추출 완료"
        main_text = "디버그 로그 번들이 생성되었습니다."
        sub_text = str(fields.get("summary_path") or fields.get("bundle_dir") or "").strip() or None
    else:
        if reason:
            main_text = humanize_reason_token(str(reason))
        elif action:
            main_text = humanize_action_token(str(action))
        if symbol:
            sub_text = symbol

    return {
        "event_type": raw_event,
        "category": category,
        "title": title,
        "main_text": main_text,
        "sub_text": sub_text,
        "event_time": event_time,
        "context": dict(fields),
    }
