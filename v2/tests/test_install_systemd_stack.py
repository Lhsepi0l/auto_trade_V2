from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "v2" / "scripts" / "install_systemd_stack.sh"


def _run_dry_run(*args: str) -> str:
    completed = subprocess.run(
        [
            "bash",
            str(SCRIPT_PATH),
            "--dry-run",
            "--user",
            "bot",
            "--workdir",
            str(REPO_ROOT),
            *args,
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def test_install_systemd_stack_defaults_to_verified_q070_profile() -> None:
    stdout = _run_dry_run()
    assert "--profile ra_2026_alpha_v2_expansion_verified_q070" in stdout
    assert "--operator-web" in stdout
    assert "discord" not in stdout.lower()


def test_install_systemd_stack_forwards_explicit_profile_to_run_stack() -> None:
    stdout = _run_dry_run("--profile", "ra_2026_alpha_v2_expansion_champion_candidate")
    assert "ExecStart=/usr/bin/env bash " in stdout
    assert "--profile ra_2026_alpha_v2_expansion_champion_candidate --mode live --env prod" in stdout


def test_install_systemd_stack_supports_web_only_flags() -> None:
    stdout = _run_dry_run("--operator-web")
    assert "--operator-web" in stdout
    assert "discord" not in stdout.lower()
