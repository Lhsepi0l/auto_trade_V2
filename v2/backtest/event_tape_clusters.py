from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from statistics import mean
from typing import Any, Callable


@dataclass(frozen=True)
class ClusterPredicate:
    name: str
    fn: Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class ClusterResult:
    rank_score: float
    count: int
    event_state: str
    alpha_reason: str
    side_hint: str
    avg_close_return_bps_8: float | None
    avg_close_return_bps_12: float | None
    avg_close_return_bps_16: float | None
    positive_rate_16: float | None
    avg_mfe_bps: float | None
    avg_mae_bps: float | None
    predicates: tuple[str, ...]


def _to_float(value: Any) -> float | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _load_event_tape_rows(csv_path: str) -> list[dict[str, Any]]:
    path = Path(csv_path)
    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        rows: list[dict[str, Any]] = []
        for row in reader:
            normalized = dict(row)
            favored_close = _to_float(row.get("favored_close_long"))
            if str(row.get("side_hint") or "") == "SHORT":
                favored_close = _to_float(row.get("favored_close_short"))
            normalized["_favored_close"] = favored_close
            rows.append(normalized)
        return rows


def _predicate_library() -> list[ClusterPredicate]:
    predicates: list[ClusterPredicate] = [
        ClusterPredicate("event_state=candidate", lambda row: str(row.get("event_state")) == "candidate"),
        ClusterPredicate("event_state=blocked", lambda row: str(row.get("event_state")) == "blocked"),
    ]
    for reason in ("entry_signal", "trigger_missing", "cost_missing", "quality_score_v2_missing", "volume_missing"):
        predicates.append(
            ClusterPredicate(
                f"alpha_reason={reason}",
                lambda row, *, _reason=reason: str(row.get("alpha_reason")) == _reason,
            )
        )
    for side in ("LONG", "SHORT"):
        predicates.append(
            ClusterPredicate(
                f"side_hint={side}",
                lambda row, *, _side=side: str(row.get("side_hint")) == _side,
            )
        )
    for threshold in (0.62, 0.70, 0.80):
        predicates.append(
            ClusterPredicate(
                f"quality_score_v2>={threshold:.2f}",
                lambda row, *, _thr=threshold: (_to_float(row.get("quality_score_v2")) or -999.0) >= _thr,
            )
        )
    for threshold in (0.90, 0.95, 1.00, 1.10):
        predicates.append(
            ClusterPredicate(
                f"edge_ratio>={threshold:.2f}",
                lambda row, *, _thr=threshold: (_to_float(row.get("edge_ratio")) or -999.0) >= _thr,
            )
        )
    for threshold in (0.70, 0.90, 1.10):
        predicates.append(
            ClusterPredicate(
                f"range_atr>={threshold:.2f}",
                lambda row, *, _thr=threshold: (_to_float(row.get("range_atr")) or -999.0) >= _thr,
            )
        )
    for threshold in (0.35, 0.45, 0.55):
        predicates.append(
            ClusterPredicate(
                f"body_ratio>={threshold:.2f}",
                lambda row, *, _thr=threshold: (_to_float(row.get("body_ratio")) or -999.0) >= _thr,
            )
        )
    for threshold in (0.60, 0.70, 0.80):
        predicates.append(
            ClusterPredicate(
                f"favored_close>={threshold:.2f}",
                lambda row, *, _thr=threshold: (_to_float(row.get("_favored_close")) or -999.0) >= _thr,
            )
        )
    for threshold in (0.02, 0.05, 0.08):
        predicates.append(
            ClusterPredicate(
                f"width_expansion>={threshold:.2f}",
                lambda row, *, _thr=threshold: (_to_float(row.get("width_expansion_frac")) or -999.0) >= _thr,
            )
        )
    for threshold in (0.90, 1.00, 1.10):
        predicates.append(
            ClusterPredicate(
                f"vol_ratio_15m>={threshold:.2f}",
                lambda row, *, _thr=threshold: (_to_float(row.get("vol_ratio_15m")) or -999.0) >= _thr,
            )
        )
    return predicates


def _cluster_score(
    *,
    count: int,
    avg8: float | None,
    avg12: float | None,
    avg16: float | None,
    pos16: float | None,
) -> float:
    if count <= 0 or avg16 is None or pos16 is None:
        return float("-inf")
    if avg8 is None or avg12 is None:
        return float("-inf")
    if not (avg8 > 0.0 and avg12 > 0.0 and avg16 > 0.0):
        return float("-inf")
    if pos16 < 0.52:
        return float("-inf")
    consistency = ((avg8 + avg12 + avg16) / 3.0) * float(pos16)
    support = math.log2(float(count) + 1.0)
    return float(consistency) * float(support)


def _rows_overlap(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def analyze_event_tape_clusters(
    *,
    csv_path: str,
    min_count: int,
    max_conditions: int,
    top_k: int,
) -> tuple[dict[str, Any], Path, Path, Path]:
    rows = _load_event_tape_rows(csv_path)
    predicates = _predicate_library()
    predicate_matches: list[tuple[ClusterPredicate, set[int]]] = []
    for predicate in predicates:
        matched = {idx for idx, row in enumerate(rows) if predicate.fn(row)}
        predicate_matches.append((predicate, matched))
    candidate_pool: list[tuple[ClusterResult, set[int]]] = []

    for width in range(2, max(int(max_conditions), 2) + 1):
        for combo in combinations(predicate_matches, width):
            combo_preds = [predicate for predicate, _ in combo]
            matched_indices = set(combo[0][1])
            for _, predicate_set in combo[1:]:
                matched_indices &= predicate_set
                if len(matched_indices) < int(min_count):
                    break
            if len(matched_indices) < int(min_count):
                continue
            matched_rows = [rows[idx] for idx in sorted(matched_indices)]
            avg8_values = [_to_float(row.get("forward_close_return_bps_8")) for row in matched_rows]
            avg12_values = [_to_float(row.get("forward_close_return_bps_12")) for row in matched_rows]
            avg16_values = [_to_float(row.get("forward_close_return_bps_16")) for row in matched_rows]
            mfe_values = [_to_float(row.get("forward_mfe_bps")) for row in matched_rows]
            mae_values = [_to_float(row.get("forward_mae_bps")) for row in matched_rows]
            avg8 = mean([value for value in avg8_values if value is not None]) if any(
                value is not None for value in avg8_values
            ) else None
            avg12 = mean([value for value in avg12_values if value is not None]) if any(
                value is not None for value in avg12_values
            ) else None
            valid16 = [value for value in avg16_values if value is not None]
            avg16 = mean(valid16) if valid16 else None
            pos16 = (
                sum(1 for value in valid16 if float(value) > 0.0) / float(len(valid16))
                if valid16
                else None
            )
            score = _cluster_score(
                count=len(matched_indices),
                avg8=avg8,
                avg12=avg12,
                avg16=avg16,
                pos16=pos16,
            )
            if not math.isfinite(score):
                continue
            event_states = {str(row.get("event_state") or "") for row in matched_rows}
            reasons = {str(row.get("alpha_reason") or "") for row in matched_rows}
            sides = {str(row.get("side_hint") or "") for row in matched_rows}
            result = ClusterResult(
                rank_score=round(score, 6),
                count=len(matched_indices),
                event_state=event_states.pop() if len(event_states) == 1 else "mixed",
                alpha_reason=reasons.pop() if len(reasons) == 1 else "mixed",
                side_hint=sides.pop() if len(sides) == 1 else "mixed",
                avg_close_return_bps_8=round(avg8, 6) if avg8 is not None else None,
                avg_close_return_bps_12=round(avg12, 6) if avg12 is not None else None,
                avg_close_return_bps_16=round(avg16, 6) if avg16 is not None else None,
                positive_rate_16=round(pos16, 6) if pos16 is not None else None,
                avg_mfe_bps=round(mean([v for v in mfe_values if v is not None]), 6) if any(v is not None for v in mfe_values) else None,
                avg_mae_bps=round(mean([v for v in mae_values if v is not None]), 6) if any(v is not None for v in mae_values) else None,
                predicates=tuple(predicate.name for predicate in combo_preds),
            )
            candidate_pool.append((result, matched_indices))

    candidate_pool.sort(key=lambda item: float(item[0].rank_score), reverse=True)
    selected: list[tuple[ClusterResult, set[int]]] = []
    for result, matched_indices in candidate_pool:
        if any(_rows_overlap(matched_indices, existing_rows) >= 0.80 for _, existing_rows in selected):
            continue
        selected.append((result, matched_indices))
        if len(selected) >= int(top_k):
            break

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "csv_path": str(csv_path),
        "row_count": len(rows),
        "min_count": int(min_count),
        "max_conditions": int(max_conditions),
        "top_clusters": [asdict(result) for result, _ in selected],
    }

    output_root = Path(csv_path).parent
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    json_path = output_root / f"alpha_expansion_event_clusters_{stamp}.json"
    md_path = output_root / f"alpha_expansion_event_clusters_{stamp}.md"
    csv_out_path = output_root / f"alpha_expansion_event_clusters_{stamp}.csv"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md_lines = [
        "# Alpha Expansion Event Clusters",
        "",
        f"- csv_path: `{csv_path}`",
        f"- row_count: `{len(rows)}`",
        "",
        "## Top Clusters",
    ]
    for result, _ in selected:
        md_lines.append(f"- `{json.dumps(asdict(result), ensure_ascii=False)}`")
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    _write_cluster_csv(csv_out_path, [asdict(result) for result, _ in selected])
    return summary, json_path, md_path, csv_out_path


def _write_cluster_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rank profitable clusters from alpha_expansion event tape CSV")
    parser.add_argument("--csv-path", required=True)
    parser.add_argument("--min-count", type=int, default=40)
    parser.add_argument("--max-conditions", type=int, default=4)
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()

    _, json_path, md_path, csv_path = analyze_event_tape_clusters(
        csv_path=str(args.csv_path),
        min_count=max(int(args.min_count), 5),
        max_conditions=max(int(args.max_conditions), 2),
        top_k=max(int(args.top_k), 1),
    )
    print(f"REPORT_JSON={json_path}")
    print(f"REPORT_MD={md_path}")
    print(f"CLUSTER_CSV={csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
