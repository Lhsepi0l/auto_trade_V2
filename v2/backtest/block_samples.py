from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
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
from v2.config.loader import load_effective_config
from v2.strategies.alpha_shared import donchian
from v2.strategies.ra_2026_alpha_v2 import _build_shared_context, _common_stop


def _parse_utc_datetime_ms(value: str) -> int:
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_bool(value: Any) -> bool:
    return bool(value)


@dataclass(frozen=True)
class TriggerSample:
    bias_side: str
    close_price: float
    long_gap_bps: float | None
    short_gap_bps: float | None
    range_atr: float
    body_ratio: float
    favored_close_long: float
    favored_close_short: float
    width_expansion_frac: float
    breakout_distance_atr_long: float
    breakout_distance_atr_short: float
    body_ready: bool
    close_ready: bool
    short_close_ready: bool
    width_ready: bool
    squeeze_ready: bool


@dataclass(frozen=True)
class VolumeSample:
    bias_side: str
    vol_ratio: float
    min_volume_ratio: float
    expected_move_frac: float
    required_move_frac: float
    spread_bps: float


@dataclass(frozen=True)
class CostSample:
    side: str
    spread_bps: float
    max_spread_bps: float
    stop_distance_frac: float
    min_stop_distance_frac: float
    expected_move_frac: float
    required_move_frac: float
    edge_ratio: float
    likely_subreason: str


@dataclass(frozen=True)
class SetupCandidateRule:
    name: str
    max_gap_bps: float
    min_body_ratio: float
    min_favored_close: float
    min_width_expansion_frac: float
    min_range_atr: float


def _build_trigger_sample(metrics: dict[str, Any]) -> TriggerSample:
    close_price = _to_float(metrics.get("close"))
    long_level = _to_float(metrics.get("breakout_level_long"))
    short_level = _to_float(metrics.get("breakout_level_short"))
    long_gap_bps = None
    short_gap_bps = None
    if long_level > 0:
        long_gap_bps = ((close_price / long_level) - 1.0) * 10000.0
    if short_level > 0:
        short_gap_bps = (1.0 - (close_price / short_level)) * 10000.0
    return TriggerSample(
        bias_side=str(metrics.get("bias_side") or ""),
        close_price=close_price,
        long_gap_bps=long_gap_bps,
        short_gap_bps=short_gap_bps,
        range_atr=_to_float(metrics.get("range_atr")),
        body_ratio=_to_float(metrics.get("body_ratio")),
        favored_close_long=_to_float(metrics.get("favored_close_long")),
        favored_close_short=_to_float(metrics.get("favored_close_short")),
        width_expansion_frac=_to_float(metrics.get("width_expansion_frac")),
        breakout_distance_atr_long=_to_float(metrics.get("breakout_distance_atr_long")),
        breakout_distance_atr_short=_to_float(metrics.get("breakout_distance_atr_short")),
        body_ready=_to_bool(metrics.get("body_ready")),
        close_ready=_to_bool(metrics.get("close_ready")),
        short_close_ready=_to_bool(metrics.get("short_close_ready")),
        width_ready=_to_bool(metrics.get("width_ready")),
        squeeze_ready=_to_bool(metrics.get("squeeze_ready")),
    )


def _build_volume_sample(*, metrics: dict[str, Any], indicators: dict[str, Any]) -> VolumeSample:
    return VolumeSample(
        bias_side=str(metrics.get("bias_side") or ""),
        vol_ratio=_to_float(metrics.get("vol_ratio_15m")),
        min_volume_ratio=_to_float(metrics.get("min_volume_ratio_15m")),
        expected_move_frac=_to_float(indicators.get("expected_move_frac")),
        required_move_frac=_to_float(indicators.get("required_move_frac")),
        spread_bps=_to_float(indicators.get("spread_estimate_bps")),
    )


def _build_cost_sample(
    *,
    snapshot: dict[str, Any],
    cfg: Any,
    symbol: str,
) -> CostSample | None:
    market = snapshot.get("market")
    if not isinstance(market, dict):
        return None
    ctx, _ = _build_shared_context(symbol=symbol, market=market, cfg=cfg)
    if ctx is None:
        return None
    previous_channel = donchian(ctx.candles_15m[:-1], cfg.donchian_period_15m)
    if previous_channel is None:
        return None
    upper, lower = previous_channel
    buffer = float(cfg.expansion_buffer_bps) / 10000.0
    long_breakout_level = float(upper) * (1.0 + buffer)
    short_breakout_level = float(lower) * (1.0 - buffer)

    side = "NONE"
    if ctx.bias_side == "LONG" and float(ctx.current_bar.close) > float(long_breakout_level):
        side = "LONG"
    elif ctx.bias_side == "SHORT" and float(ctx.current_bar.close) < float(short_breakout_level):
        side = "SHORT"
    elif ctx.bias_side in {"LONG", "SHORT"}:
        side = str(ctx.bias_side)

    stop_distance_frac = 0.0
    if side in {"LONG", "SHORT"}:
        _, stop_distance_frac = _common_stop(
            side=side,
            entry_price=float(ctx.current_bar.close),
            ctx=ctx,
            cfg=cfg,
        )

    edge_ratio = 0.0
    if float(ctx.required_move_frac) > 0.0:
        edge_ratio = float(ctx.expected_move_frac) / float(ctx.required_move_frac)

    likely_subreason = "edge_shortfall"
    if float(ctx.spread_estimate_bps) > float(cfg.max_spread_bps):
        likely_subreason = "spread_cap"
    elif float(stop_distance_frac) < float(cfg.min_stop_distance_frac):
        likely_subreason = "stop_distance_too_small"
    elif float(ctx.expected_move_frac) <= 0.0 or float(ctx.required_move_frac) <= 0.0:
        likely_subreason = "expected_move_unavailable"

    return CostSample(
        side=side,
        spread_bps=float(ctx.spread_estimate_bps),
        max_spread_bps=float(cfg.max_spread_bps),
        stop_distance_frac=float(stop_distance_frac),
        min_stop_distance_frac=float(cfg.min_stop_distance_frac),
        expected_move_frac=float(ctx.expected_move_frac),
        required_move_frac=float(ctx.required_move_frac),
        edge_ratio=float(edge_ratio),
        likely_subreason=likely_subreason,
    )


def _trigger_readiness_count(sample: TriggerSample) -> int:
    return sum(
        [
            bool(sample.body_ready),
            bool(sample.close_ready or sample.short_close_ready),
            bool(sample.width_ready),
            bool(sample.squeeze_ready),
        ]
    )


def _trigger_gap_score(sample: TriggerSample) -> float:
    if sample.bias_side == "LONG" and sample.long_gap_bps is not None:
        return abs(float(sample.long_gap_bps))
    if sample.bias_side == "SHORT" and sample.short_gap_bps is not None:
        return abs(float(sample.short_gap_bps))
    return 999999.0


def _is_near_buffer_candidate(sample: TriggerSample) -> bool:
    if sample.bias_side == "LONG" and sample.long_gap_bps is not None:
        return (
            float(sample.long_gap_bps) >= -8.0
            and float(sample.long_gap_bps) < 0.0
            and sample.body_ready
            and sample.close_ready
            and sample.width_ready
            and sample.squeeze_ready
        )
    if sample.bias_side == "SHORT" and sample.short_gap_bps is not None:
        return (
            float(sample.short_gap_bps) >= -8.0
            and float(sample.short_gap_bps) < 0.0
            and sample.body_ready
            and sample.short_close_ready
            and sample.width_ready
            and sample.squeeze_ready
        )
    return False


def _sample_gap_bps(sample: TriggerSample) -> float:
    if sample.bias_side == "LONG" and sample.long_gap_bps is not None:
        return abs(float(sample.long_gap_bps))
    if sample.bias_side == "SHORT" and sample.short_gap_bps is not None:
        return abs(float(sample.short_gap_bps))
    return 999999.0


def _sample_favored_close(sample: TriggerSample) -> float:
    if sample.bias_side == "LONG":
        return float(sample.favored_close_long)
    if sample.bias_side == "SHORT":
        return float(sample.favored_close_short)
    return 0.0


def _candidate_rules() -> list[SetupCandidateRule]:
    return [
        SetupCandidateRule("strict_core", 1.5, 0.55, 0.75, 0.10, 1.20),
        SetupCandidateRule("strict_balanced", 2.5, 0.50, 0.72, 0.08, 1.00),
        SetupCandidateRule("balanced_core", 3.5, 0.45, 0.68, 0.08, 0.95),
        SetupCandidateRule("balanced_range", 5.0, 0.40, 0.65, 0.05, 0.90),
        SetupCandidateRule("broad_quality", 8.0, 0.35, 0.60, 0.05, 0.85),
    ]


def _rule_matches(sample: TriggerSample, rule: SetupCandidateRule) -> bool:
    return (
        _sample_gap_bps(sample) <= float(rule.max_gap_bps)
        and float(sample.body_ratio) >= float(rule.min_body_ratio)
        and _sample_favored_close(sample) >= float(rule.min_favored_close)
        and float(sample.width_expansion_frac) >= float(rule.min_width_expansion_frac)
        and float(sample.range_atr) >= float(rule.min_range_atr)
        and bool(sample.squeeze_ready)
    )


def _summarize_setup_candidates(
    samples: list[TriggerSample],
    *,
    sample_limit: int,
) -> dict[str, Any]:
    near_buffer = [item for item in samples if _is_near_buffer_candidate(item)]
    ranked_rows: list[dict[str, Any]] = []
    for rule in _candidate_rules():
        matched = [item for item in near_buffer if _rule_matches(item, rule)]
        if not matched:
            continue
        long_count = sum(1 for item in matched if item.bias_side == "LONG")
        short_count = sum(1 for item in matched if item.bias_side == "SHORT")
        avg_gap_bps = mean(_sample_gap_bps(item) for item in matched)
        avg_body_ratio = mean(float(item.body_ratio) for item in matched)
        avg_favored_close = mean(_sample_favored_close(item) for item in matched)
        avg_width = mean(float(item.width_expansion_frac) for item in matched)
        avg_range_atr = mean(float(item.range_atr) for item in matched)
        support_score = min(len(matched) / 120.0, 1.0)
        quality_score = (
            min(avg_body_ratio / 0.60, 1.0)
            + min(avg_favored_close / 0.80, 1.0)
            + min(avg_width / 0.15, 1.0)
            + min(avg_range_atr / 2.0, 1.0)
            + max(1.0 - (avg_gap_bps / 8.0), 0.0)
        ) / 5.0
        balance_score = (
            min(long_count, short_count) / max(long_count, short_count)
            if max(long_count, short_count) > 0
            else 0.0
        )
        research_score = (quality_score * 0.55) + (support_score * 0.35) + (balance_score * 0.10)
        ranked_rows.append(
            {
                "name": rule.name,
                "max_gap_bps": rule.max_gap_bps,
                "min_body_ratio": rule.min_body_ratio,
                "min_favored_close": rule.min_favored_close,
                "min_width_expansion_frac": rule.min_width_expansion_frac,
                "min_range_atr": rule.min_range_atr,
                "count": len(matched),
                "long_count": long_count,
                "short_count": short_count,
                "avg_gap_bps": round(avg_gap_bps, 6),
                "avg_body_ratio": round(avg_body_ratio, 6),
                "avg_favored_close": round(avg_favored_close, 6),
                "avg_width_expansion_frac": round(avg_width, 6),
                "avg_range_atr": round(avg_range_atr, 6),
                "research_score": round(research_score, 6),
            }
        )

    ranked_rows.sort(
        key=lambda item: (
            float(item["research_score"]),
            int(item["count"]),
            -float(item["avg_gap_bps"]),
        ),
        reverse=True,
    )
    return {
        "count": len(near_buffer),
        "top_rules": ranked_rows[:sample_limit],
    }


def _summarize_trigger_samples(samples: list[TriggerSample], *, sample_limit: int) -> dict[str, Any]:
    counts = Counter()
    for sample in samples:
        if sample.body_ready:
            counts["body_ready"] += 1
        if sample.close_ready or sample.short_close_ready:
            counts["close_ready"] += 1
        if sample.width_ready:
            counts["width_ready"] += 1
        if sample.squeeze_ready:
            counts["squeeze_ready"] += 1
        if sample.bias_side == "LONG" and sample.long_gap_bps is not None:
            if sample.long_gap_bps > -10.0:
                counts["long_gap_lt_10bps"] += 1
            if sample.long_gap_bps > -8.0:
                counts["long_gap_lt_8bps"] += 1
            if sample.long_gap_bps > -5.0:
                counts["long_gap_lt_5bps"] += 1
            if (
                sample.long_gap_bps > -8.0
                and sample.body_ready
                and sample.close_ready
                and sample.width_ready
            ):
                counts["long_gap8_body_close_width"] += 1
        if sample.bias_side == "SHORT" and sample.short_gap_bps is not None:
            if sample.short_gap_bps > -10.0:
                counts["short_gap_lt_10bps"] += 1
            if sample.short_gap_bps > -8.0:
                counts["short_gap_lt_8bps"] += 1
            if sample.short_gap_bps > -5.0:
                counts["short_gap_lt_5bps"] += 1
            if (
                sample.short_gap_bps > -8.0
                and sample.body_ready
                and sample.short_close_ready
                and sample.width_ready
            ):
                counts["short_gap8_body_close_width"] += 1

    ordered = sorted(
        samples,
        key=lambda item: (-_trigger_readiness_count(item), _trigger_gap_score(item)),
    )
    near_buffer = [item for item in ordered if _is_near_buffer_candidate(item)]
    return {
        "count": len(samples),
        "aggregate": dict(counts),
        "top_samples": [asdict(item) for item in ordered[:sample_limit]],
        "near_buffer_candidates": {
            "count": len(near_buffer),
            "long_count": sum(1 for item in near_buffer if item.bias_side == "LONG"),
            "short_count": sum(1 for item in near_buffer if item.bias_side == "SHORT"),
            "top_samples": [asdict(item) for item in near_buffer[:sample_limit]],
        },
        "setup_candidates": _summarize_setup_candidates(samples, sample_limit=3),
    }


def _summarize_volume_samples(samples: list[VolumeSample], *, sample_limit: int) -> dict[str, Any]:
    counts = Counter()
    ratios = [float(item.vol_ratio) for item in samples]
    for sample in samples:
        if sample.vol_ratio >= 0.84:
            counts["vol_ge_084"] += 1
        if sample.vol_ratio >= 0.80:
            counts["vol_ge_080"] += 1
        if sample.vol_ratio >= 0.75:
            counts["vol_ge_075"] += 1

    ordered = sorted(samples, key=lambda item: float(item.vol_ratio), reverse=True)
    return {
        "count": len(samples),
        "aggregate": dict(counts),
        "stats": {
            "avg_vol_ratio": round(mean(ratios), 6) if ratios else 0.0,
            "min_vol_ratio": round(min(ratios), 6) if ratios else 0.0,
            "max_vol_ratio": round(max(ratios), 6) if ratios else 0.0,
        },
        "top_samples": [asdict(item) for item in ordered[:sample_limit]],
    }


def _summarize_cost_samples(samples: list[CostSample], *, sample_limit: int) -> dict[str, Any]:
    counts = Counter(str(item.likely_subreason) for item in samples)
    edge_ratios = [float(item.edge_ratio) for item in samples]
    for sample in samples:
        if sample.edge_ratio >= 0.95:
            counts["edge_ratio_ge_095"] += 1
        if sample.edge_ratio >= 0.90:
            counts["edge_ratio_ge_090"] += 1
        if sample.edge_ratio >= 0.85:
            counts["edge_ratio_ge_085"] += 1
    ordered = sorted(samples, key=lambda item: float(item.edge_ratio), reverse=True)
    return {
        "count": len(samples),
        "aggregate": dict(counts),
        "stats": {
            "avg_edge_ratio": round(mean(edge_ratios), 6) if edge_ratios else 0.0,
            "max_edge_ratio": round(max(edge_ratios), 6) if edge_ratios else 0.0,
        },
        "top_samples": [asdict(item) for item in ordered[:sample_limit]],
    }


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Alpha Expansion Block Samples",
        "",
        f"- profile: `{report['profile']}`",
        f"- symbol: `{report['symbol']}`",
        f"- window: `{report['start_utc']}` -> `{report['end_utc']}`",
        f"- generated_at: `{report['generated_at']}`",
        "",
        "## Reason Counts",
    ]
    for reason, count in report["reason_counts"]:
        lines.append(f"- `{reason}`: {count}")
    trigger = report["trigger_missing"]
    lines.extend(
        [
            "",
            "## Trigger Missing",
            f"- count: {trigger['count']}",
            f"- aggregate: `{json.dumps(trigger['aggregate'], ensure_ascii=False)}`",
            f"- near_buffer_candidates: `{json.dumps(trigger['near_buffer_candidates'], ensure_ascii=False)}`",
            f"- setup_candidates: `{json.dumps(trigger['setup_candidates'], ensure_ascii=False)}`",
            "",
            "### Top Trigger Samples",
        ]
    )
    for sample in trigger["top_samples"]:
        lines.append(f"- `{json.dumps(sample, ensure_ascii=False)}`")
    volume = report["volume_missing"]
    lines.extend(
        [
            "",
            "## Volume Missing",
            f"- count: {volume['count']}",
            f"- stats: `{json.dumps(volume['stats'], ensure_ascii=False)}`",
            f"- aggregate: `{json.dumps(volume['aggregate'], ensure_ascii=False)}`",
            "",
            "### Top Volume Samples",
        ]
    )
    for sample in volume["top_samples"]:
        lines.append(f"- `{json.dumps(sample, ensure_ascii=False)}`")
    cost = report["cost_missing"]
    lines.extend(
        [
            "",
            "## Cost Missing",
            f"- count: {cost['count']}",
            f"- stats: `{json.dumps(cost['stats'], ensure_ascii=False)}`",
            f"- aggregate: `{json.dumps(cost['aggregate'], ensure_ascii=False)}`",
            "",
            "### Top Cost Samples",
        ]
    )
    for sample in cost["top_samples"]:
        lines.append(f"- `{json.dumps(sample, ensure_ascii=False)}`")
    return "\n".join(lines) + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_block_sample_report(
    *,
    profile: str,
    symbol: str,
    start_utc: str,
    end_utc: str,
    sample_limit: int,
    report_dir: str,
) -> tuple[dict[str, Any], Path, Path, Path, Path, Path]:
    start_ms = _parse_utc_datetime_ms(start_utc)
    end_ms = _parse_utc_datetime_ms(end_utc)
    cache_root = Path(report_dir) / "_cache"

    def _raw_interval(interval: str) -> list[Any]:
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

    candles_15m = _raw_interval("15m")
    candles_1h = _raw_interval("1h")
    candles_4h = _raw_interval("4h")
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
    strategy = get_build_strategy_selector()(
        behavior=cfg.behavior,
        snapshot_provider=provider,
        overheat_fetcher=None,
        journal_logger=None,
    )
    runtime_updater = getattr(strategy, "set_runtime_params", None)
    if callable(runtime_updater):
        runtime_updater(**_local_backtest_profile_alpha_overrides(profile))

    reason_counter = Counter()
    trigger_samples: list[TriggerSample] = []
    volume_samples: list[VolumeSample] = []
    cost_samples: list[CostSample] = []
    for _ in range(len(provider)):
        snapshot = provider()
        if not snapshot:
            break
        decision = strategy.decide(snapshot)
        if not isinstance(decision, dict):
            continue
        reason = str(decision.get("reason") or "")
        reason_counter[reason] += 1
        alpha_diag = ((decision.get("alpha_diagnostics") or {}).get("alpha_expansion") or {})
        metrics = alpha_diag.get("metrics") or {}
        indicators = decision.get("indicators") or {}
        if reason == "trigger_missing":
            trigger_samples.append(_build_trigger_sample(metrics))
        elif reason == "volume_missing":
            volume_samples.append(_build_volume_sample(metrics=metrics, indicators=indicators))
        elif reason == "cost_missing":
            sample = _build_cost_sample(snapshot=snapshot, cfg=strategy._cfg, symbol=symbol)
            if sample is not None:
                cost_samples.append(sample)

    generated_at = datetime.now(timezone.utc).isoformat()
    report = {
        "generated_at": generated_at,
        "profile": profile,
        "symbol": symbol,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "reason_counts": reason_counter.most_common(10),
        "trigger_missing": _summarize_trigger_samples(trigger_samples, sample_limit=sample_limit),
        "volume_missing": _summarize_volume_samples(volume_samples, sample_limit=sample_limit),
        "cost_missing": _summarize_cost_samples(cost_samples, sample_limit=sample_limit),
    }

    output_root = Path(report_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    json_path = output_root / f"alpha_expansion_block_samples_{stamp}.json"
    md_path = output_root / f"alpha_expansion_block_samples_{stamp}.md"
    near_buffer_csv_path = output_root / f"alpha_expansion_near_buffer_candidates_{stamp}.csv"
    cost_csv_path = output_root / f"alpha_expansion_cost_candidates_{stamp}.csv"
    setup_csv_path = output_root / f"alpha_expansion_setup_candidates_{stamp}.csv"
    near_buffer_rows = [
        asdict(item) for item in trigger_samples if _is_near_buffer_candidate(item)
    ]
    cost_rows = [asdict(item) for item in cost_samples]
    setup_rows = list(report["trigger_missing"]["setup_candidates"]["top_rules"])
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    _write_csv(near_buffer_csv_path, near_buffer_rows)
    _write_csv(cost_csv_path, cost_rows)
    _write_csv(setup_csv_path, setup_rows)
    return report, json_path, md_path, near_buffer_csv_path, cost_csv_path, setup_csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract alpha_expansion block samples from cached replay data")
    parser.add_argument("--profile", default="ra_2026_alpha_v2_expansion_live_candidate")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--end-utc", required=True)
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--report-dir", default="local_backtest/reports")
    args = parser.parse_args()

    _, json_path, md_path, near_buffer_csv_path, cost_csv_path, setup_csv_path = build_block_sample_report(
        profile=str(args.profile),
        symbol=str(args.symbol).strip().upper(),
        start_utc=str(args.start_utc),
        end_utc=str(args.end_utc),
        sample_limit=max(int(args.sample_limit), 1),
        report_dir=str(args.report_dir),
    )
    print(f"REPORT_JSON={json_path}")
    print(f"REPORT_MD={md_path}")
    print(f"NEAR_BUFFER_CSV={near_buffer_csv_path}")
    print(f"COST_CSV={cost_csv_path}")
    print(f"SETUP_CSV={setup_csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
