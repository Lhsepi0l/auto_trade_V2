from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "v2" / "scripts" / "export_runtime_debug_bundle.py"


def export_runtime_debug_bundle(*, label: str, base_url: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--label",
            str(label),
            "--base-url",
            str(base_url),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60.0,
        check=False,
    )

    stdout_lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": "debug_bundle_export_failed",
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip(),
            "stdout": proc.stdout.strip(),
        }

    if len(stdout_lines) < 2:
        return {
            "ok": False,
            "error": "debug_bundle_export_output_invalid",
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip(),
            "stdout": proc.stdout.strip(),
        }

    bundle_dir = Path(stdout_lines[0]).resolve()
    summary_path = Path(stdout_lines[1]).resolve()
    return {
        "ok": True,
        "bundle_dir": str(bundle_dir),
        "summary_path": str(summary_path),
        "returncode": proc.returncode,
    }
