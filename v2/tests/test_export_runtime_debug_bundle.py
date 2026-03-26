from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from v2.operator.debug_bundle import create_runtime_debug_bundle_archive
from v2.storage import RuntimeStorage


def test_export_runtime_debug_bundle_creates_summary_and_sqlite_exports(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    sqlite_path = tmp_path / "runtime.sqlite3"
    storage = RuntimeStorage(sqlite_path=str(sqlite_path))
    storage.ensure_schema()
    storage.set_ops_state(paused=True, safe_mode=True)
    storage.save_runtime_risk_config(
        config={
            "max_leverage": 50.0,
            "margin_budget_usdt": 35.0,
            "margin_use_pct": 0.1,
            "universe_symbols": ["BTCUSDT"],
        }
    )
    storage.save_runtime_marker(
        marker_key="runtime_boot",
        payload={"profile": "ra_2026_alpha_v2_expansion_verified_q070", "mode": "live"},
    )
    storage.append_operator_event(
        event_type="user_stream_disconnect",
        category="state",
        title="프라이빗 스트림 끊김",
        main_text="user_stream_disconnected",
        sub_text="state_uncertain=true",
        event_time="2026-03-27T00:00:00+00:00",
        context={"reason": "user_stream_disconnected"},
    )

    custom_log_dir = tmp_path / "logs"
    custom_log_dir.mkdir(parents=True)
    log_file = custom_log_dir / "control_api.log"
    log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

    output_root = tmp_path / "bundle_out"
    script = Path("v2/scripts/export_runtime_debug_bundle.py")
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--label",
            "unit_test",
            "--sqlite-path",
            str(sqlite_path),
            "--output-root",
            str(output_root),
            "--skip-journal",
            "--base-url",
            "http://127.0.0.1:9",
            "--log-dir",
            str(custom_log_dir),
            "--tail-lines",
            "2",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) >= 2

    bundle_dir = Path(lines[0])
    summary_path = Path(lines[1])
    assert bundle_dir.exists()
    assert summary_path.exists()

    summary_text = summary_path.read_text(encoding="utf-8")
    assert "Runtime Debug Bundle" in summary_text
    assert "프라이빗 스트림 끊김" in summary_text
    assert "paused=True" in summary_text
    assert "safe_mode=True" in summary_text

    operator_events = json.loads((bundle_dir / "sqlite" / "operator_events.json").read_text(encoding="utf-8"))
    assert operator_events[0]["title"] == "프라이빗 스트림 끊김"

    risk_payload = json.loads(
        (bundle_dir / "sqlite" / "runtime_risk_config.json").read_text(encoding="utf-8")
    )
    assert risk_payload["config"]["max_leverage"] == 50.0

    log_tails = list((bundle_dir / "logs").glob("*.tail.log"))
    assert log_tails
    tail_text = log_tails[0].read_text(encoding="utf-8")
    assert "line2" in tail_text
    assert "line3" in tail_text
    assert "line1" not in tail_text


def test_create_runtime_debug_bundle_archive_writes_zip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bundle_root = tmp_path / "logs" / "runtime_debug"
    bundle_dir = bundle_root / "20260327T000000Z_unit"
    nested = bundle_dir / "sqlite"
    nested.mkdir(parents=True)
    (bundle_dir / "SUMMARY.md").write_text("# summary\n", encoding="utf-8")
    (nested / "operator_events.json").write_text("[]\n", encoding="utf-8")

    from v2.operator import debug_bundle as debug_bundle_module

    original_root = debug_bundle_module.DEBUG_BUNDLE_ROOT
    debug_bundle_module.DEBUG_BUNDLE_ROOT = bundle_root
    try:
        archive_path = create_runtime_debug_bundle_archive(bundle_dir=bundle_dir)
    finally:
        debug_bundle_module.DEBUG_BUNDLE_ROOT = original_root

    assert archive_path.exists()
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert f"{bundle_dir.name}/SUMMARY.md" in names
    assert f"{bundle_dir.name}/sqlite/operator_events.json" in names


def test_export_runtime_debug_bundle_all_mode_captures_full_log_file(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    sqlite_path = tmp_path / "runtime_all.sqlite3"
    storage = RuntimeStorage(sqlite_path=str(sqlite_path))
    storage.ensure_schema()
    storage.append_operator_event(
        event_type="runtime_start",
        category="action",
        title="엔진 시작",
        main_text="started",
        sub_text=None,
        event_time="2026-03-27T00:00:00+00:00",
        context={},
    )

    custom_log_dir = tmp_path / "logs_all"
    custom_log_dir.mkdir(parents=True)
    log_file = custom_log_dir / "control_api.log"
    log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

    output_root = tmp_path / "bundle_all_out"
    script = Path("v2/scripts/export_runtime_debug_bundle.py")
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--label",
            "unit_all",
            "--sqlite-path",
            str(sqlite_path),
            "--output-root",
            str(output_root),
            "--skip-journal",
            "--skip-http",
            "--log-dir",
            str(custom_log_dir),
            "--all",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    bundle_dir = Path(lines[0])
    tail_text = next((bundle_dir / "logs").glob("*.tail.log")).read_text(encoding="utf-8")
    assert "line1" in tail_text
    assert "line2" in tail_text
    assert "line3" in tail_text

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["full_export"] is True
