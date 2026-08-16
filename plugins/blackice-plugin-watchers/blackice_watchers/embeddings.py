"""Vectors, and how they are compared and stored.

Embeddings are stored L2-normalised, which makes cosine similarity a dot
product and makes averaging several enrolment photos into one prototype the
cheap thing it should be. They are stored as raw float32 bytes rather than JSON
because a 512-float face embedding is 2KB of binary and 9KB of text, and this
table grows with every enrolment.

Nothing here reconstructs an image. An ArcFace embedding is not invertible to a
photograph, which is the point of storing these instead of the crops.
"""

from __future__ import annotations

import numpy as np

#: Anything below this is not a vector we can use — a model returning zeros,
#: or a crop so small the embedder gave up.
MIN_NORM = 1e-6


def normalise(vec: np.ndarray) -> np.ndarray:
    """Unit-length float32, so similarity is a dot product."""
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(arr))
    if norm < MIN_NORM:
        return arr
    return arr / norm


def usable(vec: np.ndarray | None) -> bool:
    if vec is None:
        return False
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    return arr.size > 0 and float(np.linalg.norm(arr)) >= MIN_NORM


def to_blob(vec: np.ndarray) -> bytes:
    return normalise(vec).astype(np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two embeddings, in [-1, 1].

    Both sides are normalised defensively: a vector that came off the wire, or
    out of a stubbed model in a test, has not necessarily been through
    `normalise` on the way in.
    """
    left, right = normalise(a), normalise(b)
    if left.size != right.size or left.size == 0:
        return 0.0
    return float(np.dot(left, right))


def mean_vector(vectors: list[np.ndarray]) -> np.ndarray | None:
    """The prototype for several enrolment photos of one person.

    Averaging normalised embeddings and re-normalising is the standard way to
    turn N shots of a face into one gallery entry; it beats keeping the best
    single shot because it averages out pose and lighting.
    """
    usable_vectors = [normalise(v) for v in vectors if usable(v)]
    if not usable_vectors:
        return None
    sizes = {v.size for v in usable_vectors}
    if len(sizes) > 1:
        # Mixing a 512-d face vector with a 256-d body vector would produce
        # nonsense rather than an error, so refuse it here.
        return None
    return normalise(np.mean(np.stack(usable_vectors), axis=0))


class VectorIndex:
    """A flat cosine index over one modality's embeddings.

    Flat because a household gallery is tens of vectors, not millions: a matrix
    multiply is faster than any approximate index at this size, and it cannot
    return a wrong answer.
    """

    __slots__ = ("_matrix", "_owners", "_dim")

    def __init__(self) -> None:
        self._matrix: np.ndarray | None = None
        self._owners: list[int] = []
        self._dim = 0

    def build(self, entries: list[tuple[int, np.ndarray]]) -> None:
        """Replace the index. `entries` is (person_id, vector)."""
        vectors, owners = [], []
        for owner, vec in entries:
            if not usable(vec):
                continue
            normalised = normalise(vec)
            if self._dim and normalised.size != self._dim:
                continue
            self._dim = self._dim or normalised.size
            vectors.append(normalised)
            owners.append(owner)
        self._owners = owners
        self._matrix = np.stack(vectors) if vectors else None

    def best(self, vec: np.ndarray) -> tuple[int, float] | None:
        """The closest person and their similarity, or None if empty.

        Returns the best match regardless of threshold — deciding whether it is
        good enough belongs to the resolver, which knows the thresholds and can
        say so in the event.
        """
        if self._matrix is None or not usable(vec):
            return None
        probe = normalise(vec)
        if probe.size != self._matrix.shape[1]:
            return None
        scores = self._matrix @ probe
        index = int(np.argmax(scores))
        return self._owners[index], float(scores[index])

    def __len__(self) -> int:
        return 0 if self._matrix is None else int(self._matrix.shape[0])
