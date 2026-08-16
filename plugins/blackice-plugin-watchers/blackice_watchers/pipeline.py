"""One worker per camera, and the supervisor that keeps them matched to the
fleet.

The fleet handle has three properties this file is built around, because a
consumer that assumes any of them away is a consumer that works on a good day:

* it may be **absent** — in the process that did not take the camera lock,
  `active_fleet()` is None for the whole run, and the right behaviour is to do
  no recognition at all and say so;
* it may be **late**, because entry-point order does not guarantee the V380
  plugin starts first;
* it may be **replaced**, when the supervisor restarts that plugin.

So this does not hold a fleet it was handed once. It registers with
`on_fleet_change`, records whatever it is given, and a reconcile loop makes the
running workers match it — which also covers cameras appearing later through
discovery, since that is the same problem.

Nothing here blocks the event loop. Decoding and inference are both blocking C
calls, so they run in a thread pool; the loop only ever awaits their result.
And the worker never grows a backlog of its own: the fleet's subscription queue
is drop-oldest by design, and while a frame is being analysed the queue is
doing exactly the job it was built for. What we do keep is the count, because
"I am dropping frames" is a state the owner is entitled to see.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .decode import AnnexBDecoder, crop, encode_jpeg
from .recognition import PersonSighting, Recognizer
from .settings import Settings
from .tracks import Decision, Resolver, TrackRecord

log = logging.getLogger("blackice.plugin.watchers.pipeline")

#: How often the fleet and the running workers are reconciled. Cameras appear
#: on a discovery interval measured in minutes, so this can be lazy.
RECONCILE_INTERVAL = 2.0
#: How often finished tracks are retired.
SWEEP_INTERVAL = 1.0
#: Window over which the achieved frame rate is measured.
FPS_WINDOW = 10.0
#: Threads for decode and inference. Small on purpose: these are C calls that
#: release the GIL and saturate cores, and a pool the size of the machine would
#: starve everything else Black Ice is doing.
DEFAULT_WORKERS = 4

DecisionSink = Callable[[Decision], Awaitable[None]]
ExpirySink = Callable[[list[TrackRecord]], Awaitable[None]]


@dataclass
class CameraStats:
    """What the performance widget and `recognition_status` report."""

    device_id: str
    camera: str = ""
    received: int = 0
    analysed: int = 0
    #: Frames the fleet threw away because we were not reading fast enough.
    dropped: int = 0
    #: Frames we chose not to decode: either the analysis rate cap, or a frame
    #: that arrived while the previous one was still in the models.
    skipped: int = 0
    #: Access units the decoder could not turn into a picture.
    decode_errors: int = 0
    #: Frames the models threw on. Counted apart from decode errors because
    #: they are different faults with different fixes.
    analysis_errors: int = 0
    people_seen: int = 0
    started_at: float = field(default_factory=time.time)
    last_frame_at: float = 0.0
    recent: deque = field(default_factory=lambda: deque(maxlen=256))

    def note_analysis(self, ts: float) -> None:
        self.analysed += 1
        self.last_frame_at = ts
        self.recent.append(ts)

    def fps(self, now: float | None = None) -> float:
        """Analysed frames per second over the last window, not since boot —
        an average since start would hide a camera that stopped an hour ago."""
        now = now if now is not None else time.time()
        cutoff = now - FPS_WINDOW
        while self.recent and self.recent[0] < cutoff:
            self.recent.popleft()
        return round(len(self.recent) / FPS_WINDOW, 2)

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "camera": self.camera or self.device_id,
            "fps": self.fps(now),
            "analysed": self.analysed,
            "received": self.received,
            "dropped": self.dropped,
            "skipped": self.skipped,
            "decode_errors": self.decode_errors,
            "analysis_errors": self.analysis_errors,
            "people_seen": self.people_seen,
            "last_frame_at": self.last_frame_at,
        }


class CameraWorker:
    """Reads one camera's frames, decodes them, and resolves who is in them."""

    def __init__(
        self,
        device_id: str,
        camera: str,
        fleet: Any,
        recognizer: Recognizer,
        resolver: Resolver,
        executor: ThreadPoolExecutor,
        settings: Settings,
        sink: DecisionSink,
    ) -> None:
        self.device_id = device_id
        self.camera = camera
        self.fleet = fleet
        self.recognizer = recognizer
        self.resolver = resolver
        self.executor = executor
        self.settings = settings
        self.sink = sink
        self.stats = CameraStats(device_id=device_id, camera=camera)
        self.decoder = AnnexBDecoder()
        self._interval = 1.0 / max(0.1, settings.analyse_fps)
        self._last_analysis = 0.0
        self._dropped_seen = 0

    async def run(self) -> None:
        try:
            subscription = self.fleet.subscribe(self.device_id, keyframes_only=False, depth=2)
        except KeyError:
            log.info("camera %s went away before we could subscribe", self.device_id)
            return
        log.info("watching %s", self.camera or self.device_id)
        try:
            with subscription as feed:
                async for frame in feed:
                    await self._handle(frame, feed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker for %s stopped", self.device_id)
        finally:
            self.recognizer.forget(self.device_id)
            self.resolver.forget_camera(self.device_id)

    async def _handle(self, frame: Any, feed: Any) -> None:
        self.stats.received += 1

        # The fleet counts what it threw away; a gap means the decoder is
        # missing references, so it must wait for the next keyframe rather than
        # produce a smeared picture for a face model to be confident about.
        if feed.dropped != self._dropped_seen:
            self.stats.dropped += feed.dropped - self._dropped_seen
            self._dropped_seen = feed.dropped
            self.decoder.note_gap()

        now = time.time()
        if now - self._last_analysis < self._interval:
            self.stats.skipped += 1
            self.decoder.note_gap()
            return
        self._last_analysis = now

        codec = str(getattr(frame, "codec", "h264"))
        if codec not in ("unknown", self.decoder.codec_name) and codec in ("h264", "h265"):
            self.decoder.reset(codec)

        loop = asyncio.get_running_loop()
        try:
            results = await loop.run_in_executor(
                self.executor, self._decode_and_analyse, frame.payload,
                bool(getattr(frame, "keyframe", False)),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("analysis failed for %s", self.device_id)
            self.stats.analysis_errors += 1
            return

        self.stats.decode_errors = self.decoder.errors
        if results is None:
            return
        self.stats.note_analysis(now)

        ts = float(getattr(frame, "received_at", now))
        for sighting, jpeg, area in results:
            self.stats.people_seen += 1
            decisions = self.resolver.observe(
                self.device_id, self.camera, sighting, ts=ts, crop=jpeg, crop_area=area
            )
            for decision in decisions:
                await self.sink(decision)

    def _decode_and_analyse(
        self, payload: bytes, keyframe: bool
    ) -> list[tuple[PersonSighting, bytes, int]] | None:
        """Runs on a worker thread. Never touches the event loop or the DB."""
        images = self.decoder.decode(payload, keyframe=keyframe)
        if not images:
            return None
        image = images[-1]
        sightings = self.recognizer.analyse(image, self.device_id)
        results = []
        for sighting in sightings:
            patch = crop(image, sighting.box)
            area = int(patch.shape[0] * patch.shape[1]) if patch.size else 0
            jpeg = encode_jpeg(patch) if area else b""
            results.append((sighting, jpeg, area))
        return results


class Pipeline:
    """Keeps a worker running for every camera the current fleet has."""

    def __init__(
        self,
        settings: Settings,
        resolver: Resolver,
        recognizer: Recognizer,
        sink: DecisionSink,
        expiry_sink: ExpirySink,
    ) -> None:
        self.settings = settings
        self.resolver = resolver
        self.recognizer = recognizer
        self.sink = sink
        self.expiry_sink = expiry_sink
        self.fleet: Any | None = None
        self.enabled = True
        self.workers: dict[str, tuple[CameraWorker, asyncio.Task]] = {}
        self.retired: dict[str, CameraStats] = {}
        self._executor: ThreadPoolExecutor | None = None
        self._tasks: list[asyncio.Task] = []
        self._unsubscribe: Callable[[], None] | None = None
        self._running = False

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Returns immediately. Everything real happens in the loops below, so
        that a camera that is merely slow cannot fail the plugin's `start()`."""
        self._running = True
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, self.settings.workers or DEFAULT_WORKERS),
            thread_name_prefix="watchers",
        )
        self._tasks = [
            asyncio.create_task(self._reconcile_loop(), name="watchers-reconcile"),
            asyncio.create_task(self._sweep_loop(), name="watchers-sweep"),
        ]
        self._unsubscribe = self._attach_to_fleet_changes()

    def _attach_to_fleet_changes(self) -> Callable[[], None] | None:
        """Register for the live fleet, if the V380 plugin is installed at all.

        The import is here rather than at module scope so that this plugin
        loads, starts, and serves its widgets on a machine where the camera
        plugin is not installed.
        """
        try:
            from blackice_v380 import on_fleet_change
        except Exception:
            log.info("the V380 plugin is not installed; no cameras to watch")
            return None
        return on_fleet_change(self._fleet_changed)

    def _fleet_changed(self, fleet: Any | None) -> None:
        """Called by the V380 plugin, possibly before we have any workers and
        possibly to hand over a replacement. Records and returns: the reconcile
        loop does the work, so this can never block the publisher."""
        if fleet is not self.fleet:
            log.info("fleet %s", "attached" if fleet is not None else "withdrawn")
        self.fleet = fleet

    async def stop(self) -> None:
        self._running = False
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        await self._stop_workers(set(self.workers))
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    # --- reconciliation ----------------------------------------------------

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile pass failed")
            await asyncio.sleep(RECONCILE_INTERVAL)

    async def reconcile(self) -> None:
        """Make the running workers match the current fleet.

        This is the whole answer to no-fleet, late-fleet, and replaced-fleet:
        each is just a difference between what is running and what should be.
        """
        wanted: dict[str, str] = {}
        if self._running and self.enabled and self.fleet is not None:
            for device_id, camera in getattr(self.fleet, "cameras", {}).items():
                if getattr(camera, "online", False):
                    wanted[device_id] = getattr(camera, "label", device_id)

        stale = {
            device_id for device_id, (worker, task) in self.workers.items()
            if device_id not in wanted or task.done() or worker.fleet is not self.fleet
        }
        await self._stop_workers(stale)

        for device_id, label in wanted.items():
            if device_id in self.workers:
                continue
            worker = CameraWorker(
                device_id, label, self.fleet, self.recognizer, self.resolver,
                self._executor, self.settings, self.sink,
            )
            self.workers[device_id] = (
                worker,
                asyncio.create_task(worker.run(), name=f"watchers-{device_id}"),
            )

    async def set_recognizer(self, recognizer: Recognizer) -> None:
        """Swap the models in. Running workers hold the old one, so they are
        stopped; the reconcile loop rebuilds them around the new one."""
        self.recognizer = recognizer
        await self._stop_workers(set(self.workers))

    async def _stop_workers(self, device_ids: set[str]) -> None:
        for device_id in device_ids:
            worker, task = self.workers.pop(device_id)
            # Keep the counters: a camera that went offline having dropped
            # thousands of frames is exactly what the owner wants to see.
            self.retired[device_id] = worker.stats
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL)
            try:
                # Unconditional, even with nothing to retire: this tick is also
                # what notices a camera that has gone blind, and a blind camera
                # is precisely one that produces no tracks to expire.
                await self.expiry_sink(self.resolver.sweep(time.time()))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sweeping finished tracks failed")

    # --- reporting ---------------------------------------------------------

    def stats(self) -> list[dict[str, Any]]:
        now = time.time()
        live = [worker.stats.as_dict(now) for worker, _ in self.workers.values()]
        seen = {row["device_id"] for row in live}
        idle = [
            stats.as_dict(now) for device_id, stats in self.retired.items()
            if device_id not in seen
        ]
        return sorted(live + idle, key=lambda row: row["camera"])

    def struggling(self) -> list[dict[str, Any]]:
        """Cameras whose achieved rate is below the floor while frames are
        still arriving — the models are not keeping up, rather than the camera
        having gone quiet."""
        now = time.time()
        return [
            row for worker, _ in self.workers.values()
            if (row := worker.stats.as_dict(now))["fps"] < self.settings.fps_floor
            and row["received"] > 0
            and now - worker.stats.started_at > FPS_WINDOW
        ]

    def state(self) -> str:
        if not self.enabled:
            return "offline"
        if self.fleet is None:
            return "offline"
        if not self.workers:
            return "degraded"
        return "degraded" if self.struggling() else "healthy"


def largest_person(sightings: list[PersonSighting]) -> PersonSighting | None:
    """The most prominent person in a frame, for enrolling from a snapshot."""
    if not sightings:
        return None
    return max(
        sightings,
        key=lambda s: (s.box[2] - s.box[0]) * (s.box[3] - s.box[1]),
    )
