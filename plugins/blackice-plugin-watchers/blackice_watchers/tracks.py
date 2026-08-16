"""Turning a stream of per-frame observations into one decision per person.

The track, not the frame, is the unit of identity. A face model asked about a
single frame will happily be confident and wrong; the same model asked about
twenty frames of the same track is not. So evidence accumulates here, a track
is called once it has enough of it, and the event is emitted once — not every
frame, which would put a hundred rows on the timeline for one person walking
past a camera.

Four things come out of this file:

* **Resolution.** A candidate needs `min_frames` frames that agree before the
  track is called. Below that the track stays pending and emits nothing.
* **Revision.** A track already called can be re-called if a different person
  later reaches the same bar — a back-of-the-head ReID guess corrected by a
  face two seconds later is the case this exists for.
* **Lingering.** An unresolved person still in frame after `linger_seconds` is
  a separate decision, emitted once, because standing at a door is different
  from walking past one.
* **Cross-camera linking.** A track whose face is never visible is compared
  against recently-seen tracks on *other* cameras. That is what turns two
  sightings into "the person who was at the drive is now at the door".

Re-acquisition needs no code: the tracker issues a new id when someone leaves
and comes back, and a new id is a new track.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .embeddings import similarity, usable
from .gallery import BODY, FACE
from .recognition import PersonSighting
from .settings import Settings

#: Decision kinds. These become `Event.kind`, so they are part of the contract
#: with alarm rules and with anything downstream that filters the timeline.
RECOGNITION = "recognition"
REVISION = "recognition_revised"
LINGERING = "person_lingering"


@dataclass(slots=True)
class Candidate:
    """One enrolled person, and how much this track looks like them."""

    person_id: int
    name: str
    hits: int = 0
    face_hits: int = 0
    body_hits: int = 0
    face_best: float = 0.0
    body_best: float = 0.0

    @property
    def confidence(self) -> float:
        return max(self.face_best, self.body_best)

    @property
    def modality(self) -> str:
        if self.face_hits and self.body_hits:
            return "both"
        return "face" if self.face_hits else "reid"


@dataclass(slots=True)
class Identity:
    person_id: int | None
    name: str | None
    confidence: float
    modality: str

    @property
    def known(self) -> bool:
        return self.person_id is not None


@dataclass(slots=True)
class TrackRecord:
    device_id: str
    camera: str
    track_id: int
    first_seen: float
    last_seen: float
    frames: int = 0
    candidates: dict[int, Candidate] = field(default_factory=dict)
    identity: Identity | None = None
    resolved: bool = False
    lingered: bool = False
    best_face: np.ndarray | None = None
    best_face_pixels: int = 0
    best_body: np.ndarray | None = None
    best_crop: bytes | None = None
    best_crop_area: int = 0
    #: Set when this track was matched to a track on another camera.
    linked_to: str = ""
    linked_score: float = 0.0
    media_path: str = ""

    @property
    def key(self) -> str:
        return f"{self.device_id}:{self.track_id}"

    @property
    def duration(self) -> float:
        return max(0.0, self.last_seen - self.first_seen)

    def best_candidate(self) -> Candidate | None:
        if not self.candidates:
            return None
        return max(self.candidates.values(), key=lambda c: (c.hits, c.confidence))


@dataclass(slots=True)
class Decision:
    """Something worth an event. The pipeline turns this into one."""

    kind: str
    track: TrackRecord
    identity: Identity
    #: Best similarity to anyone enrolled, even when below threshold, so an
    #: unknown event can say how close the nearest call was.
    closest_name: str | None = None
    closest_score: float = 0.0


@dataclass(slots=True)
class RecentTrack:
    """A track that has finished, kept briefly for cross-camera matching."""

    key: str
    device_id: str
    camera: str
    track_id: int
    ts: float
    body: np.ndarray | None = None
    face: np.ndarray | None = None
    person_id: int | None = None
    name: str | None = None


class RecentIndex:
    """Tracks seen in the last window, for matching across cameras.

    In memory and bounded: this is a lookup on the recognition path, and it
    exists to answer "have I just seen this person somewhere else", which is a
    question about the last few minutes, not about history.
    """

    def __init__(self, window: float, limit: int = 512) -> None:
        self.window = window
        self._items: deque[RecentTrack] = deque(maxlen=limit)

    def add(self, item: RecentTrack) -> None:
        self._items.append(item)

    def prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._items and self._items[0].ts < cutoff:
            self._items.popleft()

    def best_body(
        self, vec: np.ndarray | None, *, exclude_device: str, now: float | None = None
    ) -> tuple[RecentTrack, float] | None:
        if not usable(vec):
            return None
        cutoff = (now if now is not None else time.time()) - self.window
        best, best_score = None, 0.0
        for item in self._items:
            if item.device_id == exclude_device or item.ts < cutoff:
                continue
            if not usable(item.body):
                continue
            score = similarity(vec, item.body)
            if score > best_score:
                best, best_score = item, score
        return (best, best_score) if best is not None else None

    def __len__(self) -> int:
        return len(self._items)


class Resolver:
    """Evidence accumulation and debouncing for every live track.

    Synchronous and free of I/O on purpose: it runs once per detected person
    per analysed frame, and it is the piece most worth testing without a model
    or a camera anywhere near it.
    """

    def __init__(self, settings: Settings, gallery: Any) -> None:
        self.settings = settings
        self.gallery = gallery
        self.tracks: dict[str, TrackRecord] = {}
        self.recent = RecentIndex(settings.recent_window)

    @property
    def thresholds(self):
        return self.settings.thresholds

    # --- observation -------------------------------------------------------

    def observe(
        self,
        device_id: str,
        camera: str,
        sighting: PersonSighting,
        *,
        ts: float,
        crop: bytes | None = None,
        crop_area: int = 0,
    ) -> list[Decision]:
        """Fold one frame's view of one person into their track."""
        key = f"{device_id}:{sighting.track_id}"
        record = self.tracks.get(key)
        if record is None:
            record = self.tracks[key] = TrackRecord(
                device_id=device_id, camera=camera, track_id=sighting.track_id,
                first_seen=ts, last_seen=ts,
            )
        record.last_seen = ts
        record.frames += 1
        record.camera = camera or record.camera

        if crop and crop_area > record.best_crop_area:
            record.best_crop = crop
            record.best_crop_area = crop_area

        matched_face = self._fold(record, FACE, sighting.face, self.thresholds.face)
        if usable(sighting.face) and sighting.face_pixels >= record.best_face_pixels:
            record.best_face = sighting.face
            record.best_face_pixels = sighting.face_pixels

        matched_body = self._fold(record, BODY, sighting.body, self.thresholds.reid)
        if usable(sighting.body):
            record.best_body = sighting.body

        # One frame, one vote per candidate, whichever modality supplied it —
        # otherwise a two-frame track with both modalities would clear a
        # three-frame bar and the threshold would not mean what it says.
        for person_id in {p for p in (matched_face, matched_body) if p is not None}:
            record.candidates[person_id].hits += 1

        return self._decide(record, ts)

    def _fold(
        self, record: TrackRecord, modality: str, vec: np.ndarray | None, threshold: float
    ) -> int | None:
        if not usable(vec):
            return None
        hit = self.gallery.match(modality, vec)
        if hit is None:
            return None
        person_id, name, score = hit
        candidate = record.candidates.get(person_id)
        if candidate is None:
            candidate = record.candidates[person_id] = Candidate(person_id, name)
        candidate.name = name
        if modality == FACE:
            candidate.face_best = max(candidate.face_best, score)
        else:
            candidate.body_best = max(candidate.body_best, score)
        if score < threshold:
            return None
        if modality == FACE:
            candidate.face_hits += 1
        else:
            candidate.body_hits += 1
        return person_id

    # --- decisions ---------------------------------------------------------

    def _decide(self, record: TrackRecord, ts: float) -> list[Decision]:
        decisions: list[Decision] = []
        best = record.best_candidate()
        confident = best if best and best.hits >= self.thresholds.min_frames else None

        if not record.resolved:
            if confident is not None:
                record.identity = Identity(
                    confident.person_id, confident.name, confident.confidence,
                    confident.modality,
                )
                record.resolved = True
                decisions.append(self._decision(RECOGNITION, record, best))
            elif record.frames >= self.thresholds.min_frames:
                record.identity = self._unknown_or_linked(record, ts)
                record.resolved = True
                decisions.append(self._decision(RECOGNITION, record, best))
        elif confident is not None and record.identity is not None and (
            confident.person_id != record.identity.person_id
        ):
            # A revision is a genuine change of mind, not a re-announcement:
            # only a different person, and only once they clear the same bar.
            record.identity = Identity(
                confident.person_id, confident.name, confident.confidence, confident.modality
            )
            decisions.append(self._decision(REVISION, record, best))

        if (
            record.resolved
            and record.identity is not None
            and not record.identity.known
            and not record.lingered
            and record.duration >= self.settings.linger_seconds
        ):
            record.lingered = True
            decisions.append(self._decision(LINGERING, record, best))

        return decisions

    def _unknown_or_linked(self, record: TrackRecord, ts: float) -> Identity:
        """Called when nobody enrolled matched. Try the other cameras first.

        A track with no usable face can still be the person who was at the
        drive a minute ago; if that track had a name, this one inherits it, and
        if it did not, the two are still linked so the timeline shows a route
        rather than two unrelated strangers.
        """
        link = self.recent.best_body(
            record.best_body, exclude_device=record.device_id, now=ts
        )
        if link is None or link[1] < self.thresholds.reid:
            return Identity(None, None, 0.0, "none")

        other, score = link
        record.linked_to = other.key
        record.linked_score = score
        if other.person_id is not None:
            return Identity(other.person_id, other.name, score, "reid")
        return Identity(None, None, score, "reid")

    @staticmethod
    def _decision(kind: str, record: TrackRecord, best: Candidate | None) -> Decision:
        assert record.identity is not None
        return Decision(
            kind=kind,
            track=record,
            identity=record.identity,
            closest_name=best.name if best else None,
            closest_score=best.confidence if best else 0.0,
        )

    # --- expiry ------------------------------------------------------------

    def sweep(self, now: float) -> list[TrackRecord]:
        """Retire tracks nobody has seen lately, and return them.

        The caller persists them, which is what makes `enrol_person(track=...)`
        possible after the person has walked away.
        """
        cutoff = now - self.settings.track_ttl
        expired = [r for r in self.tracks.values() if r.last_seen < cutoff]
        for record in expired:
            del self.tracks[record.key]
            self.recent.add(
                RecentTrack(
                    key=record.key, device_id=record.device_id, camera=record.camera,
                    track_id=record.track_id, ts=record.last_seen,
                    body=record.best_body, face=record.best_face,
                    person_id=record.identity.person_id if record.identity else None,
                    name=record.identity.name if record.identity else None,
                )
            )
        self.recent.prune(now)
        return expired

    def forget_camera(self, device_id: str) -> None:
        for key in [k for k, r in self.tracks.items() if r.device_id == device_id]:
            del self.tracks[key]
