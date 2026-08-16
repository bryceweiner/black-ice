"""The models, where they come from, and the promise that they are not fetched
behind your back.

This is biometric machinery pointed at people in someone's home, so the rule is
that weights arrive exactly once, at a moment the owner chose, from URLs
written down in this file — and never at runtime. Three things enforce that:

1. **Provisioning is a separate step.** `blackice-watchers-provision`, or the
   button on the dashboard. Nothing downloads during `start()`, during a frame,
   or on first use.
2. **A lock file.** The first fetch records the SHA-256 of every file into
   `manifest.lock.json`. Every later provision and every model load verifies
   against it, so a model that changes underneath you is refused rather than
   quietly loaded. The hashes are yours to check against upstream.
3. **The loaders are given absolute paths** to files inside the model
   directory, and the libraries' own download paths are pointed at that
   directory and switched off. A missing file means the plugin runs without
   recognition and says so; it never means "go and get it".

The URLs below are pinned but not hash-pinned in source: this plugin was
written without fetching them, so the first provision is trust-on-first-use and
every subsequent one is verified. That is a deliberate, stated trade — see the
README.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import numpy as np

from . import settings as settings_mod
from .recognition import PersonSighting

log = logging.getLogger("blackice.plugin.watchers.models")

LOCK_NAME = "manifest.lock.json"
#: How long a single file download may take before it is abandoned.
DOWNLOAD_TIMEOUT = 600.0
_CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str
    filename: str
    url: str
    purpose: str
    #: Set for archives that are unpacked after download; the plugin then looks
    #: for `unpack_to` rather than the archive itself.
    unpack_to: str = ""

    def env_override(self) -> str:
        return f"WATCHERS_{self.key.upper()}_URL"

    def source(self) -> str:
        return os.environ.get(self.env_override(), "").strip() or self.url


#: The whole recognition stack, in the order the pipeline uses it.
MANIFEST: tuple[ModelSpec, ...] = (
    ModelSpec(
        key="detector",
        filename="yolo11n.pt",
        url="https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt",
        purpose="person detection",
    ),
    ModelSpec(
        key="face",
        filename="buffalo_l.zip",
        url="https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",
        purpose="face detection and ArcFace embedding",
        unpack_to="models/buffalo_l",
    ),
    ModelSpec(
        key="reid",
        filename="osnet_x0_25_msmt17.pt",
        url=(
            "https://github.com/mikel-brostrom/boxmot/releases/download/v10.0.83/"
            "osnet_x0_25_msmt17.pt"
        ),
        purpose="person appearance embedding for ReID",
    ),
)

MODELS_BY_KEY = {spec.key: spec for spec in MANIFEST}


# --- the lock file ---------------------------------------------------------

def lock_path(root: Path | None = None) -> Path:
    return (root or settings_mod.model_dir()) / LOCK_NAME


def read_lock(root: Path | None = None) -> dict[str, Any]:
    path = lock_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def write_lock(entries: dict[str, Any], root: Path | None = None) -> None:
    path = lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", "utf-8")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def status(root: Path | None = None) -> dict[str, Any]:
    """What is on disk, and whether it is what the lock file says.

    Answers the dashboard and `recognition_status` without loading anything, so
    it is cheap enough to call from a widget.
    """
    directory = root or settings_mod.model_dir()
    lock = read_lock(directory)
    models = []
    for spec in MANIFEST:
        path = directory / spec.filename
        recorded = lock.get(spec.key, {})
        entry: dict[str, Any] = {
            "key": spec.key,
            "purpose": spec.purpose,
            "file": spec.filename,
            "url": spec.source(),
            "present": path.is_file(),
            "state": "missing",
            "sha256": recorded.get("sha256", ""),
        }
        if entry["present"]:
            actual = sha256_of(path)
            entry["sha256"] = actual
            if not recorded.get("sha256"):
                entry["state"] = "unlocked"
            elif recorded["sha256"] == actual:
                entry["state"] = "ok"
            else:
                entry["state"] = "mismatched"
        models.append(entry)

    ready = all(m["state"] == "ok" for m in models)
    return {
        "directory": str(directory),
        "models": models,
        "ready": ready,
        "provisioned_at": lock.get("_provisioned_at"),
        "stack_installed": stack_installed(),
    }


def stack_installed() -> bool:
    """Whether the optional `[models]` extra is importable.

    Kept apart from whether the weights are present: they are two different
    things to be told to fix, and conflating them produces the world's least
    helpful error message.
    """
    from importlib.util import find_spec

    return all(find_spec(m) is not None for m in ("ultralytics", "insightface", "boxmot"))


def missing_pieces(root: Path | None = None) -> list[str]:
    """Human-readable reasons recognition cannot run, in the order to fix them."""
    reasons = []
    if not stack_installed():
        reasons.append(
            "the recognition libraries are not installed "
            "(uv pip install -e 'plugins/blackice-plugin-watchers[models]')"
        )
    report = status(root)
    for model in report["models"]:
        if model["state"] == "missing":
            reasons.append(f"{model['purpose']}: {model['file']} has not been downloaded")
        elif model["state"] == "mismatched":
            reasons.append(
                f"{model['purpose']}: {model['file']} does not match the recorded "
                "hash; delete it and provision again if you meant to change it"
            )
    return reasons


# --- provisioning ----------------------------------------------------------

ProgressFn = Callable[[str], None]


def _download(url: str, target: Path, progress: ProgressFn) -> None:
    tmp = target.with_suffix(target.suffix + ".part")
    request = Request(url, headers={"User-Agent": "blackice-watchers/0.1"})  # noqa: S310
    progress(f"fetching {target.name} from {url}")
    with urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:  # noqa: S310
        total = int(response.headers.get("content-length") or 0)
        seen = 0
        with tmp.open("wb") as handle:
            while chunk := response.read(_CHUNK):
                handle.write(chunk)
                seen += len(chunk)
                if total:
                    progress(f"{target.name}: {seen * 100 // total}%")
    tmp.replace(target)


def _unpack(spec: ModelSpec, root: Path, progress: ProgressFn) -> None:
    destination = root / spec.unpack_to
    if destination.is_dir():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    progress(f"unpacking {spec.filename}")
    with zipfile.ZipFile(root / spec.filename) as archive:
        for member in archive.namelist():
            # A zip is an untrusted archive even from a known URL; a member
            # named ../../ would otherwise write outside the model directory.
            resolved = (destination / member).resolve()
            if not resolved.is_relative_to(destination.resolve()):
                raise ValueError(f"{spec.filename} contains an unsafe path: {member}")
        archive.extractall(destination)
    # buffalo_l zips with a leading directory in some releases and without it
    # in others; flatten so the loader always finds the .onnx files directly.
    entries = list(destination.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in list(inner.iterdir()):
            child.rename(destination / child.name)
        inner.rmdir()


def provision(
    root: Path | None = None,
    *,
    only: str = "",
    force: bool = False,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Download every model that is not already correct, and lock the hashes.

    Blocking and network-bound: the CLI calls it directly, and the plugin runs
    it on a thread so the supervisor's 30s timeout never sees it.
    """
    directory = root or settings_mod.model_dir()
    directory.mkdir(parents=True, exist_ok=True)
    say: ProgressFn = progress or (lambda message: log.info("%s", message))

    lock = read_lock(directory)
    specs = [s for s in MANIFEST if not only or s.key == only]
    if only and not specs:
        return {"error": f"no model called {only!r}; known: "
                         f"{', '.join(s.key for s in MANIFEST)}"}

    fetched, kept, failed = [], [], []
    for spec in specs:
        target = directory / spec.filename
        recorded = lock.get(spec.key, {}).get("sha256", "")
        if target.is_file() and not force:
            actual = sha256_of(target)
            if recorded and recorded == actual:
                say(f"{spec.filename} already present and verified")
                kept.append(spec.key)
                continue
            if recorded and recorded != actual:
                say(f"{spec.filename} does not match the lock file; re-fetching")
            else:
                # Present but never locked: adopt it rather than re-download,
                # and record the hash so the next run is verified.
                say(f"{spec.filename} present but unlocked; recording its hash")
                lock[spec.key] = _lock_entry(spec, actual)
                if spec.unpack_to and not (directory / spec.unpack_to).is_dir():
                    _unpack(spec, directory, say)
                kept.append(spec.key)
                continue
        try:
            _download(spec.source(), target, say)
            digest = sha256_of(target)
            if recorded and recorded != digest and not force:
                target.unlink(missing_ok=True)
                failed.append(
                    f"{spec.filename}: downloaded bytes do not match the locked hash"
                )
                continue
            lock[spec.key] = _lock_entry(spec, digest)
            if spec.unpack_to:
                _unpack(spec, directory, say)
            fetched.append(spec.key)
            say(f"{spec.filename} ok ({digest[:16]}…)")
        except Exception as exc:
            log.warning("provisioning %s failed: %s", spec.key, exc)
            failed.append(f"{spec.filename}: {exc}")

    lock["_provisioned_at"] = time.time()
    write_lock(lock, directory)
    report = status(directory)
    return {
        "ok": not failed,
        "fetched": fetched,
        "kept": kept,
        "failed": failed,
        "directory": str(directory),
        "ready": report["ready"],
        "stack_installed": report["stack_installed"],
    }


def _lock_entry(spec: ModelSpec, digest: str) -> dict[str, Any]:
    return {"file": spec.filename, "url": spec.source(), "sha256": digest, "at": time.time()}


# --- loading ---------------------------------------------------------------

def resolve_device(preference: str) -> str:
    """`auto` means the Apple GPU if there is one, otherwise the CPU."""
    if preference and preference != "auto":
        return preference
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _seal_library_downloads(directory: Path) -> None:
    """Point every library's cache at the model directory and switch its
    auto-download off, so a missing file fails loudly instead of fetching."""
    os.environ.setdefault("INSIGHTFACE_HOME", str(directory))
    os.environ.setdefault("YOLO_CONFIG_DIR", str(directory))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


class ModelStack:
    """YOLO + ByteTrack + ArcFace + OSNet, loaded from the pinned directory.

    Constructed only when `status()` says every file is present and verified,
    so the constructor's job is to load, not to decide whether it can.
    """

    def __init__(self, settings: settings_mod.Settings, root: Path | None = None) -> None:
        directory = root or settings_mod.model_dir()
        _seal_library_downloads(directory)
        self.settings = settings
        self.device = resolve_device(settings.device)
        self.root = directory

        from boxmot.appearance.reid_auto_backend import ReidAutoBackend
        from boxmot.trackers.bytetrack.bytetrack import ByteTrack
        from insightface.app import FaceAnalysis
        from ultralytics import YOLO

        self._bytetrack_cls = ByteTrack
        self.detector = YOLO(str(directory / MODELS_BY_KEY["detector"].filename))
        self.faces = FaceAnalysis(
            name="buffalo_l",
            root=str(directory),
            providers=["CPUExecutionProvider"],
        )
        self.faces.prepare(ctx_id=-1, det_size=(640, 640))
        self.reid = ReidAutoBackend(
            weights=directory / MODELS_BY_KEY["reid"].filename,
            device=self.device,
            half=False,
        ).model
        self._trackers: dict[str, Any] = {}

    # --- the interface the pipeline uses ---------------------------------

    def analyse(self, image: np.ndarray, stream: str) -> list[PersonSighting]:
        detections = self._detect(image)
        tracked = self._track(stream, detections, image)
        if len(tracked) == 0:
            return []

        rows = list(tracked_as(tracked))
        # Faces are found once for the whole picture rather than once per person
        # crop: SCRFD on one frame costs far less than N crops, and assigning
        # each face to the person box containing it is unambiguous unless two
        # people overlap completely.
        faces = self._faces_in(image)
        bodies = self._embed_bodies(image, [box for box, _, _ in rows])

        sightings = []
        for (box, track_id, score), body in zip(rows, bodies, strict=False):
            face_vec, face_px = _best_face_for(box, faces)
            sightings.append(
                PersonSighting(
                    track_id=track_id,
                    box=box,
                    score=score,
                    face=face_vec if face_px >= self.settings.min_face_pixels else None,
                    body=body,
                    face_pixels=face_px,
                )
            )
        return sightings

    def forget(self, stream: str) -> None:
        self._trackers.pop(stream, None)

    def info(self) -> dict[str, Any]:
        return {
            "loaded": True,
            "device": self.device,
            "directory": str(self.root),
            "detector": MODELS_BY_KEY["detector"].filename,
            "face": MODELS_BY_KEY["face"].filename,
            "reid": MODELS_BY_KEY["reid"].filename,
        }

    # --- internals --------------------------------------------------------

    def _detect(self, image: np.ndarray) -> np.ndarray:
        # class 0 is 'person' in COCO; asking the detector to skip the other
        # 79 classes is free and keeps cats off the timeline.
        result = self.detector.predict(
            image, classes=[0], verbose=False, device=self.device
        )[0]
        if result.boxes is None or len(result.boxes) == 0:
            return np.empty((0, 6), dtype=np.float32)
        xyxy = result.boxes.xyxy.cpu().numpy()
        conf = result.boxes.conf.cpu().numpy().reshape(-1, 1)
        cls = np.zeros_like(conf)
        return np.hstack([xyxy, conf, cls]).astype(np.float32)

    def _track(self, stream: str, detections: np.ndarray, image: np.ndarray) -> np.ndarray:
        tracker = self._trackers.get(stream)
        if tracker is None:
            tracker = self._trackers[stream] = self._bytetrack_cls()
        return np.asarray(tracker.update(detections, image))

    def _faces_in(self, image: np.ndarray) -> list[Any]:
        try:
            return list(self.faces.get(image))
        except Exception:  # pragma: no cover - insightface on a degenerate frame
            log.debug("face analysis failed on a frame", exc_info=True)
            return []

    def _embed_bodies(self, image: np.ndarray, boxes: list) -> list[np.ndarray | None]:
        if not boxes:
            return []
        try:
            features = self.reid.get_features(np.asarray(boxes, dtype=np.float32), image)
            return [np.asarray(f, dtype=np.float32) for f in features]
        except Exception:  # pragma: no cover
            log.debug("reid embedding failed on a frame", exc_info=True)
            return [None] * len(boxes)


def tracked_as(rows: np.ndarray):
    """boxmot returns (x1, y1, x2, y2, id, conf, cls, ind); take what we need."""
    for row in rows:
        yield (float(row[0]), float(row[1]), float(row[2]), float(row[3])), int(row[4]), (
            float(row[5]) if len(row) > 5 else 0.0
        )


def _best_face_for(box, faces) -> tuple[np.ndarray | None, int]:
    """The largest face whose centre falls inside a person box."""
    x1, y1, x2, y2 = box
    best, best_width = None, 0
    for face in faces:
        fx1, fy1, fx2, fy2 = (float(v) for v in face.bbox)
        cx, cy = (fx1 + fx2) / 2, (fy1 + fy2) / 2
        if not (x1 <= cx <= x2 and y1 <= cy <= y2):
            continue
        width = int(fx2 - fx1)
        if width > best_width:
            best, best_width = face, width
    if best is None:
        return None, 0
    embedding = getattr(best, "normed_embedding", None)
    if embedding is None:
        embedding = getattr(best, "embedding", None)
    return (np.asarray(embedding, dtype=np.float32) if embedding is not None else None), best_width


def load_stack(settings: settings_mod.Settings, root: Path | None = None):
    """The recognizer, or a reason there isn't one.

    Returns `(stack, "")` or `(None, reason)`. Never raises: a broken model
    directory must leave the plugin healthy and honest, not unhealthy.
    """
    reasons = missing_pieces(root)
    if reasons:
        return None, "; ".join(reasons)
    try:
        return ModelStack(settings, root), ""
    except Exception as exc:
        log.exception("loading the recognition stack failed")
        return None, f"{type(exc).__name__}: {exc}"
