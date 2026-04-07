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
        title = "전체 로그 추출 완료" if bool(fields.get("full_export")) else "빠른 로그 추출 완료"
        main_text = "디버그 로그 번들이 생성되었습니다."
        sub_text = (
            str(
                fields.get("download_url")
                or fields.get("summary_path")
                or fields.get("bundle_dir")
                or ""
            ).strip()
            or None
        )
    elif raw_event == "position_management_update":
        category = "decision"
        title = "보유 관리 업데이트"
        main_text = humanize_reason_token(str(reason or "position_management_update"))
        held_bars = fields.get("held_bars")
        mfe_r = fields.get("max_favorable_r")
        sub_parts = []
        if symbol:
            sub_parts.append(symbol)
        if held_bars is not None:
            sub_parts.append(f"held={held_bars}")
        if mfe_r is not None:
            sub_parts.append(f"mfe_r={mfe_r}")
        sub_text = " / ".join(sub_parts) if sub_parts else None
    elif raw_event == "position_management_exit":
        category = "position"
        title = "보유 관리 청산"
        main_text = humanize_reason_token(str(reason or "position_management_exit"))
        held_bars = fields.get("held_bars")
        mfe_r = fields.get("max_favorable_r")
        sub_parts = []
        if symbol:
            sub_parts.append(symbol)
        if held_bars is not None:
            sub_parts.append(f"held={held_bars}")
        if mfe_r is not None:
            sub_parts.append(f"mfe_r={mfe_r}")
        sub_text = " / ".join(sub_parts) if sub_parts else None
    elif raw_event == "position_entry_opened":
        category = "position"
        side = str(fields.get("side") or "").strip().upper()
        action_state = str(fields.get("action") or "").strip().lower()
        side_label = "LONG" if side == "BUY" else "SHORT" if side == "SELL" else side or "-"
        if action_state == "dry_run":
            title = f"{symbol or '-'} {side_label} 모의 진입"
        else:
            title = f"{symbol or '-'} {side_label} 진입"
        alpha_id = str(fields.get("alpha_id") or "").strip()
        entry_family = str(fields.get("entry_family") or "").strip()
        parts = [part for part in [alpha_id, entry_family] if part]
        main_text = " / ".join(parts) if parts else "진입 실행"
        detail_parts = []
        if fields.get("qty") is not None:
            detail_parts.append(f"qty={fields.get('qty'):.6f}" if isinstance(fields.get("qty"), (int, float)) else f"qty={fields.get('qty')}")
        if fields.get("leverage") is not None:
            detail_parts.append(f"lev={fields.get('leverage')}")
        if fields.get("notional") is not None:
            detail_parts.append(f"notional={fields.get('notional')}")
        if fields.get("entry_price") is not None:
            detail_parts.append(f"entry={fields.get('entry_price')}")
        sub_text = " / ".join(detail_parts) if detail_parts else None
    elif raw_event == "alpha_drift_setup_queued":
        category = "decision"
        title = f"{symbol or '-'} drift setup 대기"
        main_text = "alpha_drift / setup queued"
        parts = []
        if fields.get("setup_open_time_ms") is not None:
            parts.append(f"setup_open_time_ms={fields.get('setup_open_time_ms')}")
        if fields.get("setup_expiry_bars") is not None:
            parts.append(f"expiry={fields.get('setup_expiry_bars')}")
        sub_text = " / ".join(parts) if parts else None
    elif raw_event == "alpha_drift_confirmed":
        category = "decision"
        side = str(fields.get("side") or "").strip().upper()
        side_label = "LONG" if side == "BUY" else "SHORT" if side == "SELL" else side or "-"
        title = f"{symbol or '-'} drift confirm"
        main_text = f"alpha_drift / {side_label} confirm"
        parts = []
        if fields.get("action") is not None:
            parts.append(str(fields.get("action")))
        if fields.get("score") is not None:
            parts.append(f"score={fields.get('score')}")
        if fields.get("entry_price") is not None:
            parts.append(f"entry={fields.get('entry_price')}")
        if fields.get("setup_open_time_ms") is not None:
            parts.append(f"setup_open_time_ms={fields.get('setup_open_time_ms')}")
        sub_text = " / ".join(parts) if parts else None
    elif raw_event == "position_reduced":
        category = "position"
        title = f"{symbol or '-'} 부분청산"
        main_text = humanize_reason_token(str(reason or "partial_reduce_executed"))
        parts = []
        if fields.get("reduced_qty") is not None:
            parts.append(f"reduced={fields.get('reduced_qty')}")
        if fields.get("remaining_qty") is not None:
            parts.append(f"remain={fields.get('remaining_qty')}")
        if fields.get("current_r") is not None:
            parts.append(f"r={fields.get('current_r')}")
        sub_text = " / ".join(parts) if parts else None
    elif raw_event == "position_closed":
        category = "position"
        raw_reason = str(reason or "position_closed").strip()
        if raw_reason == "take_profit":
            title = f"{symbol or '-'} 익절 청산"
        elif raw_reason == "stop_loss":
            title = f"{symbol or '-'} 손절 청산"
        elif raw_reason == "progress_failed_close":
            title = f"{symbol or '-'} 진전부족 청산"
        elif raw_reason == "time_stop_close":
            title = f"{symbol or '-'} 시간종료 청산"
        elif raw_reason == "management_breakeven_close":
            title = f"{symbol or '-'} 본전보호 청산"
        elif raw_reason == "signal_flip_close":
            title = f"{symbol or '-'} 신호반전 청산"
        elif raw_reason == "regime_bias_lost_close":
            title = f"{symbol or '-'} 레짐상실 청산"
        elif raw_reason == "auto_risk_close":
            title = f"{symbol or '-'} 자동 리스크 청산"
        elif raw_reason == "close_all":
            title = "전체 포지션 종료"
        else:
            title = f"{symbol or '-'} 포지션 종료"
        main_text = humanize_reason_token(raw_reason)
        parts = []
        realized_pnl = fields.get("realized_pnl")
        if realized_pnl is not None:
            parts.append(f"realized={realized_pnl}")
        if fields.get("closed_qty") is not None:
            parts.append(f"qty={fields.get('closed_qty')}")
        outcome = str(fields.get("outcome") or "").strip()
        if outcome:
            parts.append(f"outcome={outcome}")
        sub_text = " / ".join(parts) if parts else None
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
