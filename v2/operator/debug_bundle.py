from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "v2" / "scripts" / "export_runtime_debug_bundle.py"
DEBUG_BUNDLE_ROOT = REPO_ROOT / "logs" / "runtime_debug"


def _safe_bundle_dir(bundle_dir: Path) -> Path:
    resolved = bundle_dir.resolve()
    root = DEBUG_BUNDLE_ROOT.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError("bundle_dir_outside_debug_root")
    return resolved


def create_runtime_debug_bundle_archive(*, bundle_dir: str | Path) -> Path:
    source_dir = _safe_bundle_dir(Path(bundle_dir))
    archive_path = source_dir.with_suffix(".zip")
    with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file():
                continue
            archive.write(path, arcname=f"{source_dir.name}/{path.relative_to(source_dir)}")
    return archive_path


def resolve_runtime_debug_bundle_archive(*, archive_name: str) -> Path | None:
    normalized = Path(str(archive_name or "").strip()).name
    if not normalized or not normalized.endswith(".zip"):
        return None
    candidate = (DEBUG_BUNDLE_ROOT / normalized).resolve()
    if candidate.parent != DEBUG_BUNDLE_ROOT.resolve():
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


def export_runtime_debug_bundle(*, label: str, base_url: str, include_all: bool = False) -> dict[str, Any]:
    command = [
        sys.executable,
        str(SCRIPT_PATH),
        "--label",
        str(label),
        "--base-url",
        str(base_url),
    ]
    if include_all:
        command.append("--all")
    proc = subprocess.run(
        command,
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
    archive_path = create_runtime_debug_bundle_archive(bundle_dir=bundle_dir)
    base_url_clean = str(base_url).rstrip("/")
    return {
        "ok": True,
        "bundle_dir": str(bundle_dir),
        "summary_path": str(summary_path),
        "archive_path": str(archive_path),
        "archive_name": archive_path.name,
        "download_url": f"{base_url_clean}/operator/api/debug-bundles/{archive_path.name}",
        "full_export": bool(include_all),
        "returncode": proc.returncode,
    }
