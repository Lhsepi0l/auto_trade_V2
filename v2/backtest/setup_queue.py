from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _to_float(value: Any) -> float | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def _load_rows(csv_path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(csv_path).open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            normalized = dict(row)
            normalized["range_atr"] = _to_float(row.get("range_atr"))
            normalized["body_ratio"] = _to_float(row.get("body_ratio"))
            normalized["width_expansion_frac"] = _to_float(row.get("width_expansion_frac"))
            normalized["edge_ratio"] = _to_float(row.get("edge_ratio"))
            normalized["forward_close_return_bps_16"] = _to_float(row.get("forward_close_return_bps_16"))
            normalized["hypothetical_quality_score_v2"] = _to_float(
                row.get("hypothetical_quality_score_v2")
            )
            normalized["hypothetical_breakout_stability_edge_score"] = _to_float(
                row.get("hypothetical_breakout_stability_edge_score")
            )
            normalized["hypothetical_stop_distance_frac"] = _to_float(
                row.get("hypothetical_stop_distance_frac")
            )
            normalized["_favored_close"] = (
                _to_float(row.get("favored_close_short"))
                if str(row.get("side_hint")) == "SHORT"
                else _to_float(row.get("favored_close_long"))
            )
            normalized["hypothetical_close_over_raw_breakout"] = _to_bool(
                row.get("hypothetical_close_over_raw_breakout")
            )
            normalized["hypothetical_buffer_pierced"] = _to_bool(
                row.get("hypothetical_buffer_pierced")
            )
            normalized["hypothetical_close_over_buffer"] = _to_bool(
                row.get("hypothetical_close_over_buffer")
            )
            rows.append(normalized)
    return rows


@dataclass(frozen=True)
class QueueFollowthroughSummary:
    cluster_name: str
    count: int
    avg_forward16_bps: float | None
    positive_rate_16: float | None
    raw_breakout_within_4: int
    buffer_pierced_within_4: int
    close_over_buffer_within_4: int
    qv2_pass_within_4: int
    cost_ok_within_4: int
    full_confirm_within_4: int


def _cluster_match(row: dict[str, Any], cluster_name: str) -> bool:
    if cluster_name == "cluster2_short_trigger":
        return (
            str(row.get("alpha_reason")) == "trigger_missing"
            and str(row.get("side_hint")) == "SHORT"
            and (row.get("range_atr") or -999.0) >= 1.2
            and (row.get("body_ratio") or -999.0) >= 0.5
            and (row.get("_favored_close") or 999.0) < 0.6
            and (row.get("width_expansion_frac") or 999.0) < 0.05
            and (row.get("edge_ratio") or 999.0) < 0.9
        )
    if cluster_name == "cluster3_long_trigger":
        return (
            str(row.get("alpha_reason")) == "trigger_missing"
            and str(row.get("side_hint")) == "LONG"
            and (row.get("range_atr") or -999.0) >= 1.2
            and (row.get("body_ratio") or -999.0) >= 0.5
            and (row.get("_favored_close") or 999.0) < 0.6
            and (row.get("width_expansion_frac") or -999.0) >= 0.10
            and (row.get("edge_ratio") or -999.0) >= 1.1
        )
    raise ValueError(f"unsupported cluster_name: {cluster_name}")


def analyze_setup_queue_followthrough(
    *,
    csv_path: str,
    cluster_name: str,
    lookahead_bars: int,
    quality_threshold: float,
    stability_threshold: float,
) -> tuple[QueueFollowthroughSummary, Path, Path]:
    rows = _load_rows(csv_path)
    matched_indices = [idx for idx, row in enumerate(rows) if _cluster_match(row, cluster_name)]
    raw_breakout = 0
    buffer_pierced = 0
    close_over_buffer = 0
    qv2_pass = 0
    cost_ok = 0
    full_confirm = 0

    forward16_values = [
        float(rows[idx]["forward_close_return_bps_16"])
        for idx in matched_indices
        if rows[idx]["forward_close_return_bps_16"] is not None
    ]
    for idx in matched_indices:
        window = rows[idx + 1 : idx + 1 + int(lookahead_bars)]
        same_side = [row for row in window if str(row.get("side_hint")) == str(rows[idx].get("side_hint"))]
        has_raw = any(bool(row.get("hypothetical_close_over_raw_breakout")) for row in same_side)
        has_buffer = any(bool(row.get("hypothetical_buffer_pierced")) for row in same_side)
        has_close_buffer = any(bool(row.get("hypothetical_close_over_buffer")) for row in same_side)
        has_qv2 = any(
            (row.get("hypothetical_quality_score_v2") or -999.0) >= float(quality_threshold)
            for row in same_side
        )
        has_cost_ok = any(
            str(row.get("hypothetical_cost_subreason") or "") in {"", "None", "none", "edge_shortfall"}
            for row in same_side
        )
        has_stability = any(
            (row.get("hypothetical_breakout_stability_edge_score") or -999.0)
            >= float(stability_threshold)
            for row in same_side
        )
        raw_breakout += int(has_raw)
        buffer_pierced += int(has_buffer)
        close_over_buffer += int(has_close_buffer)
        qv2_pass += int(has_qv2)
        cost_ok += int(has_cost_ok)
        full_confirm += int(has_buffer and has_qv2 and has_cost_ok and has_stability)

    summary = QueueFollowthroughSummary(
        cluster_name=cluster_name,
        count=len(matched_indices),
        avg_forward16_bps=round(sum(forward16_values) / len(forward16_values), 6)
        if forward16_values
        else None,
        positive_rate_16=round(
            sum(1 for value in forward16_values if float(value) > 0.0) / len(forward16_values),
            6,
        )
        if forward16_values
        else None,
        raw_breakout_within_4=raw_breakout,
        buffer_pierced_within_4=buffer_pierced,
        close_over_buffer_within_4=close_over_buffer,
        qv2_pass_within_4=qv2_pass,
        cost_ok_within_4=cost_ok,
        full_confirm_within_4=full_confirm,
    )

    output_root = Path(csv_path).parent
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    json_path = output_root / f"{cluster_name}_setup_queue_{stamp}.json"
    md_path = output_root / f"{cluster_name}_setup_queue_{stamp}.md"
    json_path.write_text(json.dumps(asdict(summary), ensure_ascii=False, indent=2), encoding="utf-8")
    md_lines = [
        f"# {cluster_name} Setup Queue",
        "",
        f"- source_csv: `{csv_path}`",
        f"- lookahead_bars: `{lookahead_bars}`",
        "",
        f"- summary: `{json.dumps(asdict(summary), ensure_ascii=False)}`",
    ]
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return summary, json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze whether profitable trigger-miss setups confirm in later bars")
    parser.add_argument("--csv-path", required=True)
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument("--lookahead-bars", type=int, default=4)
    parser.add_argument("--quality-threshold", type=float, default=0.62)
    parser.add_argument("--stability-threshold", type=float, default=0.62)
    args = parser.parse_args()

    _, json_path, md_path = analyze_setup_queue_followthrough(
        csv_path=str(args.csv_path),
        cluster_name=str(args.cluster_name),
        lookahead_bars=max(int(args.lookahead_bars), 1),
        quality_threshold=float(args.quality_threshold),
        stability_threshold=float(args.stability_threshold),
    )
    print(f"REPORT_JSON={json_path}")
    print(f"REPORT_MD={md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
