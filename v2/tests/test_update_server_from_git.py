from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "v2" / "scripts" / "update_server_from_git.sh"


def _run_dry_run(*args: str) -> str:
    completed = subprocess.run(
        ["bash", str(SCRIPT_PATH), "--dry-run", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def test_update_server_from_git_defaults_to_integration_branch() -> None:
    stdout = _run_dry_run()
    assert "git fetch origin" in stdout
    assert "git checkout migration/web-operator-panel" in stdout
    assert "git pull --ff-only origin migration/web-operator-panel" in stdout
    assert "sudo systemctl restart v2-stack.service" in stdout


def test_update_server_from_git_supports_main_without_restart() -> None:
    stdout = _run_dry_run("--branch", "main", "--no-restart")
    assert "git checkout main" in stdout
    assert "git pull --ff-only origin main" in stdout
    assert "sudo systemctl restart v2-stack.service" not in stdout
