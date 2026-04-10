from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from v2.backtest.cache_loader import _read_klines_csv_rows
from v2.backtest.cache_paths import _cache_file_for_klines
from v2.backtest.local_runner import _local_backtest_profile_alpha_overrides
from v2.backtest.providers import _HistoricalSnapshotProvider
from v2.backtest.runtime_deps import get_build_strategy_selector
from v2.backtest.snapshots import _Kline15m
from v2.config.loader import load_effective_config
from v2.strategies.alpha_shared import bollinger_bandwidth, donchian
from v2.strategies.ra_2026_alpha_v2 import (
    _breakout_stability_edge_score,
    _build_shared_context,
    _common_stop,
    _expansion_quality_score_v2,
)
from v2.strategies.ra_2026_alpha_v2_helpers import _common_cost_subreason

DEFAULT_HORIZONS = (4, 8, 12, 16)


def _parse_utc_datetime_ms(value: str) -> int:
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _utc_iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _signed_close_return_bps(*, entry_close: float, future_close: float, side: str) -> float:
    if float(entry_close) <= 0.0:
        return 0.0
    if str(side) == "SHORT":
        return ((float(entry_close) / max(float(future_close), 1e-9)) - 1.0) * 10000.0
    return ((float(future_close) / float(entry_close)) - 1.0) * 10000.0


def _forward_outcomes(
    *,
    candles_15m: list[_Kline15m],
    idx: int,
    side: str,
    horizons: tuple[int, ...],
) -> dict[str, float | int | None]:
    current = candles_15m[idx]
    entry_close = float(current.close)
    max_horizon = max(horizons) if horizons else 0
    future_rows = candles_15m[idx + 1 : idx + 1 + max_horizon]
    payload: dict[str, float | int | None] = {"forward_window_bars": len(future_rows)}
    for horizon in horizons:
        key = f"forward_close_return_bps_{int(horizon)}"
        if idx + int(horizon) >= len(candles_15m):
            payload[key] = None
            continue
        future_close = float(candles_15m[idx + int(horizon)].close)
        payload[key] = round(
            _signed_close_return_bps(
                entry_close=entry_close,
                future_close=future_close,
                side=side,
            ),
            6,
        )
    if not future_rows:
        payload["forward_mfe_bps"] = None
        payload["forward_mae_bps"] = None
        return payload

    if str(side) == "SHORT":
        best_price = min(float(row.low) for row in future_rows)
        worst_price = max(float(row.high) for row in future_rows)
    else:
        best_price = max(float(row.high) for row in future_rows)
        worst_price = min(float(row.low) for row in future_rows)
    payload["forward_mfe_bps"] = round(
        _signed_close_return_bps(entry_close=entry_close, future_close=best_price, side=side),
        6,
    )
    payload["forward_mae_bps"] = round(
        _signed_close_return_bps(entry_close=entry_close, future_close=worst_price, side=side),
        6,
    )
    return payload


def _hypothetical_expansion_fields(
    *,
    market: dict[str, Any],
    symbol: str,
    cfg: Any,
) -> dict[str, Any]:
    ctx, _ = _build_shared_context(symbol=symbol, market=market, cfg=cfg)
    if ctx is None or str(ctx.bias_side or "") not in {"LONG", "SHORT"}:
        return {}
    previous_channel = donchian(ctx.candles_15m[:-1], cfg.donchian_period_15m)
    if previous_channel is None:
        return {}
    closes = [float(bar.close) for bar in ctx.candles_15m]
    widths: list[float] = []
    for idx in range(int(cfg.bb_period_15m), len(closes) + 1):
        value = bollinger_bandwidth(closes[:idx], cfg.bb_period_15m, cfg.bb_std_15m)
        if value is not None:
            widths.append(float(value))
    if len(widths) < 2:
        return {}

    upper, lower = previous_channel
    buffer = float(cfg.expansion_buffer_bps) / 10000.0
    long_breakout_level = float(upper) * (1.0 + buffer)
    short_breakout_level = float(lower) * (1.0 - buffer)
    true_range = max(float(ctx.current_bar.high) - float(ctx.current_bar.low), 1e-9)
    width_expansion_frac = max(
        (float(widths[-1]) - float(widths[-2])) / max(float(widths[-2]), 1e-9),
        0.0,
    )

    if str(ctx.bias_side) == "SHORT":
        close_over_raw = float(ctx.current_bar.close) < float(lower)
        buffer_pierced = float(ctx.current_bar.low) < float(short_breakout_level)
        close_over_buffer = float(ctx.current_bar.close) < float(short_breakout_level)
        overhang_frac = max(
            (float(short_breakout_level) - float(ctx.current_bar.close)) / float(true_range),
            0.0,
        )
        rejection_wick_frac = max(
            (float(ctx.current_bar.close) - float(ctx.current_bar.low)) / float(true_range),
            0.0,
        )
        stop_side = "SHORT"
    else:
        close_over_raw = float(ctx.current_bar.close) > float(upper)
        buffer_pierced = float(ctx.current_bar.high) > float(long_breakout_level)
        close_over_buffer = float(ctx.current_bar.close) > float(long_breakout_level)
        overhang_frac = max(
            (float(ctx.current_bar.close) - float(long_breakout_level)) / float(true_range),
            0.0,
        )
        rejection_wick_frac = max(
            (float(ctx.current_bar.high) - float(ctx.current_bar.close)) / float(true_range),
            0.0,
        )
        stop_side = "LONG"

    qv2 = _expansion_quality_score_v2(
        overhang_frac=float(overhang_frac),
        rejection_wick_frac=float(rejection_wick_frac),
        width_expansion_frac=float(width_expansion_frac),
        expected_move_frac=float(ctx.expected_move_frac),
        required_move_frac=float(ctx.required_move_frac),
        cfg=cfg,
    )
    stability_edge = _breakout_stability_edge_score(
        overhang_frac=float(overhang_frac),
        rejection_wick_frac=float(rejection_wick_frac),
        width_expansion_frac=float(width_expansion_frac),
        expected_move_frac=float(ctx.expected_move_frac),
        required_move_frac=float(ctx.required_move_frac),
        cfg=cfg,
    )
    _stop_price, stop_distance_frac = _common_stop(
        side=stop_side,
        entry_price=float(ctx.current_bar.close),
        ctx=ctx,
        cfg=cfg,
    )
    cost_subreason = _common_cost_subreason(
        stop_distance_frac=float(stop_distance_frac),
        ctx=ctx,
        cfg=cfg,
    )
    return {
        "hypothetical_close_over_raw_breakout": bool(close_over_raw),
        "hypothetical_buffer_pierced": bool(buffer_pierced),
        "hypothetical_close_over_buffer": bool(close_over_buffer),
        "hypothetical_quality_score_v2": round(float(qv2), 6),
        "hypothetical_breakout_stability_edge_score": round(float(stability_edge), 6),
        "hypothetical_stop_distance_frac": round(float(stop_distance_frac), 6),
        "hypothetical_cost_subreason": str(cost_subreason) if cost_subreason is not None else None,
    }


@dataclass(frozen=True)
class EventTapeRow:
    open_time_ms: int
    close_time_ms: int
    open_time_utc: str
    close_time_utc: str
    event_state: str
    decision_reason: str
    alpha_reason: str
    alpha_id: str
    side_hint: str
    regime: str
    bias_side: str
    score: float
    vol_ratio_15m: float
    spread_estimate_bps: float
    expected_move_frac: float
    required_move_frac: float
    edge_ratio: float
    range_atr: float
    body_ratio: float
    favored_close_long: float
    favored_close_short: float
    width_expansion_frac: float
    breakout_distance_atr_long: float
    breakout_distance_atr_short: float
    quality_score_v2: float
    forward_window_bars: int
    forward_close_return_bps_4: float | None
    forward_close_return_bps_8: float | None
    forward_close_return_bps_12: float | None
    forward_close_return_bps_16: float | None
    forward_mfe_bps: float | None
    forward_mae_bps: float | None
    hypothetical_close_over_raw_breakout: bool | None = None
    hypothetical_buffer_pierced: bool | None = None
    hypothetical_close_over_buffer: bool | None = None
    hypothetical_quality_score_v2: float | None = None
    hypothetical_breakout_stability_edge_score: float | None = None
    hypothetical_stop_distance_frac: float | None = None
    hypothetical_cost_subreason: str | None = None


def _load_interval_rows(
    *,
    report_dir: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[_Kline15m]:
    cache_root = Path(report_dir) / "_cache"
    path = _cache_file_for_klines(
        cache_root=cache_root,
        symbol=symbol,
        interval=interval,
        years=1,
    )
    rows = _read_klines_csv_rows(path)
    return [
        row
        for row in rows
        if int(row.open_time_ms) >= int(start_ms) and int(row.open_time_ms) < int(end_ms)
    ]


def _build_event_tape_rows(
    *,
    profile: str,
    symbol: str,
    start_utc: str,
    end_utc: str,
    horizons: tuple[int, ...],
    report_dir: str,
) -> list[EventTapeRow]:
    start_ms = _parse_utc_datetime_ms(start_utc)
    end_ms = _parse_utc_datetime_ms(end_utc)
    candles_15m = _load_interval_rows(
        report_dir=report_dir,
        symbol=symbol,
        interval="15m",
        start_ms=start_ms,
        end_ms=end_ms,
    )
    candles_1h = _load_interval_rows(
        report_dir=report_dir,
        symbol=symbol,
        interval="1h",
        start_ms=start_ms,
        end_ms=end_ms,
    )
    candles_4h = _load_interval_rows(
        report_dir=report_dir,
        symbol=symbol,
        interval="4h",
        start_ms=start_ms,
        end_ms=end_ms,
    )

    provider = _HistoricalSnapshotProvider(
        symbol=symbol,
        candles_15m=candles_15m,
        market_candles={"1h": candles_1h, "4h": candles_4h},
        market_intervals=["15m", "1h", "4h"],
    )
    cfg = load_effective_config(
        profile=profile,
        mode="shadow",
        env="prod",
        env_file_path=".env",
        config_path=None,
    )
    selector = get_build_strategy_selector()(
        behavior=cfg.behavior,
        snapshot_provider=provider,
        overheat_fetcher=None,
        journal_logger=None,
    )
    runtime_updater = getattr(selector, "set_runtime_params", None)
    if callable(runtime_updater):
        runtime_updater(**_local_backtest_profile_alpha_overrides(profile))
    strategy_cfg = getattr(selector, "_cfg", None)

    rows: list[EventTapeRow] = []
    for idx in range(len(provider)):
        snapshot = provider()
        if not snapshot:
            break
        candle = provider.candle_at(idx)
        decision = selector.decide(snapshot)
        if not isinstance(decision, dict):
            continue

        alpha_diag = ((decision.get("alpha_diagnostics") or {}).get("alpha_expansion") or {})
        alpha_reason = str(alpha_diag.get("reason") or decision.get("reason") or "")
        alpha_state = (
            "candidate"
            if str(alpha_diag.get("state") or "").strip() == "candidate"
            or (
                str(decision.get("alpha_id") or "") == "alpha_expansion"
                and str(decision.get("intent") or "NONE") in {"LONG", "SHORT"}
            )
            else "blocked"
        )
        metrics = dict((decision.get("alpha_reject_metrics") or {}).get("alpha_expansion") or {})
        indicators = dict(decision.get("indicators") or {})
        execution = dict(decision.get("execution") or {})

        side_hint = str(metrics.get("bias_side") or decision.get("intent") or "NONE")
        if side_hint not in {"LONG", "SHORT"}:
            side_hint = "NONE"

        quality_score_v2 = _to_float(execution.get("entry_quality_score_v2"))
        if quality_score_v2 <= 0.0:
            quality_score_v2 = _to_float(metrics.get("quality_score_v2"))

        expected_move_frac = _to_float(indicators.get("expected_move_frac"))
        required_move_frac = _to_float(indicators.get("required_move_frac"))
        edge_ratio = (
            float(expected_move_frac) / max(float(required_move_frac), 1e-9)
            if float(required_move_frac) > 0.0
            else 0.0
        )

        forward = _forward_outcomes(
            candles_15m=candles_15m,
            idx=idx,
            side=side_hint if side_hint in {"LONG", "SHORT"} else "LONG",
            horizons=horizons,
        )
        hypothetical = (
            _hypothetical_expansion_fields(
                market=dict(snapshot.get("market") or {}),
                symbol=symbol,
                cfg=strategy_cfg,
            )
            if strategy_cfg is not None
            else {}
        )
        rows.append(
            EventTapeRow(
                open_time_ms=int(candle.open_time_ms),
                close_time_ms=int(candle.close_time_ms),
                open_time_utc=_utc_iso_from_ms(int(candle.open_time_ms)),
                close_time_utc=_utc_iso_from_ms(int(candle.close_time_ms)),
                event_state=alpha_state,
                decision_reason=str(decision.get("reason") or ""),
                alpha_reason=alpha_reason,
                alpha_id="alpha_expansion",
                side_hint=side_hint,
                regime=str(decision.get("regime") or ""),
                bias_side=str(metrics.get("bias_side") or ""),
                score=_to_float(decision.get("score")),
                vol_ratio_15m=_to_float(metrics.get("vol_ratio_15m")),
                spread_estimate_bps=_to_float(indicators.get("spread_estimate_bps")),
                expected_move_frac=float(expected_move_frac),
                required_move_frac=float(required_move_frac),
                edge_ratio=float(edge_ratio),
                range_atr=_to_float(metrics.get("range_atr")),
                body_ratio=_to_float(metrics.get("body_ratio")),
                favored_close_long=_to_float(metrics.get("favored_close_long")),
                favored_close_short=_to_float(metrics.get("favored_close_short")),
                width_expansion_frac=_to_float(metrics.get("width_expansion_frac")),
                breakout_distance_atr_long=_to_float(metrics.get("breakout_distance_atr_long")),
                breakout_distance_atr_short=_to_float(metrics.get("breakout_distance_atr_short")),
                quality_score_v2=float(quality_score_v2),
                forward_window_bars=int(forward["forward_window_bars"] or 0),
                forward_close_return_bps_4=forward.get("forward_close_return_bps_4"),
                forward_close_return_bps_8=forward.get("forward_close_return_bps_8"),
                forward_close_return_bps_12=forward.get("forward_close_return_bps_12"),
                forward_close_return_bps_16=forward.get("forward_close_return_bps_16"),
                forward_mfe_bps=forward.get("forward_mfe_bps"),
                forward_mae_bps=forward.get("forward_mae_bps"),
                hypothetical_close_over_raw_breakout=hypothetical.get("hypothetical_close_over_raw_breakout"),
                hypothetical_buffer_pierced=hypothetical.get("hypothetical_buffer_pierced"),
                hypothetical_close_over_buffer=hypothetical.get("hypothetical_close_over_buffer"),
                hypothetical_quality_score_v2=hypothetical.get("hypothetical_quality_score_v2"),
                hypothetical_breakout_stability_edge_score=hypothetical.get("hypothetical_breakout_stability_edge_score"),
                hypothetical_stop_distance_frac=hypothetical.get("hypothetical_stop_distance_frac"),
                hypothetical_cost_subreason=hypothetical.get("hypothetical_cost_subreason"),
            )
        )
    return rows


def _summarize_event_tape(rows: list[EventTapeRow], *, horizons: tuple[int, ...]) -> dict[str, Any]:
    reason_counts = Counter(str(row.alpha_reason or row.decision_reason or "") for row in rows)
    grouped: dict[str, list[EventTapeRow]] = defaultdict(list)
    for row in rows:
        grouped[str(row.alpha_reason or row.decision_reason or "")].append(row)

    reason_stats: list[dict[str, Any]] = []
    max_horizon = max(horizons) if horizons else 0
    horizon_key = f"forward_close_return_bps_{int(max_horizon)}" if max_horizon > 0 else None
    for reason, items in grouped.items():
        row_stat: dict[str, Any] = {
            "reason": reason,
            "count": len(items),
            "candidate_count": sum(1 for item in items if item.event_state == "candidate"),
        }
        for horizon in horizons:
            values = [
                float(value)
                for value in (
                    getattr(item, f"forward_close_return_bps_{int(horizon)}") for item in items
                )
                if value is not None
            ]
            row_stat[f"avg_close_return_bps_{int(horizon)}"] = round(mean(values), 6) if values else None
            row_stat[f"positive_rate_{int(horizon)}"] = (
                round(sum(1 for value in values if float(value) > 0.0) / len(values), 6)
                if values
                else None
            )
        mfe_values = [float(item.forward_mfe_bps) for item in items if item.forward_mfe_bps is not None]
        mae_values = [float(item.forward_mae_bps) for item in items if item.forward_mae_bps is not None]
        row_stat["avg_mfe_bps"] = round(mean(mfe_values), 6) if mfe_values else None
        row_stat["avg_mae_bps"] = round(mean(mae_values), 6) if mae_values else None
        reason_stats.append(row_stat)

    if horizon_key is not None:
        reason_stats.sort(
            key=lambda item: (
                -999999.0
                if item.get(f"avg_close_return_bps_{int(max_horizon)}") is None
                else float(item[f"avg_close_return_bps_{int(max_horizon)}"])
            ),
            reverse=True,
        )

    candidate_rows = [row for row in rows if row.event_state == "candidate"]
    summary = {
        "count": len(rows),
        "candidate_count": len(candidate_rows),
        "reason_counts": reason_counts.most_common(12),
        "top_reason_stats": reason_stats[:10],
    }
    for horizon in horizons:
        values = [
            float(value)
            for value in (
                getattr(row, f"forward_close_return_bps_{int(horizon)}") for row in candidate_rows
            )
            if value is not None
        ]
        summary[f"candidate_avg_close_return_bps_{int(horizon)}"] = (
            round(mean(values), 6) if values else None
        )
    return summary


def _render_markdown(
    *,
    profile: str,
    symbol: str,
    start_utc: str,
    end_utc: str,
    horizons: tuple[int, ...],
    summary: dict[str, Any],
) -> str:
    lines = [
        "# Alpha Expansion Event Tape",
        "",
        f"- profile: `{profile}`",
        f"- symbol: `{symbol}`",
        f"- window: `{start_utc}` -> `{end_utc}`",
        f"- horizons: `{','.join(str(item) for item in horizons)}`",
        "",
        "## Summary",
        f"- rows: `{summary['count']}`",
        f"- candidate_count: `{summary['candidate_count']}`",
        f"- reason_counts: `{json.dumps(summary['reason_counts'], ensure_ascii=False)}`",
        "",
        "## Top Reason Stats",
    ]
    for item in summary["top_reason_stats"]:
        lines.append(f"- `{json.dumps(item, ensure_ascii=False)}`")
    return "\n".join(lines) + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_event_tape_report(
    *,
    profile: str,
    symbol: str,
    start_utc: str,
    end_utc: str,
    horizons: tuple[int, ...],
    report_dir: str,
) -> tuple[dict[str, Any], Path, Path, Path]:
    rows = _build_event_tape_rows(
        profile=profile,
        symbol=symbol,
        start_utc=start_utc,
        end_utc=end_utc,
        horizons=horizons,
        report_dir=report_dir,
    )
    summary = _summarize_event_tape(rows, horizons=horizons)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "symbol": symbol,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "horizons": list(horizons),
        "summary": summary,
    }

    output_root = Path(report_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    json_path = output_root / f"alpha_expansion_event_tape_{stamp}.json"
    md_path = output_root / f"alpha_expansion_event_tape_{stamp}.md"
    csv_path = output_root / f"alpha_expansion_event_tape_{stamp}.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(
        _render_markdown(
            profile=profile,
            symbol=symbol,
            start_utc=start_utc,
            end_utc=end_utc,
            horizons=horizons,
            summary=summary,
        ),
        encoding="utf-8",
    )
    _write_csv(csv_path, [asdict(row) for row in rows])
    return report, json_path, md_path, csv_path


def _parse_horizons(raw: str) -> tuple[int, ...]:
    values: list[int] = []
    for item in str(raw).split(","):
        text = str(item).strip()
        if not text:
            continue
        values.append(max(int(text), 1))
    deduped: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return tuple(deduped or DEFAULT_HORIZONS)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract alpha_expansion event tape with forward outcomes from cached replay data"
    )
    parser.add_argument("--profile", default="ra_2026_alpha_v2_expansion_live_candidate")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--end-utc", required=True)
    parser.add_argument("--horizons", default="4,8,12,16")
    parser.add_argument("--report-dir", default="local_backtest/reports")
    args = parser.parse_args()

    _, json_path, md_path, csv_path = build_event_tape_report(
        profile=str(args.profile),
        symbol=str(args.symbol).strip().upper(),
        start_utc=str(args.start_utc),
        end_utc=str(args.end_utc),
        horizons=_parse_horizons(str(args.horizons)),
        report_dir=str(args.report_dir),
    )
    print(f"REPORT_JSON={json_path}")
    print(f"REPORT_MD={md_path}")
    print(f"EVENT_TAPE_CSV={csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
