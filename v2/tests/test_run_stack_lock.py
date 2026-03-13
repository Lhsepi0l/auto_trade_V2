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


def _write_fake_python(
    *,
    target: Path,
    control_started: Path,
    bot_started: Path,
    bot_base_url: Path,
) -> None:
    target.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import os
            import pathlib
            import signal
            import sys
            import threading
            import time
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

            control_started = pathlib.Path({str(control_started)!r})
            bot_started = pathlib.Path({str(bot_started)!r})
            bot_base_url = pathlib.Path({str(bot_base_url)!r})
            ready_delay_sec = float(os.environ.get("FAKE_READY_DELAY_SEC", "0"))
            bot_import_ok = os.environ.get("FAKE_BOT_IMPORT_OK", "1") == "1"
            ready_requires_start = os.environ.get("FAKE_READY_REQUIRES_START", "0") == "1"
            start_state = {{"requested": False}}

            if len(sys.argv) > 1 and sys.argv[1] == "-c":
                if bot_import_ok:
                    raise SystemExit(0)
                print("ModuleNotFoundError: No module named 'v2.discord_bot.bot'", file=sys.stderr)
                raise SystemExit(1)

            if len(sys.argv) > 1 and sys.argv[1] == "-":
                code = sys.stdin.read()
                sys.argv = [sys.argv[0], *sys.argv[2:]]
                scope = {{"__name__": "__main__", "__file__": "<stdin>"}}
                exec(compile(code, "<stdin>", "exec"), scope, scope)
                raise SystemExit(0)

            if sys.argv[1:3] == ["-m", "v2.discord_bot.bot"]:
                bot_started.write_text("started", encoding="utf-8")
                bot_base_url.write_text(os.environ.get("TRADER_API_BASE_URL", ""), encoding="utf-8")

                def _stop_bot(_signum, _frame):
                    raise SystemExit(0)

                signal.signal(signal.SIGTERM, _stop_bot)
                signal.signal(signal.SIGINT, _stop_bot)
                while True:
                    time.sleep(0.1)

            port = 8101
            if "--control-http-port" in sys.argv:
                idx = sys.argv.index("--control-http-port")
                port = int(sys.argv[idx + 1])

            started_at = time.monotonic()

            class _Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    ready = (time.monotonic() - started_at) >= ready_delay_sec and (
                        start_state["requested"] or not ready_requires_start
                    )
                    if self.path == "/readyz":
                        payload = json.dumps({{"ready": ready}}).encode("utf-8")
                        self.send_response(200 if ready else 503)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    payload = b"{{}}"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def do_POST(self):
                    if self.path == "/start":
                        start_state["requested"] = True
                        payload = b'{{"state":"RUNNING"}}'
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    self.send_response(404)
                    self.end_headers()

                def log_message(self, format, *args):
                    return

            httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
            control_started.write_text("started", encoding="utf-8")

            def _shutdown(_signum, _frame):
                threading.Thread(target=httpd.shutdown, daemon=True).start()

            signal.signal(signal.SIGTERM, _shutdown)
            signal.signal(signal.SIGINT, _shutdown)

            try:
                httpd.serve_forever()
            finally:
                httpd.server_close()
            """
        ),
        encoding="utf-8",
    )
    target.chmod(0o755)


def test_run_stack_rejects_duplicate_instance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo_root = Path(__file__).resolve().parents[2]
    fake_python = tmp_path / "fake_python.py"
    control_started = tmp_path / "control.started"
    bot_started = tmp_path / "bot.started"
    bot_base_url = tmp_path / "bot.base_url"
    lock_path = tmp_path / "stack.lock"
    _write_fake_python(
        target=fake_python,
        control_started=control_started,
        bot_started=bot_started,
        bot_base_url=bot_base_url,
    )

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


def test_run_stack_waits_for_readyz_before_starting_bot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo_root = Path(__file__).resolve().parents[2]
    fake_python = tmp_path / "fake_python.py"
    control_started = tmp_path / "control.started"
    bot_started = tmp_path / "bot.started"
    bot_base_url = tmp_path / "bot.base_url"
    lock_path = tmp_path / "stack.lock"
    _write_fake_python(
        target=fake_python,
        control_started=control_started,
        bot_started=bot_started,
        bot_base_url=bot_base_url,
    )

    env = os.environ.copy()
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
        time.sleep(0.2)
        assert bot_started.exists() is False

        _wait_for_file(bot_started, timeout_sec=3.0)
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
    bot_started = tmp_path / "bot.started"
    bot_base_url = tmp_path / "bot.base_url"
    lock_path = tmp_path / "stack.lock"
    _write_fake_python(
        target=fake_python,
        control_started=control_started,
        bot_started=bot_started,
        bot_base_url=bot_base_url,
    )

    env = os.environ.copy()
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
        _wait_for_file(bot_started, timeout_sec=3.0)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3.0)


def test_run_stack_overrides_trader_api_base_url_for_bot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo_root = Path(__file__).resolve().parents[2]
    fake_python = tmp_path / "fake_python.py"
    control_started = tmp_path / "control.started"
    bot_started = tmp_path / "bot.started"
    bot_base_url = tmp_path / "bot.base_url"
    lock_path = tmp_path / "stack.lock"
    _write_fake_python(
        target=fake_python,
        control_started=control_started,
        bot_started=bot_started,
        bot_base_url=bot_base_url,
    )

    env = os.environ.copy()
    env["PYTHON_BIN"] = str(fake_python)
    env["STACK_LOCK_FILE"] = str(lock_path)
    env["TRADER_API_BASE_URL"] = "http://localhost:9999"

    proc = subprocess.Popen(  # noqa: S603
        [
            "bash",
            "v2/scripts/run_stack.sh",
            "--mode",
            "live",
            "--env",
            "prod",
            "--host",
            "127.0.0.1",
            "--port",
            "8101",
        ],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(control_started)
        _wait_for_file(bot_started)
        _wait_for_file(bot_base_url)
        assert bot_base_url.read_text(encoding="utf-8") == "http://127.0.0.1:8101"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3.0)


def test_run_stack_fails_when_bot_import_is_unavailable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo_root = Path(__file__).resolve().parents[2]
    fake_python = tmp_path / "fake_python.py"
    control_started = tmp_path / "control.started"
    bot_started = tmp_path / "bot.started"
    bot_base_url = tmp_path / "bot.base_url"
    lock_path = tmp_path / "stack.lock"
    _write_fake_python(
        target=fake_python,
        control_started=control_started,
        bot_started=bot_started,
        bot_base_url=bot_base_url,
    )

    env = os.environ.copy()
    env["PYTHON_BIN"] = str(fake_python)
    env["STACK_LOCK_FILE"] = str(lock_path)
    env["FAKE_BOT_IMPORT_OK"] = "0"

    out = subprocess.run(  # noqa: S603
        ["bash", "v2/scripts/run_stack.sh", "--mode", "shadow", "--env", "testnet"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert out.returncode != 0
    assert "discord bot import failed" in out.stdout
