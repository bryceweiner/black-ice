"""One live session with one V380 camera.

`V380Client.run()` is a supervised loop: authenticate, open the media socket,
reassemble fragments into frames, hand each frame to whoever registered a sink,
and reconnect when the camera drops us. It only returns when cancelled or when
the camera rejects the credentials, since retrying a wrong password forever
just locks the account out.

Frames are pushed to *sinks* rather than pulled from a queue. The plugin's own
snapshot cache is one sink; a downstream recogniser is another. Sinks are
called inline on the receive path, so a slow one stalls the stream — see
`FrameSubscription` in `fleet.py` for the buffered, drop-oldest wrapper that
recognition consumers should use instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import audio as audio_codec
from . import codec, protocol

log = logging.getLogger("blackice.plugin.v380.client")

#: Handshake and per-read deadlines. The read timeout doubles as liveness
#: detection: a healthy camera never goes this long without a fragment.
CONNECT_TIMEOUT = 5.0
HANDSHAKE_TIMEOUT = 5.0
READ_TIMEOUT = 8.0
#: Backoff between reconnect attempts, and its ceiling.
RETRY_BASE = 3.0
RETRY_MAX = 60.0
#: How far to hunt for the fragment magic byte after losing sync before
#: concluding the socket is not carrying a V380 stream at all.
RESYNC_LIMIT = 65536


class AuthenticationError(Exception):
    """The camera rejected the credentials. Retrying will not help."""


async def authenticate(
    ip: str,
    device_id: int,
    username: str,
    password: str,
    *,
    port: int = protocol.DEFAULT_PORT,
    cloud: bool = False,
) -> protocol.LoginReply:
    """Command 1167 on a socket of its own, which the camera then closes.

    Standalone because every kind of session needs a ticket first: the video
    stream, and each talkback session (see `talkback.py`).
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(ip, port), CONNECT_TIMEOUT
    )
    try:
        writer.write(
            protocol.build_login(device_id, username, password, cloud=cloud, port=port)
        )
        await writer.drain()
        raw = await asyncio.wait_for(
            reader.readexactly(protocol.LOGIN_REPLY_LEN), HANDSHAKE_TIMEOUT
        )
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    reply = protocol.parse_login_reply(raw)
    if not reply.ok:
        raise AuthenticationError(reply.fatal)
    return reply


@dataclass(slots=True)
class VideoFrame:
    """One access unit of Annex B video, plus what the camera said about it."""

    payload: bytes
    codec: codec.Codec
    keyframe: bool
    frame_id: int
    timestamp: int
    frame_rate: int
    received_at: float


@dataclass(slots=True)
class AudioFrame:
    """G.711 A-law audio, whatever the camera originally sent."""

    payload: bytes
    timestamp: int
    received_at: float


@dataclass(slots=True)
class ClientStats:
    connected_since: float | None = None
    frames: int = 0
    keyframes: int = 0
    audio_frames: int = 0
    resyncs: int = 0
    unclassified: int = 0
    reconnects: int = 0
    last_frame_at: float | None = None
    last_error: str = ""

    @property
    def connected(self) -> bool:
        return self.connected_since is not None


VideoSink = Callable[[VideoFrame], None]
AudioSink = Callable[[AudioFrame], None]


class V380Client:
    """A single camera session. One instance per camera, driven by `run()`."""

    def __init__(
        self,
        ip: str,
        device_id: int,
        username: str,
        password: str,
        *,
        port: int = protocol.DEFAULT_PORT,
        cloud: bool = False,
        quality: int = protocol.QUALITY_HD,
        name: str = "",
    ) -> None:
        self.ip = ip
        self.port = port
        self.device_id = device_id
        self.username = username
        self.password = password
        self.cloud = cloud
        self.quality = quality
        self.name = name or str(device_id)
        self.log = log.getChild(self.name)

        self.stats = ClientStats()
        self.parameter_sets = codec.ParameterSets()
        self.latest_keyframe: bytes | None = None
        self.latest_keyframe_at: float = 0.0
        self.width = 1280
        self.height = 720
        self.device_version = 0
        self.communication_version = 0
        self.audio_bits = 8

        self._video_sinks: list[VideoSink] = []
        self._audio_sinks: list[AudioSink] = []
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._media_key: bytes | None = None
        self._reassembler = protocol.Reassembler()
        self._connected_event = asyncio.Event()

    # --- sinks -------------------------------------------------------------

    def add_video_sink(self, sink: VideoSink) -> Callable[[], None]:
        """Register a video callback. Returns the function that removes it."""
        self._video_sinks.append(sink)
        return lambda: self._discard(self._video_sinks, sink)

    def add_audio_sink(self, sink: AudioSink) -> Callable[[], None]:
        self._audio_sinks.append(sink)
        return lambda: self._discard(self._audio_sinks, sink)

    @staticmethod
    def _discard(sinks: list, sink) -> None:
        with contextlib.suppress(ValueError):
            sinks.remove(sink)

    # --- lifecycle ---------------------------------------------------------

    async def wait_connected(self, timeout: float) -> bool:
        """Block until the media stream is up. False if it did not come up."""
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def run(self) -> None:
        """Stay connected until cancelled. Never raises for a transient fault."""
        delay = RETRY_BASE
        while True:
            try:
                await self._connect()
                delay = RETRY_BASE
                await self._receive_loop()
            except asyncio.CancelledError:
                raise
            except AuthenticationError as exc:
                # Credentials are wrong: stop, and leave the reason visible.
                self.stats.last_error = str(exc)
                self.log.error("%s — giving up on this camera", exc)
                return
            except Exception as exc:
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                self.log.warning("session failed: %s", self.stats.last_error)
            finally:
                await self._close_stream()

            self.stats.reconnects += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX)

    async def close(self) -> None:
        await self._close_stream()

    async def _close_stream(self) -> None:
        self._connected_event.clear()
        self.stats.connected_since = None
        writer, self._writer, self._reader = self._writer, None, None
        self._reassembler.reset()
        if writer is None:
            return
        writer.close()
        # A camera that vanished mid-session never completes the close
        # handshake; the socket is already gone either way.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=2.0)

    # --- handshake ---------------------------------------------------------

    async def _connect(self) -> None:
        reply = await self._authenticate()
        await self._stream_login(reply)
        await self._send(protocol.build_stream_start())

        self.stats.connected_since = time.time()
        self.stats.last_error = ""
        self._connected_event.set()
        self.log.info(
            "connected to %s:%s (device version %s, %s)",
            self.ip, self.port, self.device_version,
            "encrypted" if self._media_key else "clear",
        )

    async def _authenticate(self) -> protocol.LoginReply:
        reply = await authenticate(
            self.ip, self.device_id, self.username, self.password,
            port=self.port, cloud=self.cloud,
        )
        self.device_version = reply.device_version
        self._media_key = protocol.media_key(reply.auth_ticket) if reply.encrypted else None
        return reply

    async def _stream_login(self, auth: protocol.LoginReply) -> None:
        """Command 301 on the socket that will carry the media."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.ip, self.port), CONNECT_TIMEOUT
        )
        self._reader, self._writer = reader, writer
        # Control words are 16 bytes; Nagle would sit on a PTZ command for
        # tens of milliseconds waiting for more to send.
        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        await self._send(
            protocol.build_stream_login(
                self.device_id, auth.auth_ticket,
                session_id=auth.session_id, quality=self.quality,
                cloud=self.cloud, port=self.port,
            )
        )
        raw = await asyncio.wait_for(
            self._read_reply(protocol.STREAM_LOGIN_REPLY_LEN), HANDSHAKE_TIMEOUT
        )
        reply = protocol.parse_stream_login_reply(raw, cloud=self.cloud)
        if not reply.ok:
            raise ConnectionError(f"camera refused the stream (result {reply.result})")

        if not self.cloud:
            self.communication_version = reply.communication_version
            self.width, self.height = reply.width, reply.height
            self.audio_bits = reply.audio_bits

    async def _read_reply(self, size: int) -> bytes:
        """Read a fixed-size reply that may arrive short.

        Nothing else is in flight at this point — the camera stays silent until
        command 303 — so draining whatever it sent is safe, and we settle for a
        short read rather than blocking on bytes that will never come.
        """
        buf = bytearray()
        assert self._reader is not None
        while len(buf) < size:
            try:
                chunk = await asyncio.wait_for(
                    self._reader.read(size - len(buf)), 0.5 if buf else HANDSHAKE_TIMEOUT
                )
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
        if len(buf) < 8:
            raise ConnectionError("camera closed before replying to the stream login")
        return bytes(buf)

    # --- receive path ------------------------------------------------------

    async def _receive_loop(self) -> None:
        assert self._reader is not None
        reader = self._reader

        while True:
            header = await self._read_fragment_header(reader)
            payload = await asyncio.wait_for(
                reader.readexactly(header.length), READ_TIMEOUT
            )

            if header.type == protocol.TYPE_IGNORED:
                continue

            completed = self._reassembler.push(header, payload)
            self.stats.resyncs = self._reassembler.resyncs
            if completed is None:
                continue

            raw_type, frame = completed
            self._handle_frame(raw_type, frame)

    async def _read_fragment_header(
        self, reader: asyncio.StreamReader
    ) -> protocol.FragmentHeader:
        """Read one fragment header, resynchronising byte-wise if needed.

        Upstream skips a whole 12 bytes on a bad header, which recovers only by
        luck. Sliding one byte at a time to the next magic marker recovers
        deterministically from a partial write or a dropped fragment.
        """
        buf = bytearray(
            await asyncio.wait_for(
                reader.readexactly(protocol.FRAGMENT_HEADER_LEN), READ_TIMEOUT
            )
        )
        header = protocol.parse_fragment_header(bytes(buf))
        if header is not None:
            return header

        # Whatever we were assembling is now unreachable.
        self._reassembler.reset()
        for _ in range(RESYNC_LIMIT):
            del buf[0]
            buf += await asyncio.wait_for(reader.readexactly(1), READ_TIMEOUT)
            header = protocol.parse_fragment_header(bytes(buf))
            if header is not None:
                self.log.debug("resynchronised on the fragment marker")
                return header
        raise ConnectionError("lost stream synchronisation")

    def _handle_frame(self, raw_type: int, frame: bytes) -> None:
        head = protocol.parse_frame_header(frame)
        body = frame[protocol.FRAME_HEADER_LEN :]

        if raw_type == protocol.TYPE_AUDIO_TIMED:
            self._emit_audio(self._decrypt_audio(body), head.timestamp)
            return
        if raw_type == protocol.TYPE_AUDIO_RAW:
            self._emit_audio(self._extract_audio(frame), head.timestamp)
            return

        payload = codec.pick_video_payload(self._video_candidates(body))
        if payload is None:
            self.stats.unclassified += 1
            return

        self.parameter_sets.observe(payload)
        keyframe = codec.is_keyframe(raw_type, head.frame_type, payload)
        now = time.time()

        if keyframe:
            self.latest_keyframe = payload
            self.latest_keyframe_at = now
            self.stats.keyframes += 1

        self.stats.frames += 1
        self.stats.last_frame_at = now

        video = VideoFrame(
            payload=payload,
            codec=codec.detect_codec(payload),
            keyframe=keyframe,
            frame_id=head.frame_id,
            timestamp=head.timestamp,
            frame_rate=head.frame_rate,
            received_at=now,
        )
        self._fan_out(self._video_sinks, video)

    def _video_candidates(self, body: bytes) -> list[bytes]:
        """Both readings of the payload, for the scorer to choose between.

        We cannot ask the camera whether a given frame is encrypted — the
        device version says only that it *may* be — so we offer the decrypted
        and the plaintext view and let whichever contains real NAL units win.
        """
        if not body:
            return []
        if self._media_key is None or len(body) < 16:
            return [body]
        return [self._decrypt_video(body), body]

    def _decrypt_video(self, body: bytes) -> bytes:
        assert self._media_key is not None
        if self.communication_version == protocol.COMM_VERSION_PRE2K:
            return protocol.decrypt_pre2k(self._media_key, body)
        return protocol.decrypt_video(self._media_key, body)

    def _decrypt_audio(self, body: bytes) -> bytes:
        if self._media_key is None or len(body) < 16:
            return body
        if self.communication_version == protocol.COMM_VERSION_PRE2K:
            return protocol.decrypt_pre2k(self._media_key, body)
        return protocol.decrypt_audio(self._media_key, body)

    def _extract_audio(self, frame: bytes) -> bytes:
        """The 0x16 audio path, whose header length depends on the device."""
        header_len = audio_codec.audio_header_length(self.audio_bits)
        body = frame[header_len:] if len(frame) > header_len else frame
        body = self._decrypt_audio(body)
        if self.audio_bits == 16 and len(body) == audio_codec.ADPCM_BLOCK_LEN:
            return audio_codec.adpcm_to_alaw(body)
        return body

    def _emit_audio(self, payload: bytes, timestamp: int) -> None:
        if not payload:
            return
        self.stats.audio_frames += 1
        self._fan_out(
            self._audio_sinks,
            AudioFrame(payload=payload, timestamp=timestamp, received_at=time.time()),
        )

    def _fan_out(self, sinks: list, frame) -> None:
        """Deliver to every sink. One broken sink must not kill the stream."""
        for sink in list(sinks):
            try:
                sink(frame)
            except Exception:
                self.log.exception("frame sink failed; dropping it")
                self._discard(sinks, sink)

    # --- control -----------------------------------------------------------

    async def send_control(self, command: str) -> bool:
        """Send a PTZ/light/image control word. False if we are not connected."""
        payload = protocol.CONTROL.get(command)
        if payload is None:
            raise KeyError(command)
        if self._writer is None:
            return False
        try:
            await self._send(payload)
        except (OSError, ConnectionError):
            return False
        return True

    async def _send(self, data: bytes) -> None:
        if self._writer is None:
            raise ConnectionError("not connected")
        async with self._write_lock:
            self._writer.write(data)
            await self._writer.drain()
