"""V380 cameras for Black Ice.

A pure-Python implementation of the V380 LAN protocol — discovery, login,
AES-decrypted H.264/H.265 video and IMA-ADPCM audio, PTZ/light/image control —
with an RTSP server for live view and a frame-subscription API for downstream
recognition models.

Ported from Vasang123/camera-v380decoder. See `protocol.py` for the wire
format and `fleet.py` for how frames reach consumers.

Another plugin that wants these frames — a face recogniser, a gait model — asks
for the live fleet with `active_fleet()` (or `wait_for_fleet()`, since start
order between plugins is not guaranteed) and subscribes to a camera:

    from blackice_v380 import wait_for_fleet

    fleet = await wait_for_fleet()
    if fleet is None:
        return          # this process does not own the cameras; do nothing
    with fleet.subscribe("95886601") as feed:
        async for frame in feed:
            ...
"""

from .client import AudioFrame, ClientStats, V380Client, VideoFrame
from .codec import Codec
from .discovery import DiscoveredCamera, discover
from .fleet import (
    Camera,
    CameraState,
    Fleet,
    FrameSubscription,
    active_fleet,
    on_fleet_change,
    wait_for_fleet,
)
from .plugin import SENSOR_ID, V380Plugin
from .settings import CameraConfig, Settings

__all__ = [
    "SENSOR_ID",
    "AudioFrame",
    "Camera",
    "CameraConfig",
    "CameraState",
    "ClientStats",
    "Codec",
    "DiscoveredCamera",
    "Fleet",
    "FrameSubscription",
    "Settings",
    "V380Client",
    "V380Plugin",
    "VideoFrame",
    "active_fleet",
    "discover",
    "on_fleet_change",
    "wait_for_fleet",
]
