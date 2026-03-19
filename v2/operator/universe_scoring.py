from __future__ import annotations

SCORING_TIMEFRAMES: tuple[str, ...] = ("10m", "15m", "30m", "1h", "4h")


def _parse_float_range(raw: str, *, field: str, min_v: float, max_v: float) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 값은 숫자여야 합니다.") from exc
    if value < min_v or value > max_v:
        raise ValueError(f"{field} 값은 {min_v} 이상 {max_v} 이하여야 합니다.")
    return value


def _parse_int_range(raw: str, *, field: str, min_v: int, max_v: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 값은 정수여야 합니다.") from exc
    if value < min_v or value > max_v:
        raise ValueError(f"{field} 값은 {min_v} 이상 {max_v} 이하여야 합니다.")
    return value


def parse_universe_symbols(raw: str) -> list[str]:
    items = [item.strip().upper() for item in str(raw).split(",") if item.strip()]
    unique: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item.endswith("USDT"):
            raise ValueError("심볼은 USDT 마켓 심볼만 입력해주세요.")
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    if not unique:
        raise ValueError("운영 심볼은 최소 1개 이상 필요합니다.")
    return unique


def parse_scoring_weight_text(raw: str) -> dict[str, float]:
    parts = [part.strip() for part in str(raw).split(",") if part.strip()]
    parsed: dict[str, float] = {}
    if not parts:
        return parsed

    has_key = all("=" in part for part in parts)
    if has_key:
        for part in parts:
            key, value = part.split("=", 1)
            normalized = key.strip().lower().replace(" ", "")
            if normalized not in SCORING_TIMEFRAMES:
                raise ValueError("지원하지 않는 시간봉이 포함되어 있습니다.")
            parsed[normalized] = _parse_float_range(
                value,
                field=f"tf_weight_{normalized}",
                min_v=0.0,
                max_v=1.0,
            )
        return parsed

    if len(parts) != len(SCORING_TIMEFRAMES):
        raise ValueError("숫자 입력은 5개(10m,15m,30m,1h,4h)를 모두 넣어주세요.")
    for key, value in zip(SCORING_TIMEFRAMES, parts, strict=True):
        parsed[key] = _parse_float_range(value, field=f"tf_weight_{key}", min_v=0.0, max_v=1.0)
    return parsed


def validate_scoring_weights(weights: dict[str, float]) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for timeframe in SCORING_TIMEFRAMES:
        normalized[timeframe] = _parse_float_range(
            str(weights.get(timeframe, 0.0)),
            field=f"tf_weight_{timeframe}",
            min_v=0.0,
            max_v=1.0,
        )
    if sum(normalized.values()) <= 0.0:
        raise ValueError("가중치 합계는 0보다 커야 합니다.")
    return normalized


def parse_momentum_ema(raw: str) -> tuple[int, int]:
    parts = [part.strip() for part in str(raw).split(",") if part.strip()]
    if len(parts) != 2:
        raise ValueError("모멘텀 속도는 '빠름,느림' 형식(예: 8,21)으로 입력해주세요.")
    fast = _parse_int_range(parts[0], field="donchian_fast_ema_period", min_v=2, max_v=30)
    slow = _parse_int_range(parts[1], field="donchian_slow_ema_period", min_v=3, max_v=80)
    if slow <= fast:
        slow = fast + 1
    return fast, slow
