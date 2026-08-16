"""Where the plugin gets its cameras and credentials.

Discovery finds cameras but cannot authenticate to them, so every camera needs
a password from somewhere. Two sources, merged:

* `V380_USERNAME` / `V380_PASSWORD` in the environment — the default applied to
  every camera found on the LAN, which is right when the whole fleet was
  provisioned from one V380 app account;
* `data/v380_cameras.json` — per-device overrides, keyed by device id, for the
  camera with its own password, a friendly label, a fixed IP, or one that
  should be left alone entirely.

A camera with no usable password is still *listed* — you want to see that a
camera exists on your network even before you have configured it — but is never
connected to.

Reading the config file is deliberately forgiving. A syntax error in it should
degrade to "no overrides", visible in the log, rather than take the whole
plugin unhealthy at boot.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .protocol import DEFAULT_PORT, QUALITY_HD, QUALITY_SD

log = logging.getLogger("blackice.plugin.v380.settings")

ENV_USERNAME = "V380_USERNAME"
ENV_PASSWORD = "V380_PASSWORD"
ENV_QUALITY = "V380_QUALITY"
ENV_CAMERAS_FILE = "V380_CAMERAS_FILE"
ENV_RTSP_PORT = "V380_RTSP_PORT"
ENV_RTSP_ENABLED = "V380_RTSP_ENABLED"
ENV_DISCOVERY_ENABLED = "V380_DISCOVERY_ENABLED"
ENV_DISCOVERY_INTERVAL = "V380_DISCOVERY_INTERVAL"
ENV_SNAPSHOT_INTERVAL = "V380_SNAPSHOT_INTERVAL"
ENV_OFFLINE_AFTER = "V380_OFFLINE_AFTER"
ENV_AUDIO_DIR = "V380_AUDIO_DIR"

DEFAULT_CAMERAS_FILE = "v380_cameras.json"
#: Clips the assistant may play through a camera speaker. A directory rather
#: than an arbitrary path: `play_sound` takes its argument from the model, and
#: that must not be able to name a file anywhere on the disk.
DEFAULT_AUDIO_DIR = "v380_audio"
#: Rescan for new cameras this often. They rarely appear, and a broadcast every
#: few seconds is noise on someone's home network.
DEFAULT_DISCOVERY_INTERVAL = 300.0
#: How often each camera's still is refreshed for the dashboard and for
#: downstream recognition.
DEFAULT_SNAPSHOT_INTERVAL = 5.0
#: No frames for this long means the camera is off, not merely slow.
DEFAULT_OFFLINE_AFTER = 45.0


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


def parse_quality(value: str | int | None, default: int = QUALITY_HD) -> int:
    """`sd`/`hd` (or 0/1) to the wire value."""
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return QUALITY_SD if value == QUALITY_SD else QUALITY_HD
    text = str(value).strip().lower()
    if text in ("sd", "0", "low"):
        return QUALITY_SD
    if text in ("hd", "1", "high"):
        return QUALITY_HD
    log.warning("unknown quality %r; using hd", value)
    return default


@dataclass(slots=True)
class CameraConfig:
    """Everything needed to open a session with one camera."""

    device_id: str
    ip: str = ""
    mac: str = ""
    label: str = ""
    username: str = ""
    password: str = ""
    port: int = DEFAULT_PORT
    quality: int = QUALITY_HD
    cloud: bool = False
    enabled: bool = True

    @property
    def numeric_id(self) -> int:
        """The device id as the protocol wants it. 0 if it is not numeric."""
        try:
            return int(self.device_id)
        except ValueError:
            return 0

    @property
    def display_name(self) -> str:
        return self.label or self.device_id

    @property
    def configured(self) -> bool:
        """Whether we could actually connect: a password, an id, and a route."""
        if not self.enabled or not self.password or not self.numeric_id:
            return False
        return bool(self.ip) or self.cloud

    @property
    def reason_unconfigured(self) -> str:
        if not self.enabled:
            return "disabled in the camera config file"
        if not self.numeric_id:
            return "device id is not numeric"
        if not self.password:
            return f"no password (set {ENV_PASSWORD} or add an entry to the camera config file)"
        if not self.ip and not self.cloud:
            return "not seen on the LAN and not configured for cloud"
        return ""


@dataclass(slots=True)
class Settings:
    username: str = "admin"
    password: str = ""
    quality: int = QUALITY_HD
    cameras_file: Path | None = None
    rtsp_enabled: bool = True
    rtsp_port: int = 8554
    #: Off means the fleet is exactly what the config file names — no
    #: broadcasts. Useful on a network where they are unwelcome, and in tests.
    discovery_enabled: bool = True
    discovery_interval: float = DEFAULT_DISCOVERY_INTERVAL
    snapshot_interval: float = DEFAULT_SNAPSHOT_INTERVAL
    offline_after: float = DEFAULT_OFFLINE_AFTER
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def camera_for(
        self, device_id: str, *, ip: str = "", mac: str = ""
    ) -> CameraConfig:
        """Merge the environment defaults with this camera's overrides."""
        override = self.overrides.get(str(device_id), {})
        # A username defaults to the device id where none is given: several
        # V380 models ship with the id as the account name rather than 'admin'.
        username = str(override.get("username") or self.username or device_id)
        return CameraConfig(
            device_id=str(device_id),
            ip=str(override.get("ip") or ip),
            mac=mac,
            label=str(override.get("label") or ""),
            username=username,
            password=str(override.get("password") or self.password),
            port=int(override.get("port") or DEFAULT_PORT),
            quality=parse_quality(override.get("quality"), self.quality),
            cloud=bool(override.get("cloud", False)),
            enabled=bool(override.get("enabled", True)),
        )

    def configured_ids(self) -> list[str]:
        """Device ids named in the config file.

        These are attempted even if discovery never sees them, which is how a
        camera on another subnet, or one that ignores the broadcast, still
        works — provided the entry gives an IP.
        """
        return list(self.overrides)


def _load_overrides(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s: %s; continuing without overrides", path, exc)
        return {}

    if not isinstance(data, dict):
        log.warning("%s should hold an object keyed by device id; ignoring it", path)
        return {}

    overrides = {}
    for device_id, entry in data.items():
        if isinstance(entry, dict):
            overrides[str(device_id)] = entry
        else:
            log.warning("ignoring malformed entry for camera %s in %s", device_id, path)
    return overrides


def _data_dir() -> Path:
    try:
        from blackice.config import get_settings

        return get_settings().data_dir
    except Exception:  # pragma: no cover - only if core config is unavailable
        return Path("data")


def _cameras_file() -> Path | None:
    explicit = os.environ.get(ENV_CAMERAS_FILE, "").strip()
    if explicit:
        return Path(explicit)
    # Sits next to the core database rather than in the plugin's own directory,
    # because it is a thing the owner edits by hand.
    return _data_dir() / DEFAULT_CAMERAS_FILE


def reload_overrides(settings: Settings) -> dict[str, dict[str, Any]]:
    """Re-read just the camera file, for picking up an edit without a restart."""
    return _load_overrides(settings.cameras_file)


def load() -> Settings:
    """Read the environment and the camera file. Never raises."""
    path = _cameras_file()
    return Settings(
        username=os.environ.get(ENV_USERNAME, "").strip() or "admin",
        password=os.environ.get(ENV_PASSWORD, "").strip(),
        quality=parse_quality(os.environ.get(ENV_QUALITY)),
        cameras_file=path,
        rtsp_enabled=_env_bool(ENV_RTSP_ENABLED, True),
        rtsp_port=_env_int(ENV_RTSP_PORT, 8554),
        discovery_enabled=_env_bool(ENV_DISCOVERY_ENABLED, True),
        discovery_interval=_env_float(ENV_DISCOVERY_INTERVAL, DEFAULT_DISCOVERY_INTERVAL),
        snapshot_interval=_env_float(ENV_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL),
        offline_after=_env_float(ENV_OFFLINE_AFTER, DEFAULT_OFFLINE_AFTER),
        overrides=_load_overrides(path),
    )


def audio_dir() -> Path:
    """Where playable clips live."""
    explicit = os.environ.get(ENV_AUDIO_DIR, "").strip()
    if explicit:
        return Path(explicit)
    return _data_dir() / DEFAULT_AUDIO_DIR


def resolve_clip(name: str) -> Path | None:
    """A clip inside the audio directory, or None if it is not there.

    The name comes from the model, so it is resolved and then checked for
    containment — `../../etc/passwd` must not escape, and neither must a
    symlink planted inside the directory.
    """
    root = audio_dir().resolve()
    try:
        target = (root / name).resolve()
    except (OSError, RuntimeError):
        return None
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


def list_clips() -> list[str]:
    root = audio_dir()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_file() and not p.name.startswith("."))


def media_root() -> Path:
    """Where snapshots go, so that `MediaRef.path` resolves under /media/."""
    try:
        from blackice.config import get_settings

        return get_settings().media_dir
    except Exception:  # pragma: no cover
        return _data_dir() / "media"
