from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from apps.trader_engine.services.notifier_service import _error_guidance

_SCORING_TIMEFRAME_ORDER = ("10m", "15m", "30m", "1h", "4h")


def _fmt_money(x: Any, *, digits: int = 4) -> str:
    try:
        return f"{float(x):.{digits}f}"
    except Exception:
        return str(x)


def _fmt_int(x: Any) -> str:
    try:
        return str(int(x))
    except Exception:
        return str(x)


def _fmt_pct(x: Any) -> str:
    try:
        v = float(x)
        return f"{v:.2f}%"
    except Exception:
        return str(x)


def _fmt_time(ts: Any) -> str:
    if not ts:
        return "-"
    if isinstance(ts, str):
        return ts
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def _truncate(s: str, *, limit: int = 1900) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _parse_ts(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            return None
    return None


def _next_tick_eta(payload: Dict[str, Any]) -> str:
    sched = _as_dict(payload.get("scheduler"))
    tick_sec = float(sched.get("tick_sec") or 1800.0)
    base_ts = sched.get("tick_finished_at") or sched.get("tick_started_at")
    if base_ts is None:
        return "확인 필요"
    base = _parse_ts(base_ts)
    if base is None:
        return "확인 필요"
    next_tick = base + timedelta(seconds=max(tick_sec, 1.0))
    remain = int((next_tick - datetime.now(timezone.utc)).total_seconds())
    if remain < 0:
        remain = 0
    return f"{remain // 60}분 {remain % 60}초 후"


def _regime_to_kor(raw_regime: Any) -> tuple[str, str]:
    code = str(raw_regime or "").strip().upper()
    if not code or code in {"NONE", "UNKNOWN", "-"}:
        return "미판단", "-"
    if code in {"BEAR", "DOWN", "D"}:
        return "하락", "BEAR"
    if code in {"BULL", "UP", "U"}:
        return "상승", "BULL"
    if code in {"NEUTRAL", "FLAT", "SIDEWAYS", "N"}:
        return "횡보", code
    return "기타", code


def _candidate_reject_stage_to_kor(raw_stage: Any) -> str:
    stage = str(raw_stage or "").strip()
    stage_map = {
        "universe_empty": "심볼 후보군 비어 있음",
        "scoring_error": "심볼 스코어 계산 오류",
        "timeframe_coverage": "심볼 유효 시간봉 부족",
        "universe_filtered": "유니버스 필터에서 탈락",
        "selection_empty": "선택 후보 없음",
        "short_filtered": "숏 진입 제한 필터 탈락",
        "confidence_filter": "신뢰도 임계치 미달",
        "gap_filter": "스코어 갭 임계치 미달",
        "selection_filtered": "최종 선택 필터 탈락",
    }
    return stage_map.get(stage, "탈락 단계 미확인")


def _reason_to_kor(raw_reason: Any) -> str:
    reason = str(raw_reason or "").strip()
    if not reason:
        return "-"
    reason_map: Dict[str, str] = {
        "no_candidate": "진입 후보가 없어 판단을 건너뜁니다.",
        "vol_shock_no_entry": "변동성 급등 구간이라 신규 진입이 보류됩니다.",
        "confidence_below_threshold": "신뢰도 점수가 기준치 아래여서 진입을 보류합니다.",
        "short_not_allowed_regime": "현재 구간에서는 숏 진입이 제한됩니다.",
        "enter_candidate": "진입 후보를 확인해 주문 준비로 진행합니다.",
        "vol_shock_close": "변동성 급등으로 청산 판단이 보류되었습니다.",
        "profit_hold": "익절 트리거 미달로 포지션을 유지합니다.",
        "same_symbol": "현재 보유 심볼과 후보 심볼이 같아 중복 진입을 생략합니다.",
        "gap_below_threshold": "점수 차이가 기준치 이하라 판단을 생략합니다.",
        "rebalance_to_better_candidate": "더 유리한 후보로 리밸런싱 예정입니다.",
        "close_symbol_missing": "종료 심볼 정보를 찾을 수 없습니다.",
        "enter_symbol_missing": "진입 심볼 정보를 찾을 수 없습니다.",
        "price_unavailable": "가격 데이터를 확인할 수 없습니다.",
        "cooldown_active": "쿨다운 상태여서 판단이 보류됩니다.",
        "daily_loss_limit_reached": "일일 손실 한도에 걸려 진입이 중단되었습니다.",
        "dd_limit_reached": "DD 한도에 걸려 진입이 중단되었습니다.",
        "lose_streak_cooldown": "연패 중이라 일시 중단됩니다.",
        "equity_unavailable": "자산(Equity) 데이터가 유효하지 않습니다.",
        "leverage_above_max_leverage": "레버리지가 허용 범위를 초과합니다.",
        "single_asset_rule_violation": "단일 자산 룰 충돌로 주문이 차단됩니다.",
        "exposure_above_max_exposure": "총 노출 한도 초과로 주문이 차단됩니다.",
        "notional_above_max_notional": "주문 기준금액이 최대 허용 한도를 초과했습니다.",
        "notional_unavailable": "명목 금액 계산값이 없습니다.",
        "per_trade_risk_exceeded": "1회 위험 한도를 초과했습니다.",
        "book_unavailable_market_disabled": "호가창 데이터가 유효하지 않습니다.",
    }

    for key in reason_map:
        if reason.startswith(key):
            return reason_map[key]
    return reason


def _format_scoring_weights(weights: Dict[str, float]) -> str:
    items: List[str] = []
    for tf in _SCORING_TIMEFRAME_ORDER:
        if tf not in weights:
            continue
        try:
            pct = float(weights[tf]) * 100.0
        except Exception:
            continue
        items.append(f"{tf}:{pct:.1f}%")
    if items:
        return ", ".join(items)
    return "-"


def _format_top_counts(counts: Dict[str, Any], *, limit: int = 6) -> str:
    if not counts:
        return "-"
    try:
        items = list(counts.items())
        sorted_items = sorted(
            [(k, int(v)) for k, v in items if int(v) > 0],
            key=lambda x: x[1],
            reverse=True,
        )
        return ", ".join(f"{k}:{v}" for k, v in sorted_items[:limit])
    except Exception:
        return str(counts)


def _translate_rejection_map(reasons: Dict[str, Any]) -> Dict[str, str]:
    translated: Dict[str, str] = {}
    for k, v in reasons.items():
        key = str(k)
        if key == "symbols_seen":
            translated["symbol_candidates"] = f"{_fmt_int(v)}"
        elif key == "scored":
            translated["scored_symbols"] = f"{_fmt_int(v)}"
        elif key == "skipped_no_usable_timeframes":
            translated["유효TF_부족"] = f"{_fmt_int(v)}"
        elif key == "skipped_scoring_exception":
            translated["점수계산_예외"] = f"{_fmt_int(v)}"
        elif key.startswith("tf_no_candles_"):
            translated[f"{key.replace('tf_no_candles_', '').upper()}_봉부족"] = f"{_fmt_int(v)}"
        elif key.startswith("tf_insufficient_bars_"):
            translated[f"{key.replace('tf_insufficient_bars_', '').upper()}_바수부족"] = f"{_fmt_int(v)}"
        elif key.startswith("tf_not_configured_"):
            translated[f"{key.replace('tf_not_configured_', '').upper()}_미사용"] = f"{_fmt_int(v)}"
        else:
            translated[key] = f"{_fmt_int(v)}"
    return translated


def _format_scan_stats(stats: Dict[str, Any]) -> str:
    if not stats:
        return "-"
    requested = stats.get("requested_symbols")
    scored = stats.get("scored_symbols")
    usable_tfs = stats.get("scoring_timeframes")
    min_bars = stats.get("min_bars_factor")
    parts: List[str] = []
    if requested is not None:
        parts.append(f"요청 {requested}")
    if scored is not None:
        parts.append(f"채점 {scored}")
    if usable_tfs is not None:
        if isinstance(usable_tfs, (list, tuple)):
            parts.append(f"TF {len(usable_tfs)}개({', '.join(usable_tfs)})")
        else:
            parts.append(f"TF {usable_tfs}")
    if min_bars is not None:
        parts.append(f"min_bars_factor {_fmt_money(min_bars, digits=3)}")
    if stats.get("score_scan_weights"):
        parts.append(f"가중치{_format_scoring_weights(_as_dict(stats.get('score_scan_weights')))}")
    return ", ".join(parts) if parts else str(stats)


def _format_candidate_scores(scores: Dict[str, float]) -> str:
    if not scores:
        return "-"
    items: List[str] = []
    for tf in _SCORING_TIMEFRAME_ORDER:
        if tf not in scores:
            continue
        try:
            v = float(scores[tf])
        except Exception:
            continue
        sign = "+" if v >= 0 else ""
        items.append(f"{tf}:{sign}{v:.3f}")
    if items:
        return ", ".join(items)
    return "-"


def _position_side(amount: Any, side_hint: Any = None) -> str:
    if side_hint is not None:
        s = str(side_hint).strip().upper()
        if s in {"LONG", "BUY", "롱"}:
            return "롱"
        if s in {"SHORT", "SELL", "숏"}:
            return "숏"
    try:
        v = float(amount)
    except Exception:
        return "-"
    if v > 0:
        return "롱"
    if v < 0:
        return "숏"
    return "-"


def _first_position(positions: Any) -> tuple[str, str, float, str]:
    if not isinstance(positions, dict):
        return "-", "-", 0.0, "-"

    fallback_symbol = "-"
    fallback_unrealized = 0.0
    fallback_side = "-"
    for sym, row in positions.items():
        if not isinstance(row, dict):
            continue
        if fallback_symbol == "-":
            fallback_symbol = str(sym)
            fallback_side = _position_side(row.get("position_amt"), row.get("position_side"))
            try:
                fallback_unrealized = float(row.get("unrealized_pnl") or 0.0)
            except Exception:
                fallback_unrealized = 0.0

        try:
            amt = float(row.get("position_amt") or 0.0)
        except Exception:
            continue
        if abs(amt) > 1e-12:
            try:
                unrealized = float(row.get("unrealized_pnl") or 0.0)
            except Exception:
                unrealized = 0.0
            return str(sym), _fmt_money(abs(amt)), unrealized, _position_side(amt, row.get("position_side"))

    if fallback_symbol == "-":
        return "-", "-", 0.0, "-"
    return fallback_symbol, _fmt_money(0.0), fallback_unrealized, fallback_side


def _collect_unrealized(positions: Any, pnl: Dict[str, Any]) -> float:
    total = 0.0
    if isinstance(positions, dict):
        for row in positions.values():
            if isinstance(row, dict):
                try:
                    total += float(row.get("unrealized_pnl") or 0.0)
                except Exception:
                    continue
    if total != 0:
        return total

    if isinstance(pnl, dict):
        try:
            return float(pnl.get("last_unrealized_pnl_usdt") or 0.0)
        except Exception:
            pass
    return 0.0


def format_status_payload(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return _truncate(f"status payload type error: {type(payload).__name__}")

    engine = _as_dict(payload.get("engine_state"))
    binance = _as_dict(payload.get("binance"))
    pnl = _as_dict(payload.get("pnl"))
    sched = _as_dict(payload.get("scheduler"))
    risk = _as_dict(payload.get("risk_config"))
    summary = _as_dict(payload.get("config_summary"))
    capital = _as_dict(payload.get("capital_snapshot"))
    watchdog = _as_dict(payload.get("watchdog"))

    state = str(engine.get("state", "UNKNOWN"))
    enabled_symbols = [str(x) for x in (binance.get("enabled_symbols") or []) if str(x).strip()]

    positions = binance.get("positions")
    pos_symbol, pos_qty, pos_unrealized, pos_side = _first_position(positions)
    unrealized_sum = _collect_unrealized(positions, pnl)
    if unrealized_sum == 0 and pos_unrealized != 0:
        unrealized_sum = pos_unrealized

    daily_pnl = pnl.get("daily_pnl_pct") if isinstance(pnl, dict) else None
    if daily_pnl is None and isinstance(pnl, dict):
        daily_pnl = pnl.get("daily_realized_pnl")
    dd = pnl.get("drawdown_pct") if isinstance(pnl, dict) else None

    cand = _as_dict(sched.get("candidate") or sched.get("last_candidate"))
    candidate_symbol = str(cand.get("symbol") or "-")
    regime_raw = sched.get("regime_4h") or cand.get("regime_4h") or sched.get("last_regime")
    regime_kor, regime_code = _regime_to_kor(regime_raw)
    scoring_weights = _as_dict(sched.get("scoring_weights") or summary.get("scoring_weights") or {})
    candidate_score_by_timeframe = dict(sched.get("candidate_score_by_timeframe") or summary.get("candidate_score_by_timeframe") or {})
    active_scoring_tfs = [str(tf) for tf in (sched.get("active_scoring_timeframes") or summary.get("active_scoring_timeframes") or [])]
    scoring_scan_stats = _as_dict(sched.get("scoring_scan_stats") or summary.get("scoring_scan_stats") or {})
    scoring_rejection_reasons = _as_dict(
        sched.get("scoring_rejection_reasons") or summary.get("scoring_rejection_reasons") or {}
    )
    candidate_selection_reasons = _as_dict(
        sched.get("candidate_selection_reasons") or summary.get("candidate_selection_reasons") or {}
    )
    scoring_drift_detected = bool(sched.get("scoring_drift_detected") or summary.get("scoring_drift_detected"))
    scoring_drift_details = (
        sched.get("scoring_drift_details")
        or summary.get("scoring_drift_details")
        or []
    )
    scoring_rejection_hotspot = str(
        sched.get("scoring_rejection_hotspot") or summary.get("scoring_rejection_hotspot") or "-"
    )
    candidate_reject_stage = str(
        sched.get("candidate_reject_stage") or summary.get("candidate_reject_stage") or ""
    )
    candidate_rejection_hotspot = str(
        sched.get("candidate_rejection_hotspot") or summary.get("candidate_rejection_hotspot") or "-"
    )
    scoring_setup_signature = _as_dict(sched.get("scoring_setup_signature") or summary.get("scoring_setup_signature") or {})
    last_scoring_validation_ts = sched.get("last_scoring_validation_ts") or summary.get("last_scoring_validation_ts")

    if not active_scoring_tfs and scoring_weights:
        active_scoring_tfs = [tf for tf in _SCORING_TIMEFRAME_ORDER if tf in scoring_weights]

    if candidate_symbol == "-":
        uni = risk.get("universe_symbols")
        if isinstance(uni, list) and uni:
            candidate_symbol = str(uni[0])

    decision_raw = sched.get("last_decision_reason")
    decision_code = str(decision_raw or "-").strip()
    decision_human = _reason_to_kor(decision_raw)

    last_action = str(sched.get("last_action") or "-")
    last_error = payload.get("last_error")

    lines: List[str] = []
    lines.append("[상태 알림]")
    lines.append(f"엔진 상태: {state}")
    if pos_symbol == "-":
        lines.append("현재 포지션: -")
    else:
        side_label = f" [{pos_side}]" if pos_side != "-" else ""
        lines.append(f"현재 포지션: {pos_symbol}{side_label} (수량 {pos_qty})")

    lines.append(
        "손익 요약: "
        f"미실현손익(uPnL) {_fmt_money(unrealized_sum)} USDT, "
        f"일일손익 {_fmt_pct(daily_pnl) if daily_pnl is not None else '-'}, "
        f"DD {_fmt_pct(dd) if dd is not None else '-'}"
    )
    lines.append(f"시장 판단: 레짐 {regime_kor}({regime_code}), 후보 심볼 {candidate_symbol}")

    if decision_code == "-":
        lines.append("이번 결정: -")
    else:
        lines.append(f"이번 결정: {decision_code} -> {decision_human}")
        if decision_code.startswith("profit_hold"):
            arm = summary.get("trail_arm_pnl_pct")
            dist = summary.get("trail_distance_pnl_pct")
            if arm is not None or dist is not None:
                arm_text = f"{_fmt_pct(arm)}" if arm is not None else "-"
                dist_text = f"{_fmt_pct(dist)}" if dist is not None else "-"
                lines.append(f"익절 트리거: ARM {arm_text}, 롤백거리 {dist_text} (트레일링 기준)")

    if active_scoring_tfs:
        lines.append(f"평가 기준봉: {', '.join(active_scoring_tfs)}")
    weight_line = _format_scoring_weights(scoring_weights)
    if weight_line != "-":
        lines.append(f"기준봉 가중치: {weight_line}")

    if scoring_drift_detected:
        drift_list = [
            str(x)
            for x in scoring_drift_details
            if x is not None and str(x).strip()
        ]
        if drift_list:
            lines.append(f"스코어링 설정 검증: ⚠ {', '.join(drift_list)}")
        else:
            lines.append("스코어링 설정 검증: ⚠ 최근 설정 반영 점검 필요")

    if scoring_setup_signature:
        setup_tfs = scoring_setup_signature.get("timeframes")
        setup_weights = scoring_setup_signature.get("weights")
        setup_min_bars = scoring_setup_signature.get("min_bars_factor")
        setup_conf = scoring_setup_signature.get("score_conf_threshold")
        setup_gap = scoring_setup_signature.get("score_gap_threshold")
        setup_parts = []
        if setup_tfs:
            setup_parts.append(f"TF={','.join([str(x) for x in setup_tfs])}")
        if isinstance(setup_weights, dict) and setup_weights:
            setup_weights_norm = {
                str(k): v for k, v in setup_weights.items() if _safe_float(v) is not None
            }
            setup_parts.append(f"가중치={_format_scoring_weights(setup_weights_norm)}")
        if setup_min_bars is not None:
            setup_parts.append(f"min_bars={_fmt_money(setup_min_bars, digits=3)}")
        if setup_conf is not None and setup_gap is not None:
            setup_parts.append(f"임계치=conf({_fmt_pct(setup_conf)}, gap({_fmt_pct(setup_gap)})")
        if setup_parts:
            lines.append(f"스코어링 검증: {', '.join(setup_parts)}")
        if last_scoring_validation_ts:
            lines.append(f"마지막 검증: {_fmt_time(last_scoring_validation_ts)}")

    scan_text = _format_scan_stats(scoring_scan_stats)
    if scan_text != "-":
        lines.append(f"스코어 스캔: {scan_text}")

    rejected = _translate_rejection_map(scoring_rejection_reasons)
    rejected_text = _format_top_counts({k: int(v) for k, v in (rejected or {}).items()})
    if rejected_text != "-":
        lines.append(f"심볼 탈락 히트맵: {rejected_text}")

    selection_text = _format_top_counts(candidate_selection_reasons)
    if selection_text != "-":
        lines.append(f"선택 탈락 히트맵: {selection_text}")

    if candidate_reject_stage:
        lines.append(f"탈락 단계: {_candidate_reject_stage_to_kor(candidate_reject_stage)} ({candidate_reject_stage})")
    if scoring_rejection_hotspot != "-":
        lines.append(f"심볼 탈락 상위 원인: {scoring_rejection_hotspot}")
    if candidate_rejection_hotspot != "-":
        lines.append(f"선택 탈락 상위 원인: {candidate_rejection_hotspot}")

    if decision_code == "no_candidate" and candidate_reject_stage:
        lines.append(f"no_candidate 사유: {_candidate_reject_stage_to_kor(candidate_reject_stage)}")

    candidate_scores = _format_candidate_scores(candidate_score_by_timeframe)
    if candidate_symbol != "-" and candidate_scores != "-":
        lines.append(f"후보 TF 점수: {candidate_scores}")

    lines.append(f"최근 액션: {last_action}")

    if enabled_symbols:
        lines.append(f"운영 심볼: {', '.join(enabled_symbols)}")

    if capital:
        budget = capital.get("budget_usdt")
        notional = capital.get("notional_usdt")
        blocked = capital.get("blocked")
        if budget is not None:
            lines.append(f"증거금: {_fmt_money(budget)} USDT")
        if notional is not None:
            lines.append(f"예상 주문금액: {_fmt_money(notional)} USDT")
        if blocked is not None:
            lines.append(f"예산 차단: {'예' if bool(blocked) else '아니오'}")

    lines.append(f"다음 판단: {_next_tick_eta(payload)}")

    ls = pnl.get("lose_streak") if isinstance(pnl, dict) else None
    cooldown_until = pnl.get("cooldown_until") if isinstance(pnl, dict) else None
    if ls is not None:
        lines.append(f"연패: {ls}")
    if cooldown_until:
        lines.append(f"쿨다운 해제 시각: {_fmt_time(cooldown_until)}")

    if watchdog.get("last_blocked_symbol"):
        lines.append(f"차단 심볼: {watchdog.get('last_blocked_symbol')}")

    if last_error:
        err_text = str(last_error)
        lines.append(f"오류: {err_text}")
        guide = _error_guidance(err_text)
        if guide is not None:
            code, issue, action = guide
            lines.append(f"권장 대응: {code} - {issue}")
            lines.append(f"대응: {action}")

    return _truncate("\n".join(lines))


def format_report_payload(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return _truncate(f"report payload type error: {type(payload).__name__}")

    day = str(payload.get("day") or "-")
    engine_state = str(payload.get("engine_state") or "-")
    reported_at = _fmt_time(payload.get("reported_at"))
    detail = _as_dict(payload.get("detail"))
    kind = str(payload.get("kind") or "DAILY_REPORT")
    sent = bool(payload.get("notifier_sent"))
    notifier_error = payload.get("notifier_error")

    entries = _fmt_int(detail.get("entries"))
    closes = _fmt_int(detail.get("closes"))
    errors = _fmt_int(detail.get("errors"))
    canceled = _fmt_int(detail.get("canceled"))
    total_records = _fmt_int(detail.get("total_records"))
    blocks = _fmt_int(detail.get("blocks"))

    lines: List[str] = []
    lines.append(f"[{kind}]")
    lines.append(f"일자: {day}")
    lines.append(f"엔진 상태: {engine_state}")
    lines.append(f"보고 시각: {reported_at}")
    lines.append(f"진입/청산: {entries} / {closes}")
    lines.append(f"오류/취소: {errors} / {canceled}")
    lines.append(f"차단/총건수: {blocks} / {total_records}")
    lines.append(f"디스코드 전송: {'성공' if sent else '실패'}")
    if not sent and notifier_error:
        lines.append(f"전송 오류: {notifier_error}")
    if entries == "0" and closes == "0" and errors == "0" and canceled == "0" and blocks == "0" and total_records == "0":
        lines.append("결과: 집계 대상 데이터가 없습니다.")

    return _truncate("\n".join(lines))
