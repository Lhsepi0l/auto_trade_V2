from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run_step(name: str, cmd: list[str], *, expect_token: str | None = None) -> None:
    print(f"[STEP] {name}: {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"{name} failed with exit code {proc.returncode}")
    if expect_token and expect_token not in (proc.stdout or ""):
        raise RuntimeError(f"{name} output missing token: {expect_token}")
    print(f"[PASS] {name}")


def _backup_self_test() -> None:
    print("[STEP] backup self-test")
    root = Path(tempfile.mkdtemp(prefix="verify_backup_"))
    try:
        code = (
            "import sqlite3, sys\n"
            "from pathlib import Path\n"
            "repo = Path(sys.argv[1])\n"
            "root = Path(sys.argv[2])\n"
            "if str(repo) not in sys.path:\n"
            "    sys.path.insert(0, str(repo))\n"
            "from scripts.backup_db import create_backup\n"
            "db_path = root / 'source.sqlite3'\n"
            "out_dir = root / 'backups'\n"
            "with sqlite3.connect(str(db_path)) as conn:\n"
            "    conn.execute('CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')\n"
            "    conn.execute('INSERT INTO sample (value) VALUES (?)', ('verify-release',))\n"
            "    conn.commit()\n"
            "backup_path = create_backup(db_path=db_path, output_dir=out_dir, keep=3)\n"
            "assert backup_path.exists()\n"
            "assert backup_path.stat().st_size > 0\n"
            "with sqlite3.connect(str(backup_path)) as conn:\n"
            "    row = conn.execute('SELECT value FROM sample WHERE id = 1').fetchone()\n"
            "assert row and str(row[0]) == 'verify-release'\n"
            "print('BACKUP_SELFTEST_OK')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, str(ROOT), str(root)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="", file=sys.stderr)
        if proc.returncode != 0 or "BACKUP_SELFTEST_OK" not in (proc.stdout or ""):
            raise RuntimeError("backup self-test failed")
    finally:
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        if root.exists():
            raise RuntimeError(f"backup self-test cleanup failed: {root}")

    print("[PASS] backup self-test")


def _ensure_clean_verify_artifacts() -> None:
    for rel in (".tmp", "tmp"):
        p = ROOT / rel
        if p.exists() and p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def main() -> int:
    try:
        _ensure_clean_verify_artifacts()
        _run_step("compileall", [sys.executable, "-m", "compileall", "."])
        _run_step("pytest not e2e", [sys.executable, "-m", "pytest", "-m", "not e2e"])
        _run_step("pytest all", [sys.executable, "-m", "pytest"])
        _run_step("smoke test mode", [sys.executable, "scripts/smoke_test_mode.py"], expect_token="SMOKE_OK")
        _backup_self_test()
        _ensure_clean_verify_artifacts()
        print("VERIFY_OK")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"VERIFY_FAIL {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
