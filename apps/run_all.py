from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ManagedProcess:
    name: str
    proc: subprocess.Popen


def _spawn(name: str, args: List[str]) -> ManagedProcess:
    proc = subprocess.Popen(args)
    return ManagedProcess(name=name, proc=proc)


def _terminate(p: ManagedProcess, *, force_after_sec: float = 5.0) -> None:
    if p.proc.poll() is not None:
        return
    try:
        p.proc.terminate()
    except Exception:
        return
    deadline = time.time() + max(force_after_sec, 0.5)
    while time.time() < deadline:
        if p.proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        p.proc.kill()
    except Exception as e:  # noqa: BLE001
        logger.warning("run_all_kill_failed", extra={"name": p.name, "err": type(e).__name__}, exc_info=True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="auto-trader-all", description="Run trader engine + discord bot together.")
    parser.add_argument("--engine-only", action="store_true", help="run only trader_engine API")
    parser.add_argument("--bot-only", action="store_true", help="run only discord bot")
    parser.add_argument("--python", default=sys.executable, help="python executable path")
    args = parser.parse_args()

    if args.engine_only and args.bot_only:
        print("cannot use both --engine-only and --bot-only", file=sys.stderr)
        return 2

    procs: List[ManagedProcess] = []
    stop = False

    def _request_stop(_sig: int, _frame: object | None) -> None:
        nonlocal stop
        stop = True

    for s in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if s is None:
            continue
        try:
            signal.signal(s, _request_stop)
        except Exception as e:  # noqa: BLE001
            logger.warning("run_all_signal_hook_failed", extra={"signal": str(s), "err": type(e).__name__}, exc_info=True)

    try:
        if not args.bot_only:
            procs.append(_spawn("engine", [args.python, "-m", "apps.trader_engine.main", "--api"]))
            print("[run_all] engine started")
        if not args.engine_only:
            procs.append(_spawn("bot", [args.python, "-m", "apps.discord_bot.bot"]))
            print("[run_all] bot started")

        if not procs:
            print("[run_all] nothing to run", file=sys.stderr)
            return 2

        while not stop:
            for p in procs:
                rc = p.proc.poll()
                if rc is not None:
                    print(f"[run_all] {p.name} exited rc={rc}; stopping others")
                    stop = True
                    break
            time.sleep(0.2)
    finally:
        for p in reversed(procs):
            _terminate(p)

    # Return non-zero only if any process failed.
    exit_codes = [p.proc.returncode for p in procs]
    for rc in exit_codes:
        if rc not in (0, None):
            return int(rc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
