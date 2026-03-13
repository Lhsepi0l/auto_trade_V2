from __future__ import annotations

from typing import Any


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def build_daily_report_payload(
    *,
    day: str,
    engine_state: str,
    detail: dict[str, Any],
    notifier_enabled: bool,
    reported_at: str,
) -> dict[str, Any]:
    return {
        "kind": "DAILY_REPORT",
        "day": day,
        "engine_state": engine_state,
        "detail": dict(detail),
        "notifier_enabled": bool(notifier_enabled),
        "notifier_sent": False,
        "notifier_error": None,
        "reported_at": reported_at,
    }


def build_daily_report_message(payload: dict[str, Any]) -> str:
    detail_raw = payload.get("detail")
    detail = detail_raw if isinstance(detail_raw, dict) else {}
    entries = int(_to_float(detail.get("entries"), default=0.0))
    closes = int(_to_float(detail.get("closes"), default=0.0))
    errors = int(_to_float(detail.get("errors"), default=0.0))
    canceled = int(_to_float(detail.get("canceled"), default=0.0))
    blocks = int(_to_float(detail.get("blocks"), default=0.0))
    total_records = int(_to_float(detail.get("total_records"), default=0.0))

    lines = [
        f"[{str(payload.get('kind') or 'DAILY_REPORT')}]",
        f"일자: {str(payload.get('day') or '-')}",
        f"엔진 상태: {str(payload.get('engine_state') or '-')}",
        f"보고 시각: {str(payload.get('reported_at') or '-')}",
        f"진입/청산: {entries} / {closes}",
        f"오류/취소: {errors} / {canceled}",
        f"차단/총건수: {blocks} / {total_records}",
    ]
    return "\n".join(lines)
