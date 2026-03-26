#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SQLITE_PATH = REPO_ROOT / "data" / "v2_runtime.sqlite3"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "logs" / "runtime_debug"
DEFAULT_LOG_DIRS = [REPO_ROOT / "v2" / "logs", REPO_ROOT / "logs"]
DEFAULT_BASE_URL = "http://127.0.0.1:8101"
DEFAULT_SERVICE_UNIT = "v2-stack.service"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _stamp() -> str:
    return _utcnow().strftime("%Y%m%dT%H%M%SZ")


def _slug(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value))
    collapsed = []
    last = ""
    for ch in text:
        if ch == "_" and last == "_":
            continue
        collapsed.append(ch)
        last = ch
    return "".join(collapsed).strip("_") or "manual"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _tail_lines(path: Path, *, limit: int | None) -> str:
    if limit is None:
        return path.read_text(encoding="utf-8", errors="replace")
    lines: deque[str] = deque(maxlen=max(1, int(limit)))
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            lines.append(line.rstrip("\n"))
    return "\n".join(lines) + ("\n" if lines else "")


def _run_command(
    argv: list[str],
    *,
    cwd: Path,
    timeout_sec: float = 15.0,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "argv": argv,
        }
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "argv": argv,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + f"\nTimeoutExpired: {timeout_sec}s",
            "argv": argv,
        }


def _fetch_endpoint(url: str, *, timeout_sec: float) -> dict[str, Any]:
    req = request.Request(url, headers={"Accept": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            headers = dict(resp.headers.items())
            status = int(resp.status)
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        headers = dict(exc.headers.items())
        status = int(exc.code)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": None,
            "headers": {},
            "raw_text": "",
            "json": None,
            "error": f"{type(exc).__name__}: {exc}",
            "url": url,
            "captured_at": _utcnow_iso(),
        }

    payload: Any = None
    try:
        payload = json.loads(raw) if raw.strip() else None
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = None
    return {
        "ok": 200 <= status < 300,
        "status": status,
        "headers": headers,
        "raw_text": raw,
        "json": payload,
        "error": None,
        "url": url,
        "captured_at": _utcnow_iso(),
    }


def _parse_json_text(raw: Any) -> Any:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _sqlite_rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _read_sqlite_snapshot(
    sqlite_path: Path,
    *,
    operator_limit: int | None,
    journal_limit: int | None,
    submission_limit: int | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "exists": sqlite_path.exists(),
        "path": str(sqlite_path),
        "tables": [],
        "operator_events": [],
        "journal_events": [],
        "submission_intents": [],
        "bracket_states": [],
        "runtime_markers": {},
        "ops_state": None,
        "runtime_risk_config": {},
        "error": None,
    }
    if not sqlite_path.exists():
        return out

    try:
        conn = sqlite3.connect(str(sqlite_path))
        conn.row_factory = sqlite3.Row
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    try:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name ASC"
            ).fetchall()
        ]
        out["tables"] = tables

        if "runtime_risk_config" in tables:
            rows = _sqlite_rows(conn, "SELECT config_json, updated_at FROM runtime_risk_config WHERE id=1")
            if rows:
                payload = _parse_json_text(rows[0].get("config_json"))
                out["runtime_risk_config"] = {
                    "config": payload if isinstance(payload, dict) else {},
                    "updated_at": rows[0].get("updated_at"),
                }

        if "ops_state" in tables:
            rows = _sqlite_rows(conn, "SELECT paused, safe_mode, updated_at FROM ops_state WHERE id=1")
            out["ops_state"] = rows[0] if rows else None

        if "runtime_markers" in tables:
            rows = _sqlite_rows(
                conn,
                "SELECT marker_key, payload_json, updated_at FROM runtime_markers ORDER BY marker_key ASC",
            )
            markers: dict[str, Any] = {}
            for row in rows:
                marker_key = str(row.get("marker_key") or "").strip()
                if not marker_key:
                    continue
                markers[marker_key] = {
                    "payload": _parse_json_text(row.get("payload_json")) or {},
                    "updated_at": row.get("updated_at"),
                }
            out["runtime_markers"] = markers

        if "operator_events" in tables:
            operator_query = """
                SELECT id, event_type, category, title, main_text, sub_text, event_time, context_json, created_at
                FROM operator_events
                ORDER BY id DESC
            """.strip()
            operator_params: tuple[Any, ...] = ()
            if operator_limit is not None:
                operator_query += "\nLIMIT ?"
                operator_params = (max(1, int(operator_limit)),)
            rows = _sqlite_rows(conn, operator_query, operator_params)
            for row in rows:
                row["context"] = _parse_json_text(row.get("context_json")) or {}
            out["operator_events"] = rows

        if "journal_events" in tables:
            journal_query = """
                SELECT id, event_id, event_type, reason, payload_json, created_at
                FROM journal_events
                ORDER BY id DESC
            """.strip()
            journal_params: tuple[Any, ...] = ()
            if journal_limit is not None:
                journal_query += "\nLIMIT ?"
                journal_params = (max(1, int(journal_limit)),)
            rows = _sqlite_rows(conn, journal_query, journal_params)
            for row in rows:
                row["payload"] = _parse_json_text(row.get("payload_json")) or {}
            out["journal_events"] = rows

        if "submission_intents" in tables:
            submission_query = """
                SELECT intent_id, client_order_id, symbol, side, status, order_id, created_at, updated_at
                FROM submission_intents
                ORDER BY updated_at DESC
            """.strip()
            submission_params: tuple[Any, ...] = ()
            if submission_limit is not None:
                submission_query += "\nLIMIT ?"
                submission_params = (max(1, int(submission_limit)),)
            out["submission_intents"] = _sqlite_rows(conn, submission_query, submission_params)

        if "bracket_states" in tables:
            out["bracket_states"] = _sqlite_rows(
                conn,
                "SELECT symbol, tp_order_client_id, sl_order_client_id, state, updated_at FROM bracket_states ORDER BY symbol ASC",
            )
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()

    return out


def _scan_log_files(log_dirs: list[Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for root in log_dirs:
        if not root.exists():
            continue
        patterns = ("*.log", "*.txt", "*/*.log", "*/*.txt", "*/*/*.log", "*/*/*.txt")
        for pattern in patterns:
            for path in sorted(root.glob(pattern)):
                if not path.is_file():
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                found.append(path)
    return sorted(found)


def _relative_label(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        relative = path.name
    return str(relative)


def _write_command_capture(path: Path, result: dict[str, Any]) -> None:
    lines = [
        f"ok={result.get('ok')}",
        f"returncode={result.get('returncode')}",
        f"argv={' '.join(result.get('argv') or [])}",
        "",
        "[stdout]",
        str(result.get("stdout") or ""),
        "",
        "[stderr]",
        str(result.get("stderr") or ""),
    ]
    _write_text(path, "\n".join(lines).rstrip() + "\n")


def _summary_lines(
    *,
    manifest: dict[str, Any],
    http_data: dict[str, dict[str, Any]],
    sqlite_data: dict[str, Any],
    captured_logs: list[str],
) -> list[str]:
    status_payload = http_data.get("status", {}).get("json")
    readyz_payload = http_data.get("readyz", {}).get("json")
    readiness_payload = http_data.get("readiness", {}).get("json")

    lines = [
        "# Runtime Debug Bundle",
        "",
        f"- 생성 시각: {manifest['generated_at']}",
        f"- 번들 경로: `{manifest['bundle_dir']}`",
        f"- Control API: `{manifest['base_url']}`",
        f"- SQLite: `{manifest['sqlite_path']}`",
        f"- Service Unit: `{manifest['service_unit']}`",
        f"- 전체 추출 모드: `{manifest['full_export']}`",
        "",
    ]

    lines.append("## 빠른 요약")
    if isinstance(readyz_payload, dict):
        lines.append(f"- `/readyz.ready`: `{readyz_payload.get('ready')}`")
        lines.append(f"- `state_uncertain`: `{readyz_payload.get('state_uncertain')}`")
        lines.append(f"- `recovery_required`: `{readyz_payload.get('recovery_required')}`")
        lines.append(f"- `user_ws_stale`: `{readyz_payload.get('user_ws_stale')}`")
        lines.append(f"- `market_data_stale`: `{readyz_payload.get('market_data_stale')}`")
        lines.append(f"- `private_auth_ok`: `{readyz_payload.get('private_auth_ok')}`")
    else:
        lines.append(
            f"- `/readyz`: 수집 실패 ({http_data.get('readyz', {}).get('error') or 'unavailable'})"
        )

    if isinstance(status_payload, dict):
        capital = status_payload.get("capital_snapshot", {})
        pnl = status_payload.get("pnl", {})
        lines.append(
            f"- `capital.block_reason`: `{capital.get('block_reason')}` / `blocked={capital.get('blocked')}`"
        )
        lines.append(
            f"- `last_strategy_block_reason`: `{pnl.get('last_strategy_block_reason')}`"
        )
        lines.append(f"- `last_auto_risk_reason`: `{pnl.get('last_auto_risk_reason')}`")
    else:
        lines.append(
            f"- `/status`: 수집 실패 ({http_data.get('status', {}).get('error') or 'unavailable'})"
        )

    if isinstance(readiness_payload, dict):
        lines.append(f"- `readiness.summary`: `{readiness_payload.get('summary')}`")
    else:
        lines.append(
            f"- `/readiness`: 수집 실패 ({http_data.get('readiness', {}).get('error') or 'unavailable'})"
        )

    ops_state = sqlite_data.get("ops_state")
    if isinstance(ops_state, dict):
        lines.append(
            f"- DB ops_state: `paused={bool(ops_state.get('paused'))}`, `safe_mode={bool(ops_state.get('safe_mode'))}`"
        )

    risk_payload = sqlite_data.get("runtime_risk_config", {})
    config = risk_payload.get("config") if isinstance(risk_payload, dict) else {}
    if isinstance(config, dict) and config:
        lines.append(
            "- DB risk: "
            f"`max_leverage={config.get('max_leverage')}`, "
            f"`margin_budget_usdt={config.get('margin_budget_usdt')}`, "
            f"`margin_use_pct={config.get('margin_use_pct')}`, "
            f"`universe_symbols={config.get('universe_symbols')}`"
        )
    lines.append("")

    lines.append("## 최근 Operator 이벤트")
    operator_events = sqlite_data.get("operator_events") if isinstance(sqlite_data, dict) else None
    if isinstance(operator_events, list) and operator_events:
        for row in operator_events[:15]:
            lines.append(
                "- "
                f"{row.get('event_time')} | {row.get('category')} | {row.get('event_type')} | "
                f"{row.get('title')} | {row.get('main_text')}"
            )
    else:
        lines.append("- operator_events 없음")
    lines.append("")

    lines.append("## 캡처된 로그 파일")
    if captured_logs:
        for item in captured_logs:
            lines.append(f"- `{item}`")
    else:
        lines.append("- 캡처된 로그 파일 없음")
    lines.append("")

    lines.append("## 파일 안내")
    lines.append("- `control/`: `/healthz`, `/readyz`, `/readiness`, `/status` 캡처")
    lines.append("- `sqlite/`: operator_events, runtime_risk_config, runtime_markers, ops_state 등 DB 스냅샷")
    lines.append("- `system/`: journalctl, systemctl, sockets, processes, git 상태")
    lines.append("- `logs/`: 로컬 로그 tail")
    return lines


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect runtime/operator logs into a single debug bundle.",
    )
    parser.add_argument("--label", default="manual", help="Bundle label suffix")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Control API base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_SQLITE_PATH),
        help=f"Runtime SQLite path (default: {DEFAULT_SQLITE_PATH})",
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"Output root directory (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument(
        "--service-unit",
        default=DEFAULT_SERVICE_UNIT,
        help=f"Systemd unit to inspect (default: {DEFAULT_SERVICE_UNIT})",
    )
    parser.add_argument(
        "--operator-limit",
        type=int,
        default=200,
        help="Operator events to export from SQLite (default: 200)",
    )
    parser.add_argument(
        "--journal-limit",
        type=int,
        default=200,
        help="Journal rows to export from SQLite journal_events (default: 200)",
    )
    parser.add_argument(
        "--journal-lines",
        type=int,
        default=400,
        help="journalctl tail lines (default: 400)",
    )
    parser.add_argument(
        "--tail-lines",
        type=int,
        default=400,
        help="Log tail lines per file (default: 400)",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=4.0,
        help="HTTP timeout seconds (default: 4.0)",
    )
    parser.add_argument(
        "--skip-http",
        action="store_true",
        help="Skip Control API endpoint capture",
    )
    parser.add_argument(
        "--skip-journal",
        action="store_true",
        help="Skip systemd/journalctl capture",
    )
    parser.add_argument(
        "--log-dir",
        action="append",
        default=[],
        help="Additional log directory to tail. Can be passed multiple times.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Export full operator/journal/log history instead of bounded tails.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    label = _slug(args.label)
    bundle_dir = Path(args.output_root) / f"{_stamp()}_{label}"
    control_dir = bundle_dir / "control"
    sqlite_dir = bundle_dir / "sqlite"
    system_dir = bundle_dir / "system"
    log_dir = bundle_dir / "logs"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    log_dirs = [Path(item) for item in args.log_dir] if args.log_dir else list(DEFAULT_LOG_DIRS)
    sqlite_path = Path(args.sqlite_path)

    manifest = {
        "generated_at": _utcnow_iso(),
        "bundle_dir": str(bundle_dir),
        "base_url": args.base_url.rstrip("/"),
        "sqlite_path": str(sqlite_path),
        "service_unit": args.service_unit,
        "repo_root": str(REPO_ROOT),
        "log_dirs": [str(path) for path in log_dirs],
        "full_export": bool(args.all),
    }
    _write_json(bundle_dir / "manifest.json", manifest)

    command_specs = [
        ("git_head.txt", ["git", "rev-parse", "HEAD"]),
        ("git_branch.txt", ["git", "branch", "--show-current"]),
        ("git_status.txt", ["git", "status", "--short"]),
        ("git_diff_stat.txt", ["git", "diff", "--stat"]),
        ("hostname.txt", ["hostname"]),
        ("uname.txt", ["uname", "-a"]),
        ("python_version.txt", [sys.executable, "-V"]),
        ("processes.txt", ["pgrep", "-af", "python -m v2.run|run_stack.sh|v2-stack.service"]),
        ("sockets.txt", ["ss", "-ltnp"]),
    ]
    for filename, command in command_specs:
        _write_command_capture(
            system_dir / filename,
            _run_command(command, cwd=REPO_ROOT),
        )

    if not args.skip_journal:
        journal_command = ["journalctl", "-u", args.service_unit, "--no-pager"]
        if not args.all:
            journal_command[3:3] = ["-n", str(max(1, int(args.journal_lines)))]
        journal_specs = [
            ("systemctl_status.txt", ["systemctl", "status", args.service_unit, "--no-pager"]),
            ("journalctl_tail.txt", journal_command),
        ]
        for filename, command in journal_specs:
            _write_command_capture(
                system_dir / filename,
                _run_command(command, cwd=REPO_ROOT),
            )

    http_data: dict[str, dict[str, Any]] = {}
    if not args.skip_http:
        for endpoint in ("healthz", "readyz", "readiness", "status"):
            payload = _fetch_endpoint(
                f"{args.base_url.rstrip('/')}/{endpoint}",
                timeout_sec=max(float(args.http_timeout), 0.1),
            )
            http_data[endpoint] = payload
            _write_json(control_dir / f"{endpoint}.meta.json", payload)
            _write_text(control_dir / f"{endpoint}.raw.txt", str(payload.get("raw_text") or ""))
            if payload.get("json") is not None:
                _write_json(control_dir / f"{endpoint}.json", payload["json"])

    sqlite_data = _read_sqlite_snapshot(
        sqlite_path,
        operator_limit=None if args.all else args.operator_limit,
        journal_limit=None if args.all else args.journal_limit,
        submission_limit=None if args.all else 200,
    )
    _write_json(sqlite_dir / "snapshot.json", sqlite_data)

    if isinstance(sqlite_data.get("operator_events"), list):
        _write_json(sqlite_dir / "operator_events.json", sqlite_data["operator_events"])
    if isinstance(sqlite_data.get("journal_events"), list):
        _write_json(sqlite_dir / "journal_events.json", sqlite_data["journal_events"])
    if isinstance(sqlite_data.get("submission_intents"), list):
        _write_json(sqlite_dir / "submission_intents.json", sqlite_data["submission_intents"])
    if isinstance(sqlite_data.get("bracket_states"), list):
        _write_json(sqlite_dir / "bracket_states.json", sqlite_data["bracket_states"])
    if sqlite_data.get("ops_state") is not None:
        _write_json(sqlite_dir / "ops_state.json", sqlite_data["ops_state"])
    if isinstance(sqlite_data.get("runtime_markers"), dict):
        _write_json(sqlite_dir / "runtime_markers.json", sqlite_data["runtime_markers"])
    if isinstance(sqlite_data.get("runtime_risk_config"), dict):
        _write_json(sqlite_dir / "runtime_risk_config.json", sqlite_data["runtime_risk_config"])

    captured_logs: list[str] = []
    for source in _scan_log_files(log_dirs):
        try:
            content = _tail_lines(
                source,
                limit=None if args.all else max(1, int(args.tail_lines)),
            )
        except Exception as exc:  # noqa: BLE001
            content = f"{type(exc).__name__}: {exc}\n"
        label_name = _slug(_relative_label(source))
        captured_logs.append(_relative_label(source))
        _write_text(log_dir / f"{label_name}.tail.log", content)

    summary = "\n".join(
        _summary_lines(
            manifest=manifest,
            http_data=http_data,
            sqlite_data=sqlite_data,
            captured_logs=captured_logs,
        )
    ).rstrip() + "\n"
    _write_text(bundle_dir / "SUMMARY.md", summary)

    print(str(bundle_dir))
    print(str(bundle_dir / "SUMMARY.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
