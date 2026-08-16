"""Speaking through a camera's speaker.

Most V380 cameras have one, and the app's push-to-talk uses a command family
separate from the video stream: log in, open a second socket, send command 377,
then push encrypted IMA-ADPCM blocks at the rate the speaker consumes them.
Protocol from jericjan/v380-audio-player.

Three things matter for the audio to come out intelligible:

* **Pacing.** The camera has a small buffer and no flow control. Blocks must
  arrive at roughly one per 63 ms — faster overruns the buffer and audio is
  dropped, slower leaves gaps. `_Pacer` schedules against a fixed origin so
  errors do not accumulate over a long clip.
* **Encoder continuity.** ADPCM is differential, so one encoder spans a whole
  session. A new session gets a new encoder; reusing one across sessions makes
  the first block of the second session sound wrong.
* **Format.** 8 kHz, 16-bit, mono, always. Everything else is converted by
  ffmpeg on the way in.

Sources are async iterators of PCM, so a finite clip and an open microphone are
the same thing to `TalkbackSession` — the difference is only whether the
iterator ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path

from . import protocol
from .audio import BLOCK_SECONDS, PCM_BLOCK_BYTES, PCM_RATE, AdpcmEncoder
from .client import authenticate

log = logging.getLogger("blackice.plugin.v380.talkback")

FFMPEG = "ffmpeg"
PIPER = "piper"
CONNECT_TIMEOUT = 5.0
#: A talkback session that runs longer than this is a bug or a stuck
#: microphone, and it is holding a camera's speaker open in someone's house.
MAX_SESSION_SECONDS = 300.0
#: Silence sent to flush the camera's buffer, so the last syllable is not
#: swallowed when the clip ends.
TRAILING_SILENCE_BLOCKS = 3


class TalkbackError(Exception):
    """Talkback could not start or could not continue."""


class _Pacer:
    """Releases one block per `interval`, timed against a fixed origin.

    Sleeping for the interval after each send would drift by however long the
    encode and the write took, which over a minute of audio is enough to be
    audible.
    """

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.origin = time.monotonic()
        self.count = 0

    async def wait(self) -> None:
        self.count += 1
        delay = self.origin + self.count * self.interval - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


class TalkbackSession:
    """One push-to-talk session with one camera.

    Holds its own socket and its own login: talkback works even when the video
    stream is down, and closing it cannot disturb the stream.
    """

    def __init__(
        self,
        ip: str,
        device_id: int,
        username: str,
        password: str,
        *,
        port: int = protocol.DEFAULT_PORT,
        cloud: bool = False,
        name: str = "",
    ) -> None:
        self.ip = ip
        self.port = port
        self.device_id = device_id
        self.username = username
        self.password = password
        self.cloud = cloud
        self.name = name or str(device_id)
        self.log = log.getChild(self.name)

        self.blocks_sent = 0
        self._writer: asyncio.StreamWriter | None = None
        self._encoder = AdpcmEncoder()
        self._key: bytes | None = None

    @property
    def active(self) -> bool:
        return self._writer is not None

    async def open(self) -> None:
        reply = await authenticate(
            self.ip, self.device_id, self.username, self.password,
            port=self.port, cloud=self.cloud,
        )
        self._key = protocol.media_key(reply.auth_ticket) if reply.encrypted else None

        _, writer = await asyncio.wait_for(
            asyncio.open_connection(self.ip, self.port), CONNECT_TIMEOUT
        )
        self._writer = writer
        writer.write(protocol.build_speak_handshake(self.device_id, reply.auth_ticket))
        await writer.drain()

        # The camera does not acknowledge; it either accepts the stream or
        # drops the socket. Give it a moment to do the latter before we start
        # pushing audio into a connection that is already gone.
        await asyncio.sleep(0.5)
        if writer.is_closing():
            self._writer = None
            raise TalkbackError(f"{self.name} refused the talkback handshake")

        self.log.info("talkback open")

    async def close(self) -> None:
        """Idempotent."""
        writer, self._writer = self._writer, None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
        self.log.info("talkback closed after %d blocks", self.blocks_sent)

    async def play(self, source: AsyncIterator[bytes]) -> int:
        """Stream PCM until the source ends or the session is cancelled.

        `source` yields 8 kHz mono 16-bit PCM in any chunk size; it is
        re-blocked here, because a codec block is 505 samples and a microphone
        will not hand them over in that shape.
        """
        if self._writer is None:
            raise TalkbackError("talkback session is not open")

        pacer = _Pacer(BLOCK_SECONDS)
        deadline = time.monotonic() + MAX_SESSION_SECONDS
        buffer = bytearray()

        async for chunk in source:
            buffer += chunk
            while len(buffer) >= PCM_BLOCK_BYTES:
                if time.monotonic() > deadline:
                    self.log.warning("talkback hit the %ss cap", MAX_SESSION_SECONDS)
                    return self.blocks_sent
                await self._send_block(bytes(buffer[:PCM_BLOCK_BYTES]))
                del buffer[:PCM_BLOCK_BYTES]
                await pacer.wait()

        if buffer:
            await self._send_block(bytes(buffer))
            await pacer.wait()

        for _ in range(TRAILING_SILENCE_BLOCKS):
            await self._send_block(bytes(PCM_BLOCK_BYTES))
            await pacer.wait()

        return self.blocks_sent

    async def _send_block(self, pcm: bytes) -> None:
        writer = self._writer
        if writer is None:
            raise TalkbackError("talkback session closed mid-stream")

        payload = self._encoder.encode_block(pcm)
        if self._key is not None:
            # Exactly 16 whole AES blocks, so nothing is left in the clear.
            payload = protocol.aes_ecb_encrypt(self._key, payload)

        writer.write(protocol.build_speak_header(self.blocks_sent) + payload)
        self.blocks_sent += 1
        try:
            await writer.drain()
        except (ConnectionError, OSError) as exc:
            raise TalkbackError(f"{self.name} closed the talkback socket") from exc

    async def __aenter__(self) -> TalkbackSession:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


# --- audio sources ---------------------------------------------------------


async def from_pcm(pcm: bytes, chunk: int = PCM_BLOCK_BYTES) -> AsyncIterator[bytes]:
    """An in-memory clip as a source."""
    for offset in range(0, len(pcm), chunk):
        yield pcm[offset : offset + chunk]


async def decode_to_pcm(data: bytes | Path) -> bytes:
    """Any audio ffmpeg understands, converted to 8 kHz mono 16-bit PCM.

    Accepts a path or the bytes of a file. Raises TalkbackError rather than
    returning silence, so a bad file is reported instead of played as nothing.
    """
    if shutil.which(FFMPEG) is None:
        raise TalkbackError("ffmpeg is not installed, so audio cannot be converted")

    source = str(data) if isinstance(data, Path) else "pipe:0"
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-i", source,
        "-ac", "1", "-ar", str(PCM_RATE), "-f", "s16le", "pipe:1",
        stdin=asyncio.subprocess.PIPE if source == "pipe:0" else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    pcm, err = await proc.communicate(data if source == "pipe:0" else None)
    if not pcm:
        detail = err.decode("utf-8", "replace").strip() or "no audio produced"
        raise TalkbackError(f"could not decode that audio: {detail}")
    return pcm


def piper_voice() -> str:
    """The Piper voice model to speak with, from the core voice settings."""
    try:
        from blackice.config import get_settings

        return get_settings().piper_voice
    except Exception:  # pragma: no cover - only if core config is unavailable
        return ""


async def synthesize(text: str) -> bytes:
    """Text to 8 kHz mono PCM, via the same Piper voice the assistant speaks in.

    Shells out to the `piper` CLI rather than reaching into voice2's engine:
    that engine only exists inside `blackice voice`, and the process that owns
    the cameras is usually `blackice serve`.
    """
    if not text.strip():
        raise TalkbackError("nothing to say")
    if shutil.which(PIPER) is None:
        raise TalkbackError(
            "the `piper` binary is not on PATH (uv pip install piper-tts)"
        )
    voice = piper_voice()
    if not voice or not Path(voice).exists():
        raise TalkbackError(
            "no Piper voice configured; set PIPER_VOICE to a .onnx voice file"
        )

    with tempfile.TemporaryDirectory(prefix="v380-tts-") as tmp:
        wav = Path(tmp) / "speech.wav"
        proc = await asyncio.create_subprocess_exec(
            PIPER, "-m", voice, "-f", str(wav),
            stdin=asyncio.subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate(text.encode("utf-8"))
        if not wav.exists() or wav.stat().st_size == 0:
            detail = err.decode("utf-8", "replace").strip() or "piper produced no audio"
            raise TalkbackError(f"speech synthesis failed: {detail}")
        # Piper writes at the voice's own rate; ffmpeg resamples to 8 kHz.
        return await decode_to_pcm(wav)


def microphone_available() -> bool:
    """Whether the microphone source can be used.

    Checked before a session starts: `microphone` is an async generator, so an
    import failure inside it would surface long after the caller was told the
    intercom had opened.
    """
    try:
        import sounddevice  # noqa: F401
    except Exception:
        return False
    return True


async def microphone(seconds: float) -> AsyncIterator[bytes]:
    """The local microphone as a source, for talking to someone at the door.

    Uses `sounddevice`, which Black Ice already depends on for voice. Capture
    happens on its own thread and is handed over through a queue, because the
    callback runs in PortAudio's thread and must never block.
    """
    try:
        import sounddevice
    except Exception as exc:  # pragma: no cover - depends on the host
        raise TalkbackError(
            "sounddevice is not available, so the microphone cannot be used "
            "(uv pip install 'blackice[voice]')"
        ) from exc

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)

    def on_audio(indata, frames, time_info, status) -> None:
        if status:
            log.debug("microphone status: %s", status)
        loop.call_soon_threadsafe(_offer, queue, bytes(indata))

    stream = sounddevice.RawInputStream(
        samplerate=PCM_RATE, channels=1, dtype="int16",
        blocksize=PCM_BLOCK_BYTES // 2, callback=on_audio,
    )
    deadline = loop.time() + min(seconds, MAX_SESSION_SECONDS)
    with stream:
        while loop.time() < deadline:
            try:
                yield await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue


def _offer(queue: asyncio.Queue[bytes], data: bytes) -> None:
    """Drop the oldest capture rather than let the microphone block."""
    try:
        queue.put_nowait(data)
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(data)
