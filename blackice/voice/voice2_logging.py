"""Route voice2's engine events through the standard logging module.

voice2 ships its own logger that calls print() directly, so its output ignores
log levels, handlers and the rotating file, and interleaves badly with ours.
Its workers call `log.event(...)` as a module attribute lookup, so replacing
the function reaches every one of them.

The JSON-lines file it writes is genuinely useful for latency work, so that is
kept exactly as it was.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

_lock = threading.Lock()
_installed = False

# Events that say "a normal thing happened, again". Useful when chasing a
# problem, noise the rest of the time.
DEBUG_EVENTS = {
    "floor_set", "transition", "new_turn", "speak_entry", "subscribed",
    "start", "complete", "worker_started", "listener_started",
}
# keyboard_start_failed is normally *us*, disabling it on purpose.
ERROR_EVENTS = {"vad_load_error", "listener_error"}
DEBUG_EVENTS_EXTRA = {"keyboard_start_failed"}


def quiet_torch_hub() -> None:
    """Stop torch.hub printing "Using cache found in ..." to stdout.

    It is emitted from the listen worker's thread whenever Silero loads, so it
    cannot be captured by redirecting stdout around startup. torch.hub.load
    takes a `verbose` flag that voice2 does not pass, so default it off and log
    the same fact ourselves.
    """
    try:
        import torch
    except ImportError:
        return
    if getattr(torch.hub.load, "_blackice_quiet", False):
        return

    original = torch.hub.load

    def load(*args: Any, **kwargs: Any):
        kwargs.setdefault("verbose", False)
        logging.getLogger("blackice.voice2.torch").debug(
            "torch.hub.load %s", args[0] if args else kwargs.get("repo_or_dir")
        )
        return original(*args, **kwargs)

    load._blackice_quiet = True
    torch.hub.load = load


def install() -> bool:
    """Replace voice2's print-based event logger. Idempotent."""
    global _installed
    if _installed:
        return True
    try:
        from voice2 import logging_util
    except ImportError:
        return False

    def event(subsystem: str, ev: str, **meta: Any) -> None:
        record = {
            "ts": time.monotonic(),
            "wall_ts": datetime.now(UTC).isoformat(),
            "subsystem": subsystem,
            "event": ev,
            **meta,
        }
        log = logging.getLogger(f"blackice.voice2.{subsystem}")
        if ev in ERROR_EVENTS:
            level = logging.ERROR
        elif ev in DEBUG_EVENTS or ev in DEBUG_EVENTS_EXTRA:
            level = logging.DEBUG
        else:
            level = logging.INFO

        if log.isEnabledFor(level):
            detail = " ".join(
                f"{k}={v}" for k, v in meta.items()
                if k not in ("turn_id", "state") and v not in (None, "")
            )
            turn = meta.get("turn_id")
            log.log(level, "%s%s%s", ev,
                    f" turn={turn}" if turn else "",
                    f" {detail}" if detail else "")

        path = getattr(logging_util, "_log_file", None)
        if path:
            line = json.dumps(record, default=str)
            with _lock:
                try:
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError:
                    pass

    logging_util.event = event
    _installed = True
    return True
