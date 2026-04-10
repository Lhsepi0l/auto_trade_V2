from __future__ import annotations

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


def _write_fake_python(*, target: Path, control_started: Path) -> None:
    control_state = control_started.with_suffix(".state.json")
    target.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import pathlib
            import signal
            import sys
            import time

            control_started = pathlib.Path({str(control_started)!r})
            control_state = pathlib.Path({str(control_state)!r})
            ready_delay_sec = float(__import__("os").environ.get("FAKE_READY_DELAY_SEC", "0"))
            ready_requires_start = __import__("os").environ.get("FAKE_READY_REQUIRES_START", "0") == "1"
            ready_requires_reconcile = __import__("os").environ.get("FAKE_READY_REQUIRES_RECONCILE", "0") == "1"

            def _load_state():
                if not control_state.exists():
                    return {{"started_at": 0.0, "requested": False, "reconciled": False}}
                return json.loads(control_state.read_text(encoding="utf-8"))

            def _save_state(payload):
                control_state.write_text(json.dumps(payload), encoding="utf-8")

            if len(sys.argv) > 2 and sys.argv[1] == "-":
                url = sys.argv[2]
                if url.endswith("/readyz"):
                    state = _load_state()
                    ready = (time.time() - float(state.get("started_at", 0.0))) >= ready_delay_sec and (
                        bool(state.get("requested")) or not ready_requires_start
                    )
                    recovery_required = bool(ready_requires_reconcile and not state.get("reconciled"))
                    if recovery_required:
                        ready = False
                    body = json.dumps({{"ready": ready, "recovery_required": recovery_required}})
                    print(body)
                    raise SystemExit(0 if ready else 1)
                if url.endswith("/start"):
                    state = _load_state()
                    state["requested"] = True
                    _save_state(state)
                    raise SystemExit(0)
                if url.endswith("/reconcile"):
                    state = _load_state()
                    state["reconciled"] = True
                    _save_state(state)
                    raise SystemExit(0)

            if len(sys.argv) > 1 and sys.argv[1] == "-":
                code = sys.stdin.read()
                sys.argv = [sys.argv[0], *sys.argv[2:]]
                scope = {{"__name__": "__main__", "__file__": "<stdin>"}}
                exec(compile(code, "<stdin>", "exec"), scope, scope)
                raise SystemExit(0)

            _save_state({{"started_at": time.time(), "requested": False, "reconciled": False}})
            control_started.write_text("started", encoding="utf-8")

            def _shutdown(_signum, _frame):
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, _shutdown)
            signal.signal(signal.SIGINT, _shutdown)

            while True:
                time.sleep(0.1)
            """
        ),
        encoding="utf-8",
    )
    target.chmod(0o755)


def test_run_stack_rejects_duplicate_instance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo_root = Path(__file__).resolve().parents[2]
    fake_python = tmp_path / "fake_python.py"
    control_started = tmp_path / "control.started"
    lock_path = tmp_path / "stack.lock"
    _write_fake_python(target=fake_python, control_started=control_started)

    env = __import__("os").environ.copy()
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


def test_run_stack_waits_for_readyz_before_returning(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo_root = Path(__file__).resolve().parents[2]
    fake_python = tmp_path / "fake_python.py"
    control_started = tmp_path / "control.started"
    lock_path = tmp_path / "stack.lock"
    _write_fake_python(target=fake_python, control_started=control_started)

    env = __import__("os").environ.copy()
    env["PYTHON_BIN"] = str(fake_python)
    env["STACK_LOCK_FILE"] = str(lock_path)
    env["FAKE_READY_DELAY_SEC"] = "1.0"

    proc = subprocess.Popen(  # noqa: S603
        ["bash", "v2/scripts/run_stack.sh", "--mode", "shadow", "--env", "testnet"],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(control_started)
        stdout = proc.stdout
        assert stdout is not None
        deadline = time.monotonic() + 4.0
        buffer = ""
        while time.monotonic() < deadline:
            chunk = stdout.readline()
            if not chunk:
                time.sleep(0.05)
                continue
            buffer += chunk
            if "stack started" in buffer:
                break
        assert "stack started" in buffer
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3.0)


def test_run_stack_posts_start_before_requiring_readyz(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo_root = Path(__file__).resolve().parents[2]
    fake_python = tmp_path / "fake_python.py"
    control_started = tmp_path / "control.started"
    lock_path = tmp_path / "stack.lock"
    _write_fake_python(target=fake_python, control_started=control_started)

    env = __import__("os").environ.copy()
    env["PYTHON_BIN"] = str(fake_python)
    env["STACK_LOCK_FILE"] = str(lock_path)
    env["FAKE_READY_DELAY_SEC"] = "0.2"
    env["FAKE_READY_REQUIRES_START"] = "1"

    proc = subprocess.Popen(  # noqa: S603
        ["bash", "v2/scripts/run_stack.sh", "--mode", "live", "--env", "prod"],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(control_started)
        state = __import__("json").loads(control_started.with_suffix(".state.json").read_text())
        deadline = time.monotonic() + 3.0
        while not state.get("requested") and time.monotonic() < deadline:
            time.sleep(0.05)
            state = __import__("json").loads(control_started.with_suffix(".state.json").read_text())
        assert state["requested"] is True
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3.0)


def test_run_stack_auto_reconciles_dirty_restart_before_ready(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo_root = Path(__file__).resolve().parents[2]
    fake_python = tmp_path / "fake_python.py"
    control_started = tmp_path / "control.started"
    lock_path = tmp_path / "stack.lock"
    _write_fake_python(target=fake_python, control_started=control_started)

    env = __import__("os").environ.copy()
    env["PYTHON_BIN"] = str(fake_python)
    env["STACK_LOCK_FILE"] = str(lock_path)
    env["FAKE_READY_REQUIRES_RECONCILE"] = "1"

    proc = subprocess.Popen(  # noqa: S603
        ["bash", "v2/scripts/run_stack.sh", "--mode", "live", "--env", "prod"],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(control_started)
        state = __import__("json").loads(control_started.with_suffix(".state.json").read_text())
        deadline = time.monotonic() + 3.0
        while not state.get("reconciled") and time.monotonic() < deadline:
            time.sleep(0.05)
            state = __import__("json").loads(control_started.with_suffix(".state.json").read_text())
        assert state["reconciled"] is True
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3.0)
