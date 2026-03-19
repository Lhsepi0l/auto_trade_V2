from __future__ import annotations


def build_operator_guidance() -> dict[str, object]:
    return {
        "panel_scope": [
            "엔진 시작/일시정지/패닉/즉시 판단, 복구(reconcile), 포지션 정리, 핵심 리스크 설정을 제어합니다.",
            "웹 패널이 일상 운영의 기본 콘솔이며, Discord는 필요 시만 켜는 선택형 백업 표면입니다.",
        ],
        "safety": [
            "실거래 전에는 mode/env와 readiness, stale 상태를 먼저 확인하세요.",
            "패닉/전체 종료/리스크 변경은 즉시 운영 상태에 영향을 주므로 현재 포지션과 차단 상태를 먼저 확인하세요.",
        ],
        "state_meanings": {
            "busy": "이전 판단 작업이 아직 끝나지 않았습니다. 잠시 후 다시 시도하세요.",
            "blocked": "현재 리스크/포지션/운영 상태 때문에 요청이 차단되었습니다.",
            "stale": "프라이빗 스트림 또는 마켓 데이터 freshness가 기준을 벗어났습니다.",
        },
        "first_checks": [
            "엔진 상태와 최근 판단 사유",
            "readiness/private 오류",
            "recovery 필요 여부와 마지막 reconcile 시각",
            "포지션/자본/차단 사유",
        ],
        "discord_fallback": [
            "Discord는 선택형 fallback 또는 emergency-use surface입니다.",
            "정상적인 일상 운영은 웹 패널에서 수행하고, Discord는 웹 접근 불가 시에만 사용합니다.",
        ],
    }
