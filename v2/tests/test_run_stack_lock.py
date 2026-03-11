from __future__ import annotations

import os
import signal
import subprocess
import textwrap
import time
from pathlib import Path


def _wait_for_file(path: Path, *, timeout_sec: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for file: {path}")


def test_run_stack_rejects_duplicate_instance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo_root = Path(__file__).resolve().parents[2]
    fake_python = tmp_path / "fake_python.py"
    control_started = tmp_path / "control.started"
    bot_started = tmp_path / "bot.started"
    lock_path = tmp_path / "stack.lock"

    fake_python.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import pathlib
            import signal
            import sys
            import time

            target = pathlib.Path({str(control_started)!r})
            if sys.argv[1:3] == ["-m", "v2.discord_bot.bot"]:
                target = pathlib.Path({str(bot_started)!r})
            target.write_text("started", encoding="utf-8")

            def _handle(_signum, _frame):
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, _handle)
            signal.signal(signal.SIGINT, _handle)

            while True:
                time.sleep(0.1)
            """
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env["PYTHON_BIN"] = str(fake_python)
    env["STACK_LOCK_FILE"] = str(lock_path)

    first = subprocess.Popen(  # noqa: S603
        ["bash", "v2/scripts/run_stack.sh", "--mode", "shadow", "--env", "testnet"],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(control_started)
        _wait_for_file(bot_started)

        second = subprocess.run(  # noqa: S603
            ["bash", "v2/scripts/run_stack.sh", "--mode", "shadow", "--env", "testnet"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert second.returncode != 0
        assert "another stack instance is already running" in second.stdout
    finally:
        first.send_signal(signal.SIGTERM)
        try:
            first.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait(timeout=3.0)
