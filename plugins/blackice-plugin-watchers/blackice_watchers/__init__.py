"""Recognising people on the V380 cameras.

Frames come from the V380 plugin's published fleet — Annex B access units on a
drop-oldest queue — and are decoded here, once per camera. Each decoded frame
goes through person detection and ByteTrack, then the face of any track large
and frontal enough is embedded with ArcFace, and every track's body crop is
embedded for ReID. Evidence accumulates per *track*, and one event reaches the
timeline when a track is resolved, not once per frame.

    from blackice_watchers import WatchersPlugin, PEOPLE_SENSOR, PIPELINE_SENSOR

The parts worth knowing about separately:

* `tracks.Resolver` — evidence, debouncing, revision, and cross-camera linking.
  Pure and synchronous; this is where the behaviour lives and where it is
  tested, with no model and no camera involved.
* `pipeline.Pipeline` — one worker per camera, and the reconciliation that
  copes with the fleet being absent, late, or replaced.
* `models` — the pinned model directory, the lock file, and the promise that
  nothing is downloaded at runtime.
* `gallery.Gallery` — enrolled people, as embeddings. Never images.
"""

from .gallery import BODY, FACE, Gallery
from .models import MANIFEST, provision, status
from .pipeline import CameraStats, CameraWorker, Pipeline
from .plugin import PEOPLE_SENSOR, PIPELINE_SENSOR, WatchersPlugin, severity_for
from .recognition import NullRecognizer, PersonSighting, Recognizer
from .settings import Settings, Thresholds
from .tracks import (
    LINGERING,
    RECOGNITION,
    REVISION,
    Decision,
    Identity,
    RecentTrack,
    Resolver,
    TrackRecord,
)

__all__ = [
    "BODY",
    "FACE",
    "LINGERING",
    "MANIFEST",
    "PEOPLE_SENSOR",
    "PIPELINE_SENSOR",
    "RECOGNITION",
    "REVISION",
    "CameraStats",
    "CameraWorker",
    "Decision",
    "Gallery",
    "Identity",
    "NullRecognizer",
    "PersonSighting",
    "Pipeline",
    "RecentTrack",
    "Recognizer",
    "Resolver",
    "Settings",
    "Thresholds",
    "TrackRecord",
    "WatchersPlugin",
    "provision",
    "severity_for",
    "status",
]
