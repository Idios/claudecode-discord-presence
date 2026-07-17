"""Cross-platform single-instance lock backed by an OS file lock.

The lock is held for the lifetime of the owning file descriptor. When the
process exits for any reason (normal, crash, kill), the OS releases the lock
automatically — so there is no stale state, PID reuse, or liveness polling to
worry about. The lock, not the file's text content, is the source of truth.

Windows note: ``msvcrt.locking`` prevents all concurrent access (even reads)
to the locked file descriptor, so on Windows we use a sibling ``.lock`` file
as the actual lock target. The ``.pid`` file (``self.path``) is written and
deleted normally and remains readable while the lock is held.
"""

import os
import sys
from pathlib import Path

PID_FILE = Path.home() / ".claude" / "claudecode-discord-presence.pid"
STOP_FILE = Path.home() / ".claude" / "claudecode-discord-presence.stop"

if sys.platform == "win32":
    import msvcrt

    def _lock_path(path: Path) -> Path:
        """Return the sibling lock file used on Windows."""
        return path.with_suffix(path.suffix + ".lock")

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_path(path: Path) -> Path:  # type: ignore[misc]
        """On POSIX the lock lives directly on the PID file."""
        return path

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class InstanceLock:
    """Exclusive OS lock on a file, held for the process lifetime."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to take the lock without blocking. Return True on success."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lp = _lock_path(self.path)
        fd = os.open(lp, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _try_lock(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        self._write_pid()
        return True

    def _write_pid(self) -> None:
        # Best-effort only: the lock is the source of truth, not this text.
        try:
            if sys.platform == "win32":
                # On Windows the PID file is separate from the lock file.
                data = str(os.getpid()).encode()
                self.path.write_bytes(data)
            else:
                os.lseek(self._fd, 0, os.SEEK_SET)
                data = str(os.getpid()).encode()
                os.write(self._fd, data)
                os.ftruncate(self._fd, len(data))
        except OSError:
            pass

    def release(self) -> None:
        """Unlock, close the fd, and remove the file. Safe if not held."""
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        # Remove the lock file (sibling on Windows, same as path on POSIX).
        try:
            _lock_path(self.path).unlink()
        except OSError:
            pass
        # On Windows also remove the PID file.
        if sys.platform == "win32":
            try:
                self.path.unlink()
            except OSError:
                pass
