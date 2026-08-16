"""One camera owner per machine.

`blackice serve` and `blackice voice` are separate processes, and both load
every installed plugin. For a plugin that only reads a clock that is harmless;
for one that opens a decrypting video session to every camera on the network it
is not — it doubles the bandwidth and CPU, makes both processes fight over the
RTSP port, and gives the cameras two concurrent logins.

So the first process to take an exclusive advisory lock owns the cameras, and
any other runs read-only, serving the dashboard from the shared database. The
lock is advisory and released by the OS if the owner dies, so a crash does not
leave the cameras permanently orphaned.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
from pathlib import Path

log = logging.getLogger("blackice.plugin.v380.singleton")


def _lock_dir() -> Path:
    try:
        from blackice.config import get_settings

        return get_settings().data_dir
    except Exception:  # pragma: no cover - only if core config is unavailable
        return Path("data")


class ProcessLock:
    """An advisory exclusive lock on a file, held for the process's lifetime."""

    def __init__(self, name: str, directory: Path | None = None) -> None:
        self.path = (directory or _lock_dir()) / f"{name}.lock"
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to become the owner. False means someone else already is.

        A filesystem that cannot lock — an exotic mount, a container quirk — is
        treated as "we are the owner". Running the cameras twice is a
        performance problem; refusing to run them at all is a broken plugin.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            log.warning("could not open %s (%s); assuming ownership", self.path, exc)
            return True

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except OSError as exc:
            os.close(fd)
            log.warning("could not lock %s (%s); assuming ownership", self.path, exc)
            return True

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        """Idempotent — `stop()` runs on failure paths too."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None
