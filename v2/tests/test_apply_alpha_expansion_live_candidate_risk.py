from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "v2" / "scripts" / "apply_alpha_expansion_live_candidate_risk.sh"


def test_apply_alpha_expansion_live_candidate_risk_dry_run_includes_core_keys() -> None:
    completed = subprocess.run(
        [
            "bash",
            str(SCRIPT_PATH),
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    stdout = completed.stdout

    assert "[profile] ra_2026_alpha_v2_expansion_live_candidate" in stdout
    assert "curl -fsS -X POST http://127.0.0.1:8101/set" in stdout
    assert '"key":"margin_budget_usdt","value":"30"' in stdout
    assert '"key":"max_leverage","value":"5"' in stdout
    assert '"key":"auto_flatten_on_risk","value":"true"' in stdout
    assert "curl -fsS http://127.0.0.1:8101/readiness" in stdout


def test_apply_alpha_expansion_live_candidate_risk_rejects_remote_base_url_without_override() -> None:
    completed = subprocess.run(
        [
            "bash",
            str(SCRIPT_PATH),
            "--dry-run",
            "--base-url",
            "http://10.0.0.5:8101",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "--base-url must stay on localhost unless --allow-remote is set" in completed.stdout
