"""V380 cameras for Black Ice.

A pure-Python implementation of the V380 LAN protocol — discovery, login,
AES-decrypted H.264/H.265 video and IMA-ADPCM audio, PTZ/light/image control —
with an RTSP server for live view and a frame-subscription API for downstream
recognition models.

Ported from Vasang123/camera-v380decoder. See `protocol.py` for the wire
format and `fleet.py` for how frames reach consumers.
"""

from .client import AudioFrame, ClientStats, V380Client, VideoFrame
from .codec import Codec
from .discovery import DiscoveredCamera, discover
from .fleet import Camera, CameraState, Fleet, FrameSubscription
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
    "discover",
]
