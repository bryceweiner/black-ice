"""What the pipeline needs from a set of models, and nothing more.

The pipeline is written against this interface rather than against ultralytics,
insightface, or boxmot directly. That is what makes the wiring — tracking,
evidence accumulation, debouncing, event shape — testable with a stub, on a
machine with no weights and no camera, which is most of what there is to get
wrong here. The models themselves are somebody else's tested code.

One call per frame, not one per model: a `Recognizer` is expected to detect
people, keep track ids stable across frames, and produce whichever embeddings
that frame can support. Returning `face=None` is the normal answer for a person
whose back is turned, and the resolver is built around that being common.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np


@dataclass(slots=True)
class PersonSighting:
    """One person, in one frame, as the models saw them."""

    #: Stable across frames for as long as the tracker holds the person. This,
    #: not the frame, is the unit of identity.
    track_id: int
    box: tuple[float, float, float, float]
    score: float = 0.0
    #: Face embedding, or None when no face was usable in this frame — too
    #: small, too far from frontal, or simply not visible.
    face: np.ndarray | None = None
    #: Appearance embedding of the whole person crop. Usually available
    #: whenever the person is, which is why it carries the faceless case.
    body: np.ndarray | None = None
    #: Width of the detected face in pixels, so the caller can prefer a good
    #: crop over a poor one when choosing what to attach to the event.
    face_pixels: int = 0


@runtime_checkable
class Recognizer(Protocol):
    """Detection, tracking, and embedding for one process."""

    def analyse(self, image: np.ndarray, stream: str) -> list[PersonSighting]:
        """Every person in one frame. `stream` scopes the tracker's state, so
        two cameras never share track ids."""

    def forget(self, stream: str) -> None:
        """Drop tracker state for a camera that has gone away."""

    def info(self) -> dict[str, Any]:
        """What loaded, for `recognition_status` and the dashboard."""


@dataclass
class NullRecognizer:
    """Stands in when no models are provisioned.

    It is a real object rather than a `None` check scattered through the
    pipeline: the plugin still starts, still serves widgets, and still reports
    honestly that it recognises nobody, which is the required behaviour in the
    process that owns no cameras as well as on a machine with no weights.
    """

    reason: str = "models are not provisioned"
    _info: dict[str, Any] = field(default_factory=dict)

    def analyse(self, image: np.ndarray, stream: str) -> list[PersonSighting]:
        return []

    def forget(self, stream: str) -> None:
        return None

    def info(self) -> dict[str, Any]:
        return {"loaded": False, "reason": self.reason, **self._info}
