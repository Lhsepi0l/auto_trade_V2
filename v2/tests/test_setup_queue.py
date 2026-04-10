from __future__ import annotations

import csv
import json

from v2.backtest.setup_queue import analyze_setup_queue_followthrough


def _write_csv(path, rows):  # type: ignore[no-untyped-def]
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_analyze_setup_queue_followthrough_counts_confirmations(tmp_path) -> None:
    rows = []
    for _idx in range(50):
        rows.append(
            {
                "alpha_reason": "trigger_missing",
                "side_hint": "SHORT",
                "range_atr": "1.5",
                "body_ratio": "0.6",
                "favored_close_short": "0.4",
                "favored_close_long": "0.6",
                "width_expansion_frac": "0.02",
                "edge_ratio": "0.8",
                "forward_close_return_bps_16": "20.0",
                "hypothetical_close_over_raw_breakout": "False",
                "hypothetical_buffer_pierced": "False",
                "hypothetical_close_over_buffer": "False",
                "hypothetical_quality_score_v2": "0.10",
                "hypothetical_breakout_stability_edge_score": "0.10",
                "hypothetical_cost_subreason": "edge_shortfall",
            }
        )
        rows.append(
            {
                "alpha_reason": "trigger_missing",
                "side_hint": "SHORT",
                "range_atr": "1.0",
                "body_ratio": "0.4",
                "favored_close_short": "0.8",
                "favored_close_long": "0.2",
                "width_expansion_frac": "0.15",
                "edge_ratio": "1.2",
                "forward_close_return_bps_16": "5.0",
                "hypothetical_close_over_raw_breakout": "True",
                "hypothetical_buffer_pierced": "True",
                "hypothetical_close_over_buffer": "True",
                "hypothetical_quality_score_v2": "0.80",
                "hypothetical_breakout_stability_edge_score": "0.85",
                "hypothetical_cost_subreason": "",
            }
        )
    csv_path = tmp_path / "event_tape.csv"
    _write_csv(csv_path, rows)

    summary, json_path, md_path = analyze_setup_queue_followthrough(
        csv_path=str(csv_path),
        cluster_name="cluster2_short_trigger",
        lookahead_bars=1,
        quality_threshold=0.62,
        stability_threshold=0.62,
    )

    assert summary.count == 50
    assert summary.raw_breakout_within_4 == 50
    assert summary.buffer_pierced_within_4 == 50
    assert summary.qv2_pass_within_4 == 50
    assert summary.cost_ok_within_4 == 50
    assert summary.full_confirm_within_4 == 50
    assert json.loads(json_path.read_text(encoding="utf-8"))["cluster_name"] == "cluster2_short_trigger"
    assert "Setup Queue" in md_path.read_text(encoding="utf-8")
