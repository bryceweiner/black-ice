"""Turning a keyframe into a JPEG.

The C# original carries its own H.264 decoder and falls back to ffmpeg. Here
ffmpeg is the only path: it handles both H.264 and the H.265 the 3-lens models
emit, it is already a dependency of this machine, and a subprocess per snapshot
is cheap next to the decode itself.

Two things make this less trivial than piping bytes at ffmpeg:

* a keyframe alone is not decodable — the parameter sets arrive separately and
  have to be prepended (`codec.ParameterSets.prepend`);
* these cameras periodically produce a frame that decodes to flat grey. It is
  a valid JPEG, so nothing downstream would reject it, and stored unchecked it
  fills the timeline with blank images. `is_blank` catches it the way upstream
  does, by sampling the decoded pixels.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .codec import Codec, ParameterSets

log = logging.getLogger("blackice.plugin.v380.snapshot")

FFMPEG = "ffmpeg"
DECODE_TIMEOUT = 8.0

#: Size of the thumbnail used for the blank-frame test. Small enough that the
#: statistics are a few hundred pixels of pure Python, large enough to notice
#: real detail.
_PROBE_W, _PROBE_H = 32, 18
#: Above this fraction of near-grey pixels *and* below this luminance variance,
#: the frame is a decode artefact rather than a picture.
_GREY_RATIO = 0.96
_GREY_VARIANCE = 180.0
#: Channels within this of each other count as grey.
_CHANNEL_SPREAD = 8


def ffmpeg_available() -> bool:
    return shutil.which(FFMPEG) is not None


@dataclass(slots=True)
class Snapshot:
    jpeg: bytes
    captured_at: float
    version: int


def is_blank(probe: bytes) -> bool:
    """Whether a 32x18 rgb24 thumbnail is a flat grey decode artefact."""
    pixels = len(probe) // 3
    if pixels == 0:
        return False

    total = 0
    total_sq = 0
    near_grey = 0
    for i in range(0, pixels * 3, 3):
        r, g, b = probe[i], probe[i + 1], probe[i + 2]
        # Rec. 601 luma, integer form, as upstream.
        luma = (r * 77 + g * 150 + b * 29) >> 8
        total += luma
        total_sq += luma * luma
        if abs(r - g) < _CHANNEL_SPREAD and abs(g - b) < _CHANNEL_SPREAD:
            near_grey += 1

    mean = total / pixels
    variance = (total_sq / pixels) - (mean * mean)
    return (near_grey / pixels) > _GREY_RATIO and variance < _GREY_VARIANCE


async def decode_jpeg(access_unit: bytes, codec: Codec) -> bytes | None:
    """Decode one access unit to JPEG. None if ffmpeg could not make a picture.

    ffmpeg writes the JPEG to stdout and a thumbnail to a temp file in the same
    pass, so the blank-frame check costs no second decode.
    """
    with tempfile.TemporaryDirectory(prefix="v380-snap-") as tmp:
        probe_path = Path(tmp) / "probe.raw"
        proc = await asyncio.create_subprocess_exec(
            FFMPEG,
            "-hide_banner", "-loglevel", "error",
            "-f", codec.ffmpeg_format,
            "-i", "pipe:0",
            "-frames:v", "1", "-q:v", "2", "-f", "image2", "pipe:1",
            "-frames:v", "1", "-vf", f"scale={_PROBE_W}:{_PROBE_H}",
            "-pix_fmt", "rgb24", "-f", "rawvideo", str(probe_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            jpeg, err = await asyncio.wait_for(
                proc.communicate(access_unit), DECODE_TIMEOUT
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            log.debug("ffmpeg timed out decoding a %s keyframe", codec)
            return None

        if not jpeg:
            log.debug("ffmpeg produced no image: %s", err.decode("utf-8", "replace").strip())
            return None

        if probe_path.exists() and is_blank(probe_path.read_bytes()):
            log.debug("discarded a blank frame")
            return None

    return jpeg


class SnapshotCache:
    """The most recent decodable picture from one camera.

    Decoding every keyframe would burn a core per camera for pictures nobody
    looks at, so decoding is throttled and, between decodes, callers get the
    last good JPEG. `capture()` forces a fresh one when a caller really needs
    now rather than recent.
    """

    def __init__(self, *, min_interval: float = 2.0, max_age: float = 10.0) -> None:
        self.min_interval = min_interval
        self.max_age = max_age
        self.current: Snapshot | None = None
        self.failures = 0
        self._version = 0
        self._last_attempt = 0.0
        self._lock = asyncio.Lock()

    @property
    def age(self) -> float | None:
        if self.current is None:
            return None
        return time.time() - self.current.captured_at

    def due(self) -> bool:
        return (time.time() - self._last_attempt) >= self.min_interval

    def fresh_enough(self) -> bool:
        age = self.age
        return age is not None and age <= self.max_age

    async def capture(
        self,
        keyframe: bytes | None,
        parameter_sets: ParameterSets,
        *,
        force: bool = False,
    ) -> Snapshot | None:
        """Decode the given keyframe, or return the cached picture.

        Returns None only when there has never been a decodable frame.
        """
        if not force and self.fresh_enough():
            return self.current
        if keyframe is None or not parameter_sets.complete:
            return self.current
        if not force and not self.due():
            return self.current

        access_unit = parameter_sets.prepend(keyframe)
        if access_unit is None:
            return self.current

        async with self._lock:
            # Another caller may have decoded while we waited for the lock.
            if not force and self.fresh_enough():
                return self.current

            self._last_attempt = time.time()
            jpeg = await decode_jpeg(access_unit, parameter_sets.codec)

        if jpeg is None:
            self.failures += 1
            return self.current

        self._version += 1
        self.current = Snapshot(
            jpeg=jpeg, captured_at=time.time(), version=self._version
        )
        return self.current
