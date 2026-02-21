from __future__ import annotations

import json
from pathlib import Path

from v2.run import _load_replay_frames, main


def _write_config_with_temp_storage(*, base_path: Path, db_path: Path, out_path: Path) -> Path:
    source = base_path.read_text(encoding="utf-8")
    if "sqlite_path: data/v2_runtime.sqlite3" not in source:
        raise RuntimeError("unexpected v2 config template")
    out_path.write_text(
        source.replace("sqlite_path: data/v2_runtime.sqlite3", f"sqlite_path: {db_path}"),
        encoding="utf-8",
    )
    return out_path


def _write_replay_json(path: Path, *, symbol: str = "BTCUSDT") -> None:
    rows = [
        {
            "symbol": symbol,
            "market": {
                "4h": [
                    {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5},
                    {"open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5},
                    {"open": 101.5, "high": 103.0, "low": 101.0, "close": 102.2},
                    {"open": 102.2, "high": 104.0, "low": 101.8, "close": 103.1},
                    {"open": 103.1, "high": 105.0, "low": 102.9, "close": 104.0},
                ],
                "1h": [
                    {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5},
                    {"open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5},
                    {"open": 101.5, "high": 103.0, "low": 101.0, "close": 102.2},
                    {"open": 102.2, "high": 104.0, "low": 101.8, "close": 103.1},
                    {"open": 103.1, "high": 105.0, "low": 102.9, "close": 104.0},
                ],
                "15m": [
                    {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5},
                    {"open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5},
                ],
            },
            "timestamp": "2026-01-01T00:00:00Z",
        }
    ]
    path.write_text(json.dumps(rows, ensure_ascii=True), encoding="utf-8")


def _write_replay_csv(path: Path, *, symbol: str = "BTCUSDT") -> None:
    path.write_text(
        "\n".join(
            [
                "symbol,interval,open,high,low,close,timestamp",
                "BTCUSDT,4h,100,101,99,100.5,2026-01-01T00:00:00Z",
                "BTCUSDT,1h,100.5,102,100,101.5,2026-01-01T00:00:00Z",
                "BTCUSDT,15m,100.0,101.0,99.0,100.5,2026-01-01T00:00:00Z",
            ]
        ),
        encoding="utf-8",
    )


def test_load_replay_frames_supports_json_and_csv(tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    replay_json = tmp_path / "replay.json"
    replay_csv = tmp_path / "replay.csv"
    _write_replay_json(replay_json)
    _write_replay_csv(replay_csv)

    json_frames = _load_replay_frames(path=str(replay_json), default_symbol="BTCUSDT")
    csv_frames = _load_replay_frames(path=str(replay_csv), default_symbol="BTCUSDT")

    assert len(json_frames) == 1
    assert json_frames[0].symbol == "BTCUSDT"
    assert set(json_frames[0].market) == {"4h", "1h", "15m"}

    assert len(csv_frames) == 1
    assert csv_frames[0].symbol == "BTCUSDT"
    assert csv_frames[0].meta == {"timestamp": "2026-01-01T00:00:00Z"}


def test_replay_run_generates_report_file(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    base_config = Path("v2/config/config.yaml")
    temp_config = tmp_path / "config.replay.yaml"
    sqlite_path = tmp_path / "replay.sqlite3"
    report_dir = tmp_path / "reports"
    _write_config_with_temp_storage(
        base_path=base_config,
        db_path=sqlite_path,
        out_path=temp_config,
    )

    replay_path = tmp_path / "replay.json"
    _write_replay_json(replay_path)

    rc = main(
        [
            "--mode",
            "shadow",
            "--config",
            str(temp_config),
            "--replay",
            str(replay_path),
            "--report-dir",
            str(report_dir),
        ]
    )

    output = capsys.readouterr().out
    assert rc == 0

    reported = None
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        replay_block = payload.get("replay")
        if isinstance(replay_block, dict) and replay_block.get("status") == "completed":
            reported = replay_block
            break
    assert reported is not None
    report_path = Path(str(reported["report"]))
    assert report_path.exists()

    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert report_payload["summary"]["total_cycles"] == 1
    assert report_payload["summary"]["state_distribution"].get("no_candidate", 0) >= 0
    assert len(report_payload["cycles"]) == 1


def test_replay_run_with_empty_input_returns_error(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    base_config = Path("v2/config/config.yaml")
    temp_config = tmp_path / "config.replay.yaml"
    sqlite_path = tmp_path / "replay.sqlite3"
    _write_config_with_temp_storage(
        base_path=base_config,
        db_path=sqlite_path,
        out_path=temp_config,
    )

    replay_path = tmp_path / "empty.json"
    replay_path.write_text("[]", encoding="utf-8")

    rc = main(
        [
            "--mode",
            "shadow",
            "--config",
            str(temp_config),
            "--replay",
            str(replay_path),
        ]
    )

    output = capsys.readouterr().out
    assert rc == 1
    assert "\"error\": \"no replay data\"" in output


def test_load_replay_frames_supports_jsonl(tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    replay_jsonl = tmp_path / "replay.jsonl"
    row = {
        "symbol": "BTCUSDT",
        "market": {
            "4h": [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}],
            "1h": [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}],
            "15m": [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}],
        },
        "timestamp": "2026-01-01T00:00:00Z",
    }
    replay_jsonl.write_text("\n".join([json.dumps(row)]), encoding="utf-8")

    frames = _load_replay_frames(path=str(replay_jsonl), default_symbol="BTCUSDT")

    assert len(frames) == 1
    assert frames[0].symbol == "BTCUSDT"
    assert frames[0].meta == {"timestamp": "2026-01-01T00:00:00Z"}
