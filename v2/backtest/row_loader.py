from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from v2.backtest.common import _to_float
from v2.backtest.snapshots import _ReplayFrame


def _safe_json_loads(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _extract_meta(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "event_time",
        "timestamp",
        "time",
        "datetime",
        "open_time",
        "close_time",
    ):
        if key in payload:
            out[key] = payload[key]
    return out


def _extract_snapshot_time(meta: dict[str, Any]) -> Any:
    for key in ("event_time", "timestamp", "time", "datetime", "open_time", "close_time"):
        if key in meta:
            return meta[key]
    return None


def _normalize_snapshot(payload: dict[str, Any], *, default_symbol: str) -> _ReplayFrame | None:
    symbol = str(payload.get("symbol") or payload.get("default_symbol") or default_symbol).upper()

    raw_market = payload.get("market")
    if not isinstance(raw_market, dict):
        raw_market = {key: payload.get(key) for key in ("4h", "1h", "15m")}

    market: dict[str, Any] = {}
    for interval in ("4h", "1h", "15m"):
        rows = raw_market.get(interval)
        if rows is None:
            continue
        if isinstance(rows, str):
            rows = _safe_json_loads(rows)
        if isinstance(rows, list):
            market[interval] = rows

    if len(market) == 0:
        return None
    return _ReplayFrame(symbol=symbol, market=market, meta=_extract_meta(payload))


def _normalize_replay_rows(items: list[Any], *, default_symbol: str) -> list[_ReplayFrame]:
    normalized = [
        _normalize_snapshot(row, default_symbol=default_symbol)
        for row in items
        if isinstance(row, dict)
    ]
    return [row for row in normalized if row is not None]


def _load_replay_rows_json(
    *, path: Path, default_symbol: str, is_jsonl: bool = False
) -> list[_ReplayFrame]:
    if is_jsonl:
        jsonl_rows: list[Any] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            raw_line = line.strip()
            if not raw_line:
                continue
            try:
                parsed = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                jsonl_rows.append(parsed)
        return _normalize_replay_rows(jsonl_rows, default_symbol=default_symbol)

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        rows_payload: Any = raw.get("rows")
        if isinstance(rows_payload, list):
            return _normalize_replay_rows(rows_payload, default_symbol=default_symbol)
        if isinstance(rows_payload, str):
            nested = _safe_json_loads(rows_payload)
            if isinstance(nested, list):
                return _normalize_replay_rows(nested, default_symbol=default_symbol)
        normalized = _normalize_snapshot(raw, default_symbol=default_symbol)
        return [normalized] if normalized is not None else []
    if isinstance(raw, list):
        return _normalize_replay_rows(raw, default_symbol=default_symbol)
    return []


def _load_replay_rows_csv(*, path: Path, default_symbol: str) -> list[_ReplayFrame]:
    frames: list[_ReplayFrame] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return frames

    reader = csv.DictReader(lines)
    rows = [row for row in reader if isinstance(row, dict)]
    if len(rows) == 0:
        return frames

    first = rows[0]
    has_market_fields = any(k in first for k in ("market_4h", "market_1h", "market_15m"))
    if has_market_fields:
        for raw in rows:
            if raw is None:
                continue
            payload = dict(raw)
            for key in ("market_4h", "market_1h", "market_15m"):
                if key in payload:
                    raw_market = _safe_json_loads(payload[key])
                    if isinstance(raw_market, list):
                        payload[key.replace("market_", "")] = raw_market
            normalized = _normalize_snapshot(payload, default_symbol=default_symbol)
            if normalized is not None:
                frames.append(normalized)
        return frames

    market_frames: dict[str, list[dict[str, float]]] = {"4h": [], "1h": [], "15m": []}
    default_symbol_upper = default_symbol.upper()
    for raw in rows:
        if raw is None:
            continue
        interval = str(raw.get("interval") or "").strip()
        if interval not in market_frames:
            continue

        open_v = _to_float(raw.get("open"))
        high_v = _to_float(raw.get("high"))
        low_v = _to_float(raw.get("low"))
        close_v = _to_float(raw.get("close"))
        if open_v is None or high_v is None or low_v is None or close_v is None:
            continue

        candle = {
            "open": open_v,
            "high": high_v,
            "low": low_v,
            "close": close_v,
        }
        market_frames[interval].append(candle)

        if all(market_frames[k] for k in ("4h", "1h", "15m")):
            payload: dict[str, Any] = {
                "symbol": str(raw.get("symbol") or default_symbol_upper),
                "market": {
                    "4h": list(market_frames["4h"]),
                    "1h": list(market_frames["1h"]),
                    "15m": list(market_frames["15m"]),
                },
            }
            for key in ("timestamp", "time", "datetime", "open_time", "close_time", "event_time"):
                if key in raw:
                    payload[key] = raw[key]
            normalized = _normalize_snapshot(payload, default_symbol=default_symbol_upper)
            if normalized is not None:
                frames.append(normalized)
    return frames


def _load_replay_frames(path: str | None, default_symbol: str) -> list[_ReplayFrame]:
    if path is None:
        return []
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"replay path not found: {source}")
    suffix = source.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        return _load_replay_rows_json(path=source, default_symbol=default_symbol, is_jsonl=True)
    if suffix == ".json":
        return _load_replay_rows_json(path=source, default_symbol=default_symbol)
    if suffix == ".csv":
        return _load_replay_rows_csv(path=source, default_symbol=default_symbol)

    try:
        rows = _load_replay_rows_json(path=source, default_symbol=default_symbol)
        if rows:
            return rows
    except json.JSONDecodeError:
        pass
    return _load_replay_rows_csv(path=source, default_symbol=default_symbol)
