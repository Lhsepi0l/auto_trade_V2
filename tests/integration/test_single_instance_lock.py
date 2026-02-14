from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_single_instance_lock_blocks_second_process(tmp_path) -> None:  # type: ignore[no-untyped-def]
    lock_path = tmp_path / "engine.lock"
    ready_path = tmp_path / "ready.flag"

    holder_code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "repo = Path(sys.argv[1])\n"
        "lock_path = Path(sys.argv[2])\n"
        "ready_path = Path(sys.argv[3])\n"
        "if str(repo) not in sys.path:\n"
        "    sys.path.insert(0, str(repo))\n"
        "from apps.trader_engine.services.single_instance import acquire_lock\n"
        "acquire_lock(str(lock_path))\n"
        "ready_path.write_text('ready', encoding='utf-8')\n"
        "print('LOCK_HELD_READY', flush=True)\n"
        "time.sleep(4)\n"
    )

    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(ROOT), str(lock_path), str(ready_path)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if ready_path.exists():
                break
            if holder.poll() is not None:
                break
            time.sleep(0.05)

        if not ready_path.exists():
            out, err = holder.communicate(timeout=1)
            pytest.fail(f"holder did not acquire lock in time: stdout={out!r} stderr={err!r}")

        contender_code = (
            "import sys\n"
            "from pathlib import Path\n"
            "repo = Path(sys.argv[1])\n"
            "lock_path = Path(sys.argv[2])\n"
            "if str(repo) not in sys.path:\n"
            "    sys.path.insert(0, str(repo))\n"
            "from apps.trader_engine.services.single_instance import acquire_lock, release_lock\n"
            "try:\n"
            "    acquire_lock(str(lock_path))\n"
            "except RuntimeError as e:\n"
            "    msg = str(e)\n"
            "    print(msg)\n"
            "    raise SystemExit(0 if 'SINGLE_INSTANCE_LOCK_HELD' in msg else 2)\n"
            "print('UNEXPECTED_SECOND_LOCK', flush=True)\n"
            "release_lock()\n"
            "raise SystemExit(3)\n"
        )
        contender = subprocess.run(
            [sys.executable, "-c", contender_code, str(ROOT), str(lock_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        combined = f"{contender.stdout}\n{contender.stderr}"
        assert contender.returncode == 0, combined
        assert "SINGLE_INSTANCE_LOCK_HELD" in combined
    finally:
        if holder.poll() is None:
            holder.terminate()
            try:
                holder.wait(timeout=2)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=2)
