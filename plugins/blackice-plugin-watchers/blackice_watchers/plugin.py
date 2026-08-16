"""The Black Ice sensor: who is on the cameras.

Two sensors rather than one. `watchers.people` is the timeline — one event per
resolved track. `watchers.pipeline` is the machinery — achieved frame rate,
dropped frames, and whether the models are even provisioned. They are separate
because they answer different questions and fail independently: recognition can
be perfectly healthy while a camera is starved, and the plugin can be entirely
unable to recognise anyone while remaining, correctly, healthy.

**Trust.** An enrolled name is text the owner typed during enrolment, so it is
plugin-authored and may go in `summary` — "Recognised Jane at the front door".
A camera label comes from the owner's V380 config file, so it is too — but a
camera with no label falls back to its device id, which came off the network,
so an unlabelled camera is described generically in `summary` and its id goes
in `sensor_text` with everything else the network supplied. No model output of
any kind reaches `summary`.

**Degraded is not unhealthy.** There is no fleet in the process that did not
take the camera lock, and there are no models on a machine that has not been
provisioned. Both are normal, both mean this plugin recognises nobody, and in
both cases it still starts, still answers its tools, and still serves every
widget from the database. A caller's bad argument comes back as
`{"error": ...}`; only a genuine fault raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from blackice.models import (
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    AlarmRuleSpec,
    Event,
    MediaRef,
    SensorDescriptor,
    ToolSpec,
    WidgetSpec,
)
from blackice.plugins.base import PluginContext, SensorPlugin

from . import models as model_store
from . import settings as settings_mod
from .decode import decode_image_file
from .embeddings import usable
from .gallery import BODY, FACE, Gallery
from .pipeline import Pipeline, largest_person
from .recognition import NullRecognizer
from .tracks import LINGERING, REVISION, Decision, Resolver, TrackRecord

log = logging.getLogger("blackice.plugin.watchers")

PEOPLE_SENSOR = "watchers.people"
PIPELINE_SENSOR = "watchers.pipeline"
MEDIA_SUBDIR = settings_mod.MEDIA_SUBDIR
PREF_THRESHOLDS = "thresholds"

CAMERA_PARAM = {
    "type": "string",
    "description": (
        "Which camera: its label if it has one (e.g. 'Front door'), otherwise its "
        "device id. The v380.list_cameras tool lists what exists."
    ),
}


class WatchersPlugin(SensorPlugin):
    name = "watchers"
    version = "0.1.0"

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.settings = settings_mod.load()
        self.gallery: Gallery | None = None
        self.resolver: Resolver | None = None
        self.pipeline: Pipeline | None = None
        self.recognizer: Any = NullRecognizer("the models have not been loaded yet")
        self.model_reason = "starting"
        self._load_task: asyncio.Task | None = None
        self._provision_task: asyncio.Task | None = None
        self._provision_log: list[str] = []
        self._degraded_since: dict[str, float] = {}

    # --- lifecycle ---------------------------------------------------------

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.gallery = Gallery(ctx.db)
        await self.gallery.create_schema()
        await self._restore_thresholds()
        await self.gallery.reload()

        self.resolver = Resolver(self.settings, self.gallery)
        self.pipeline = Pipeline(
            self.settings, self.resolver, self.recognizer,
            sink=self._on_decision, expiry_sink=self._on_expired,
        )
        self.pipeline.enabled = self.settings.enabled
        await self.pipeline.start()

        # Loading YOLO, insightface, and OSNet takes seconds and imports torch.
        # Doing it here would spend most of the supervisor's 30s budget on work
        # that no part of `start()` depends on, so it happens in the background
        # and the pipeline picks the models up when they arrive.
        self._load_task = asyncio.create_task(self._load_models(), name="watchers-load")

    async def stop(self) -> None:
        for task in (self._load_task, self._provision_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._load_task = self._provision_task = None
        if self.pipeline is not None:
            await self.pipeline.stop()
            self.pipeline = None

    async def _load_models(self) -> None:
        """Bring up the recognition stack, or record why there isn't one."""
        stack, reason = await asyncio.to_thread(model_store.load_stack, self.settings)
        if stack is None:
            self.recognizer = NullRecognizer(reason)
            self.model_reason = reason
            log.info("recognition is off: %s", reason)
        else:
            self.recognizer = stack
            self.model_reason = ""
            log.info("recognition models loaded on %s", stack.device)
        if self.pipeline is not None:
            await self.pipeline.set_recognizer(self.recognizer)

    async def _restore_thresholds(self) -> None:
        assert self.gallery is not None
        raw = await self.gallery.get_pref(PREF_THRESHOLDS)
        if not raw:
            return
        try:
            face, reid, frames = (part.strip() for part in raw.split(","))
            self.settings.thresholds.face = float(face)
            self.settings.thresholds.reid = float(reid)
            self.settings.thresholds.min_frames = int(frames)
        except (ValueError, TypeError):
            log.warning("stored thresholds %r are unreadable; using the defaults", raw)

    # --- description -------------------------------------------------------

    def describe(self) -> list[SensorDescriptor]:
        return [self._people_sensor(), self._pipeline_sensor()]

    def _people_sensor(self) -> SensorDescriptor:
        return SensorDescriptor(
            id=PEOPLE_SENSOR,
            name="Watchers",
            kind="recognition",
            widgets=[
                WidgetSpec(type="stat", title="People seen today",
                           data_source="people_today", span=3),
                WidgetSpec(type="bar", title="Sightings by camera",
                           data_source="sightings_by_camera", span=5),
                WidgetSpec(type="table", title="Enrolled people",
                           data_source="enrolled", span=4),
                WidgetSpec(type="gallery", title="Recent faces",
                           data_source="recent_crops", span=6),
                WidgetSpec(type="log", title="Recent recognitions",
                           data_source="recent_log", span=6),
            ],
            alarm_rules=[
                AlarmRuleSpec(
                    key="unknown_at_night",
                    name="Unrecognised person at night",
                    description=(
                        "Someone not enrolled was resolved on a camera between "
                        f"{self.settings.night_start:02d}:00 and "
                        f"{self.settings.night_end:02d}:00."
                    ),
                    sensor_id=PEOPLE_SENSOR,
                    default_armed=True,
                ),
                AlarmRuleSpec(
                    key="unknown_lingering",
                    name="Unrecognised person lingering",
                    description=(
                        "Someone not enrolled stayed in view of one camera for more "
                        f"than {self.settings.linger_seconds:.0f}s — standing at a "
                        "door rather than walking past it."
                    ),
                    sensor_id=PEOPLE_SENSOR,
                    default_armed=True,
                ),
            ],
            tools=self._tools(),
        )

    def _pipeline_sensor(self) -> SensorDescriptor:
        return SensorDescriptor(
            id=PIPELINE_SENSOR,
            name="Watchers pipeline",
            kind="diagnostic",
            widgets=[
                WidgetSpec(type="status", title="Recognition",
                           data_source="pipeline_status", span=3),
                WidgetSpec(type="stat", title="Analysed frames per second",
                           data_source="throughput", span=3),
                WidgetSpec(type="action", title="Recognition models",
                           data_source="model_status", span=6),
                WidgetSpec(type="table", title="Per-camera throughput",
                           data_source="camera_stats", span=12),
            ],
            alarm_rules=[
                AlarmRuleSpec(
                    key="recognition_degraded",
                    name="Recognition is not keeping up",
                    description=(
                        "A camera is sending frames but fewer than "
                        f"{self.settings.fps_floor:g} per second are reaching the "
                        "models — the plugin is largely blind on that camera."
                    ),
                    sensor_id=PIPELINE_SENSOR,
                    default_armed=True,
                ),
            ],
        )

    def _tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="list_enrolled",
                description=(
                    "List everyone this system has been taught to recognise, with how "
                    "many face and body samples it holds for each, and where and when "
                    "each was last seen. Call this before enrolling or forgetting "
                    "someone, to find out the exact name to use."
                ),
                parameters={"type": "object", "properties": {}},
            ),
            ToolSpec(
                name="enrol_person",
                description=(
                    "Teach the system to recognise someone, so that future sightings "
                    "of them say their name instead of 'unknown'. Give the person's "
                    "name and exactly one source to learn from: 'track' to use a "
                    "person the cameras resolved in the last hour (the track id is on "
                    "the recognition event), 'camera' to take a picture now and learn "
                    "whoever is most prominent in it, or 'files' to read photographs "
                    "the owner has put in the enrolment folder. Several photographs of "
                    "one person give a better result than one. Enrolling a name that "
                    "already exists adds to that person rather than replacing them. "
                    "This stores mathematical descriptors, never photographs."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "What to call this person, e.g. 'Jane'.",
                        },
                        "track": {
                            "type": "string",
                            "description": (
                                "A track key of the form '<device id>:<track id>' from "
                                "a recognition event in the last hour."
                            ),
                        },
                        "camera": CAMERA_PARAM,
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "File names inside the enrolment folder. Call with an "
                                "empty list to find out which files are there."
                            ),
                        },
                    },
                    "required": ["name"],
                },
            ),
            ToolSpec(
                name="forget_person",
                description=(
                    "Permanently delete an enrolled person and the biometric "
                    "descriptors held for them, and remove their name from this "
                    "plugin's sighting history. They will be reported as an unknown "
                    "person from then on. This cannot be undone and it is not a hiding "
                    "of the name — the descriptors are deleted. Events already on the "
                    "timeline keep whatever they said at the time."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "The enrolled name."}
                    },
                    "required": ["name"],
                },
            ),
            ToolSpec(
                name="rename_person",
                description=(
                    "Change the name of an enrolled person, keeping everything the "
                    "system has learned about recognising them."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "The current name."},
                        "new_name": {"type": "string", "description": "The name to use instead."},
                    },
                    "required": ["name", "new_name"],
                },
            ),
            ToolSpec(
                name="who_was_seen",
                description=(
                    "Who has been seen on one camera recently, most recent first — "
                    "both people recognised by name and unrecognised ones. Use this to "
                    "answer 'who was at the front door', or with no camera named, to "
                    "get the most recent sightings across every camera."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "camera": CAMERA_PARAM,
                        "hours": {
                            "type": "number",
                            "description": "How far back to look. Defaults to 6.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "How many sightings at most. Defaults to 20.",
                        },
                    },
                },
            ),
            ToolSpec(
                name="where_has_person_been",
                description=(
                    "The trail of one enrolled person across every camera, most recent "
                    "first. This is how to answer 'where is Jane' or 'has anyone been "
                    "round the back' for someone by name."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "The enrolled name."},
                        "hours": {
                            "type": "number",
                            "description": "How far back to look. Defaults to 6.",
                        },
                    },
                    "required": ["name"],
                },
            ),
            ToolSpec(
                name="recognition_status",
                description=(
                    "Whether the system can currently recognise anyone, and how well: "
                    "which models are loaded, whether it has access to the cameras in "
                    "this process, the frames per second actually reaching the models "
                    "on each camera, and how many frames are being dropped. Use this "
                    "when asked why nobody is being recognised, or before promising "
                    "that someone will be spotted."
                ),
                parameters={"type": "object", "properties": {}},
            ),
            ToolSpec(
                name="set_thresholds",
                description=(
                    "Adjust how readily a face or body is accepted as a match. Higher "
                    "values mean fewer people wrongly given a name and more people "
                    "reported as unknown; lower values mean the opposite. Sensible "
                    "range is 0.3 to 0.9. Pass only the values to change; the rest are "
                    "left alone. The change takes effect immediately and survives a "
                    "restart."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "face": {
                            "type": "number",
                            "description": "Face match threshold. Default 0.55.",
                        },
                        "reid": {
                            "type": "number",
                            "description": (
                                "Body-appearance match threshold, used when no face is "
                                "visible. Default 0.70."
                            ),
                        },
                        "min_frames": {
                            "type": "integer",
                            "description": (
                                "How many frames must agree before anyone is named. "
                                "Default 3."
                            ),
                        },
                    },
                },
            ),
        ]

    # --- commands ----------------------------------------------------------

    async def handle_command(self, cmd: str, **kwargs: Any) -> Any:
        match cmd:
            case "list_enrolled":
                return await self._list_enrolled()
            case "enrol_person":
                return await self._enrol(
                    kwargs.get("name"), kwargs.get("track"),
                    kwargs.get("camera"), kwargs.get("files"),
                )
            case "forget_person":
                return await self._forget(kwargs.get("name"))
            case "rename_person":
                return await self._rename(kwargs.get("name"), kwargs.get("new_name"))
            case "who_was_seen":
                return await self._who_was_seen(
                    kwargs.get("camera"), kwargs.get("hours"), kwargs.get("limit")
                )
            case "where_has_person_been":
                return await self._where_has_person_been(
                    kwargs.get("name"), kwargs.get("hours")
                )
            case "recognition_status":
                return await self._status()
            case "set_thresholds":
                return await self._set_thresholds(
                    kwargs.get("face"), kwargs.get("reid"), kwargs.get("min_frames")
                )
            case "provision_models":
                # Driven by the button on the dashboard rather than declared as
                # a tool: fetching model weights over the network is the
                # owner's decision to make, not the assistant's.
                return await self._provision()
        return await super().handle_command(cmd, **kwargs)

    async def _list_enrolled(self) -> dict[str, Any]:
        assert self.gallery is not None
        people = await self.gallery.people()
        return {
            "people": [
                {
                    "name": row["name"],
                    "face_samples": row["faces"],
                    "body_samples": row["bodies"],
                    "enrolled": row["created_at"],
                    "last_seen": _stamp(row["last_seen"]),
                    "last_camera": row["last_camera"],
                }
                for row in people
            ],
            "count": len(people),
            "enrolment_folder": str(settings_mod.enrol_dir()),
        }

    async def _enrol(
        self, name: Any, track: Any, camera: Any, files: Any
    ) -> dict[str, Any]:
        assert self.gallery is not None
        if not isinstance(name, str) or not name.strip():
            return {"error": "who is this? Pass the person's name as 'name'."}

        sources = [s for s in ("track" if track else "", "camera" if camera else "",
                               "files" if files else "") if s]
        if len(sources) > 1:
            return {"error": f"give exactly one source, not {' and '.join(sources)}"}

        if track:
            return await self._enrol_from_track(name, track)
        if camera:
            return await self._enrol_from_camera(name, camera)
        if isinstance(files, list) and files:
            return await self._enrol_from_files(name, files)
        return {
            "error": "nothing to learn from: pass 'track', 'camera', or 'files'",
            "available_files": settings_mod.list_enrol_files(),
            "enrolment_folder": str(settings_mod.enrol_dir()),
        }

    async def _enrol_from_track(self, name: str, track: Any) -> dict[str, Any]:
        assert self.gallery is not None
        if not isinstance(track, str) or ":" not in track:
            return {"error": f"{track!r} is not a track key; they look like '95886601:7'"}
        row = await self.gallery.track(track.strip())
        if row is None:
            return {
                "error": (
                    f"no track {track!r} in the last hour. Track descriptors are kept "
                    "for an hour; enrol from a camera or from files instead."
                )
            }
        vectors = []
        for modality, column in ((FACE, "face"), (BODY, "body")):
            if row[column]:
                from .embeddings import from_blob

                vectors.append((modality, from_blob(row[column])))
        if not vectors:
            return {"error": f"track {track!r} has no usable descriptors"}
        result = await self.gallery.enrol(name, vectors, source=f"track {track}")
        return await self._enrolled(result, f"track {track}")

    async def _enrol_from_camera(self, name: str, camera: Any) -> dict[str, Any]:
        if not self._models_ready():
            return {"error": self._models_error()}
        fleet = self.pipeline.fleet if self.pipeline else None
        if fleet is None:
            return {"error": _NO_FLEET}

        resolved = await self._resolve_camera(camera, fleet)
        if isinstance(resolved, dict):
            return resolved
        device_id, label = resolved

        try:
            shot = await fleet.snapshot(device_id, force=True)
        except KeyError:
            return {"error": f"camera {camera!r} is no longer present"}
        if shot is None or not getattr(shot, "jpeg", b""):
            return {"error": f"no picture available from {label} right now"}

        vectors = await asyncio.to_thread(self._embed_jpeg, shot.jpeg)
        if not vectors:
            return {"error": f"nobody recognisable is in view of {label} right now"}
        assert self.gallery is not None
        result = await self.gallery.enrol(name, vectors, source=f"snapshot {device_id}")
        return await self._enrolled(result, f"a snapshot from {label}")

    async def _enrol_from_files(self, name: str, files: list) -> dict[str, Any]:
        if not self._models_ready():
            return {"error": self._models_error()}

        chosen, missing = [], []
        for entry in files:
            path = settings_mod.resolve_enrol_file(str(entry))
            (chosen if path else missing).append(path or entry)
        if missing:
            return {
                "error": f"not in the enrolment folder: {', '.join(str(m) for m in missing)}",
                "available_files": settings_mod.list_enrol_files(),
                "enrolment_folder": str(settings_mod.enrol_dir()),
            }

        vectors = await asyncio.to_thread(self._embed_files, chosen)
        if not vectors:
            return {"error": "no person could be found in any of those photographs"}
        assert self.gallery is not None
        result = await self.gallery.enrol(
            name, vectors, source=f"files {', '.join(p.name for p in chosen)}"
        )
        return await self._enrolled(result, f"{len(chosen)} photograph(s)")

    def _embed_files(self, paths: list[Path]) -> list[tuple[str, Any]]:
        vectors: list[tuple[str, Any]] = []
        for path in paths:
            image = decode_image_file(str(path))
            if image is None:
                continue
            vectors.extend(self._embed_image(image))
        return vectors

    def _embed_jpeg(self, jpeg: bytes) -> list[tuple[str, Any]]:
        import tempfile

        # A snapshot arrives as bytes and PyAV wants something openable; the
        # suffix is what tells it this is a JPEG.
        with tempfile.NamedTemporaryFile(suffix=".jpg") as handle:
            handle.write(jpeg)
            handle.flush()
            image = decode_image_file(handle.name)
        return self._embed_image(image) if image is not None else []

    def _embed_image(self, image) -> list[tuple[str, Any]]:
        """Face and body vectors for the most prominent person in one picture."""
        sightings = self.recognizer.analyse(image, "enrolment")
        person = largest_person(sightings)
        if person is None:
            return []
        vectors = []
        if usable(person.face) and person.face_pixels >= self.settings.min_face_pixels:
            vectors.append((FACE, person.face))
        if usable(person.body):
            vectors.append((BODY, person.body))
        return vectors

    async def _enrolled(self, result: dict[str, Any], source: str) -> dict[str, Any]:
        assert self.ctx is not None
        await self.ctx.emit(
            Event(
                sensor_id=PEOPLE_SENSOR,
                severity=SEVERITY_INFO,
                kind="enrolment",
                # The name is the owner's own words, typed during enrolment.
                summary=f"Enrolled {result['name']} from {source}",
                payload={
                    "name": result["name"],
                    "person_id": result["person_id"],
                    "face_samples_added": result["added"][FACE],
                    "body_samples_added": result["added"][BODY],
                    "source": source,
                },
            )
        )
        return {
            "ok": True,
            "name": result["name"],
            "learned": {
                "face": result["added"][FACE],
                "body": result["added"][BODY],
            },
            "from": source,
            "note": (
                "Descriptors only; no photographs were stored."
                if result["added"][FACE]
                else "No usable face was found, so this person will be matched by "
                     "appearance only, which is weaker. Enrol a clear frontal photo "
                     "to improve it."
            ),
        }

    async def _forget(self, name: Any) -> dict[str, Any]:
        assert self.gallery is not None and self.ctx is not None
        if not isinstance(name, str) or not name.strip():
            return {"error": "who should be forgotten? Pass a name."}
        result = await self.gallery.forget(name)
        if result is None:
            return {
                "error": f"nobody called {name!r} is enrolled",
                "enrolled": sorted(self.gallery.names.values()),
            }
        await self.ctx.emit(
            Event(
                sensor_id=PEOPLE_SENSOR,
                severity=SEVERITY_LOW,
                kind="enrolment",
                summary=f"Deleted the enrolment for {name.strip()}",
                payload={
                    "embeddings_deleted": result["embeddings_deleted"],
                    "sightings_scrubbed": result["sightings_scrubbed"],
                },
            )
        )
        return {
            "ok": True,
            "forgotten": name.strip(),
            "descriptors_deleted": result["embeddings_deleted"],
            "sightings_anonymised": result["sightings_scrubbed"],
            "note": (
                "The biometric descriptors are gone. Events already on the timeline "
                "still contain the name that was used at the time."
            ),
        }

    async def _rename(self, name: Any, new_name: Any) -> dict[str, Any]:
        assert self.gallery is not None
        if not isinstance(name, str) or not name.strip():
            return {"error": "which person? Pass their current name."}
        if not isinstance(new_name, str) or not new_name.strip():
            return {"error": "what should they be called? Pass 'new_name'."}
        if self.gallery.id_of(new_name) is not None and (
            self.gallery.id_of(new_name) != self.gallery.id_of(name)
        ):
            return {"error": f"{new_name.strip()!r} is already enrolled"}
        result = await self.gallery.rename(name, new_name)
        if result is None:
            return {
                "error": f"nobody called {name!r} is enrolled",
                "enrolled": sorted(self.gallery.names.values()),
            }
        return {"ok": True, "was": name.strip(), "now": result["name"]}

    async def _who_was_seen(self, camera: Any, hours: Any, limit: Any) -> dict[str, Any]:
        assert self.ctx is not None
        window = _positive(hours, 6.0)
        if window is None:
            return {"error": f"{hours!r} is not a number of hours"}
        count = _positive(limit, 20)
        if count is None:
            return {"error": f"{limit!r} is not a number of sightings"}
        since = time.time() - window * 3600

        device_id = ""
        label = "every camera"
        if camera:
            resolved = await self._resolve_camera(
                camera, self.pipeline.fleet if self.pipeline else None
            )
            if isinstance(resolved, dict):
                return resolved
            device_id, label = resolved

        sql = (
            "SELECT ts, camera, device_id, identity, confidence, modality, kind, track_id"
            " FROM sightings WHERE ts >= ?"
        )
        params: list[Any] = [since]
        if device_id:
            sql += " AND device_id = ?"
            params.append(device_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(count))

        cur = await self.ctx.db.execute(sql, params)
        rows = [dict(r) for r in await cur.fetchall()]
        return {
            "camera": label,
            "hours": window,
            "sightings": [
                {
                    "when": _stamp(row["ts"]),
                    "camera": row["camera"] or row["device_id"],
                    "who": row["identity"] or "unknown person",
                    "confidence": round(row["confidence"], 3),
                    "recognised_by": row["modality"],
                    "track": f"{row['device_id']}:{row['track_id']}",
                }
                for row in rows
            ],
            "count": len(rows),
        }

    async def _where_has_person_been(self, name: Any, hours: Any) -> dict[str, Any]:
        assert self.gallery is not None and self.ctx is not None
        if not isinstance(name, str) or not name.strip():
            return {"error": "which person? Pass their enrolled name."}
        person_id = self.gallery.id_of(name)
        if person_id is None:
            return {
                "error": f"nobody called {name!r} is enrolled",
                "enrolled": sorted(self.gallery.names.values()),
            }
        window = _positive(hours, 6.0)
        if window is None:
            return {"error": f"{hours!r} is not a number of hours"}

        cur = await self.ctx.db.execute(
            "SELECT ts, camera, device_id, confidence, modality, track_id FROM sightings"
            " WHERE person_id = ? AND ts >= ? ORDER BY ts DESC LIMIT 100",
            (person_id, time.time() - window * 3600),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        return {
            "name": self.gallery.name_of(person_id),
            "hours": window,
            "trail": [
                {
                    "when": _stamp(row["ts"]),
                    "camera": row["camera"] or row["device_id"],
                    "confidence": round(row["confidence"], 3),
                    "recognised_by": row["modality"],
                }
                for row in rows
            ],
            "last_seen": _stamp(rows[0]["ts"]) if rows else None,
            "count": len(rows),
        }

    async def _status(self) -> dict[str, Any]:
        report = model_store.status()
        cameras = self.pipeline.stats() if self.pipeline else []
        struggling = self.pipeline.struggling() if self.pipeline else []
        return {
            "recognising": self._models_ready() and bool(cameras),
            "models_loaded": self._models_ready(),
            "models": self.recognizer.info(),
            "reason": self.model_reason or None,
            "has_cameras_in_this_process": (
                self.pipeline is not None and self.pipeline.fleet is not None
            ),
            "cameras": cameras,
            "not_keeping_up": [row["camera"] for row in struggling],
            "thresholds": self.settings.thresholds.as_dict(),
            "enrolled": len(self.gallery.names) if self.gallery else 0,
            "model_files": report["models"],
            "model_directory": report["directory"],
            "next_step": self._next_step(report),
        }

    def _next_step(self, report: dict[str, Any]) -> str | None:
        if not report["stack_installed"]:
            return (
                "Install the recognition libraries: "
                "uv pip install -e 'plugins/blackice-plugin-watchers[models]'"
            )
        if not report["ready"]:
            return (
                "Download the models: run blackice-watchers-provision, or press "
                "Download on the Watchers pipeline panel."
            )
        if self.pipeline is not None and self.pipeline.fleet is None:
            return (
                "This process does not own the cameras, so it does no recognition. "
                "The process holding data/v380.lock does."
            )
        return None

    async def _set_thresholds(self, face: Any, reid: Any, min_frames: Any) -> dict[str, Any]:
        assert self.gallery is not None
        thresholds = self.settings.thresholds
        for label, value in (("face", face), ("reid", reid)):
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                return {"error": f"{label} threshold {value!r} is not a number"}
            if not 0.0 < number < 1.0:
                return {
                    "error": (
                        f"{label} threshold must be between 0 and 1 "
                        "(0.3 to 0.9 is the useful range)"
                    )
                }
            setattr(thresholds, label, number)

        if min_frames is not None:
            try:
                frames = int(min_frames)
            except (TypeError, ValueError):
                return {"error": f"min_frames {min_frames!r} is not a whole number"}
            if not 1 <= frames <= 60:
                return {"error": "min_frames must be between 1 and 60"}
            thresholds.min_frames = frames

        await self.gallery.set_pref(
            PREF_THRESHOLDS, f"{thresholds.face},{thresholds.reid},{thresholds.min_frames}"
        )
        return {"ok": True, "thresholds": thresholds.as_dict()}

    async def _provision(self) -> dict[str, Any]:
        """Kick off the model download and return at once.

        Every call into a plugin is under a 30s timeout and this is a
        several-hundred-megabyte download, so the work goes on a task and the
        widget reports progress by polling `model_status`.
        """
        if self._provision_task is not None and not self._provision_task.done():
            return {"ok": True, "busy": True, "progress": self._provision_log[-5:]}
        self._provision_log = ["starting"]
        self._provision_task = asyncio.create_task(
            self._run_provision(), name="watchers-provision"
        )
        return {
            "ok": True,
            "started": True,
            "note": "downloading the recognition models; this panel will update",
        }

    async def _run_provision(self) -> None:
        assert self.ctx is not None
        result = await asyncio.to_thread(
            model_store.provision, None, progress=self._provision_log.append
        )
        severity = SEVERITY_INFO if result.get("ok") else SEVERITY_LOW
        await self.ctx.emit(
            Event(
                sensor_id=PIPELINE_SENSOR,
                severity=severity,
                kind="models",
                summary=(
                    "Recognition models are ready"
                    if result.get("ready")
                    else "Downloading the recognition models did not complete"
                ),
                payload=result,
            )
        )
        if result.get("ready"):
            await self._load_models()

    # --- widgets -----------------------------------------------------------

    async def query(self, source: str, **kwargs: Any) -> Any:
        assert self.ctx is not None
        match source:
            case "people_today":
                return await self._people_today()
            case "sightings_by_camera":
                return await self._sightings_by_camera()
            case "enrolled":
                return await self._enrolled_rows()
            case "recent_crops":
                return await self._recent_crops()
            case "recent_log":
                return await self._recent_log()
            case "pipeline_status":
                return {"state": self.pipeline.state() if self.pipeline else "offline"}
            case "throughput":
                return self._throughput()
            case "camera_stats":
                return self._camera_rows()
            case "model_status":
                return await self._model_status()
        return await super().query(source, **kwargs)

    async def _people_today(self) -> dict[str, Any]:
        assert self.ctx is not None
        midnight = datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        cur = await self.ctx.db.execute(
            "SELECT count(DISTINCT COALESCE(identity, 'unknown:' || device_id || track_id))"
            " AS n, sum(identity IS NULL) AS unknown FROM sightings"
            " WHERE ts >= ? AND kind = 'recognition'",
            (midnight,),
        )
        row = await cur.fetchone()
        total = int(row["n"] or 0) if row else 0
        unknown = int(row["unknown"] or 0) if row else 0
        return {
            "value": total,
            "label": f"{total - unknown} known · {unknown} unknown" if total else "nobody yet",
        }

    async def _sightings_by_camera(self) -> list[dict[str, Any]]:
        assert self.ctx is not None
        cur = await self.ctx.db.execute(
            "SELECT COALESCE(NULLIF(camera, ''), device_id) AS camera, count(*) AS sightings"
            " FROM sightings WHERE ts >= ? GROUP BY camera ORDER BY sightings DESC LIMIT 12",
            (time.time() - 86400,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def _enrolled_rows(self) -> list[dict[str, Any]]:
        assert self.gallery is not None
        return [
            {
                "person": row["name"],
                "face": row["faces"],
                "body": row["bodies"],
                "last seen": _stamp(row["last_seen"]) or "—",
                "where": row["last_camera"] or "—",
            }
            for row in await self.gallery.people()
        ]

    async def _recent_crops(self) -> list[dict[str, str]]:
        assert self.ctx is not None
        cur = await self.ctx.db.execute(
            "SELECT media_path FROM sightings WHERE media_path != ''"
            " ORDER BY ts DESC LIMIT 12"
        )
        return [
            {"url": f"/media/{r['media_path']}", "thumb": f"/media/{r['media_path']}"}
            for r in await cur.fetchall()
        ]

    async def _recent_log(self) -> list[dict[str, Any]]:
        assert self.ctx is not None
        cur = await self.ctx.db.execute(
            "SELECT ts, camera, device_id, identity, confidence, modality, kind"
            " FROM sightings ORDER BY ts DESC LIMIT 50"
        )
        return [
            {
                "when": _stamp(r["ts"]),
                "camera": r["camera"] or r["device_id"],
                "who": r["identity"] or "unknown",
                "confidence": round(r["confidence"], 2),
                "by": r["modality"],
                "kind": r["kind"],
            }
            for r in await cur.fetchall()
        ]

    def _throughput(self) -> dict[str, Any]:
        rows = self.pipeline.stats() if self.pipeline else []
        total = round(sum(row["fps"] for row in rows), 1)
        dropped = sum(row["dropped"] for row in rows)
        if not rows:
            return {"value": "—", "label": "no cameras in this process"}
        return {
            "value": total,
            "label": f"across {len(rows)} camera(s) · {dropped} frames dropped",
        }

    def _camera_rows(self) -> list[dict[str, Any]]:
        rows = self.pipeline.stats() if self.pipeline else []
        return [
            {
                "camera": row["camera"],
                "fps": row["fps"],
                "analysed": row["analysed"],
                "received": row["received"],
                "dropped": row["dropped"],
                "skipped": row["skipped"],
                "decode errors": row["decode_errors"],
                "model errors": row["analysis_errors"],
                "last frame": _stamp(row["last_frame_at"]) or "—",
            }
            for row in rows
        ]

    async def _model_status(self) -> dict[str, Any]:
        report = await asyncio.to_thread(model_store.status)
        busy = self._provision_task is not None and not self._provision_task.done()
        missing = [m["file"] for m in report["models"] if m["state"] != "ok"]
        if busy:
            state, detail = "busy", "; ".join(self._provision_log[-2:]) or "downloading…"
        elif not report["stack_installed"]:
            state = "blocked"
            detail = (
                "The recognition libraries are not installed. Run: uv pip install -e "
                "'plugins/blackice-plugin-watchers[models]', then press Download."
            )
        elif report["ready"]:
            state = "ready"
            detail = f"All models verified in {report['directory']}."
        else:
            state = "missing"
            detail = f"Not downloaded yet: {', '.join(missing)}."
        return {
            "label": "Download models" if state != "ready" else "Re-verify models",
            "command": "provision_models",
            "state": state,
            "detail": detail,
            "busy": busy,
            "confirm": (
                "This downloads several hundred megabytes of model weights from the "
                "pinned URLs. It is the only time this plugin uses the network."
            ),
            "models": [
                {"file": m["file"], "state": m["state"], "purpose": m["purpose"],
                 "sha256": m["sha256"][:16]}
                for m in report["models"]
            ],
        }

    # --- events ------------------------------------------------------------

    async def _on_decision(self, decision: Decision) -> None:
        """One resolved track becomes one event. Called from the worker task."""
        assert self.ctx is not None and self.gallery is not None
        track = decision.track
        identity = decision.identity

        new_camera = False
        if identity.person_id is not None:
            new_camera = not await self.gallery.seen_on_camera(
                identity.person_id, track.device_id
            )

        when = datetime.fromtimestamp(track.last_seen).astimezone()
        night = self.settings.is_night(when.hour)
        lingering = decision.kind == LINGERING
        severity = severity_for(
            known=identity.known, night=night, lingering=lingering, new_camera=new_camera
        )

        media: list[MediaRef] = []
        rel = ""
        if track.best_crop:
            rel = self._write_crop(track)
            media.append(
                MediaRef(
                    path=rel,
                    mime="image/jpeg",
                    bytes=len(track.best_crop),
                    sha256=hashlib.sha256(track.best_crop).hexdigest(),
                )
            )
            track.media_path = rel

        event_id = await self.ctx.emit(
            Event(
                sensor_id=PEOPLE_SENSOR,
                severity=severity,
                kind=decision.kind,
                summary=self._summary(decision, night, new_camera),
                # Everything the network or the camera supplied, and nothing a
                # model wrote. The device id is a number off the LAN.
                sensor_text=f"camera device id {track.device_id}",
                payload={
                    "device_id": track.device_id,
                    "camera": track.camera,
                    "track": track.key,
                    "track_id": track.track_id,
                    "identity": identity.name,
                    "person_id": identity.person_id,
                    "known": identity.known,
                    "confidence": round(identity.confidence, 3),
                    "modality": identity.modality,
                    "frames": track.frames,
                    "duration_s": round(track.duration, 1),
                    "night": night,
                    "lingering": lingering,
                    "new_camera_for_person": new_camera,
                    "linked_track": track.linked_to or None,
                    "linked_score": round(track.linked_score, 3) or None,
                    "closest_enrolled": decision.closest_name,
                    "closest_score": round(decision.closest_score, 3),
                    "url": f"/media/{rel}" if rel else None,
                },
                media=media,
            )
        )

        await self.gallery.record_sighting(
            ts=track.last_seen, device_id=track.device_id, camera=track.camera,
            track_id=track.track_id, person_id=identity.person_id,
            identity=identity.name, confidence=identity.confidence,
            modality=identity.modality, severity=severity, kind=decision.kind,
            media_path=rel, event_id=event_id,
        )

    def _summary(self, decision: Decision, night: bool, new_camera: bool) -> str:
        """Plugin-authored, always. The only variable text is an enrolled name,
        which the owner typed, and a camera label, which the owner wrote in the
        camera config file. An unlabelled camera is described rather than named,
        because its fallback label is a device id that came off the network."""
        track = decision.track
        where = self._camera_phrase(track)
        identity = decision.identity

        if decision.kind == LINGERING:
            return f"Unrecognised person still {where} after {track.duration:.0f}s"
        if not identity.known:
            hint = " at night" if night else ""
            if track.linked_to:
                return f"Unrecognised person {where}{hint}, last seen on another camera"
            return f"Unrecognised person {where}{hint}"
        if decision.kind == REVISION:
            return f"Now recognised as {identity.name} {where}"
        if new_camera:
            return f"Recognised {identity.name} {where} — the first time there"
        return f"Recognised {identity.name} {where}"

    @staticmethod
    def _camera_phrase(track: TrackRecord) -> str:
        """`at the front door` for a labelled camera; a generic phrase for one
        whose only name is its device id."""
        if track.camera and track.camera != track.device_id:
            return f"at {track.camera}"
        return "on an unlabelled camera"

    def _write_crop(self, track: TrackRecord) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(track.last_seen))
        rel = Path(MEDIA_SUBDIR) / track.device_id / f"{stamp}-{track.track_id}.jpg"
        target = settings_mod.media_root() / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(track.best_crop or b"")
        return str(rel)

    async def _on_expired(self, tracks: list[TrackRecord]) -> None:
        """Finished tracks are kept for an hour so they can be enrolled from."""
        assert self.gallery is not None
        for track in tracks:
            if not usable(track.best_face) and not usable(track.best_body):
                continue
            await self.gallery.remember_track(
                key=track.key, device_id=track.device_id, camera=track.camera,
                track_id=track.track_id, ts=track.last_seen,
                face=track.best_face, body=track.best_body,
                person_id=track.identity.person_id if track.identity else None,
                identity=track.identity.name if track.identity else None,
                media_path=track.media_path,
            )
        await self._report_degradation()

    async def _report_degradation(self) -> None:
        """Say once, on the timeline, when a camera stops keeping up — and say
        so again only when it recovers and slips a second time."""
        assert self.ctx is not None
        if self.pipeline is None:
            return
        struggling = {row["device_id"]: row for row in self.pipeline.struggling()}
        for device_id, row in struggling.items():
            if device_id in self._degraded_since:
                continue
            self._degraded_since[device_id] = time.time()
            await self.ctx.emit(
                Event(
                    sensor_id=PIPELINE_SENSOR,
                    severity=SEVERITY_LOW,
                    kind="throughput",
                    summary=(
                        f"Recognition is not keeping up on {row['camera']}: "
                        f"{row['fps']:.2f} frames per second reaching the models"
                    ),
                    sensor_text=f"camera device id {device_id}",
                    payload=row,
                )
            )
        for device_id in list(self._degraded_since):
            if device_id not in struggling:
                del self._degraded_since[device_id]

    # --- helpers -----------------------------------------------------------

    def _models_ready(self) -> bool:
        return bool(self.recognizer.info().get("loaded"))

    def _models_error(self) -> str:
        return (
            f"recognition is not available: {self.model_reason or 'the models are not loaded'}"
        )

    async def _resolve_camera(self, ref: Any, fleet: Any) -> tuple[str, str] | dict[str, str]:
        """A camera by label or device id. An error as data, never a raise."""
        assert self.ctx is not None
        if not isinstance(ref, str) or not ref.strip():
            return {"error": "which camera? Pass a label or a device id."}
        needle = ref.strip().casefold()

        if fleet is not None:
            for device_id, camera in getattr(fleet, "cameras", {}).items():
                label = getattr(camera, "label", device_id)
                if needle in (device_id.casefold(), str(label).casefold()):
                    return device_id, str(label)
            known = ", ".join(
                str(getattr(c, "label", i)) for i, c in fleet.cameras.items()
            ) or "none"
            return {"error": f"no camera called {ref!r}. Known cameras: {known}"}

        # No fleet in this process — the read-only side. The sightings we have
        # already recorded know which label goes with which device id, so a
        # question about the front door is still answerable here. Taking the
        # label as if it were a device id would silently match nothing.
        row = await (
            await self.ctx.db.execute(
                "SELECT device_id, camera FROM sightings"
                " WHERE lower(camera) = ? OR device_id = ? ORDER BY ts DESC LIMIT 1",
                (needle, ref.strip()),
            )
        ).fetchone()
        if row is not None:
            return row["device_id"], row["camera"] or row["device_id"]

        cur = await self.ctx.db.execute(
            "SELECT DISTINCT COALESCE(NULLIF(camera, ''), device_id) AS camera FROM sightings"
        )
        known = ", ".join(r["camera"] for r in await cur.fetchall()) or "none"
        return {
            "error": (
                f"no camera called {ref!r} has been seen here. "
                f"Cameras with sightings on record: {known}"
            )
        }


_NO_FLEET = (
    "the cameras are owned by another Black Ice process, so no picture can be taken here"
)


def severity_for(*, known: bool, night: bool, lingering: bool, new_camera: bool) -> int:
    """The agreed scheme, in one place so it can be tested on its own.

    A member of the household is unremarkable; the same person somewhere they
    have never been is worth a glance. A stranger is worth a glance by day and
    more at night, and a stranger who stays put is worth more again — standing
    at a door for a minute is a different act from walking past it.
    """
    if known:
        return SEVERITY_LOW if new_camera else SEVERITY_INFO
    if lingering:
        return SEVERITY_HIGH if night else SEVERITY_MEDIUM
    return SEVERITY_MEDIUM if night else SEVERITY_LOW


def _positive(value: Any, default: float) -> float | None:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _stamp(ts: Any) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None
