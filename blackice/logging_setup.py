"""Standard-library logging configuration for every entry point.

Console output plus an optional rotating file. Third-party loggers that are
merely noisy are turned down here rather than at their call sites.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .config import get_settings

FORMAT = "%(asctime)s %(levelname)-7s %(name)-26s %(message)s"
DATEFMT = "%H:%M:%S"

NOISY = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "faster_whisper": logging.WARNING,
    "asyncio": logging.WARNING,
    "sentence_transformers": logging.WARNING,
    "transformers": logging.WARNING,
}

_configured = False


class _CarriageReturnFormatter(logging.Formatter):
    """Terminate lines with CRLF.

    voice2's keyboard worker puts the tty in raw mode, which clears OPOST, so
    the driver stops expanding LF into CRLF and console output walks diagonally
    down the screen. Emitting the CR ourselves is harmless when OPOST is on.
    """

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace("\n", "\r\n")


def configure(level: str | None = None, *, force: bool = False) -> None:
    global _configured
    if _configured and not force:
        return

    s = get_settings()
    root = logging.getLogger()
    root.setLevel(getattr(logging, (level or s.log_level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(_CarriageReturnFormatter(FORMAT, DATEFMT))
    root.addHandler(console)

    if s.log_file:
        path = Path(s.log_file)
        if not path.is_absolute():
            path = s.log_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        rotating.setFormatter(logging.Formatter(FORMAT, DATEFMT))
        root.addHandler(rotating)

    for name, lvl in NOISY.items():
        logging.getLogger(name).setLevel(lvl)

    _configured = True
