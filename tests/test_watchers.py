"""The watchers plugin: recognising people on the V380 cameras.

Nothing here downloads a model or touches a camera. Three things stand in for
the world:

* `StubRecognizer` for the models, because what is worth testing is the wiring
  around them — tracking, evidence, debouncing, event shape — and that is
  exactly what a real model would make untestable.
* real Annex B, encoded here by PyAV, for the decoder. The bitstream is
  genuine H.264 and H.265; only the picture is synthetic.
* `FakeFleet`, which hands out the *real* `FrameSubscription` from the V380
  plugin, so the drop-oldest behaviour under test is the shipped behaviour and
  not a re-implementation of it.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from datetime import datetime

import av
import numpy as np
import pytest
from blackice_v380 import fleet as v380_fleet
from blackice_v380.client import VideoFrame
from blackice_v380.codec import Codec
from blackice_v380.fleet import Camera, CameraState, FrameSubscription
from blackice_v380.settings import CameraConfig
from blackice_watchers import (
    LINGERING,
    PEOPLE_SENSOR,
    PIPELINE_SENSOR,
    REVISION,
    Gallery,
    NullRecognizer,
    PersonSighting,
    Resolver,
    Settings,
    WatchersPlugin,
    models,
    severity_for,
)
from blackice_watchers import settings as watcher_settings
from blackice_watchers.decode import AnnexBDecoder, crop, encode_jpeg
from blackice_watchers.embeddings import normalise, similarity

from blackice import db
from blackice.llm.tools import ToolRegistry, project_plugin_tools
from blackice.models import (
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
)
from blackice.plugins.registry import Registry
from blackice.services import events

DEVICE = "95886601"
LABEL = "Front door"
OTHER_DEVICE = "95886602"
OTHER_LABEL = "Drive"


# --- synthetic material ----------------------------------------------------

def vec(index: int, dim: int = 16) -> np.ndarray:
    """A basis vector, so similarity between two of them is exactly 1 or 0.

    Random vectors would leave the threshold assertions at the mercy of the
    seed; these make "the same person" and "somebody else" unambiguous.
    """
    out = np.zeros(dim, dtype=np.float32)
    out[index % dim] = 1.0
    return out


def blend(a: np.ndarray, b: np.ndarray, towards_b: float) -> np.ndarray:
    return normalise((1 - towards_b) * a + towards_b * b)


def pictures(count: int = 4, width: int = 160, height: int = 128) -> list[np.ndarray]:
    """A pale rectangle moving across a dark field: enough real detail for an
    encoder to produce a plausible bitstream."""
    frames = []
    for i in range(count):
        image = np.zeros((height, width, 3), dtype=np.uint8)
        image[:, :, 1] = 40
        image[30:90, 10 + i * 12 : 60 + i * 12] = (200, 180, 170)
        frames.append(image)
    return frames


def encode_annexb(images: list[np.ndarray], codec: str = "h264") -> list[bytes]:
    """Real Annex B access units, one per picture.

    All-intra with repeated headers, so every unit is self-contained and the
    test can drop any of them without the decoder losing references — which is
    the point, since dropping frames is the behaviour being exercised.
    """
    container_format, encoder = (
        ("h264", "libx264") if codec == "h264" else ("hevc", "libx265")
    )
    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format=container_format) as container:
        stream = container.add_stream(encoder, rate=15)
        stream.width, stream.height = images[0].shape[1], images[0].shape[0]
        stream.pix_fmt = "yuv420p"
        stream.options = (
            {"x264-params": "keyint=1:repeat-headers=1", "tune": "zerolatency"}
            if encoder == "libx264"
            else {"x265-params": "keyint=1:repeat-headers=1:log-level=none"}
        )
        for image in images:
            container.mux(stream.encode(av.VideoFrame.from_ndarray(image, format="rgb24")))
        container.mux(stream.encode(None))
    return _split(buffer.getvalue(), codec)


def _split(data: bytes, codec: str) -> list[bytes]:
    # Each picture starts with its parameter set: SPS (type 7) for H.264, VPS
    # (type 32, so a 0x40 header byte) for HEVC.
    marker = b"\x00\x00\x00\x01\x67" if codec == "h264" else b"\x00\x00\x00\x01\x40"
    starts, at = [], data.find(marker)
    while at != -1:
        starts.append(at)
        at = data.find(marker, at + 1)
    return [data[a:b] for a, b in zip(starts, starts[1:] + [len(data)], strict=True)]


def video_frame(payload: bytes, *, codec: str = "h264", keyframe: bool = True) -> VideoFrame:
    return VideoFrame(
        payload=payload,
        codec=Codec.H264 if codec == "h264" else Codec.H265,
        keyframe=keyframe,
        frame_id=1,
        timestamp=0,
        frame_rate=15,
        received_at=time.time(),
    )


def sighting(track_id: int, *, face=None, body=None, box=(10, 30, 60, 90),
             face_pixels: int = 80) -> PersonSighting:
    return PersonSighting(
        track_id=track_id, box=box, score=0.9, face=face, body=body,
        face_pixels=face_pixels if face is not None else 0,
    )


class StubRecognizer:
    """Stands in for YOLO + ByteTrack + ArcFace + OSNet.

    Given a list of per-call results, or a callable, it returns them in order.
    The pipeline is written against the `Recognizer` protocol precisely so this
    can exist.
    """

    def __init__(self, script=None) -> None:
        self.script = script
        self.calls = 0
        self.streams: list[str] = []
        self.forgotten: list[str] = []
        self.images: list[np.ndarray] = []

    def analyse(self, image, stream):
        self.calls += 1
        self.streams.append(stream)
        self.images.append(image)
        if self.script is None:
            return []
        if callable(self.script):
            return self.script(image, stream, self.calls)
        if not self.script:
            return []
        return self.script[min(self.calls - 1, len(self.script) - 1)]

    def forget(self, stream):
        self.forgotten.append(stream)

    def info(self):
        return {"loaded": True, "device": "stub"}


class FakeFleet:
    """The V380 fleet's shape, with real subscriptions.

    `subscribe` hands back the shipped `FrameSubscription`, so the drop-oldest
    queue the plugin is written around is the actual one — a hand-rolled queue
    here would be testing the test.
    """

    def __init__(self, cameras: dict[str, str], *, snapshot_jpeg: bytes | None = None) -> None:
        self.cameras: dict[str, Camera] = {}
        for device_id, label in cameras.items():
            camera = Camera(
                config=CameraConfig(device_id=device_id, label=label, ip="127.0.0.1")
            )
            camera.state = CameraState.ONLINE
            self.cameras[device_id] = camera
        self.snapshot_jpeg = snapshot_jpeg
        self.snapshot_calls: list[str] = []

    def subscribe(self, device_id, *, keyframes_only=False, depth=2):
        camera = self.cameras[device_id]  # KeyError, like the real one
        sub = FrameSubscription(camera, depth=depth, keyframes_only=keyframes_only)
        camera.subscriptions.add(sub)
        return sub

    def push(self, device_id: str, frame: VideoFrame) -> None:
        for sub in list(self.cameras[device_id].subscriptions):
            sub.offer(frame)

    async def snapshot(self, device_id, *, force=False):
        self.snapshot_calls.append(device_id)
        if device_id not in self.cameras:
            raise KeyError(device_id)
        if self.snapshot_jpeg is None:
            return None

        class Shot:
            jpeg = self.snapshot_jpeg
            captured_at = time.time()

        return Shot()


async def until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


# --- fixtures --------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_fleet_handle():
    """The published fleet is process-global; never let it leak between tests."""
    yield
    v380_fleet.publish_fleet(None)


def night_window(*, covering_now: bool) -> tuple[str, str]:
    """A night window that either contains the current hour or misses it.

    Severity and the wording of a summary both depend on the time of day, so a
    test that does not pin the window passes all afternoon and fails after ten
    at night. Pinning it against `now` rather than to fixed hours keeps that
    true in every timezone the suite is run in.
    """
    hour = datetime.now().astimezone().hour
    if covering_now:
        return str(hour), str((hour + 1) % 24)
    return str((hour + 2) % 24), str((hour + 3) % 24)


@pytest.fixture
def watcher_env(monkeypatch):
    # No analysis-rate cap and a two-frame bar, so the tests drive the state
    # machine rather than the wall clock.
    monkeypatch.setenv(watcher_settings.ENV_ANALYSE_FPS, "1000")
    monkeypatch.setenv(watcher_settings.ENV_MIN_FRAMES, "2")
    monkeypatch.setenv(watcher_settings.ENV_LINGER_SECONDS, "60")
    # Daytime, whenever the suite happens to run. The night case is exercised
    # deliberately, by `test_an_unknown_person_at_night_is_worth_more`.
    start, end = night_window(covering_now=False)
    monkeypatch.setenv(watcher_settings.ENV_NIGHT_START, start)
    monkeypatch.setenv(watcher_settings.ENV_NIGHT_END, end)


@pytest.fixture
async def reg(data_dir, watcher_env):
    r = Registry()
    await r.start_plugin(WatchersPlugin, events.record)
    yield r
    await r.stop_all()


def plugin_of(reg) -> WatchersPlugin:
    return reg.supervisors["watchers"].plugin


def healthy(reg) -> bool:
    return reg.supervisors["watchers"].health()["state"] == "healthy"


async def timeline(kind: str | None = None):
    if kind:
        return await db.fetchall(
            "SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,)
        )
    return await db.fetchall(
        "SELECT * FROM events WHERE sensor_id LIKE 'watchers.%' ORDER BY id"
    )


async def wait_events(kind: str, count: int = 1, timeout: float = 5.0):
    """Events reach the database from the worker task, so a test that counts
    them has to wait for the task rather than for the frame."""
    deadline = time.monotonic() + timeout
    rows = await timeline(kind)
    while len(rows) < count and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
        rows = await timeline(kind)
    return rows


async def attach(plugin: WatchersPlugin, fleet, recognizer=None):
    """Give the plugin a fleet and a recognizer, and settle the workers.

    `reconcile` only creates the worker tasks; a frame pushed before one has
    actually subscribed would vanish without even being counted as dropped, so
    this waits for the subscriptions to exist.
    """
    if recognizer is not None:
        plugin.recognizer = recognizer
        await plugin.pipeline.set_recognizer(recognizer)
    v380_fleet.publish_fleet(fleet)
    await plugin.pipeline.reconcile()
    await until(
        lambda: all(
            fleet.cameras[device_id].subscriptions for device_id in plugin.pipeline.workers
        )
    )


async def feed(plugin, fleet, device: str, units: list[bytes], *, codec: str = "h264"):
    """Push access units one at a time, waiting for each to be analysed.

    One at a time on purpose: the subscription is two deep and drop-oldest, so
    firing them all at once would test the queue rather than the pipeline.
    """
    worker = plugin.pipeline.workers[device][0]
    for unit in units:
        before = worker.stats.analysed
        fleet.push(device, video_frame(unit, codec=codec))
        await until(lambda b=before: worker.stats.analysed > b)


# --- discovery and projection ----------------------------------------------

async def test_discovery_finds_the_installed_plugin(data_dir):
    assert "watchers" in [c.name for c in Registry().discover()]


async def test_start_projects_both_sensors(reg):
    ids = [r["id"] for r in await db.fetchall("SELECT id FROM sensors ORDER BY id")]
    assert PEOPLE_SENSOR in ids
    assert PIPELINE_SENSOR in ids


async def test_start_projects_the_alarm_rules(reg):
    rows = await db.fetchall(
        "SELECT r.key, r.sensor_id, s.armed FROM alarm_rules r"
        " JOIN alarm_state s ON s.rule_id = r.id WHERE r.plugin = 'watchers'"
    )
    rules = {r["key"]: r for r in rows}
    assert set(rules) == {"unknown_at_night", "unknown_lingering", "recognition_degraded"}
    # All three were agreed as armed by default.
    assert all(r["armed"] for r in rules.values())
    assert rules["recognition_degraded"]["sensor_id"] == PIPELINE_SENSOR


async def test_tools_reach_the_llm_under_the_plugin_name(reg):
    into = ToolRegistry()
    project_plugin_tools(reg, into)
    names = set(into.tools)
    assert {
        "watchers.list_enrolled",
        "watchers.enrol_person",
        "watchers.forget_person",
        "watchers.rename_person",
        "watchers.who_was_seen",
        "watchers.where_has_person_been",
        "watchers.recognition_status",
        "watchers.set_thresholds",
    } <= names
    # Downloading model weights is the owner's decision, made with the button,
    # not something the assistant can trigger.
    assert "watchers.provision_models" not in names


async def test_every_tool_description_says_what_it_is_for(reg):
    for descriptor in plugin_of(reg).describe():
        for tool in descriptor.tools:
            assert len(tool.description) > 80, tool.name
            assert tool.parameters["type"] == "object"


# --- widgets ---------------------------------------------------------------

async def test_every_declared_widget_data_source_answers(reg):
    plugin = plugin_of(reg)
    sources = [
        widget.data_source
        for descriptor in plugin.describe()
        for widget in descriptor.widgets
        if widget.data_source
    ]
    assert len(sources) == 9
    for source in sources:
        result = await reg.query("watchers", source)
        assert result is not None, source
    assert healthy(reg)


async def test_the_action_widget_names_the_command_it_runs(reg):
    data = await reg.query("watchers", "model_status")
    assert data["command"] == "provision_models"
    assert data["state"] in ("missing", "blocked", "busy", "ready")
    assert data["confirm"]
    assert data["label"]


async def test_pipeline_status_is_offline_without_a_fleet(reg):
    assert (await reg.query("watchers", "pipeline_status"))["state"] == "offline"
    assert (await reg.query("watchers", "throughput"))["label"] == (
        "no cameras in this process"
    )


async def test_an_unknown_widget_source_does_not_break_the_plugin(reg):
    from blackice.plugins.supervisor import PluginFailure

    with pytest.raises(PluginFailure):
        await reg.query("watchers", "no_such_source")


# --- the fleet handle ------------------------------------------------------

async def test_no_fleet_means_no_workers_and_no_recognition(reg):
    """The normal state in the process that did not take the camera lock."""
    plugin = plugin_of(reg)
    await plugin.pipeline.reconcile()

    assert plugin.pipeline.fleet is None
    assert plugin.pipeline.workers == {}
    assert plugin.pipeline.state() == "offline"
    status = await reg.command("watchers", "recognition_status")
    assert status["has_cameras_in_this_process"] is False
    assert healthy(reg)


async def test_a_late_fleet_is_picked_up(reg):
    """Entry-point order may start this plugin first; it must not have given up."""
    plugin = plugin_of(reg)
    await plugin.pipeline.reconcile()
    assert plugin.pipeline.workers == {}

    await attach(plugin, FakeFleet({DEVICE: LABEL}), StubRecognizer())

    assert set(plugin.pipeline.workers) == {DEVICE}
    assert (await reg.command("watchers", "recognition_status"))[
        "has_cameras_in_this_process"
    ] is True


async def test_a_replaced_fleet_is_taken_over(reg):
    """A supervisor restart of the V380 plugin builds a new Fleet. A consumer
    still holding the old one would go quiet without ever failing."""
    plugin = plugin_of(reg)
    first = FakeFleet({DEVICE: LABEL})
    await attach(plugin, first, StubRecognizer())
    first_worker = plugin.pipeline.workers[DEVICE][0]

    second = FakeFleet({DEVICE: LABEL})
    await attach(plugin, second)

    worker = plugin.pipeline.workers[DEVICE][0]
    assert worker is not first_worker
    assert worker.fleet is second
    # And frames from the new fleet actually arrive.
    await feed(plugin, second, DEVICE, encode_annexb(pictures(1)))
    assert worker.stats.analysed == 1
    assert healthy(reg)


async def test_a_withdrawn_fleet_stops_the_workers(reg):
    plugin = plugin_of(reg)
    await attach(plugin, FakeFleet({DEVICE: LABEL}), StubRecognizer())
    assert plugin.pipeline.workers

    v380_fleet.publish_fleet(None)
    await plugin.pipeline.reconcile()

    assert plugin.pipeline.workers == {}
    assert plugin.pipeline.state() == "offline"
    assert healthy(reg)


async def test_an_offline_camera_is_not_watched(reg):
    plugin = plugin_of(reg)
    fleet = FakeFleet({DEVICE: LABEL, OTHER_DEVICE: OTHER_LABEL})
    fleet.cameras[OTHER_DEVICE].state = CameraState.OFFLINE
    await attach(plugin, fleet, StubRecognizer())

    assert set(plugin.pipeline.workers) == {DEVICE}


# --- decoding --------------------------------------------------------------

@pytest.mark.parametrize("codec", ["h264", "h265"])
def test_access_units_decode_to_pictures(codec):
    """The 3-lens cameras emit HEVC, so both paths are the shipped path."""
    units = encode_annexb(pictures(3), codec)
    decoder = AnnexBDecoder(codec)

    images = [image for unit in units for image in decoder.decode(unit, keyframe=True)]

    assert len(images) == 3
    assert images[0].shape == (128, 160, 3)
    assert decoder.errors == 0


def test_a_decoder_that_missed_frames_waits_for_a_keyframe():
    """A gap means missing references. A smeared picture fed to a face model is
    worse than no picture, so nothing is decoded until the stream restarts."""
    units = encode_annexb(pictures(2))
    decoder = AnnexBDecoder("h264")
    decoder.decode(units[0], keyframe=True)

    decoder.note_gap()
    assert decoder.decode(units[1], keyframe=False) == []
    assert len(decoder.decode(units[1], keyframe=True)) == 1


def test_garbage_costs_a_frame_and_not_the_worker():
    decoder = AnnexBDecoder("h264")
    assert decoder.decode(b"\x00\x00\x00\x01\x67not-a-stream", keyframe=True) == []


def test_a_crop_is_clamped_to_the_picture_and_encodes_as_jpeg():
    image = pictures(1)[0]
    patch = crop(image, (-50, -50, 10_000, 10_000))

    assert patch.shape[:2] == image.shape[:2]
    assert encode_jpeg(patch).startswith(b"\xff\xd8\xff")
    assert encode_jpeg(crop(image, (10, 10, 10, 10))) == b""


# --- the resolver ----------------------------------------------------------

class FakeGallery:
    """The `match` half of a Gallery, without a database."""

    def __init__(self, people: dict[int, tuple[str, np.ndarray, np.ndarray | None]]) -> None:
        self.people = people
        self.names = {pid: name for pid, (name, _, _) in people.items()}

    def match(self, modality, probe):
        best, score = None, 0.0
        for person_id, (name, face, body) in self.people.items():
            reference = face if modality == "face" else body
            if reference is None:
                continue
            value = similarity(probe, reference)
            if value > score:
                best, score = (person_id, name), value
        return (best[0], best[1], score) if best else None


def resolver(**overrides) -> Resolver:
    settings = Settings()
    settings.thresholds.min_frames = overrides.pop("min_frames", 2)
    for key, value in overrides.items():
        setattr(settings, key, value)
    gallery = FakeGallery({1: ("Jane", vec(0), vec(5)), 2: ("Sam", vec(1), vec(6))})
    return Resolver(settings, gallery)


def test_one_frame_is_never_enough():
    r = resolver(min_frames=3)
    for frame in range(2):
        decisions = r.observe(DEVICE, LABEL, sighting(1, face=vec(0)), ts=100 + frame)
        assert decisions == []


def test_a_track_is_emitted_once_not_every_frame():
    r = resolver(min_frames=2)
    emitted = []
    for frame in range(8):
        emitted += r.observe(DEVICE, LABEL, sighting(1, face=vec(0)), ts=100 + frame)

    assert len(emitted) == 1
    assert emitted[0].identity.name == "Jane"
    assert emitted[0].identity.modality == "face"
    assert emitted[0].identity.confidence == pytest.approx(1.0)


def test_a_track_nobody_matches_resolves_as_unknown():
    r = resolver(min_frames=2)
    emitted = []
    for frame in range(4):
        emitted += r.observe(DEVICE, LABEL, sighting(1, face=vec(9), body=vec(10)),
                             ts=100 + frame)

    assert len(emitted) == 1
    assert emitted[0].identity.known is False
    assert emitted[0].identity.name is None


def test_both_modalities_still_count_as_one_frame():
    """Face and body agreeing in one frame is one piece of evidence, not two —
    otherwise a two-frame track would clear a three-frame bar."""
    r = resolver(min_frames=3)
    decisions = []
    for frame in range(2):
        decisions += r.observe(
            DEVICE, LABEL, sighting(1, face=vec(0), body=vec(5)), ts=100 + frame
        )
    assert decisions == []

    decisions += r.observe(DEVICE, LABEL, sighting(1, face=vec(0), body=vec(5)), ts=102)
    assert len(decisions) == 1
    assert decisions[0].identity.modality == "both"


def test_a_revised_identity_re_emits_once():
    """A ReID guess corrected by a face two seconds later is a genuine change
    of mind, and the only thing that re-opens a resolved track."""
    r = resolver(min_frames=2)
    emitted = []
    for frame in range(2):
        emitted += r.observe(DEVICE, LABEL, sighting(1, body=vec(5)), ts=100 + frame)
    assert emitted[0].identity.name == "Jane"

    for frame in range(2, 6):
        emitted += r.observe(DEVICE, LABEL, sighting(1, face=vec(1)), ts=100 + frame)

    assert len(emitted) == 2
    assert emitted[1].kind == REVISION
    assert emitted[1].identity.name == "Sam"


def test_re_confirming_the_same_person_does_not_re_emit():
    r = resolver(min_frames=2)
    emitted = []
    for frame in range(20):
        emitted += r.observe(DEVICE, LABEL, sighting(1, face=vec(0), body=vec(5)),
                             ts=100 + frame)
    assert len(emitted) == 1


def test_an_unknown_who_stays_is_reported_once_more():
    r = resolver(min_frames=2, linger_seconds=30)
    emitted = []
    for frame in range(3):
        emitted += r.observe(DEVICE, LABEL, sighting(1, face=vec(9)), ts=100 + frame)
    assert len(emitted) == 1

    emitted += r.observe(DEVICE, LABEL, sighting(1, face=vec(9)), ts=140)
    assert len(emitted) == 2
    assert emitted[1].kind == LINGERING

    emitted += r.observe(DEVICE, LABEL, sighting(1, face=vec(9)), ts=200)
    assert len(emitted) == 2


def test_a_known_person_who_stays_is_not_reported_as_lingering():
    r = resolver(min_frames=2, linger_seconds=10)
    emitted = []
    for ts in (100, 101, 150, 200):
        emitted += r.observe(DEVICE, LABEL, sighting(1, face=vec(0)), ts=ts)
    assert [d.kind for d in emitted] == ["recognition"]


def test_a_re_acquired_person_is_a_new_track_and_emits_again():
    r = resolver(min_frames=2)
    emitted = []
    for frame in range(2):
        emitted += r.observe(DEVICE, LABEL, sighting(7, face=vec(0)), ts=100 + frame)
    r.sweep(200)

    for frame in range(2):
        emitted += r.observe(DEVICE, LABEL, sighting(8, face=vec(0)), ts=200 + frame)

    assert len(emitted) == 2
    assert [d.track.track_id for d in emitted] == [7, 8]


def test_a_faceless_track_inherits_the_identity_from_another_camera():
    """The person who was at the drive is now at the door."""
    r = resolver(min_frames=2)
    body = vec(11)
    for frame in range(2):
        r.observe(OTHER_DEVICE, OTHER_LABEL, sighting(1, face=vec(0), body=body),
                  ts=100 + frame)
    r.sweep(120)

    emitted = []
    for frame in range(2):
        emitted += r.observe(DEVICE, LABEL, sighting(1, body=body), ts=130 + frame)

    assert len(emitted) == 1
    assert emitted[0].identity.name == "Jane"
    assert emitted[0].identity.modality == "reid"
    assert emitted[0].track.linked_to == f"{OTHER_DEVICE}:1"


def test_two_unknown_tracks_on_different_cameras_are_still_linked():
    r = resolver(min_frames=2)
    body = vec(12)
    for frame in range(2):
        r.observe(OTHER_DEVICE, OTHER_LABEL, sighting(1, body=body), ts=100 + frame)
    r.sweep(120)

    emitted = []
    for frame in range(2):
        emitted += r.observe(DEVICE, LABEL, sighting(1, body=body), ts=130 + frame)

    assert emitted[0].identity.known is False
    assert emitted[0].track.linked_to == f"{OTHER_DEVICE}:1"


def test_a_stale_track_is_not_used_for_linking():
    r = resolver(min_frames=2, recent_window=60)
    body = vec(13)
    for frame in range(2):
        r.observe(OTHER_DEVICE, OTHER_LABEL, sighting(1, face=vec(0), body=body),
                  ts=100 + frame)
    r.sweep(120)

    emitted = []
    for frame in range(2):
        emitted += r.observe(DEVICE, LABEL, sighting(1, body=body), ts=1000 + frame)

    assert emitted[0].identity.known is False
    assert emitted[0].track.linked_to == ""


def test_a_near_miss_below_the_threshold_is_reported_but_not_believed():
    r = resolver(min_frames=2)
    r.settings.thresholds.face = 0.9
    emitted = []
    for frame in range(3):
        emitted += r.observe(
            DEVICE, LABEL, sighting(1, face=blend(vec(0), vec(3), 0.5)), ts=100 + frame
        )

    assert emitted[0].identity.known is False
    assert emitted[0].closest_name == "Jane"
    assert 0.6 < emitted[0].closest_score < 0.9


def test_finished_tracks_are_swept_and_kept_for_linking():
    r = resolver(min_frames=2)
    for frame in range(2):
        r.observe(DEVICE, LABEL, sighting(1, face=vec(0), body=vec(5)), ts=100 + frame)

    expired = r.sweep(200)

    assert [t.key for t in expired] == [f"{DEVICE}:1"]
    assert r.tracks == {}
    assert len(r.recent) == 1


# --- severity --------------------------------------------------------------

@pytest.mark.parametrize(
    ("known", "night", "lingering", "new_camera", "expected"),
    [
        (True, False, False, False, SEVERITY_INFO),
        (True, True, False, False, SEVERITY_INFO),
        (True, False, False, True, SEVERITY_LOW),
        (False, False, False, False, SEVERITY_LOW),
        (False, True, False, False, SEVERITY_MEDIUM),
        (False, False, True, False, SEVERITY_MEDIUM),
        (False, True, True, False, SEVERITY_HIGH),
    ],
)
def test_the_severity_scheme(known, night, lingering, new_camera, expected):
    assert severity_for(
        known=known, night=night, lingering=lingering, new_camera=new_camera
    ) == expected


@pytest.mark.parametrize(
    ("hour", "night"),
    [(23, True), (0, True), (5, True), (6, False), (12, False), (21, False), (22, True)],
)
def test_the_night_window_wraps_midnight(hour, night):
    assert Settings(night_start=22, night_end=6).is_night(hour) is night


# --- recognition end to end, with the models stubbed ------------------------

async def enrol_directly(plugin, name, face=None, body=None):
    vectors = []
    if face is not None:
        vectors.append(("face", face))
    if body is not None:
        vectors.append(("body", body))
    return await plugin.gallery.enrol(name, vectors, source="test")


async def test_a_recognised_track_becomes_one_event_with_a_crop(reg):
    plugin = plugin_of(reg)
    await enrol_directly(plugin, "Jane", face=vec(0))
    # Jane has been seen on this camera before, so this is the ordinary case
    # rather than the first-time-there one.
    await plugin.gallery.record_sighting(
        ts=time.time() - 3600, device_id=DEVICE, camera=LABEL, track_id=1,
        person_id=plugin.gallery.id_of("Jane"), identity="Jane",
    )
    recognizer = StubRecognizer(lambda image, stream, call: [sighting(4, face=vec(0))])
    fleet = FakeFleet({DEVICE: LABEL})
    await attach(plugin, fleet, recognizer)

    await feed(plugin, fleet, DEVICE, encode_annexb(pictures(4)))
    rows = await wait_events("recognition")

    assert len(rows) == 1
    event = rows[0]
    assert event["sensor_id"] == PEOPLE_SENSOR
    assert event["severity"] == SEVERITY_INFO
    assert event["summary"] == "Recognised Jane at Front door"
    # Everything the network supplied, and nothing a model wrote.
    assert event["sensor_text"] == f"camera device id {DEVICE}"

    payload = json.loads(event["payload"])
    assert payload["identity"] == "Jane"
    assert payload["known"] is True
    assert payload["modality"] == "face"
    assert payload["track"] == f"{DEVICE}:4"
    assert payload["confidence"] == pytest.approx(1.0)

    # The crop is attached as core media, which is what the existing retention
    # prunes — the plugin keeps no copy of its own.
    media = await db.fetchall(
        "SELECT path, mime FROM event_media WHERE event_id = ?", (event["id"],)
    )
    assert len(media) == 1
    assert media[0]["mime"] == "image/jpeg"
    written = watcher_settings.media_root() / media[0]["path"]
    assert written.is_file() and written.read_bytes().startswith(b"\xff\xd8\xff")


async def test_an_unknown_person_is_reported_and_named_generically(reg):
    plugin = plugin_of(reg)
    recognizer = StubRecognizer(lambda image, stream, call: [sighting(1, face=vec(9))])
    fleet = FakeFleet({DEVICE: LABEL})
    await attach(plugin, fleet, recognizer)

    await feed(plugin, fleet, DEVICE, encode_annexb(pictures(3)))
    rows = await wait_events("recognition")

    assert len(rows) == 1
    assert rows[0]["summary"] == "Unrecognised person at Front door"
    assert rows[0]["severity"] == SEVERITY_LOW
    payload = json.loads(rows[0]["payload"])
    assert payload["identity"] is None
    assert payload["night"] is False


async def test_an_unlabelled_camera_is_described_not_named(reg):
    """A camera with no owner-written label falls back to its device id, which
    came off the network — so it must not be composed into `summary`."""
    plugin = plugin_of(reg)
    recognizer = StubRecognizer(lambda image, stream, call: [sighting(1, face=vec(9))])
    fleet = FakeFleet({DEVICE: ""})
    await attach(plugin, fleet, recognizer)

    await feed(plugin, fleet, DEVICE, encode_annexb(pictures(3)))
    rows = await wait_events("recognition")

    assert rows[0]["summary"] == "Unrecognised person on an unlabelled camera"
    assert DEVICE not in rows[0]["summary"]
    assert DEVICE in rows[0]["sensor_text"]


async def test_a_known_person_on_a_new_camera_is_worth_more(reg):
    plugin = plugin_of(reg)
    await enrol_directly(plugin, "Jane", face=vec(0))
    recognizer = StubRecognizer(lambda image, stream, call: [sighting(1, face=vec(0))])
    fleet = FakeFleet({DEVICE: LABEL})
    await attach(plugin, fleet, recognizer)

    await feed(plugin, fleet, DEVICE, encode_annexb(pictures(3)))
    rows = await wait_events("recognition")

    assert rows[0]["severity"] == SEVERITY_LOW
    assert rows[0]["summary"].endswith("the first time there")
    assert json.loads(rows[0]["payload"])["new_camera_for_person"] is True


async def test_the_same_person_walking_past_does_not_flood_the_timeline(reg):
    plugin = plugin_of(reg)
    await enrol_directly(plugin, "Jane", face=vec(0))
    recognizer = StubRecognizer(lambda image, stream, call: [sighting(2, face=vec(0))])
    fleet = FakeFleet({DEVICE: LABEL})
    await attach(plugin, fleet, recognizer)

    await feed(plugin, fleet, DEVICE, encode_annexb(pictures(4)) * 5)

    assert len(await wait_events("recognition")) == 1
    assert plugin.pipeline.workers[DEVICE][0].stats.analysed == 20


async def test_dropped_frames_are_counted_rather_than_queued(reg):
    """The fleet queue is drop-oldest and this plugin keeps no backlog of its
    own; falling behind must cost frames, and be visible."""
    plugin = plugin_of(reg)
    fleet = FakeFleet({DEVICE: LABEL})
    await attach(plugin, fleet, StubRecognizer())
    worker = plugin.pipeline.workers[DEVICE][0]
    units = encode_annexb(pictures(4))

    # Nothing is consuming yet: the two-deep queue keeps the newest and the
    # count of what it threw away.
    for _ in range(6):
        for unit in units:
            fleet.push(DEVICE, video_frame(unit))
    await until(lambda: worker.stats.dropped > 0)

    assert worker.stats.dropped > 0
    row = next(r for r in plugin.pipeline.stats() if r["device_id"] == DEVICE)
    assert row["dropped"] == worker.stats.dropped
    assert healthy(reg)


async def test_two_cameras_get_a_worker_each_and_separate_track_ids(reg):
    plugin = plugin_of(reg)
    recognizer = StubRecognizer(lambda image, stream, call: [sighting(1, face=vec(9))])
    fleet = FakeFleet({DEVICE: LABEL, OTHER_DEVICE: OTHER_LABEL})
    await attach(plugin, fleet, recognizer)

    units = encode_annexb(pictures(3))
    await feed(plugin, fleet, DEVICE, units)
    await feed(plugin, fleet, OTHER_DEVICE, units)

    rows = await wait_events("recognition", 2)
    tracks = {json.loads(r["payload"])["track"] for r in rows}
    assert tracks == {f"{DEVICE}:1", f"{OTHER_DEVICE}:1"}
    assert set(recognizer.streams) == {DEVICE, OTHER_DEVICE}


async def test_a_hevc_camera_is_decoded_too(reg):
    plugin = plugin_of(reg)
    recognizer = StubRecognizer(lambda image, stream, call: [sighting(1, face=vec(9))])
    fleet = FakeFleet({DEVICE: LABEL})
    await attach(plugin, fleet, recognizer)

    await feed(plugin, fleet, DEVICE, encode_annexb(pictures(3), "h265"), codec="h265")

    assert plugin.pipeline.workers[DEVICE][0].stats.analysed == 3
    assert len(await wait_events("recognition")) == 1


async def test_a_recognizer_that_explodes_costs_a_frame_not_the_plugin(reg):
    def explode(image, stream, call):
        raise RuntimeError("model blew up")

    plugin = plugin_of(reg)
    fleet = FakeFleet({DEVICE: LABEL})
    await attach(plugin, fleet, StubRecognizer(explode))
    worker = plugin.pipeline.workers[DEVICE][0]

    fleet.push(DEVICE, video_frame(encode_annexb(pictures(1))[0]))
    await until(lambda: worker.stats.analysis_errors > 0)

    # A model fault is counted apart from a decode fault: different problem,
    # different fix, and the widget says which.
    assert worker.stats.analysis_errors == 1
    assert worker.stats.decode_errors == 0
    assert healthy(reg)
    assert plugin.pipeline.workers[DEVICE][1].done() is False


async def test_no_models_means_no_recognition_and_a_healthy_plugin(reg):
    plugin = plugin_of(reg)
    plugin.recognizer = NullRecognizer("no weights here")
    plugin.model_reason = "no weights here"
    fleet = FakeFleet({DEVICE: LABEL})
    await attach(plugin, fleet, plugin.recognizer)

    await feed(plugin, fleet, DEVICE, encode_annexb(pictures(3)))

    assert await timeline("recognition") == []
    status = await reg.command("watchers", "recognition_status")
    assert status["models_loaded"] is False
    assert status["reason"] == "no weights here"
    assert healthy(reg)


# --- tools -----------------------------------------------------------------

async def test_list_enrolled_starts_empty_and_says_where_photos_go(reg):
    result = await reg.command("watchers", "list_enrolled")
    assert result == {
        "people": [],
        "count": 0,
        "enrolment_folder": str(watcher_settings.enrol_dir()),
    }


async def test_enrol_from_a_recent_track(reg):
    plugin = plugin_of(reg)
    await plugin.gallery.remember_track(
        key=f"{DEVICE}:5", device_id=DEVICE, camera=LABEL, track_id=5,
        ts=time.time(), face=vec(0), body=vec(5),
    )

    result = await reg.command("watchers", "enrol_person", name="Jane",
                               track=f"{DEVICE}:5")

    assert result["ok"] is True
    assert result["learned"] == {"face": 1, "body": 1}
    listed = await reg.command("watchers", "list_enrolled")
    assert listed["people"][0]["name"] == "Jane"
    assert listed["people"][0]["face_samples"] == 1
    # And it goes on the timeline, because enrolling someone is a real act.
    assert (await timeline("enrolment"))[0]["summary"].startswith("Enrolled Jane")


async def test_enrol_from_files_reads_the_enrolment_folder(reg, tmp_path):
    plugin = plugin_of(reg)
    plugin.recognizer = StubRecognizer(
        lambda image, stream, call: [sighting(1, face=vec(0), body=vec(5))]
    )
    folder = watcher_settings.enrol_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("jane-1.jpg", "jane-2.jpg"):
        (folder / name).write_bytes(encode_jpeg(pictures(1)[0]))

    result = await reg.command("watchers", "enrol_person", name="Jane",
                               files=["jane-1.jpg", "jane-2.jpg"])

    assert result["ok"] is True
    # Two photographs collapse into one prototype per modality, not four rows.
    assert result["learned"] == {"face": 1, "body": 1}
    assert plugin.recognizer.calls == 2


async def test_enrol_from_files_refuses_to_leave_the_folder(reg):
    plugin = plugin_of(reg)
    plugin.recognizer = StubRecognizer(lambda i, s, c: [sighting(1, face=vec(0))])
    watcher_settings.enrol_dir().mkdir(parents=True, exist_ok=True)

    result = await reg.command("watchers", "enrol_person", name="Jane",
                               files=["../../../etc/passwd"])

    assert "not in the enrolment folder" in result["error"]
    assert healthy(reg)


async def test_enrol_from_a_camera_snapshot(reg):
    plugin = plugin_of(reg)
    recognizer = StubRecognizer(
        lambda image, stream, call: [sighting(1, face=vec(0), body=vec(5))]
    )
    fleet = FakeFleet({DEVICE: LABEL}, snapshot_jpeg=encode_jpeg(pictures(1)[0]))
    await attach(plugin, fleet, recognizer)

    result = await reg.command("watchers", "enrol_person", name="Jane", camera=LABEL)

    assert result["ok"] is True
    assert fleet.snapshot_calls == [DEVICE]
    assert plugin.gallery.id_of("Jane") is not None


async def test_enrolling_the_same_name_twice_adds_rather_than_replaces(reg):
    plugin = plugin_of(reg)
    await enrol_directly(plugin, "Jane", face=vec(0))
    await enrol_directly(plugin, "Jane", face=vec(2))

    rows = await plugin.ctx.db.execute_fetchall(
        "SELECT count(*) FROM embeddings WHERE modality = 'face'"
    )
    assert rows[0][0] == 2
    assert len(await plugin.gallery.people()) == 1


async def test_forget_deletes_the_descriptors_not_just_the_name(reg):
    plugin = plugin_of(reg)
    await enrol_directly(plugin, "Jane", face=vec(0), body=vec(5))
    await plugin.gallery.record_sighting(
        ts=time.time(), device_id=DEVICE, camera=LABEL, track_id=1,
        person_id=plugin.gallery.id_of("Jane"), identity="Jane", confidence=0.9,
    )

    result = await reg.command("watchers", "forget_person", name="Jane")

    assert result["ok"] is True
    assert result["descriptors_deleted"] == 2
    left = await plugin.ctx.db.execute_fetchall("SELECT count(*) FROM embeddings")
    assert left[0][0] == 0
    people = await plugin.ctx.db.execute_fetchall("SELECT count(*) FROM people")
    assert people[0][0] == 0
    # The history survives, anonymised: something was seen, but not who.
    rows = await plugin.ctx.db.execute_fetchall("SELECT identity FROM sightings")
    assert [r[0] for r in rows] == [None]
    assert plugin.gallery.match("face", vec(0)) is None


async def test_a_forgotten_person_is_unknown_again(reg):
    plugin = plugin_of(reg)
    await enrol_directly(plugin, "Jane", face=vec(0))
    await reg.command("watchers", "forget_person", name="Jane")
    recognizer = StubRecognizer(lambda i, s, c: [sighting(1, face=vec(0))])
    fleet = FakeFleet({DEVICE: LABEL})
    await attach(plugin, fleet, recognizer)

    await feed(plugin, fleet, DEVICE, encode_annexb(pictures(3)))

    assert (await wait_events("recognition"))[0]["summary"] == (
        "Unrecognised person at Front door"
    )


async def test_rename_keeps_what_was_learned(reg):
    plugin = plugin_of(reg)
    await enrol_directly(plugin, "jane", face=vec(0))

    result = await reg.command("watchers", "rename_person", name="jane",
                               new_name="Jane Doe")

    assert result == {"ok": True, "was": "jane", "now": "Jane Doe"}
    assert plugin.gallery.match("face", vec(0))[1] == "Jane Doe"


async def test_who_was_seen_reports_one_camera_newest_first(reg):
    plugin = plugin_of(reg)
    now = time.time()
    for offset, identity in ((300, "Jane"), (100, None)):
        await plugin.gallery.record_sighting(
            ts=now - offset, device_id=DEVICE, camera=LABEL, track_id=1,
            identity=identity, confidence=0.8, modality="face",
        )
    await plugin.gallery.record_sighting(
        ts=now - 50, device_id=OTHER_DEVICE, camera=OTHER_LABEL, track_id=2,
        identity="Sam", confidence=0.7, modality="reid",
    )
    await attach(plugin, FakeFleet({DEVICE: LABEL, OTHER_DEVICE: OTHER_LABEL}))

    result = await reg.command("watchers", "who_was_seen", camera=LABEL)

    assert result["camera"] == LABEL
    assert [s["who"] for s in result["sightings"]] == ["unknown person", "Jane"]
    everywhere = await reg.command("watchers", "who_was_seen")
    assert everywhere["count"] == 3


async def test_where_has_person_been_follows_one_person(reg):
    plugin = plugin_of(reg)
    await enrol_directly(plugin, "Jane", face=vec(0))
    person_id = plugin.gallery.id_of("Jane")
    now = time.time()
    for offset, device, label in ((200, OTHER_DEVICE, OTHER_LABEL), (60, DEVICE, LABEL)):
        await plugin.gallery.record_sighting(
            ts=now - offset, device_id=device, camera=label, track_id=1,
            person_id=person_id, identity="Jane", confidence=0.9, modality="face",
        )

    result = await reg.command("watchers", "where_has_person_been", name="Jane")

    assert [step["camera"] for step in result["trail"]] == [LABEL, OTHER_LABEL]
    assert result["count"] == 2
    assert result["last_seen"]


async def test_recognition_status_explains_what_to_do_next(reg):
    result = await reg.command("watchers", "recognition_status")

    assert result["recognising"] is False
    assert result["models_loaded"] is False
    assert result["thresholds"] == {"face": 0.55, "reid": 0.7, "min_frames": 2}
    assert "install" in result["next_step"].lower() or "download" in result[
        "next_step"
    ].lower()
    assert [m["state"] for m in result["model_files"]] == ["missing"] * 3


async def test_set_thresholds_takes_effect_and_survives_a_restart(reg, data_dir):
    result = await reg.command("watchers", "set_thresholds", face=0.7, min_frames=5)

    assert result["thresholds"] == {"face": 0.7, "reid": 0.7, "min_frames": 5}
    assert plugin_of(reg).settings.thresholds.face == 0.7

    await reg.stop_all()
    again = Registry()
    await again.start_plugin(WatchersPlugin, events.record)
    try:
        assert plugin_of(again).settings.thresholds.face == 0.7
        assert plugin_of(again).settings.thresholds.min_frames == 5
    finally:
        await again.stop_all()


# --- bad input is data, not a fault ----------------------------------------

@pytest.mark.parametrize(
    ("cmd", "kwargs", "fragment"),
    [
        ("enrol_person", {}, "name"),
        ("enrol_person", {"name": "Jane"}, "nothing to learn from"),
        ("enrol_person", {"name": "Jane", "track": "nonsense"}, "not a track key"),
        ("enrol_person", {"name": "Jane", "track": "1:2"}, "no track"),
        ("enrol_person", {"name": "Jane", "track": "1:2", "camera": "x"}, "exactly one"),
        ("enrol_person", {"name": "  ", "files": ["a.jpg"]}, "name"),
        ("forget_person", {}, "who should be forgotten"),
        ("forget_person", {"name": "Nobody"}, "nobody called"),
        ("rename_person", {"name": "Nobody", "new_name": "Someone"}, "nobody called"),
        ("rename_person", {"name": "x"}, "new_name"),
        ("where_has_person_been", {"name": "Ghost"}, "nobody called"),
        ("where_has_person_been", {"name": "Ghost", "hours": "soon"}, "nobody called"),
        ("who_was_seen", {"hours": "lots"}, "not a number"),
        ("who_was_seen", {"camera": "Nowhere"}, "no camera called"),
        ("set_thresholds", {"face": 4}, "between 0 and 1"),
        ("set_thresholds", {"face": "high"}, "not a number"),
        ("set_thresholds", {"min_frames": 0}, "between 1 and 60"),
        ("set_thresholds", {"min_frames": "three"}, "not a whole number"),
    ],
)
async def test_bad_input_is_returned_as_data_and_leaves_the_plugin_healthy(
    reg, cmd, kwargs, fragment
):
    plugin = plugin_of(reg)
    await attach(plugin, FakeFleet({DEVICE: LABEL}), StubRecognizer())

    result = await reg.command("watchers", cmd, **kwargs)

    assert "error" in result, result
    assert fragment in result["error"]
    assert healthy(reg)


async def test_enrolling_from_a_camera_without_a_fleet_is_refused_cleanly(reg):
    plugin = plugin_of(reg)
    plugin.recognizer = StubRecognizer(lambda i, s, c: [sighting(1, face=vec(0))])

    result = await reg.command("watchers", "enrol_person", name="Jane", camera=LABEL)

    assert "another Black Ice process" in result["error"]
    assert healthy(reg)


async def test_enrolling_without_models_says_so_rather_than_failing(reg):
    plugin = plugin_of(reg)
    plugin.model_reason = "the models have not been downloaded"
    await attach(plugin, FakeFleet({DEVICE: LABEL}), NullRecognizer("no models"))
    plugin.recognizer = NullRecognizer("no models")

    result = await reg.command("watchers", "enrol_person", name="Jane", camera=LABEL)

    assert "recognition is not available" in result["error"]
    assert healthy(reg)


async def test_an_unknown_command_is_a_plugin_failure_not_a_silent_none(reg):
    from blackice.plugins.supervisor import PluginFailure

    with pytest.raises(PluginFailure):
        await reg.command("watchers", "no_such_tool")


# --- privacy ---------------------------------------------------------------

async def test_the_database_holds_descriptors_and_never_images(reg):
    plugin = plugin_of(reg)
    await enrol_directly(plugin, "Jane", face=vec(0), body=vec(5))

    tables = await plugin.ctx.db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    assert {t[0] for t in tables} == {
        "people", "embeddings", "sightings", "recent_tracks", "prefs"
    }
    rows = await plugin.ctx.db.execute_fetchall("SELECT vec, dim FROM embeddings")
    for blob, dim in rows:
        assert len(blob) == dim * 4  # float32, and nothing else
        assert not blob.startswith(b"\xff\xd8\xff")


async def test_the_recent_track_cache_expires(reg):
    plugin = plugin_of(reg)
    old = time.time() - 7200
    await plugin.gallery.remember_track(key="a:1", device_id="a", track_id=1,
                                        ts=old, face=vec(0))
    await plugin.gallery.remember_track(key="b:1", device_id="b", track_id=1,
                                        ts=time.time(), face=vec(1))

    remaining = await plugin.ctx.db.execute_fetchall(
        "SELECT track_key FROM recent_tracks"
    )
    assert [r[0] for r in remaining] == ["b:1"]


# --- provisioning ----------------------------------------------------------

def test_status_reports_every_model_as_missing_on_a_fresh_machine(tmp_path):
    report = models.status(tmp_path)

    assert report["ready"] is False
    assert [m["state"] for m in report["models"]] == ["missing"] * 3
    assert {m["key"] for m in report["models"]} == {"detector", "face", "reid"}


def test_provisioning_records_a_hash_and_then_verifies_it(tmp_path, monkeypatch):
    def fake_download(url, target, progress):
        target.write_bytes(b"weights for " + target.name.encode())

    monkeypatch.setattr(models, "_download", fake_download)
    monkeypatch.setattr(models, "_unpack", lambda spec, root, progress: None)

    first = models.provision(tmp_path)
    assert first["ok"] is True
    assert sorted(first["fetched"]) == ["detector", "face", "reid"]
    assert models.status(tmp_path)["ready"] is True

    lock = json.loads((tmp_path / models.LOCK_NAME).read_text())
    assert len(lock["detector"]["sha256"]) == 64

    # A second run downloads nothing and still verifies.
    monkeypatch.setattr(models, "_download", lambda *a: pytest.fail("re-downloaded"))
    second = models.provision(tmp_path)
    assert second["fetched"] == []
    assert sorted(second["kept"]) == ["detector", "face", "reid"]


def test_a_model_that_changed_underneath_us_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(
        models, "_download", lambda url, target, progress: target.write_bytes(b"original")
    )
    monkeypatch.setattr(models, "_unpack", lambda spec, root, progress: None)
    models.provision(tmp_path)

    (tmp_path / "yolo11n.pt").write_bytes(b"something else entirely")

    report = models.status(tmp_path)
    assert next(m for m in report["models"] if m["key"] == "detector")["state"] == (
        "mismatched"
    )
    assert report["ready"] is False
    assert any("does not match" in reason for reason in models.missing_pieces(tmp_path))


def test_a_present_but_unlocked_model_is_adopted_and_hashed(tmp_path, monkeypatch):
    (tmp_path / "yolo11n.pt").write_bytes(b"dropped in by hand")
    monkeypatch.setattr(models, "_download", lambda *a: pytest.fail("re-downloaded"))

    result = models.provision(tmp_path, only="detector")

    assert result["kept"] == ["detector"]
    assert next(
        m for m in models.status(tmp_path)["models"] if m["key"] == "detector"
    )["state"] == "ok"


def test_an_archive_that_tries_to_escape_the_model_directory_is_refused(tmp_path):
    archive = tmp_path / "buffalo_l.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../escaped.txt", "no")

    with pytest.raises(ValueError, match="unsafe path"):
        models._unpack(models.MODELS_BY_KEY["face"], tmp_path, lambda m: None)


def test_provisioning_an_unknown_model_is_an_error_not_a_crash(tmp_path):
    assert "no model called" in models.provision(tmp_path, only="nonsense")["error"]


def test_a_download_that_fails_is_reported_and_the_rest_continue(tmp_path, monkeypatch):
    def flaky(url, target, progress):
        if "yolo" in target.name:
            raise OSError("connection reset")
        target.write_bytes(b"ok")

    monkeypatch.setattr(models, "_download", flaky)
    monkeypatch.setattr(models, "_unpack", lambda spec, root, progress: None)

    result = models.provision(tmp_path)

    assert result["ok"] is False
    assert sorted(result["fetched"]) == ["face", "reid"]
    assert "connection reset" in result["failed"][0]


def test_nothing_is_loaded_when_the_libraries_are_absent(tmp_path):
    stack, reason = models.load_stack(Settings(), tmp_path)
    assert stack is None
    assert reason


async def test_the_button_starts_a_download_without_blocking(reg, monkeypatch):
    calls = []

    def fake_provision(root=None, **kwargs):
        calls.append(root)
        return {"ok": True, "ready": False, "fetched": [], "kept": [], "failed": []}

    monkeypatch.setattr(models, "provision", fake_provision)

    result = await reg.command("watchers", "provision_models")

    # Returns at once: this is a several-hundred-megabyte download and every
    # call into a plugin is under a 30s timeout.
    assert result["started"] is True
    assert await until(lambda: bool(calls))
    rows = await wait_events("models")
    assert rows[0]["sensor_id"] == PIPELINE_SENSOR
    assert healthy(reg)


# --- gallery internals -----------------------------------------------------

async def test_the_gallery_index_survives_a_reload(reg):
    plugin = plugin_of(reg)
    await enrol_directly(plugin, "Jane", face=vec(0))

    fresh = Gallery(plugin.ctx.db)
    await fresh.reload()

    assert fresh.match("face", vec(0))[1] == "Jane"
    assert fresh.match("face", vec(7))[2] == pytest.approx(0.0)


async def test_averaging_several_photos_beats_any_one_of_them(reg):
    """Enrolment from N shots stores one prototype, and it sits between them."""
    plugin = plugin_of(reg)
    await plugin.gallery.enrol(
        "Jane", [("face", vec(0)), ("face", blend(vec(0), vec(1), 0.5))], source="test"
    )

    rows = await plugin.ctx.db.execute_fetchall(
        "SELECT count(*) FROM embeddings WHERE modality = 'face'"
    )
    assert rows[0][0] == 1
    assert 0.9 < plugin.gallery.match("face", vec(0))[2] < 1.0


# --- the real models, when they are actually there --------------------------
#
# Everything above deliberately runs without weights. These two need the
# `[models]` extra installed and `blackice-watchers-provision` to have run, so
# they are opt-in:  uv run pytest -m integration

@pytest.mark.integration
def test_the_real_stack_loads_from_the_pinned_directory():
    stack, reason = models.load_stack(Settings())
    assert stack is not None, reason
    info = stack.info()
    assert info["loaded"] is True
    assert info["device"] in ("cpu", "mps", "cuda")


@pytest.mark.integration
def test_the_real_stack_finds_a_person_and_keeps_the_track_id():
    stack, reason = models.load_stack(Settings())
    assert stack is not None, reason

    # A synthetic rectangle is not a person, so this asserts the plumbing --
    # that a frame goes in, a list comes out, and track ids are per-stream --
    # rather than detection quality, which is not ours to test.
    first = stack.analyse(pictures(1)[0], "camera-a")
    second = stack.analyse(pictures(1)[0], "camera-b")

    assert isinstance(first, list)
    assert isinstance(second, list)
    for person in first + second:
        assert person.body is None or person.body.size > 0


async def test_a_camera_that_goes_blind_is_reported_even_with_nobody_in_frame(reg):
    """The sweep tick, not a finished track, is what notices this — a camera
    the models cannot keep up with is precisely one producing no tracks."""
    plugin = plugin_of(reg)
    fleet = FakeFleet({DEVICE: LABEL})
    await attach(plugin, fleet, StubRecognizer())
    worker = plugin.pipeline.workers[DEVICE][0]
    await feed(plugin, fleet, DEVICE, encode_annexb(pictures(1)))
    # Old enough to be judged, and far below the floor.
    worker.stats.started_at = time.time() - 120
    worker.stats.recent.clear()

    await plugin._on_expired([])

    rows = await wait_events("throughput")
    assert len(rows) == 1
    assert rows[0]["sensor_id"] == PIPELINE_SENSOR
    assert "not keeping up" in rows[0]["summary"]
    assert rows[0]["sensor_text"] == f"camera device id {DEVICE}"

    # Said once, not once a second.
    await plugin._on_expired([])
    assert len(await timeline("throughput")) == 1


async def test_who_was_seen_still_names_a_camera_without_a_fleet(reg):
    """On the read-only side there is no fleet to ask, but the sightings on
    record already know which label goes with which device id."""
    plugin = plugin_of(reg)
    await plugin.gallery.record_sighting(
        ts=time.time(), device_id=DEVICE, camera=LABEL, track_id=1,
        identity="Jane", confidence=0.9, modality="face",
    )
    assert plugin.pipeline.fleet is None

    result = await reg.command("watchers", "who_was_seen", camera=LABEL)

    assert result["camera"] == LABEL
    assert [s["who"] for s in result["sightings"]] == ["Jane"]

    unknown = await reg.command("watchers", "who_was_seen", camera="Cellar")
    assert "has been seen here" in unknown["error"]
    assert LABEL in unknown["error"]
    assert healthy(reg)


async def test_an_unknown_person_at_night_is_worth_more(data_dir, watcher_env, monkeypatch):
    """The same sighting, in the night window: `MEDIUM` rather than `LOW`, and
    the summary says so. Its own registry because the night window has to be
    pinned before the plugin reads its settings."""
    start, end = night_window(covering_now=True)
    monkeypatch.setenv(watcher_settings.ENV_NIGHT_START, start)
    monkeypatch.setenv(watcher_settings.ENV_NIGHT_END, end)

    reg = Registry()
    await reg.start_plugin(WatchersPlugin, events.record)
    try:
        plugin = plugin_of(reg)
        recognizer = StubRecognizer(lambda i, s, c: [sighting(1, face=vec(9))])
        fleet = FakeFleet({DEVICE: LABEL})
        await attach(plugin, fleet, recognizer)

        await feed(plugin, fleet, DEVICE, encode_annexb(pictures(3)))
        rows = await wait_events("recognition")

        assert rows[0]["summary"] == "Unrecognised person at Front door at night"
        assert rows[0]["severity"] == SEVERITY_MEDIUM
        assert json.loads(rows[0]["payload"])["night"] is True
    finally:
        await reg.stop_all()


async def test_a_known_person_at_night_is_still_unremarkable(data_dir, watcher_env,
                                                             monkeypatch):
    """Night raises the stakes for strangers, not for the household."""
    start, end = night_window(covering_now=True)
    monkeypatch.setenv(watcher_settings.ENV_NIGHT_START, start)
    monkeypatch.setenv(watcher_settings.ENV_NIGHT_END, end)

    reg = Registry()
    await reg.start_plugin(WatchersPlugin, events.record)
    try:
        plugin = plugin_of(reg)
        await enrol_directly(plugin, "Jane", face=vec(0))
        await plugin.gallery.record_sighting(
            ts=time.time() - 3600, device_id=DEVICE, camera=LABEL, track_id=1,
            person_id=plugin.gallery.id_of("Jane"), identity="Jane",
        )
        recognizer = StubRecognizer(lambda i, s, c: [sighting(2, face=vec(0))])
        fleet = FakeFleet({DEVICE: LABEL})
        await attach(plugin, fleet, recognizer)

        await feed(plugin, fleet, DEVICE, encode_annexb(pictures(3)))
        rows = await wait_events("recognition")

        assert rows[0]["summary"] == "Recognised Jane at Front door"
        assert rows[0]["severity"] == SEVERITY_INFO
        assert json.loads(rows[0]["payload"])["night"] is True
    finally:
        await reg.stop_all()
