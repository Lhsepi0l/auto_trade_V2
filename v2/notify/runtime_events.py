from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from v2.common.operator_labels import humanize_reason_token

from .models import NotificationMessage


@dataclass(frozen=True)
class RuntimeNotificationContext:
    profile: str
    mode: str
    env: str

    @property
    def mode_label(self) -> str:
        return "실거래" if str(self.mode or "").strip().lower() == "live" else "모의"

    @property
    def identity_line(self) -> str:
        env = str(self.env or "").strip()
        if env:
            return f"{self.mode_label} | {env}"
        return self.mode_label


def _join_tags(*values: str) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in out:
            out.append(item)
    return tuple(out)


def _build_body(*lines: str | None) -> str:
    return "\n".join(str(line).strip() for line in lines if str(line or "").strip())


def _dedupe_key(*parts: Any) -> str:
    return ":".join(str(part or "").strip() for part in parts if str(part or "").strip())


def _position_side_label(side: Any) -> str:
    normalized = str(side or "").strip().upper()
    if normalized == "BUY":
        return "LONG"
    if normalized == "SELL":
        return "SHORT"
    return normalized


def _cycle_reason_text(*, reason: str, human_reason: str) -> str:
    normalized = str(reason or "").strip().lower()
    if normalized == "position_open":
        return "기존 포지션 관리 중"
    return human_reason


def _position_open_heartbeat_window_sec(*, notify_interval_sec: Any) -> float:
    try:
        base_interval = float(notify_interval_sec)
    except (TypeError, ValueError):
        base_interval = 30.0
    base_interval = max(base_interval, 1.0)
    # Keep position-management heartbeats infrequent enough for mobile push,
    # while preventing multi-hour silent gaps during long holds.
    return min(max(base_interval * 30.0, 900.0), 3600.0)


def _message_from_parts(
    *,
    title: str | None,
    main_text: str | None,
    sub_text: str | None,
    context: RuntimeNotificationContext,
    priority: str | int | None = None,
    tags: tuple[str, ...] = (),
    event_type: str | None = None,
    dedupe_key: str | None = None,
    suppress_window_sec: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> NotificationMessage | None:
    title = str(title or "").strip() or None
    main_text = str(main_text or "").strip() or None
    sub_text = str(sub_text or "").strip() or None
    body = _build_body(main_text, sub_text, context.identity_line)
    if not title and not body:
        return None
    return NotificationMessage(
        title=title,
        body=body,
        priority=priority,
        tags=tags,
        event_type=event_type,
        dedupe_key=dedupe_key,
        suppress_window_sec=suppress_window_sec,
        metadata=dict(metadata or {}),
    )


def _symbol_side_line(*, symbol: Any, side: Any) -> str | None:
    symbol_text = str(symbol or "").strip().upper()
    side_text = _position_side_label(side)
    parts = [part for part in (symbol_text, side_text) if part]
    if not parts:
        return None
    return " | ".join(parts)


def _entry_detail_line(*, fields: dict[str, Any]) -> str | None:
    parts: list[str] = []
    qty = fields.get("qty")
    if qty is not None:
        if isinstance(qty, (int, float)):
            parts.append(f"qty={qty:.6f}")
        else:
            parts.append(f"qty={qty}")
    leverage = fields.get("leverage")
    if leverage is not None:
        parts.append(f"lev={leverage}")
    notional = fields.get("notional")
    if notional is not None:
        parts.append(f"notional={notional}")
    entry_price = fields.get("entry_price")
    if entry_price is not None:
        parts.append(f"entry={entry_price}")
    return " / ".join(parts) if parts else None


def build_runtime_event_notification(
    *,
    event: str,
    fields: dict[str, Any],
    context: RuntimeNotificationContext,
) -> NotificationMessage | None:
    raw_event = str(event or "").strip()
    if not raw_event:
        return None

    if raw_event == "runtime_start":
        return _message_from_parts(
            title="엔진 시작",
            main_text="엔진이 실행 상태로 전환되었습니다.",
            sub_text=None,
            context=context,
            priority=3,
            tags=(),
            event_type=raw_event,
            metadata=dict(fields),
        )
    if raw_event == "runtime_stop":
        return _message_from_parts(
            title="엔진 일시정지",
            main_text="엔진이 일시정지 상태로 전환되었습니다.",
            sub_text=None,
            context=context,
            priority=3,
            tags=(),
            event_type=raw_event,
            metadata=dict(fields),
        )
    if raw_event == "panic_triggered":
        return _message_from_parts(
            title="패닉 트리거",
            main_text="패닉 정리 절차가 시작되었습니다.",
            sub_text=None,
            context=context,
            priority=5,
            tags=_join_tags("rotating_light", "warning"),
            event_type=raw_event,
            metadata=dict(fields),
        )
    if raw_event == "risk_trip":
        return _message_from_parts(
            title="자동 리스크 트립",
            main_text=humanize_reason_token(str(fields.get("reason") or "risk_trip")),
            sub_text=None,
            context=context,
            priority=5,
            tags=_join_tags("warning", "chart_with_downwards_trend"),
            event_type=raw_event,
            dedupe_key=_dedupe_key("risk_trip", fields.get("reason")),
            suppress_window_sec=300.0,
            metadata=dict(fields),
        )
    if raw_event == "stale_transition":
        if not bool(fields.get("stale")):
            return None
        stale_type = str(fields.get("stale_type") or "").strip().lower()
        stale_target = "프라이빗 스트림" if stale_type == "user_ws" else "시장 데이터"
        age_sec = fields.get("age_sec")
        return _message_from_parts(
            title=f"{stale_target} 지연",
            main_text="갱신 지연 감지",
            sub_text=None if age_sec is None else f"age={age_sec}초",
            context=context,
            priority=3,
            tags=(),
            event_type=raw_event,
            dedupe_key=_dedupe_key("stale", stale_type),
            suppress_window_sec=180.0,
            metadata=dict(fields),
        )
    if raw_event == "ready_transition":
        if bool(fields.get("ready")):
            return None
        blockers: list[str] = []
        if bool(fields.get("recovery_required")):
            blockers.append("복구 필요")
        if not bool(fields.get("submission_recovery_ok", True)):
            blockers.append("주문 복구 확인 필요")
        if bool(fields.get("user_ws_stale")):
            blockers.append("프라이빗 스트림 stale")
        if bool(fields.get("market_data_stale")):
            blockers.append("시장 데이터 stale")
        return _message_from_parts(
            title="운영 준비 대기",
            main_text="운영 준비 미완료",
            sub_text=", ".join(blockers) if blockers else "준비도 조건 재확인 필요",
            context=context,
            priority=3,
            tags=(),
            event_type=raw_event,
            dedupe_key="ready_transition:not_ready",
            suppress_window_sec=180.0,
            metadata=dict(fields),
        )
    if raw_event == "recovery_transition":
        if not bool(fields.get("recovery_required")):
            return None
        return _message_from_parts(
            title="복구 확인 필요",
            main_text=humanize_reason_token(str(fields.get("reason") or "recovery_required")),
            sub_text=None,
            context=context,
            priority=3,
            tags=_join_tags("tools"),
            event_type=raw_event,
            dedupe_key=_dedupe_key("recovery_required", fields.get("reason")),
            suppress_window_sec=300.0,
            metadata=dict(fields),
        )
    if raw_event == "uncertainty_transition":
        if not bool(fields.get("state_uncertain")):
            return None
        return _message_from_parts(
            title="상태 확인 필요",
            main_text=humanize_reason_token(str(fields.get("reason") or "state_uncertain")),
            sub_text=None,
            context=context,
            priority=3,
            tags=(),
            event_type=raw_event,
            dedupe_key=_dedupe_key("state_uncertain", fields.get("reason")),
            suppress_window_sec=300.0,
            metadata=dict(fields),
        )
    if raw_event == "user_stream_disconnect":
        return _message_from_parts(
            title="프라이빗 스트림 끊김",
            main_text=humanize_reason_token(str(fields.get("reason") or "user_stream_disconnect")),
            sub_text=None,
            context=context,
            priority=4,
            tags=_join_tags("satellite"),
            event_type=raw_event,
            dedupe_key=_dedupe_key("user_stream_disconnect", fields.get("reason")),
            suppress_window_sec=180.0,
            metadata=dict(fields),
        )
    if raw_event == "position_entry_opened":
        symbol_text = str(fields.get("symbol") or "").strip().upper()
        side_text = _position_side_label(fields.get("side"))
        action_state = str(fields.get("action") or "").strip().lower()
        title_suffix = "모의 진입" if action_state == "dry_run" else "진입"
        title = f"{symbol_text} {side_text} {title_suffix}".strip()
        if not symbol_text and not side_text:
            title = "모의 진입" if action_state == "dry_run" else "진입 오픈"
        return _message_from_parts(
            title=title,
            main_text="모의 주문 실행 완료" if action_state == "dry_run" else "주문 실행 완료",
            sub_text=_entry_detail_line(fields=fields),
            context=context,
            priority=4 if action_state != "dry_run" else 3,
            tags=(),
            event_type=raw_event,
            metadata=dict(fields),
        )
    if raw_event == "cycle_result":
        return build_cycle_result_notification(fields=fields, context=context)
    return None


def build_cycle_result_notification(
    *,
    fields: dict[str, Any],
    context: RuntimeNotificationContext,
) -> NotificationMessage | None:
    action = str(fields.get("action") or "").strip()
    reason = str(fields.get("reason") or "").strip()
    trigger_source = str(fields.get("trigger_source") or "scheduler").strip().lower()
    symbol_line = _symbol_side_line(
        symbol=fields.get("candidate_symbol"),
        side=fields.get("candidate_side"),
    )
    human_reason = humanize_reason_token(reason)
    title_prefix = "즉시 판단" if trigger_source == "manual_tick" else "실시간 판단"

    if action in {"executed", "dry_run"}:
        if symbol_line is not None:
            return None
        title = f"{title_prefix} 진입" if trigger_source == "manual_tick" else "진입 오픈"
        if action == "dry_run":
            title = f"{title_prefix} 모의실행" if trigger_source == "manual_tick" else "모의 진입"
        body = _build_body(
            symbol_line or human_reason,
            None if symbol_line is None else human_reason,
            context.identity_line,
        )
        return NotificationMessage(
            title=title,
            body=body,
            priority=4 if trigger_source == "manual_tick" else 3,
            tags=(),
            event_type="cycle_result",
            dedupe_key=None,
            suppress_window_sec=None,
            metadata=dict(fields),
        )

    if action == "no_candidate":
        body = _build_body(symbol_line, human_reason, context.identity_line)
        return NotificationMessage(
            title=f"{title_prefix} 대기",
            body=body,
            priority=2,
            tags=(),
            event_type="cycle_result",
            dedupe_key=None,
            suppress_window_sec=None,
            metadata=dict(fields),
        )

    if action in {"blocked", "risk_rejected"}:
        if reason == "position_open" and trigger_source == "scheduler":
            return NotificationMessage(
                title="포지션 관리중",
                body=_build_body(
                    symbol_line,
                    _cycle_reason_text(reason=reason, human_reason=human_reason),
                    context.identity_line,
                ),
                priority=2,
                tags=(),
                event_type="cycle_result",
                dedupe_key=_dedupe_key("cycle_result", "scheduler", "position_open"),
                suppress_window_sec=_position_open_heartbeat_window_sec(
                    notify_interval_sec=fields.get("notify_interval_sec")
                ),
                metadata=dict(fields),
            )
        title = "포지션 관리중" if reason == "position_open" else f"{title_prefix} 보류"
        body = _build_body(
            symbol_line,
            _cycle_reason_text(reason=reason, human_reason=human_reason),
            context.identity_line,
        )
        return NotificationMessage(
            title=title,
            body=body,
            priority=2 if reason == "position_open" else 3,
            tags=(),
            event_type="cycle_result",
            dedupe_key=None,
            suppress_window_sec=None,
            metadata=dict(fields),
        )

    if action in {"execution_failed", "error"}:
        title = f"{title_prefix} 실패"
        if action == "execution_failed":
            title = "즉시 진입 실패" if trigger_source == "manual_tick" else "실시간 진입 실패"
        body = _build_body(
            None if symbol_line is None else f"진입 시도: {symbol_line}",
            human_reason,
            context.identity_line,
        )
        return NotificationMessage(
            title=title,
            body=body,
            priority=4,
            tags=_join_tags("warning"),
            event_type="cycle_result",
            dedupe_key=None,
            suppress_window_sec=None,
            metadata=dict(fields),
        )

    return None


def build_bracket_exit_notification(
    *,
    symbol: str,
    outcome: str,
    realized_pnl: float | None,
    context: RuntimeNotificationContext,
) -> NotificationMessage:
    normalized_symbol = str(symbol or "").strip().upper() or "-"
    realized = realized_pnl
    if realized is not None and abs(realized) < 0.00005:
        realized = 0.0

    if realized is None:
        title = "익절 완료!" if outcome == "TP" else "손절 완료!"
        body = _build_body(
            f"{normalized_symbol} | 실현PnL 집계중",
            context.identity_line,
        )
    elif realized > 0.0:
        title = "익절 완료!"
        body = _build_body(
            f"{normalized_symbol} | {realized:+.4f} USDT",
            context.identity_line,
        )
    elif realized < 0.0:
        title = "손절 완료!"
        body = _build_body(
            f"{normalized_symbol} | {realized:+.4f} USDT",
            context.identity_line,
        )
    else:
        title = "손익없음 청산!"
        body = _build_body(
            f"{normalized_symbol} | 0.0000 USDT",
            context.identity_line,
        )

    priority = 4 if outcome == "SL" else 3
    tags = _join_tags("chart_with_downwards_trend" if outcome == "SL" else "moneybag")
    return NotificationMessage(
        title=title,
        body=body,
        priority=priority,
        tags=tags,
        event_type="trade_exit",
        dedupe_key=None,
        suppress_window_sec=None,
        metadata={
            "symbol": normalized_symbol,
            "outcome": outcome,
            "realized_pnl": realized,
        },
    )


def build_position_close_notification(
    *,
    symbol: str,
    reason: str,
    context: RuntimeNotificationContext,
) -> NotificationMessage:
    normalized_symbol = str(symbol or "").strip().upper() or "-"
    raw_reason = str(reason or "forced_close").strip().lower()
    if raw_reason == "panic_close":
        title = "패닉 청산"
        detail = "패닉 정리 완료"
        priority = 5
    elif raw_reason == "auto_risk_close":
        title = "자동 리스크 청산"
        detail = "리스크 회로 차단 후 정리"
        priority = 5
    elif raw_reason == "stoploss_forced_close":
        title = "SL 후 잔여 정리"
        detail = "손절 후 잔여 포지션 정리"
        priority = 4
    else:
        title = "강제 청산"
        detail = "운영자 강제 정리"
        priority = 4

    return NotificationMessage(
        title=title,
        body=_build_body(f"{normalized_symbol} | {detail}", context.identity_line),
        priority=priority,
        tags=_join_tags("warning"),
        event_type="trade_close",
        dedupe_key=None,
        suppress_window_sec=None,
        metadata={"symbol": normalized_symbol, "reason": raw_reason},
    )


def build_report_notification(
    *,
    payload: dict[str, Any],
    context: RuntimeNotificationContext,
) -> NotificationMessage:
    detail_raw = payload.get("detail")
    detail = detail_raw if isinstance(detail_raw, dict) else {}
    entries = int(detail.get("entries") or 0)
    closes = int(detail.get("closes") or 0)
    errors = int(detail.get("errors") or 0)
    blocks = int(detail.get("blocks") or 0)
    engine_state = str(payload.get("engine_state") or "-").strip()

    return NotificationMessage(
        title="일일 리포트",
        body=_build_body(
            f"상태={engine_state} | 진입/청산={entries}/{closes}",
            f"오류/차단={errors}/{blocks}",
            context.identity_line,
        ),
        priority=2,
        tags=_join_tags("memo"),
        event_type=str(payload.get("kind") or "DAILY_REPORT"),
        dedupe_key=None,
        suppress_window_sec=None,
        metadata=dict(payload),
    )


def build_runtime_boot_notification(
    *,
    context: RuntimeNotificationContext,
) -> NotificationMessage:
    return NotificationMessage(
        title="런타임 부팅 완료",
        body=_build_body("standalone runtime 초기화 완료", context.identity_line),
        priority=2,
        tags=_join_tags("rocket"),
        event_type="runtime_boot",
        dedupe_key=_dedupe_key("runtime_boot", context.profile, context.mode, context.env),
        suppress_window_sec=120.0,
        metadata={
            "profile": context.profile,
            "mode": context.mode,
            "env": context.env,
        },
    )
