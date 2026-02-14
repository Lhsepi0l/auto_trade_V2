from __future__ import annotations

import atexit
import os
from pathlib import Path
from typing import Optional, TextIO

_LOCK_HANDLE: Optional[TextIO] = None
_LOCK_PATH: Optional[Path] = None


def _lock_file(handle: TextIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        handle.write("0")
        handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: TextIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def release_lock() -> None:
    global _LOCK_HANDLE
    handle = _LOCK_HANDLE
    if handle is None:
        return
    try:
        _unlock_file(handle)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass
    _LOCK_HANDLE = None


def acquire_lock(lock_path: str) -> TextIO:
    global _LOCK_HANDLE, _LOCK_PATH

    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if _LOCK_HANDLE is not None and _LOCK_PATH == path:
        return _LOCK_HANDLE

    handle = path.open("a+", encoding="utf-8")
    try:
        _lock_file(handle)
    except Exception as e:
        try:
            handle.close()
        except Exception:
            pass
        raise RuntimeError(f"SINGLE_INSTANCE_LOCK_HELD: {path}") from e

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()

    _LOCK_HANDLE = handle
    _LOCK_PATH = path
    return handle


atexit.register(release_lock)
