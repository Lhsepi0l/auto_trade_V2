from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from v2.common.operator_labels import humanize_action_token, humanize_reason_token

if TYPE_CHECKING:
    from v2.control.api import RuntimeController


_ACTION_LABELS_KO: dict[str, str] = {
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


_REASON_LABELS_KO: dict[str, str] = {
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
}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def format_signed(value: float) -> str:
    return f"{float(value):+.4f}"


def position_side_label(position_amt: float) -> str:
    return "롱" if position_amt > 0 else "숏"


def translate_status_token(raw: str, labels: dict[str, str]) -> str:
    value = str(raw or "").strip()
    if not value or value == "-":
        return "-"
    direct = labels.get(value)
    if direct is not None:
        return direct
    head, sep, tail = value.partition(":")
    head_ko = labels.get(head)
    if head_ko is None:
        return value
    if sep:
        return f"{head_ko}:{tail}"
    return head_ko


def build_portfolio_slot_summary(portfolio: Any) -> str:
    if not isinstance(portfolio, dict):
        return "-"
    slots_used = int(_to_float(portfolio.get("slots_used"), default=0.0))
    slots_total = int(_to_float(portfolio.get("slots_total"), default=0.0))
    if slots_total <= 0:
        return "-"
    return f"{slots_used}/{slots_total}"


def build_status_pnl_summary(
    *,
    positions: list[tuple[str, float, float, float]],
    fills: Iterable[Any],
) -> tuple[str, str]:
    total_unrealized = 0.0
    per_symbol: list[str] = []
    position_labels: list[str] = []
    for symbol, position_amt, pnl, _entry_price in positions:
        total_unrealized += pnl
        per_symbol.append(f"{symbol}:{format_signed(pnl)}")
        position_labels.append(f"{symbol}[{position_side_label(position_amt)}]")

    parts = [f"미실현PnL={format_signed(total_unrealized)} USDT"]
    if per_symbol:
        preview = ", ".join(per_symbol[:3])
        if len(per_symbol) > 3:
            preview = f"{preview}, ..."
        parts.append(f"포지션별={preview}")

    latest_realized: float | None = None
    for fill in fills:
        realized_pnl = getattr(fill, "realized_pnl", None)
        if realized_pnl is None:
            continue
        latest_realized = _to_float(realized_pnl, default=0.0)
        break
    if latest_realized is not None:
        parts.append(f"최근실현PnL={format_signed(latest_realized)} USDT")

    position_summary = ", ".join(position_labels[:3]) if position_labels else "없음"
    if len(position_labels) > 3:
        position_summary = f"{position_summary}, ..."

    return position_summary, ", ".join(parts)


def build_status_summary(controller: RuntimeController) -> str:
    state_raw = str(controller.state_store.get().status)
    state_ko = {
        "RUNNING": "실행중",
        "PAUSED": "일시정지",
        "STOPPED": "중지",
        "KILLED": "강제중지",
    }.get(state_raw, state_raw)
    last_action = humanize_action_token(str(controller._last_cycle.get("last_action") or "-"))
    reason = humanize_reason_token(str(controller._last_cycle.get("last_decision_reason") or "-"))
    portfolio_summary = build_portfolio_slot_summary(controller._last_cycle.get("portfolio"))
    position_summary, pnl_summary = build_status_pnl_summary(
        positions=controller._status_positions_source(),
        fills=controller.state_store.get().last_fills,
    )
    return (
        "상태 알림: "
        f"엔진={state_ko}, 판단={last_action}, 사유={reason}, "
        f"포지션={position_summary}, 슬롯={portfolio_summary}, {pnl_summary}"
    )


def build_risk_response(risk_config: dict[str, Any]) -> dict[str, Any]:
    return risk_config


def build_scheduler_response(*, tick_sec: float, running: bool) -> dict[str, Any]:
    return {
        "tick_sec": float(tick_sec),
        "running": bool(running),
        "min_tick_sec": 1.0,
    }


def build_reconcile_response(
    *,
    state_uncertain: bool,
    state_uncertain_reason: str | None,
    startup_reconcile_ok: bool,
    last_reconcile_at: str | None,
) -> dict[str, Any]:
    return {
        "ok": not state_uncertain,
        "state_uncertain": bool(state_uncertain),
        "state_uncertain_reason": state_uncertain_reason,
        "startup_reconcile_ok": bool(startup_reconcile_ok),
        "last_reconcile_at": last_reconcile_at,
    }
