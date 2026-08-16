"""An RTSP server for the whole fleet.

The C# original runs one process, and therefore one RTSP port, per camera.
Here every camera is served by one listener and selected by path:

    rtsp://<host>:8554/<device_id>

RTP is interleaved over the same TCP connection (RFC 2326 §10.12). UDP would be
lower overhead, but interleaved needs no second port, no NAT hole, and no
client-side firewall change — and for a handful of viewers on a LAN the
difference does not show.

Media timing does **not** use the camera's timestamps. Those are a free-running
device clock with an arbitrary origin, so passing them through makes RTP jump
by billions of ticks and clients abandon the stream as non-monotonic. Video is
timed from wall clock since the session started playing, and audio simply
counts samples, which is what PCMA at 8 kHz means anyway.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import random
import time
from dataclasses import dataclass, field

from .client import AudioFrame, VideoFrame
from .codec import H264_PPS, H264_SPS, H264_VCL, Codec, h264_type, iter_nals

log = logging.getLogger("blackice.plugin.v380.rtsp")

DEFAULT_RTSP_PORT = 8554

#: RTP payload types. 96 is the usual dynamic type for H.264; 8 is the static
#: assignment for G.711 A-law, so no rtpmap negotiation is needed for audio.
PT_VIDEO = 96
PT_AUDIO = 8
VIDEO_CLOCK = 90000
AUDIO_CLOCK = 8000
#: Conservative for LAN Ethernet: 1400 leaves room for RTP, TCP, and IP headers
#: inside a 1500-byte MTU.
MTU = 1400
#: 20 ms of A-law at 8 kHz.
AUDIO_CHUNK = 160
#: Fallback profile-level-id when we have not seen an SPS yet: baseline 3.1.
DEFAULT_PROFILE = "64001F"

TRACK_VIDEO = "trackID=0"
TRACK_AUDIO = "trackID=1"


@dataclass(slots=True)
class _Track:
    channel: int
    sequence: int = 0
    ssrc: int = field(default_factory=lambda: random.getrandbits(32))


class RtspStream:
    """One camera's published media, and the sessions watching it.

    Frames are pushed in by the fleet; this object holds only what a late
    joiner needs — the parameter sets for the SDP — and forwards the rest.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.sessions: set[RtspSession] = set()
        self.sps: bytes | None = None
        self.pps: bytes | None = None

    @property
    def viewers(self) -> int:
        return sum(1 for s in self.sessions if s.playing)

    def push_video(self, frame: VideoFrame) -> None:
        self._cache_parameter_sets(frame.payload)
        for session in list(self.sessions):
            session.send_video(frame.payload)

    def push_audio(self, frame: AudioFrame) -> None:
        for session in list(self.sessions):
            session.send_audio(frame.payload)

    def _cache_parameter_sets(self, payload: bytes) -> None:
        if self.sps is not None and self.pps is not None:
            return
        for nal_type, nal in iter_nals(payload, Codec.H264):
            if nal_type == H264_SPS and self.sps is None:
                self.sps = nal
            elif nal_type == H264_PPS and self.pps is None:
                self.pps = nal

    def sdp(self) -> str:
        """The session description. Always H.264 — H.265 cameras are
        transcoded before their frames reach here."""
        fmtp = ""
        if self.sps and self.pps:
            profile = self.sps[:3].hex().upper() if len(self.sps) >= 3 else DEFAULT_PROFILE
            fmtp = (
                "a=fmtp:96 packetization-mode=1;"
                f"sprop-parameter-sets={base64.b64encode(self.sps).decode()},"
                f"{base64.b64encode(self.pps).decode()};"
                f"profile-level-id={profile}\r\n"
            )
        return (
            "v=0\r\n"
            "o=- 1 1 IN IP4 0.0.0.0\r\n"
            f"s=V380 {self.name}\r\n"
            "t=0 0\r\n"
            "a=recvonly\r\n"
            "m=video 0 RTP/AVP 96\r\n"
            "a=rtpmap:96 H264/90000\r\n"
            f"{fmtp}"
            f"a=control:{TRACK_VIDEO}\r\n"
            "m=audio 0 RTP/AVP 8\r\n"
            "a=rtpmap:8 PCMA/8000/1\r\n"
            f"a=control:{TRACK_AUDIO}\r\n"
        )


class RtspSession:
    """One client connection."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, server: RtspServer
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.server = server
        self.stream: RtspStream | None = None
        self.playing = False
        self.video = _Track(channel=0)
        self.audio = _Track(channel=2)
        self._started_at = 0.0
        self._audio_ts = 0
        self._alive = True

    async def serve(self) -> None:
        try:
            while self._alive:
                request = await self._read_request()
                if request is None:
                    return
                await self._handle(request)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.close()

    async def _read_request(self) -> list[str] | None:
        """Read one request. Interleaved data from a client is ignored — we do
        not accept media, only send it."""
        lines: list[str] = []
        while True:
            raw = await self.reader.readline()
            if not raw:
                return None
            line = raw.decode("ascii", "replace").rstrip("\r\n")
            if not line:
                if lines:
                    return lines
                continue
            lines.append(line)

    async def _handle(self, lines: list[str]) -> None:
        parts = lines[0].split()
        method = parts[0].upper() if parts else ""
        url = parts[1] if len(parts) > 1 else ""
        headers = self._headers(lines[1:])
        cseq = headers.get("cseq", "0")

        match method:
            case "OPTIONS":
                await self._reply(cseq, "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN")
            case "DESCRIBE":
                await self._describe(cseq, url)
            case "SETUP":
                await self._setup(cseq, url, headers.get("transport", ""))
            case "PLAY":
                await self._play(cseq, url)
            case "TEARDOWN":
                await self._reply(cseq, "Session: 1")
                self.close()
            case _:
                await self._send_text(f"RTSP/1.0 501 Not Implemented\r\nCSeq: {cseq}\r\n\r\n")

    @staticmethod
    def _headers(lines: list[str]) -> dict[str, str]:
        headers = {}
        for line in lines:
            name, _, value = line.partition(":")
            if value:
                headers[name.strip().lower()] = value.strip()
        return headers

    async def _describe(self, cseq: str, url: str) -> None:
        stream = self.server.resolve(url)
        if stream is None:
            await self._send_text(f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n")
            return
        self.stream = stream
        body = stream.sdp().encode("ascii")
        await self._send_text(
            f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n"
            "Content-Type: application/sdp\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
            f"{body.decode('ascii')}"
        )

    async def _setup(self, cseq: str, url: str, transport: str) -> None:
        if self.stream is None:
            self.stream = self.server.resolve(url)
        if self.stream is None:
            await self._send_text(f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n")
            return

        is_audio = TRACK_AUDIO in url
        channel = 2 if is_audio else 0
        # Honour the client's channel numbering when it asks for specific ones.
        if "interleaved=" in transport:
            with contextlib.suppress(ValueError, IndexError):
                spec = transport.split("interleaved=", 1)[1].split(";")[0]
                channel = int(spec.split("-")[0])

        if is_audio:
            self.audio.channel = channel
        else:
            self.video.channel = channel

        await self._reply(
            cseq,
            f"Transport: RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1}",
            "Session: 1",
        )

    async def _play(self, cseq: str, url: str) -> None:
        if self.stream is None:
            await self._send_text(f"RTSP/1.0 404 Not Found\r\nCSeq: {cseq}\r\n\r\n")
            return
        base = url.split("/" + TRACK_VIDEO)[0]
        await self._reply(
            cseq,
            "Session: 1",
            f"RTP-Info: url={base}/{TRACK_VIDEO};seq={self.video.sequence},"
            f"url={base}/{TRACK_AUDIO};seq={self.audio.sequence}",
        )
        self._started_at = time.monotonic()
        self.playing = True
        self.stream.sessions.add(self)
        log.info("client playing %s", self.stream.name)

    async def _reply(self, cseq: str, *headers: str) -> None:
        head = "".join(f"{h}\r\n" for h in headers)
        await self._send_text(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{head}\r\n")

    async def _send_text(self, text: str) -> None:
        self._write(text.encode("ascii", "replace"))

    # --- media -------------------------------------------------------------

    def send_video(self, payload: bytes) -> None:
        if not self.playing:
            return
        timestamp = int((time.monotonic() - self._started_at) * VIDEO_CLOCK) & 0xFFFFFFFF
        for nal_type, nal in iter_nals(payload, Codec.H264):
            if not nal:
                continue
            # RFC 6184: the marker bit ends an access unit, so it belongs on the
            # slice, not on the parameter sets and delimiters preceding it.
            last = nal_type in H264_VCL
            if len(nal) <= MTU:
                self._send_rtp(self.video, PT_VIDEO, timestamp, nal, marker=last)
            else:
                self._send_fragmented(nal, timestamp, last)

    def _send_fragmented(self, nal: bytes, timestamp: int, last_of_unit: bool) -> None:
        """FU-A fragmentation for a NAL too large for one packet."""
        header = nal[0]
        indicator = (header & 0xE0) | 28
        offset = 1
        first = True
        while offset < len(nal):
            chunk = nal[offset : offset + MTU - 2]
            offset += len(chunk)
            last = offset >= len(nal)

            fu_header = h264_type(header)
            if first:
                fu_header |= 0x80
            if last:
                fu_header |= 0x40

            self._send_rtp(
                self.video,
                PT_VIDEO,
                timestamp,
                bytes([indicator, fu_header]) + chunk,
                marker=last and last_of_unit,
            )
            first = False

    def send_audio(self, payload: bytes) -> None:
        if not self.playing:
            return
        for offset in range(0, len(payload), AUDIO_CHUNK):
            chunk = payload[offset : offset + AUDIO_CHUNK]
            self._send_rtp(self.audio, PT_AUDIO, self._audio_ts, chunk, marker=False)
            # One A-law byte is one sample, so the sample clock advances by the
            # payload length. This is what keeps the RTP clock monotonic.
            self._audio_ts = (self._audio_ts + len(chunk)) & 0xFFFFFFFF

    def _send_rtp(
        self, track: _Track, payload_type: int, timestamp: int, payload: bytes, *, marker: bool
    ) -> None:
        header = bytearray(12)
        header[0] = 0x80  # version 2, no padding, no extension, no CSRCs
        header[1] = (0x80 if marker else 0) | (payload_type & 0x7F)
        header[2:4] = track.sequence.to_bytes(2, "big")
        header[4:8] = (timestamp & 0xFFFFFFFF).to_bytes(4, "big")
        header[8:12] = track.ssrc.to_bytes(4, "big")
        track.sequence = (track.sequence + 1) & 0xFFFF

        packet = bytes(header) + payload
        # Interleaved framing: '$', channel, 16-bit length.
        self._write(b"$" + bytes([track.channel]) + len(packet).to_bytes(2, "big") + packet)

    def _write(self, data: bytes) -> None:
        if not self._alive:
            return
        try:
            self.writer.write(data)
        except (ConnectionError, RuntimeError):
            self.close()

    def close(self) -> None:
        if not self._alive:
            return
        self._alive = False
        self.playing = False
        if self.stream is not None:
            self.stream.sessions.discard(self)
        with contextlib.suppress(Exception):
            self.writer.close()


class RtspServer:
    """One listener, every camera, addressed by path."""

    def __init__(self, port: int = DEFAULT_RTSP_PORT, host: str = "0.0.0.0") -> None:
        self.host = host
        self.port = port
        self.streams: dict[str, RtspStream] = {}
        self._server: asyncio.Server | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def stream_for(self, name: str) -> RtspStream:
        """The stream for a camera, created on first use."""
        stream = self.streams.get(name)
        if stream is None:
            stream = RtspStream(name)
            self.streams[name] = stream
        return stream

    def resolve(self, url: str) -> RtspStream | None:
        """Map a request URL to a stream.

        The path is the camera name; `trackID=` suffixes from SETUP are
        stripped. A single-camera fleet also answers on any path, so the
        conventional rtsp://host/live keeps working.
        """
        path = url.split("://", 1)[-1]
        path = path.split("/", 1)[1] if "/" in path else ""
        path = path.split("?", 1)[0].strip("/")
        if path.endswith(TRACK_VIDEO) or path.endswith(TRACK_AUDIO):
            path = path.rsplit("/", 1)[0]

        if path in self.streams:
            return self.streams[path]
        if len(self.streams) == 1:
            return next(iter(self.streams.values()))
        return None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self.host, self.port)
        log.info("RTSP listening on rtsp://%s:%s/<device_id>", self.host, self.port)

    async def stop(self) -> None:
        for stream in self.streams.values():
            for session in list(stream.sessions):
                session.close()
        self.streams.clear()

        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        session = RtspSession(reader, writer, self)
        try:
            await session.serve()
        except Exception:
            log.exception("RTSP session failed")
        finally:
            session.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
