"""Where the watchers plugin gets its numbers.

Everything here has a default that works, because the plugin has to start
cleanly on a machine with no models provisioned and no cameras present. The
thresholds are the balanced insightface/OSNet operating point; they are also
stored in the plugin database once `set_thresholds` has been called, and the
stored value wins over the environment so a runtime tune survives a restart.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("blackice.plugin.watchers.settings")

ENV_ENABLED = "WATCHERS_ENABLED"
ENV_MODEL_DIR = "WATCHERS_MODEL_DIR"
ENV_ENROL_DIR = "WATCHERS_ENROL_DIR"
ENV_DEVICE = "WATCHERS_DEVICE"
ENV_FACE_THRESHOLD = "WATCHERS_FACE_THRESHOLD"
ENV_REID_THRESHOLD = "WATCHERS_REID_THRESHOLD"
ENV_MIN_FRAMES = "WATCHERS_MIN_FRAMES"
ENV_ANALYSE_FPS = "WATCHERS_ANALYSE_FPS"
ENV_NIGHT_START = "WATCHERS_NIGHT_START"
ENV_NIGHT_END = "WATCHERS_NIGHT_END"
ENV_LINGER_SECONDS = "WATCHERS_LINGER_SECONDS"
ENV_TRACK_TTL = "WATCHERS_TRACK_TTL"
ENV_RECENT_WINDOW = "WATCHERS_RECENT_WINDOW"
ENV_FPS_FLOOR = "WATCHERS_FPS_FLOOR"
ENV_WORKERS = "WATCHERS_WORKERS"
ENV_MIN_FACE_PIXELS = "WATCHERS_MIN_FACE_PIXELS"

DEFAULT_MODEL_DIR = "watchers_models"
DEFAULT_ENROL_DIR = "watchers_enrol"
MEDIA_SUBDIR = "watchers"

#: Cosine similarity above which an ArcFace embedding is the same person. The
#: usual balanced operating point for w600k_r50; below ~0.45 it starts putting
#: the wrong name on a stranger, which is the expensive error for an event that
#: says "Jane is at the door".
DEFAULT_FACE_THRESHOLD = 0.55
#: Same for the OSNet body embedding. Higher, because clothing similarity makes
#: appearance a weaker signal than a face.
DEFAULT_REID_THRESHOLD = 0.70
#: How many frames must agree before a track is called. One frame is a guess.
DEFAULT_MIN_FRAMES = 3
#: Frames per second actually pushed through the models, per camera. The
#: cameras send 15-25; running every frame buys nothing for identity and is the
#: quickest way to fall behind.
DEFAULT_ANALYSE_FPS = 5.0
#: Local hours that count as night for severity. Wraps midnight.
DEFAULT_NIGHT_START = 22
DEFAULT_NIGHT_END = 6
#: An unknown person still in frame after this long is loitering, not passing.
DEFAULT_LINGER_SECONDS = 60.0
#: A track not seen for this long is over. ByteTrack issues a new id when the
#: person comes back, which is what makes re-acquisition re-emit.
DEFAULT_TRACK_TTL = 5.0
#: How far back to look for the same body on another camera. This is what
#: answers "the person who was at the drive is now at the door".
DEFAULT_RECENT_WINDOW = 600.0
#: Analysed frames per second below which a camera counts as not keeping up.
DEFAULT_FPS_FLOOR = 1.0
#: A face smaller than this across is not worth embedding — ArcFace on a 20px
#: face returns a confident vector for nobody in particular.
DEFAULT_MIN_FACE_PIXELS = 50


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _data_dir() -> Path:
    try:
        from blackice.config import get_settings

        return get_settings().data_dir
    except Exception:  # pragma: no cover - only if core config is unavailable
        return Path("data")


def media_root() -> Path:
    """Where crops go, so that `MediaRef.path` resolves under /media/."""
    try:
        from blackice.config import get_settings

        return get_settings().media_dir
    except Exception:  # pragma: no cover
        return _data_dir() / "media"


def model_dir() -> Path:
    """The one directory models are ever loaded from.

    Nothing is fetched at runtime: the provisioning step fills this, and the
    loader refuses anything that is not in the lock file.
    """
    explicit = os.environ.get(ENV_MODEL_DIR, "").strip()
    return Path(explicit) if explicit else _data_dir() / DEFAULT_MODEL_DIR


def enrol_dir() -> Path:
    """Photographs the owner drops in to enrol someone from a file.

    A directory rather than an arbitrary path, for the same reason v380's
    `play_sound` works that way: the file name comes from the model, and it
    must not be able to name a file anywhere on the disk.
    """
    explicit = os.environ.get(ENV_ENROL_DIR, "").strip()
    return Path(explicit) if explicit else _data_dir() / DEFAULT_ENROL_DIR


def resolve_enrol_file(name: str) -> Path | None:
    """A photo inside the enrolment directory, or None if it is not there."""
    root = enrol_dir()
    try:
        resolved_root = root.resolve()
        target = (root / name).resolve()
    except (OSError, RuntimeError):
        return None
    if not target.is_relative_to(resolved_root) or not target.is_file():
        return None
    return target


def list_enrol_files() -> list[str]:
    root = enrol_dir()
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_file() and not p.name.startswith(".")
    )


@dataclass(slots=True)
class Thresholds:
    """The knobs `set_thresholds` moves. Separated from the rest of the
    settings because they are the only ones persisted in the database."""

    face: float = DEFAULT_FACE_THRESHOLD
    reid: float = DEFAULT_REID_THRESHOLD
    min_frames: int = DEFAULT_MIN_FRAMES

    def as_dict(self) -> dict[str, float | int]:
        return {"face": self.face, "reid": self.reid, "min_frames": self.min_frames}


@dataclass(slots=True)
class Settings:
    enabled: bool = True
    device: str = "auto"
    thresholds: Thresholds = field(default_factory=Thresholds)
    analyse_fps: float = DEFAULT_ANALYSE_FPS
    night_start: int = DEFAULT_NIGHT_START
    night_end: int = DEFAULT_NIGHT_END
    linger_seconds: float = DEFAULT_LINGER_SECONDS
    track_ttl: float = DEFAULT_TRACK_TTL
    recent_window: float = DEFAULT_RECENT_WINDOW
    fps_floor: float = DEFAULT_FPS_FLOOR
    workers: int = 0
    min_face_pixels: int = DEFAULT_MIN_FACE_PIXELS

    def is_night(self, hour: int) -> bool:
        """Whether a local hour falls in the night window, midnight included."""
        start, end = self.night_start % 24, self.night_end % 24
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end


def load() -> Settings:
    """Read the environment. Never raises."""
    return Settings(
        enabled=_env_bool(ENV_ENABLED, True),
        device=os.environ.get(ENV_DEVICE, "").strip().lower() or "auto",
        thresholds=Thresholds(
            face=_env_float(ENV_FACE_THRESHOLD, DEFAULT_FACE_THRESHOLD),
            reid=_env_float(ENV_REID_THRESHOLD, DEFAULT_REID_THRESHOLD),
            min_frames=max(1, _env_int(ENV_MIN_FRAMES, DEFAULT_MIN_FRAMES)),
        ),
        analyse_fps=max(0.1, _env_float(ENV_ANALYSE_FPS, DEFAULT_ANALYSE_FPS)),
        night_start=_env_int(ENV_NIGHT_START, DEFAULT_NIGHT_START),
        night_end=_env_int(ENV_NIGHT_END, DEFAULT_NIGHT_END),
        linger_seconds=_env_float(ENV_LINGER_SECONDS, DEFAULT_LINGER_SECONDS),
        track_ttl=_env_float(ENV_TRACK_TTL, DEFAULT_TRACK_TTL),
        recent_window=_env_float(ENV_RECENT_WINDOW, DEFAULT_RECENT_WINDOW),
        fps_floor=_env_float(ENV_FPS_FLOOR, DEFAULT_FPS_FLOOR),
        workers=_env_int(ENV_WORKERS, 0),
        min_face_pixels=_env_int(ENV_MIN_FACE_PIXELS, DEFAULT_MIN_FACE_PIXELS),
    )
