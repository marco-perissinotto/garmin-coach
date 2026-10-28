"""Cross-process refresh lock (``refresh.lock`` in the app data dir) + in-process serialization (PLAN §6.5.3)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import TracebackType

from filelock import FileLock, Timeout

_INPROC: dict[str, threading.Lock] = {}
_INPROC_GUARD = threading.Lock()


def _inproc_lock(path: Path) -> threading.Lock:
    with _INPROC_GUARD:
        return _INPROC.setdefault(str(path.resolve()), threading.Lock())


class RefreshLock:
    """Only one fetcher at a time per machine: threads in this process and other garmin-coach processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._thread_lock = _inproc_lock(path)
        self._file_lock = FileLock(str(path), thread_local=False, is_singleton=False)
        self._held = False

    def acquire(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(timeout, 0)
        got = self._thread_lock.acquire(timeout=timeout) if timeout > 0 else self._thread_lock.acquire(blocking=False)
        if not got:
            return False
        try:
            remaining = max(deadline - time.monotonic(), 0)
            self._file_lock.acquire(timeout=remaining, poll_interval=0.1)
        except Timeout:
            self._thread_lock.release()
            return False
        self._held = True
        return True

    def release(self) -> None:
        if self._held:
            self._held = False
            self._file_lock.release()
            self._thread_lock.release()

    def __enter__(self) -> RefreshLock:
        return self

    def __exit__(self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None) -> None:
        self.release()
