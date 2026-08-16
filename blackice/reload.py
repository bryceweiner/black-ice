"""What the auto-reloader watches.

Core code, prompt files, and installed plugins. Plugins matter most: they are
installed editable, so their source lives outside the package tree and uvicorn's
default watch of the working directory would miss one installed from elsewhere.

The exclusions matter just as much. `data/` holds the SQLite WAL, the rotating
log and captured media, all of which change constantly while the service runs;
watching them would restart the process in a loop.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from importlib.util import find_spec
from pathlib import Path

from .config import get_settings

log = logging.getLogger("blackice.reload")

ROOT = Path(__file__).resolve().parents[1]

# Prompt and schema files count as source: editing one should take effect
# without remembering to restart.
INCLUDES = ("*.py", "*.sql", "*.prompt", "*.prompt.md", "*.prompt.txt")

EXCLUDES = (
    "*/data/*", "*.db", "*.db-wal", "*.db-shm", "*.log", "*.jsonl",
    "*/node_modules/*", "*/dist/*", "*/.git/*", "*/__pycache__/*",
    "*/.pytest_cache/*", "*/.ruff_cache/*", "*.pyc",
)


def plugin_dirs() -> list[Path]:
    """Source directory of every installed plugin.

    find_spec resolves an editable install back to its real source, which is
    where someone actually edits it; site-packages for a normal install.
    """
    found: set[Path] = set()
    for ep in entry_points(group="blackice.plugins"):
        module = ep.module.split(".")[0]
        try:
            spec = find_spec(module)
        except Exception:
            log.debug("could not locate plugin module %s", module)
            continue
        if spec is None or not spec.origin:
            continue
        path = Path(spec.origin).resolve().parent
        if path.is_dir():
            found.add(path)
    return sorted(found)


def watch_dirs() -> list[str]:
    """Directories the reloader should watch, de-duplicated and non-overlapping."""
    candidates = [ROOT / "blackice", ROOT / "plugins", *plugin_dirs()]

    kept: list[Path] = []
    for path in sorted({p.resolve() for p in candidates if p.is_dir()}):
        # Drop anything already covered by a parent, so watchfiles is not
        # handed the same tree twice.
        if any(path.is_relative_to(existing) for existing in kept):
            continue
        kept.append(path)

    data = get_settings().data_dir.resolve()
    return [str(p) for p in kept if not p.is_relative_to(data)]


def describe() -> str:
    dirs = watch_dirs()
    return f"watching {len(dirs)} path(s) for {', '.join(INCLUDES)}"
