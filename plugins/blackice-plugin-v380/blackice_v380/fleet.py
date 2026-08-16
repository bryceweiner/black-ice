"""Every V380 camera on the LAN, as one object.

The fleet owns discovery, one `V380Client` per camera, the snapshot cache, the
RTSP publication, and the transcoder those H.265 cameras need. It reports state
changes through a callback rather than emitting events itself, so it stays
testable without a running Black Ice.

The frame path is the point of the whole plugin. Every decoded frame goes to:

1. the RTSP stream, if anyone is watching (through the transcoder for H.265);
2. the snapshot cache, which keeps the latest picture for the dashboard;
3. any `FrameSubscription` — the buffered, drop-oldest hand-off that
   recognition models consume.

Subscribers get Annex B access units, not pictures. Decoding once per consumer
is wasteful, but decoding to an image *here* would be worse: a face recogniser
and a gait model want different resolutions, formats, and cadences, and a
recogniser that falls behind must never be able to stall the camera stream —
which is exactly what a drop-oldest queue guarantees and an inline callback
does not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from . import relay
from . import settings as settings_mod
from .client import AudioFrame, V380Client, VideoFrame
from .codec import Codec
from .discovery import DiscoveredCamera, discover
from .rtsp import RtspServer, RtspStream
from .settings import CameraConfig, Settings
from .snapshot import SnapshotCache, ffmpeg_available
from .talkback import TalkbackError, TalkbackSession
from .transcode import Transcoder

log = logging.getLogger("blackice.plugin.v380.fleet")

#: How long `snapshot(force=True)` waits for a camera that has just connected
#: to produce its first keyframe.
FIRST_KEYFRAME_WAIT = 12.0
#: Default depth of a subscriber's queue. Two frames is enough to absorb a
#: scheduling hiccup without letting a slow consumer watch stale video.
SUBSCRIPTION_DEPTH = 2


class CameraState(StrEnum):
    UNCONFIGURED = "unconfigured"
    CONNECTING = "connecting"
    ONLINE = "online"
    OFFLINE = "offline"
    ERROR = "error"


@dataclass(slots=True)
class StateChange:
    device_id: str
    label: str
    previous: CameraState
    current: CameraState
    detail: str = ""


StateListener = Callable[[StateChange], None]


class FrameSubscription:
    """A drop-oldest queue of frames from one camera.

    Async-iterable, so a recogniser is a `async for frame in sub:` loop. When
    the consumer is slower than the camera the *oldest* frame is discarded,
    which for live analysis is the right one to lose.
    """

    def __init__(
        self,
        camera: Camera,
        *,
        depth: int = SUBSCRIPTION_DEPTH,
        keyframes_only: bool = False,
    ) -> None:
        self.camera = camera
        self.keyframes_only = keyframes_only
        self.dropped = 0
        self._queue: asyncio.Queue[VideoFrame] = asyncio.Queue(maxsize=depth)
        self._closed = False

    def offer(self, frame: VideoFrame) -> None:
        if self._closed or (self.keyframes_only and not frame.keyframe):
            return
        while True:
            try:
                self._queue.put_nowait(frame)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
                self.dropped += 1

    async def get(self) -> VideoFrame:
        return await self._queue.get()

    def __aiter__(self) -> AsyncIterator[VideoFrame]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[VideoFrame]:
        while not self._closed:
            yield await self._queue.get()

    def close(self) -> None:
        self._closed = True
        self.camera.subscriptions.discard(self)

    def __enter__(self) -> FrameSubscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass(slots=True)
class Camera:
    """One camera: its configuration, its session, and everything derived."""

    config: CameraConfig
    state: CameraState = CameraState.UNCONFIGURED
    detail: str = ""
    first_seen: float = field(default_factory=time.time)
    last_seen: float = 0.0
    client: V380Client | None = None
    task: asyncio.Task | None = None
    snapshots: SnapshotCache = field(default_factory=SnapshotCache)
    subscriptions: set[FrameSubscription] = field(default_factory=set)
    stream: RtspStream | None = None
    transcoder: Transcoder | None = None
    codec: Codec = Codec.UNKNOWN
    #: The in-flight talkback session, if the speaker is currently in use.
    talkback: asyncio.Task | None = None
    #: Set when the camera rejected our credentials. Retrying a wrong password
    #: every discovery pass would hammer the device and can lock the account,
    #: so this camera is left alone until its configuration changes.
    fatal: bool = False
    transcode_task: asyncio.Task | None = None

    @property
    def device_id(self) -> str:
        return self.config.device_id

    @property
    def label(self) -> str:
        return self.config.display_name

    @property
    def online(self) -> bool:
        return self.state is CameraState.ONLINE

    def summary(self) -> dict:
        """The camera as the dashboard and the LLM see it.

        `label` can come from the config file, which the owner wrote, so it is
        plugin-authored rather than device-supplied. The device id and MAC do
        come from the camera and are only ever rendered as identifiers.
        """
        stats = self.client.stats if self.client else None
        return {
            "device_id": self.device_id,
            "label": self.label,
            "ip": self.config.ip,
            "state": str(self.state),
            "detail": self.detail,
            "codec": str(self.codec),
            "frames": stats.frames if stats else 0,
            "keyframes": stats.keyframes if stats else 0,
            "reconnects": stats.reconnects if stats else 0,
            "viewers": self.stream.viewers if self.stream else 0,
            "last_seen": self.last_seen,
            "has_snapshot": self.snapshots.current is not None,
            "speaking": self.talkback is not None and not self.talkback.done(),
        }


class Fleet:
    """Discovery, sessions, and frame distribution for every camera."""

    def __init__(
        self,
        config: Settings | None = None,
        *,
        on_state_change: StateListener | None = None,
    ) -> None:
        self.settings = config or settings_mod.load()
        self.cameras: dict[str, Camera] = {}
        self.on_state_change = on_state_change
        self.rtsp: RtspServer | None = None
        self.discovery_error = ""
        self._tasks: list[asyncio.Task] = []
        self._running = False

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Bring up the fleet. Returns quickly: cameras connect in the
        background, because `start()` runs under the supervisor's timeout and a
        camera that is merely slow must not fail the plugin."""
        self._running = True

        if self.settings.rtsp_enabled:
            self.rtsp = RtspServer(port=self.settings.rtsp_port)
            try:
                await self.rtsp.start()
            except OSError as exc:
                # A busy port should cost live view, not the whole plugin.
                log.warning("RTSP disabled: %s", exc)
                self.rtsp = None

        # Cameras named in the config file exist whether or not the broadcast
        # finds them, so they can be brought up before the first scan returns.
        for device_id in self.settings.configured_ids():
            self._ensure(self.settings.camera_for(device_id))

        self._tasks = [
            asyncio.create_task(self._snapshot_loop(), name="v380-snapshots"),
            asyncio.create_task(self._liveness_loop(), name="v380-liveness"),
        ]
        if self.settings.discovery_enabled:
            self._tasks.append(
                asyncio.create_task(self._discovery_loop(), name="v380-discovery")
            )
        self._sync_sessions()

    async def stop(self) -> None:
        """Idempotent: called on failure paths as well as on shutdown."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        for camera in self.cameras.values():
            await self._stop_camera(camera)

        if self.rtsp is not None:
            await self.rtsp.stop()
            self.rtsp = None

    # --- discovery ---------------------------------------------------------

    def reload_overrides(self) -> None:
        """Re-read the camera file, so editing it takes effect without a
        restart, and reconcile every camera against what it now says."""
        self.settings.overrides = settings_mod.reload_overrides(self.settings)
        for device_id in self.settings.configured_ids():
            self._ensure(self.settings.camera_for(device_id))
        for camera in self.cameras.values():
            self._apply_config(
                camera,
                self.settings.camera_for(
                    camera.device_id, ip=camera.config.ip, mac=camera.config.mac
                ),
            )

    def _apply_config(self, camera: Camera, config: CameraConfig) -> None:
        """Adopt a new configuration, restarting the session if it matters."""
        old = camera.config
        camera.config = config
        changed = (old.username, old.password, old.ip, old.port, old.cloud) != (
            config.username, config.password, config.ip, config.port, config.cloud
        )
        if not changed:
            return
        # New credentials or a new address deserve a fresh attempt even if the
        # last one was rejected outright.
        camera.fatal = False
        if camera.task is not None and not camera.task.done():
            camera.task.cancel()
            camera.task = None

    async def scan(self) -> list[DiscoveredCamera]:
        """Broadcast for cameras and reconcile the result into the fleet."""
        self.reload_overrides()
        if not self.settings.discovery_enabled:
            self.discovery_error = "LAN discovery is switched off"
            self._sync_sessions()
            return []
        try:
            found = await discover()
            self.discovery_error = ""
        except OSError as exc:
            self.discovery_error = str(exc)
            log.warning("discovery failed: %s", exc)
            return []

        for device in found:
            config = self.settings.camera_for(
                device.device_id, ip=device.ip, mac=device.mac
            )
            camera = self._ensure(config)
            # Re-merge: the camera may have moved, or the file may have changed.
            self._apply_config(camera, config)
            camera.last_seen = time.time()

        self._sync_sessions()
        return found

    async def _discovery_loop(self) -> None:
        while True:
            try:
                await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("discovery pass failed")
            await asyncio.sleep(self.settings.discovery_interval)

    # --- camera sessions ---------------------------------------------------

    def _ensure(self, config: CameraConfig) -> Camera:
        camera = self.cameras.get(config.device_id)
        if camera is None:
            camera = Camera(config=config)
            self.cameras[config.device_id] = camera
            log.info("camera %s discovered at %s", config.device_id, config.ip or "?")
        return camera

    def _sync_sessions(self) -> None:
        """Start a session for every configured camera, stop the rest."""
        for camera in self.cameras.values():
            if camera.config.configured:
                self._start_camera(camera)
            elif camera.task is None:
                self._set_state(
                    camera, CameraState.UNCONFIGURED, camera.config.reason_unconfigured
                )

    def _start_camera(self, camera: Camera) -> None:
        if camera.task is not None and not camera.task.done():
            return
        if camera.fatal:
            return

        self._set_state(camera, CameraState.CONNECTING, "")
        camera.task = asyncio.create_task(
            self._run_camera(camera), name=f"v380-{camera.device_id}"
        )

    async def _run_camera(self, camera: Camera) -> None:
        config = camera.config
        ip = config.ip
        if config.cloud:
            ip = await relay.find_relay(config.numeric_id, port=config.port) or ""
            if not ip:
                self._set_state(camera, CameraState.ERROR, "no relay server available")
                return

        client = V380Client(
            ip,
            config.numeric_id,
            config.username,
            config.password,
            port=config.port,
            cloud=config.cloud,
            quality=config.quality,
            name=config.display_name,
        )
        camera.client = client

        if self.rtsp is not None:
            camera.stream = self.rtsp.stream_for(camera.device_id)

        client.add_video_sink(lambda frame: self._on_video(camera, frame))
        client.add_audio_sink(lambda frame: self._on_audio(camera, frame))

        try:
            # run() only returns of its own accord when the credentials were
            # rejected; every other fault it retries internally.
            await client.run()
            camera.fatal = True
        finally:
            await self._teardown_media(camera)
            camera.client = None
            if client.stats.last_error:
                self._set_state(camera, CameraState.ERROR, client.stats.last_error)

    async def _stop_camera(self, camera: Camera) -> None:
        task, camera.task = camera.task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if camera.client is not None:
            await camera.client.close()
            camera.client = None
        await self._teardown_media(camera)

    async def _teardown_media(self, camera: Camera) -> None:
        for sub in list(camera.subscriptions):
            sub.close()
        task, camera.transcode_task = camera.transcode_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        transcoder, camera.transcoder = camera.transcoder, None
        if transcoder is not None:
            await transcoder.stop()
        # A talkback session outliving its camera would keep a speaker open in
        # someone's house with nothing driving it.
        talkback, camera.talkback = camera.talkback, None
        if talkback is not None and not talkback.done():
            talkback.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await talkback

    # --- frame distribution ------------------------------------------------

    def _on_video(self, camera: Camera, frame: VideoFrame) -> None:
        """Called on the receive path. Must stay cheap and must not block."""
        camera.last_seen = frame.received_at
        if frame.codec is not Codec.UNKNOWN:
            camera.codec = frame.codec
        if camera.state is not CameraState.ONLINE:
            self._set_state(camera, CameraState.ONLINE, "")

        for sub in list(camera.subscriptions):
            sub.offer(frame)

        self._publish(camera, frame)

    def _publish(self, camera: Camera, frame: VideoFrame) -> None:
        """Hand the frame to RTSP, transcoding first when it is H.265.

        Nothing runs unless someone is watching: the transcoder is the most
        expensive thing here and starting it for an idle stream would burn a
        core per camera for nobody.
        """
        stream = camera.stream
        if stream is None or not stream.sessions:
            return

        if frame.codec is not Codec.H265:
            stream.push_video(frame)
            return

        if camera.transcoder is None:
            camera.transcoder = Transcoder(
                lambda payload: stream.push_video(
                    VideoFrame(
                        payload=payload,
                        codec=Codec.H264,
                        keyframe=False,
                        frame_id=0,
                        timestamp=0,
                        frame_rate=frame.frame_rate,
                        received_at=time.time(),
                    )
                )
            )
            # Held on the camera: a bare create_task can be garbage collected
            # mid-flight, which would leave the transcoder half-started.
            camera.transcode_task = asyncio.create_task(
                camera.transcoder.start(), name=f"v380-xcode-{camera.device_id}"
            )
        camera.transcoder.push(frame.payload)

    def _on_audio(self, camera: Camera, frame: AudioFrame) -> None:
        if camera.stream is not None and camera.stream.sessions:
            camera.stream.push_audio(frame)

    def subscribe(
        self, device_id: str, *, keyframes_only: bool = False, depth: int = SUBSCRIPTION_DEPTH
    ) -> FrameSubscription:
        """A live feed of Annex B access units from one camera.

        This is the hook for recognition models. Close it when done, or use it
        as a context manager — an abandoned subscription keeps being fed.
        """
        camera = self.cameras.get(device_id)
        if camera is None:
            raise KeyError(device_id)
        sub = FrameSubscription(camera, depth=depth, keyframes_only=keyframes_only)
        camera.subscriptions.add(sub)
        return sub

    # --- snapshots ---------------------------------------------------------

    async def snapshot(self, device_id: str, *, force: bool = False):
        """The latest picture from a camera, decoding one if needed."""
        camera = self.cameras.get(device_id)
        if camera is None:
            raise KeyError(device_id)
        client = camera.client
        if client is None:
            return None

        if force and client.latest_keyframe is None:
            await client.wait_connected(FIRST_KEYFRAME_WAIT)
            await self._await_keyframe(client, FIRST_KEYFRAME_WAIT)

        return await camera.snapshots.capture(
            client.latest_keyframe, client.parameter_sets, force=force
        )

    @staticmethod
    async def _await_keyframe(client: V380Client, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while client.latest_keyframe is None and time.monotonic() < deadline:
            await asyncio.sleep(0.2)

    async def _snapshot_loop(self) -> None:
        """Keep every online camera's still reasonably fresh."""
        if not ffmpeg_available():
            log.warning("ffmpeg not found; snapshots are unavailable")
            return
        while True:
            await asyncio.sleep(self.settings.snapshot_interval)
            for camera in list(self.cameras.values()):
                if not camera.online or camera.client is None:
                    continue
                try:
                    await camera.snapshots.capture(
                        camera.client.latest_keyframe, camera.client.parameter_sets
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("snapshot failed for %s", camera.device_id)

    # --- liveness ----------------------------------------------------------

    async def _liveness_loop(self) -> None:
        """Notice a camera that stopped sending without closing the socket.

        A V380 that loses power leaves the TCP connection open on our side, so
        silence is the only signal that it is gone.
        """
        while True:
            await asyncio.sleep(min(self.settings.offline_after / 3, 15.0))
            cutoff = time.time() - self.settings.offline_after
            for camera in self.cameras.values():
                if camera.state is CameraState.ONLINE and camera.last_seen < cutoff:
                    self._set_state(
                        camera, CameraState.OFFLINE,
                        f"no frames for {self.settings.offline_after:.0f}s",
                    )

    # --- talkback ----------------------------------------------------------

    def speaking(self, device_id: str) -> bool:
        camera = self.cameras.get(device_id)
        return bool(camera and camera.talkback is not None and not camera.talkback.done())

    async def speak(self, device_id: str, source: AsyncIterator[bytes]) -> None:
        """Play PCM through a camera's speaker, in the background.

        Starting a second one interrupts the first: a camera has one speaker,
        and two overlaid streams are noise. Returns as soon as the session is
        open, so the caller is not blocked for the length of the audio.
        """
        camera = self.cameras.get(device_id)
        if camera is None:
            raise KeyError(device_id)
        if not camera.config.configured:
            raise TalkbackError(
                f"{camera.label} has no usable credentials: "
                f"{camera.config.reason_unconfigured}"
            )

        await self.stop_speaking(device_id)
        session = TalkbackSession(
            camera.config.ip,
            camera.config.numeric_id,
            camera.config.username,
            camera.config.password,
            port=camera.config.port,
            cloud=camera.config.cloud,
            name=camera.label,
        )
        # Opened here rather than inside the task so that a camera that refuses
        # talkback is reported to the caller instead of failing silently later.
        await session.open()
        camera.talkback = asyncio.create_task(
            self._run_talkback(camera, session, source), name=f"v380-speak-{device_id}"
        )

    async def _run_talkback(
        self, camera: Camera, session: TalkbackSession, source: AsyncIterator[bytes]
    ) -> None:
        try:
            await session.play(source)
        except asyncio.CancelledError:
            raise
        except TalkbackError as exc:
            log.warning("talkback to %s ended: %s", camera.label, exc)
        except Exception:
            log.exception("talkback to %s failed", camera.label)
        finally:
            await session.close()

    async def stop_speaking(self, device_id: str) -> bool:
        """Cut a talkback session short. False if nothing was playing."""
        camera = self.cameras.get(device_id)
        if camera is None:
            raise KeyError(device_id)
        task, camera.talkback = camera.talkback, None
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return True

    # --- control -----------------------------------------------------------

    async def control(self, device_id: str, command: str) -> bool:
        camera = self.cameras.get(device_id)
        if camera is None:
            raise KeyError(device_id)
        if camera.client is None:
            return False
        return await camera.client.send_control(command)

    # --- state -------------------------------------------------------------

    def _set_state(self, camera: Camera, state: CameraState, detail: str) -> None:
        if camera.state is state and camera.detail == detail:
            return
        change = StateChange(
            device_id=camera.device_id,
            label=camera.label,
            previous=camera.state,
            current=state,
            detail=detail,
        )
        camera.state = state
        camera.detail = detail
        log.info("camera %s -> %s %s", camera.device_id, state, detail)

        if self.on_state_change is None:
            return
        try:
            self.on_state_change(change)
        except Exception:
            log.exception("state listener failed")

    def summaries(self) -> list[dict]:
        return [c.summary() for c in sorted(self.cameras.values(), key=lambda c: c.device_id)]

    def rtsp_url(self, device_id: str, host: str = "localhost") -> str | None:
        if self.rtsp is None:
            return None
        return f"rtsp://{host}:{self.rtsp.port}/{device_id}"
