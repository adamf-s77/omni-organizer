"""File lock checks and write-quiescence debouncing for Omni-Organizer v3."""

from __future__ import annotations

import ctypes
import os
import threading
import time
from pathlib import Path
from typing import Callable

ERROR_SHARING_VIOLATION = 32
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80


def is_file_locked(path: Path | str) -> bool:
    """Check on Windows whether another process holds an exclusive/write lock on path."""
    if os.name != "nt":
        return False

    p = Path(path)
    if not p.exists() or not p.is_file():
        return False

    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(
            str(p),
            GENERIC_READ,
            0,  # dwShareMode = 0 checks exclusive readability without modifying file
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            err = kernel32.GetLastError()
            return err == ERROR_SHARING_VIOLATION
        kernel32.CloseHandle(handle)
        return False
    except Exception:
        return False


class FileQuiescenceTracker:
    """Per-file debounce tracker ensuring writes settle and locks release before processing."""

    def __init__(
        self,
        quiescence_seconds: float = 6.0,
        on_ready: Callable[[Path], None] | None = None,
        settle_check_seconds: float = 0.5,
        max_retries: int = 5,
    ) -> None:
        self.quiescence_seconds = max(0.05, float(quiescence_seconds))
        self.on_ready = on_ready
        self.settle_check_seconds = max(0.02, float(settle_check_seconds))
        self.max_retries = max(1, int(max_retries))
        self._lock = threading.RLock()
        self._timers: dict[str, threading.Timer] = {}
        self._stopped = False

    @staticmethod
    def _key(path: Path | str) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def notify(self, path: Path | str, retries: int = 0) -> None:
        """Schedule or reset the quiescence timer for a file path."""
        p = Path(os.path.abspath(str(path)))
        key = self._key(p)
        delay = self.quiescence_seconds if retries == 0 else max(1.0, self.quiescence_seconds / 2)

        with self._lock:
            if self._stopped:
                return
            existing = self._timers.pop(key, None)
            if existing is not None:
                existing.cancel()

            timer = threading.Timer(delay, self._evaluate, args=(p, key, retries))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _evaluate(self, path: Path, key: str, retries: int) -> None:
        with self._lock:
            self._timers.pop(key, None)
            if self._stopped:
                return

        if not path.exists() or not path.is_file():
            return

        if is_file_locked(path):
            if retries < self.max_retries:
                self.notify(path, retries=retries + 1)
            return

        try:
            st1 = path.stat()
            sig1 = (st1.st_size, st1.st_mtime_ns)
        except OSError:
            return

        time.sleep(self.settle_check_seconds)

        if self._stopped or not path.exists() or not path.is_file():
            return

        try:
            st2 = path.stat()
            sig2 = (st2.st_size, st2.st_mtime_ns)
        except OSError:
            return

        if sig1 != sig2 or is_file_locked(path):
            if retries < self.max_retries:
                self.notify(path, retries=retries + 1)
            return

        if self.on_ready is not None:
            self.on_ready(path)

    def pending_count(self) -> int:
        """Return count of files currently awaiting write-quiescence."""
        with self._lock:
            return len(self._timers)

    def stop(self) -> None:
        """Cancel all active timers and reject new notifications."""
        with self._lock:
            self._stopped = True
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
