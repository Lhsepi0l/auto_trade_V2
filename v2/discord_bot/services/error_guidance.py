from __future__ import annotations

import re

_ERROR_GUIDE_TABLE: list[tuple[str, str, str]] = [
    ("engine_in_panic", "엔진이 PANIC 상태입니다. 즉시 복구가 필요합니다.", "자동/수동 모드 상태를 확인하고 안전 모드에서 벗어난 뒤 조치하세요."),
    ("recovery_lock_active", "복구 락이 활성화되어 있습니다.", "복구 락이 해제될 때까지 진입/청산이 잠깐 중단됩니다."),
    ("ws_down_safe_mode", "WebSocket이 불안정해 안전 모드가 활성화되었습니다.", "네트워크/API 상태를 확인한 뒤 Binance WebSocket 재연결을 기다립니다."),
    ("multiple_open_positions_detected", "동일 계열 심볼에서 동일 방향 포지션이 2개 이상 감지됨", "동일 종목/방향 중복 포지션은 무시되고 추가 진입이 차단됩니다."),
    ("symbol_not_allowed", "요청한 심볼이 허용 목록에 없습니다.", "설정 파일의 enabled_symbols에 심볼을 추가하거나 심볼 필터를 점검하세요."),
    ("symbol_required", "심볼이 지정되지 않았습니다.", "BTCUSDT 또는 기본 심볼을 명시해 주세요."),
    ("quantity_below_min_qty", "수량이 최소 주문 수량보다 작습니다.", "거래소 LOT_SIZE 최소 수량/단위를 확인해 수량을 상향 조정하세요."),
    ("notional_below_min_notional", "명목가치가 최소 Notional 미달입니다.", "1회 최대 리스크 비중 또는 per_trade_risk_pct를 조정하세요."),
    ("min_qty", "주문 수량이 최소 수량 조건 미달입니다.", "해당 심볼의 minQty 규칙을 반영해 수량을 맞추세요."),
    ("hedge_mode_enabled", "헤지 모드가 활성화되어 있어 단방향 모드가 강제됩니다.", "거래 모드(ONEWAY)로 전환 후 재시도하세요."),
    ("adding_to_position_not_allowed", "포지션 추가 진입이 허용되지 않습니다.", "현재 규칙 또는 증거금 여유를 확인해 재설정하세요."),
    ("single_asset_rule_unresolved", "단일 자산 룰 충돌이 해결되지 않았습니다.", "룰 우선순위와 심볼 설정 충돌을 점검하세요."),
    ("risk_guard_failed", "리스크 가드 조건 미충족입니다.", "리스크 가드 파라미터(DD, drawdown, max_notional)를 완화하거나 조정하세요."),
    ("book_ticker_unavailable", "현재 책정 티커 조회가 불안정합니다.", "API 연결/권한/네트워크를 점검하세요."),
    ("market_fallback_blocked_by_spread_guard", "스프레드 가드로 인해 시장 fallback이 차단됩니다.", "spread_max_pct를 완화하거나 주문 방식을 조정하세요."),
    ("engine_not_running", "엔진이 RUNNING 상태가 아닙니다.", "엔진 로그를 확인 후 재시작 또는 수동 복구하세요."),
    ("binance_auth_error", "바이낸스 인증 오류", "API Key/Secret, IP 제한, 권한 설정을 점검하세요."),
]


def _normalize_error_code(err: str) -> str:
    return " ".join(str(err).strip().split()).lower()


def error_guidance(err: str) -> tuple[str, str, str] | None:
    text = _normalize_error_code(err)
    if not text:
        return None

    if text.startswith("engine_not_running:"):
        return (
            "engine_not_running",
            "엔진이 RUNNING 상태가 아닙니다.",
            "엔진 상태를 확인하고 재시작 후 재시도하세요.",
        )

    m = re.match(r"binance_http_(\d+)_code_(-?\d+)", text)
    if m:
        status = m.group(1)
        code = m.group(2)
        if status == "401":
            return (
                f"binance_http_{status}_code_{code}",
                "인증 실패(401)",
                "API Key/Secret 권한, IP 제한, API 토큰 상태를 점검하세요.",
            )
        if status == "403":
            return (
                f"binance_http_{status}_code_{code}",
                "권한 거부(403)",
                "거래소 API 권한, IP 화이트리스트, 계정 제약 조건을 점검하세요.",
            )
        if status == "429":
            return (
                f"binance_http_{status}_code_{code}",
                "요청 제한 초과(429)",
                "요청 간격을 늘려서 재시도하세요.",
            )
        if status and status.startswith("5"):
            return (
                f"binance_http_{status}_code_{code}",
                "바이낸스 서버 오류",
                "일시 장애 가능성이 있으니 잠시 후 재시도 후, 잔고/계정 상태를 확인하세요.",
            )
        return (
            f"binance_http_{status}_code_{code}",
            "예상치 못한 HTTP 오류",
            "요청 파라미터, 심볼, 네트워크 상태를 함께 점검하세요.",
        )

    for code, issue, action in _ERROR_GUIDE_TABLE:
        if code in text:
            return (code, issue, action)
    return None


_error_guidance = error_guidance

__all__ = ["error_guidance", "_error_guidance"]
