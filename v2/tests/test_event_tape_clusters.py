from __future__ import annotations

import csv
import json

from v2.backtest.event_tape_clusters import analyze_event_tape_clusters


def _write_csv(path, rows):  # type: ignore[no-untyped-def]
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_analyze_event_tape_clusters_picks_profitable_rule(tmp_path) -> None:
    rows = []
    for _idx in range(60):
        rows.append(
            {
                "event_state": "blocked",
                "alpha_reason": "trigger_missing",
                "side_hint": "LONG",
                "quality_score_v2": "0.82",
                "edge_ratio": "1.05",
                "range_atr": "1.20",
                "body_ratio": "0.52",
                "_favored_close": "",
                "favored_close_long": "0.81",
                "favored_close_short": "0.19",
                "width_expansion_frac": "0.11",
                "vol_ratio_15m": "1.05",
                "forward_close_return_bps_8": "18.0",
                "forward_close_return_bps_12": "22.0",
                "forward_close_return_bps_16": "28.0",
                "forward_mfe_bps": "75.0",
                "forward_mae_bps": "-20.0",
            }
        )
    for _idx in range(60):
        rows.append(
            {
                "event_state": "blocked",
                "alpha_reason": "volume_missing",
                "side_hint": "LONG",
                "quality_score_v2": "0.10",
                "edge_ratio": "0.70",
                "range_atr": "0.40",
                "body_ratio": "0.20",
                "_favored_close": "",
                "favored_close_long": "0.45",
                "favored_close_short": "0.55",
                "width_expansion_frac": "0.01",
                "vol_ratio_15m": "0.50",
                "forward_close_return_bps_8": "-5.0",
                "forward_close_return_bps_12": "-8.0",
                "forward_close_return_bps_16": "-10.0",
                "forward_mfe_bps": "20.0",
                "forward_mae_bps": "-40.0",
            }
        )
    csv_path = tmp_path / "event_tape.csv"
    _write_csv(csv_path, rows)

    summary, json_path, md_path, csv_out_path = analyze_event_tape_clusters(
        csv_path=str(csv_path),
        min_count=20,
        max_conditions=3,
        top_k=3,
    )

    assert summary["row_count"] == 120
    top = summary["top_clusters"][0]
    assert top["alpha_reason"] in {"trigger_missing", "mixed"}
    assert top["avg_close_return_bps_16"] is not None
    assert float(top["avg_close_return_bps_16"]) > 0.0
    assert json.loads(json_path.read_text(encoding="utf-8"))["top_clusters"]
    assert "Alpha Expansion Event Clusters" in md_path.read_text(encoding="utf-8")
    assert "rank_score" in csv_out_path.read_text(encoding="utf-8")
