from __future__ import annotations

ACTION_LABELS_KO: dict[str, str] = {
    "blocked": "차단",
    "no_candidate": "대기",
    "risk_rejected": "리스크거부",
    "size_invalid": "수량오류",
    "executed": "실행완료",
    "dry_run": "모의실행",
    "execution_failed": "실행실패",
    "error": "오류",
    "hold": "대기",
    "enter": "진입",
    "close": "청산",
}

REASON_LABELS_KO: dict[str, str] = {
    "ops_paused": "운영 일시정지",
    "safe_mode": "안전모드",
    "position_open": "기존 포지션 보유중",
    "portfolio_symbol_open": "포트폴리오 동일 심볼 보유중",
    "portfolio_bucket_cap": "포트폴리오 버킷 한도 도달",
    "portfolio_cap_reached": "포트폴리오 최대 포지션 도달",
    "no_candidate": "현재 진입 후보가 없습니다",
    "no_candidate_multi": "복수 전략 후보가 정합성에서 탈락",
    "invalid_size": "유효하지 않은 주문 수량",
    "would_execute": "모의모드에서 실행 가능",
    "executed": "주문 실행 완료",
    "execution_failed": "주문 실행 실패",
    "risk_rejected": "리스크 검증에서 거부됨",
    "size_invalid": "수량 검증 실패",
    "tick_busy": "이미 판단 작업이 진행중",
    "cycle_failed": "사이클 실행 실패",
    "live_order_failed": "실주문 제출 실패",
    "bracket_failed": "TP/SL 브래킷 주문 실패",
    "no_entry": "진입 조건 미충족",
    "cooldown_active": "쿨다운 중",
    "daily_loss_limit": "일일 손실 제한 도달",
    "drawdown_limit": "드로우다운 제한 도달",
    "spread_block": "스프레드 과대",
    "edge_below_cost": "기대 수익 대비 비용 우위 부족",
    "confidence_below_threshold": "최소 신호 점수 미달",
    "gap_below_threshold": "신호 격차 미달",
    "risk_ok_scaled": "리스크 감속 적용",
    "signal_conflict": "전략 간 방향 충돌",
    "regime_conflict": "전략 간 레짐 충돌",
    "network_error": "네트워크 오류",
    "state_uncertain": "거래소 상태 정합성 불확실",
    "user_ws_stale": "프라이빗 스트림 freshness 초과",
    "market_data_stale": "마켓 데이터 freshness 초과",
    "recovery_required": "더티 재시작 복구 필요",
    "submit_recovery_required": "주문 제출 확정 복구 필요",
    "regime_missing": "레짐 판별 조건 미충족",
    "regime_adx_window_missing": "레짐 ADX 비교 구간 부족",
    "regime_adx_rising_missing": "레짐 ADX 상승 추세 조건 미충족",
    "bias_missing": "방향성 바이어스 조건 미충족",
    "trigger_missing": "트리거 조건 미충족",
    "missing_market": "시장 컨텍스트 데이터 미도착",
    "quality_score_missing": "확장 품질 점수 기준 미충족",
    "quality_score_v2_missing": "확장 품질 점수 V2 기준 미충족",
    "short_overextension_risk": "급락 과확장 추격 위험",
    "breakout_efficiency_missing": "돌파 효율 기준 미충족",
    "breakout_stability_missing": "돌파 안정성 기준 미충족",
    "breakout_stability_edge_missing": "돌파 안정성 엣지 기준 미충족",
    "volume_missing": "거래량 조건 미충족",
    "cost_missing": "비용 대비 기대값 부족",
    "partial_reduce_executed": "부분청산 실행",
    "partial_reduce_reprice": "부분청산 후 브래킷 재배치",
    "selective_extension_reprice": "연장 조건 충족으로 TP 재배치",
    "breakeven_reprice": "본전 보호 브래킷 재배치",
    "progress_extension_applied": "진전 확인으로 보유 연장",
    "selective_extension_activated": "선별 연장 조건 충족",
    "management_breakeven_close": "본전 보호 청산",
    "progress_failed_close": "진전 부족 청산",
    "time_stop_close": "시간 종료 청산",
    "take_profit": "익절 청산",
    "stop_loss": "손절 청산",
    "signal_flip_close": "신호 반전 청산",
    "regime_bias_lost_close": "레짐/바이어스 상실 청산",
    "signal_weakness_reduce": "신호 약화 감속",
    "weakness_reduce_reprice": "약화 감속 후 브래킷 재배치",
    "runner_lock_reprice": "러너 보호 브래킷 재배치",
}

REASON_PREFIX_LABELS_KO: dict[str, str] = {
    "min_hold_active:": "최소 보유 시간 조건이 활성화되어 있습니다",
    "sizing_blocked:": "주문 계산이 차단됨",
    "spread_too_wide_market_disabled:": "스프레드가 너무 넓어 시장가 주문이 비활성입니다",
    "daily_loss_limit_reached:": "일일 손실 제한에 걸림",
    "dd_limit_reached:": "DD 제한에 걸림",
    "notional_": "주문 기준금액이 제한 조건을 벗어났습니다",
    "cycle_failed:": "즉시 판단 실행 중 내부 오류가 발생했습니다",
    "bracket_failed:": "진입 후 TP/SL 브래킷 주문 생성에 실패했습니다",
    "no_candidate_multi:": "심볼별 후보 부재 사유",
    "no_entry:": "진입 조건 미충족",
}

NO_ENTRY_DETAIL_LABELS_KO: dict[str, str] = {
    "donchian": "돈치안 진입 조건 미충족",
    "pullback": "풀백 진입 조건 미충족",
    "mean_reversion": "평균회귀 진입 조건 미충족",
}


def _humanize_live_order_failed_detail(detail: str) -> str:
    normalized = str(detail or "").strip()
    if not normalized:
        return REASON_LABELS_KO["live_order_failed"]
    if normalized.startswith("BinanceRESTError:"):
        code = normalized.split(":")[-1].strip()
        if code == "-2019":
            return "실주문 제출 실패: 가용 마진 부족"
        if code == "-1111":
            return "실주문 제출 실패: 수량/가격 정밀도 오류"
        return f"실주문 제출 실패: 바이낸스 오류 ({code})"
    if normalized.startswith("insufficient_available_margin:"):
        return "실주문 제출 실패: 가용 마진 부족"
    return f"실주문 제출 실패: {normalized}"


def humanize_action_token(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value or value == "-":
        return "-"
    return ACTION_LABELS_KO.get(value, value)


def humanize_reason_token(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value or value == "-":
        return "-"
    if value.startswith("live_order_failed:"):
        return _humanize_live_order_failed_detail(value.split(":", 1)[1])
    direct = REASON_LABELS_KO.get(value)
    if direct is not None:
        return direct
    for prefix, label in REASON_PREFIX_LABELS_KO.items():
        if not value.startswith(prefix):
            continue
        detail = value[len(prefix) :].strip()
        if prefix == "no_entry:":
            if detail:
                return NO_ENTRY_DETAIL_LABELS_KO.get(detail, f"{label} ({detail})")
            return label
        if detail:
            return f"{label}: {detail}"
        return label
    return value
