"""HEVC to H.264, for RTSP clients that cannot take H.265.

The 3-lens cameras emit H.265. Most RTSP consumers — browsers via WebRTC
gateways, older NVR software, the SDP this server advertises — expect H.264, so
frames from those cameras go through a long-lived ffmpeg process instead of
straight to the sessions.

The output has to be re-split into access units before it can be packetised,
because ffmpeg emits a byte stream with no frame boundaries. `-x264-params
aud=1` makes it insert access unit delimiters, which is the boundary we cut on;
a second VCL slice without an intervening delimiter is the fallback.

This is by far the most expensive thing in the plugin. It only starts when a
camera actually produces H.265, and only while an RTSP client is attached.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from .codec import (
    H264_AUD,
    H264_VCL,
    Codec,
    find_start_code,
    h264_type,
    start_code_length,
)

log = logging.getLogger("blackice.plugin.v380.transcode")

FFMPEG = "ffmpeg"
READ_CHUNK = 65536
#: Frames waiting to enter ffmpeg. Small on purpose: if the encoder cannot keep
#: up, dropping live video is better than growing an unbounded backlog of it.
INPUT_QUEUE = 8

FFMPEG_ARGS = (
    "-hide_banner", "-loglevel", "error",
    "-fflags", "nobuffer",
    "-f", "hevc", "-i", "pipe:0",
    "-an",
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-tune", "zerolatency",
    "-bf", "0",
    "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
    "-pix_fmt", "yuv420p",
    # aud=1 gives us frame boundaries; repeat-headers=1 puts SPS/PPS in front
    # of every keyframe, so a client that joins mid-stream can decode at once.
    "-x264-params", "aud=1:repeat-headers=1",
    "-f", "h264", "pipe:1",
)


class Transcoder:
    """One ffmpeg process turning HEVC access units into H.264 ones."""

    def __init__(self, on_frame: Callable[[bytes], None]) -> None:
        self.on_frame = on_frame
        self.available = False
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=INPUT_QUEUE)
        self._buffer = bytearray()
        self._unit: list[bytes] = []
        self._unit_has_vcl = False
        self._dropped = 0

    async def start(self) -> bool:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                FFMPEG, *FFMPEG_ARGS,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as exc:
            log.warning("could not start ffmpeg: %s", exc)
            return False

        self.available = True
        self._tasks = [
            asyncio.create_task(self._feed(), name="v380-xcode-in"),
            asyncio.create_task(self._drain(), name="v380-xcode-out"),
            asyncio.create_task(self._log_stderr(), name="v380-xcode-err"),
        ]
        log.debug("transcoder started")
        return True

    def push(self, payload: bytes) -> None:
        """Offer a frame. Dropped rather than queued if the encoder is behind."""
        if not self.available or not payload:
            return
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                log.debug("transcoder behind; dropped %d frames", self._dropped)

    async def stop(self) -> None:
        self.available = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        proc, self._proc = self._proc, None
        if proc is None:
            return
        with contextlib.suppress(Exception):
            if proc.stdin is not None:
                proc.stdin.close()
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()

    # --- pipes -------------------------------------------------------------

    async def _feed(self) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        stdin = self._proc.stdin
        while True:
            payload = await self._queue.get()
            try:
                stdin.write(payload)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                log.warning("transcoder input closed: %s", exc)
                self.available = False
                return

    async def _drain(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        while True:
            chunk = await stdout.read(READ_CHUNK)
            if not chunk:
                self.available = False
                self._flush_unit()
                return
            self._buffer += chunk
            self._split_units()

    async def _log_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            log.debug("ffmpeg: %s", line.decode("utf-8", "replace").strip())

    # --- access unit assembly ----------------------------------------------

    def _split_units(self) -> None:
        """Cut the buffered byte stream into NALs and group them into frames.

        The last NAL is left in the buffer: without a following start code we
        cannot know it is complete.
        """
        data = bytes(self._buffer)
        consumed = 0
        while True:
            start = find_start_code(data, consumed)
            if start < 0:
                break
            nxt = find_start_code(data, start + 3)
            if nxt < 0:
                break
            self._take_nal(data[start:nxt])
            consumed = nxt

        if consumed:
            del self._buffer[:consumed]

    def _take_nal(self, nal: bytes) -> None:
        sc_len = start_code_length(nal, 0)
        if len(nal) <= sc_len:
            return

        nal_type = h264_type(nal[sc_len])
        if nal_type == H264_AUD:
            self._flush_unit()
            return

        is_vcl = nal_type in H264_VCL
        if is_vcl and self._unit_has_vcl:
            self._flush_unit()

        self._unit.append(nal)
        if is_vcl:
            self._unit_has_vcl = True

    def _flush_unit(self) -> None:
        if not self._unit:
            return
        payload = b"".join(self._unit)
        self._unit.clear()
        self._unit_has_vcl = False
        try:
            self.on_frame(payload)
        except Exception:
            log.exception("transcoded frame consumer failed")


def needs_transcode(stream_codec: Codec) -> bool:
    return stream_codec is Codec.H265
