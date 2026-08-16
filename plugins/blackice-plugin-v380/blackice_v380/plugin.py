"""The Black Ice plugin: every V380 camera on the LAN, as one sensor.

One sensor rather than one per camera, because `Registry` mirrors descriptors
into the core tables once at start and cameras appear later — a per-camera
descriptor would only ever project the ones that happened to answer the first
broadcast. Events therefore carry the camera in `payload.device_id`, and the
table widget is the per-camera view.

Anything the cameras themselves supply — device ids, MAC addresses, the
hostname in a discovery reply — is attacker-influenceable: anyone on the LAN
can answer a discovery broadcast. That text goes in `Event.sensor_text`, never
in `summary`. Labels are different: they come from the owner's config file, so
they are plugin-authored and safe to compose into a summary.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from pathlib import Path
from typing import Any

from blackice.models import (
    SEVERITY_INFO,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    AlarmRuleSpec,
    Event,
    MediaRef,
    SensorDescriptor,
    StreamDescriptor,
    ToolSpec,
    WidgetSpec,
)
from blackice.plugins.base import PluginContext, SensorPlugin

from . import audio as v380_audio
from . import settings as settings_mod
from . import talkback
from .fleet import CameraState, Fleet, StateChange
from .protocol import IMAGE_MODES, LIGHT_MODES, PTZ_DIRECTIONS
from .singleton import ProcessLock

log = logging.getLogger("blackice.plugin.v380")

SENSOR_ID = "v380.cameras"
MEDIA_SUBDIR = "v380"
#: How long the intercom stays open when the caller does not say.
DEFAULT_INTERCOM_SECONDS = 15.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    device_id TEXT PRIMARY KEY,
    mac       TEXT NOT NULL DEFAULT '',
    label     TEXT NOT NULL DEFAULT '',
    ip        TEXT NOT NULL DEFAULT '',
    state     TEXT NOT NULL DEFAULT 'unconfigured',
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS snapshots (
    id        INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL,
    ts        TEXT NOT NULL DEFAULT (datetime('now')),
    path      TEXT NOT NULL,
    bytes     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS snapshots_by_camera ON snapshots (device_id, id DESC);

CREATE TABLE IF NOT EXISTS state_changes (
    id        INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL,
    ts        TEXT NOT NULL DEFAULT (datetime('now')),
    previous  TEXT NOT NULL,
    current   TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT ''
);
"""

CAMERA_PARAM = {
    "type": "string",
    "description": (
        "Which camera: its label if it has one (e.g. 'Front door'), otherwise its "
        "device id. Call list_cameras first if you are not sure what is available."
    ),
}

#: Severity by state. A camera going dark is worth waking someone for; a new
#: one appearing on the network is worth noticing but is usually the owner
#: plugging something in.
_STATE_SEVERITY = {
    CameraState.ONLINE: SEVERITY_INFO,
    CameraState.OFFLINE: SEVERITY_MEDIUM,
    CameraState.ERROR: SEVERITY_MEDIUM,
    CameraState.CONNECTING: SEVERITY_INFO,
    CameraState.UNCONFIGURED: SEVERITY_LOW,
}


class V380Plugin(SensorPlugin):
    name = "v380"
    version = "0.1.0"

    def __init__(self) -> None:
        self.ctx: PluginContext | None = None
        self.settings = settings_mod.load()
        self.fleet: Fleet | None = None
        self.lock = ProcessLock("v380")
        self.active = False
        self._pending: asyncio.Queue[StateChange] | None = None
        self._drain_task: asyncio.Task | None = None

    # --- lifecycle ---------------------------------------------------------

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        await ctx.db.executescript(SCHEMA)
        await ctx.db.commit()

        # `blackice serve` and `blackice voice` are separate processes that
        # both load every plugin. Two of them streaming from the same cameras
        # would double the network and CPU cost and fight over the RTSP port,
        # so only the first to take the lock runs sessions; the other serves
        # the dashboard from what is already in the database.
        self.active = self.lock.acquire()
        if not self.active:
            log.info("another Black Ice process owns the cameras; running read-only")
            return

        self._pending = asyncio.Queue()
        self.fleet = Fleet(self.settings, on_state_change=self._queue_state_change)
        self._drain_task = asyncio.create_task(self._drain_loop(), name="v380-events")
        await self.fleet.start()

    async def stop(self) -> None:
        if self._drain_task is not None:
            self._drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain_task
            self._drain_task = None

        if self.fleet is not None:
            await self.fleet.stop()
            self.fleet = None

        self.lock.release()
        self.active = False

    def describe(self) -> list[SensorDescriptor]:
        return [
            SensorDescriptor(
                id=SENSOR_ID,
                name="V380 cameras",
                kind="camera",
                streams=self._streams(),
                widgets=[
                    WidgetSpec(type="stat", title="Cameras online",
                               data_source="online_count", span=3),
                    WidgetSpec(type="status", title="Fleet",
                               data_source="fleet_status", span=3),
                    WidgetSpec(type="image", title="Latest view",
                               data_source="latest_snapshot", span=6),
                    WidgetSpec(type="table", title="Cameras",
                               data_source="cameras", span=12),
                    WidgetSpec(type="gallery", title="Recent snapshots",
                               data_source="snapshot_gallery", span=6),
                    WidgetSpec(type="log", title="Camera state changes",
                               data_source="recent_changes", span=6),
                ],
                alarm_rules=[
                    AlarmRuleSpec(
                        key="camera_offline",
                        name="Camera stopped streaming",
                        description=(
                            "A camera that was online stopped sending frames for more "
                            f"than {self.settings.offline_after:.0f}s."
                        ),
                        default_armed=True,
                    ),
                    AlarmRuleSpec(
                        key="new_camera",
                        name="Unknown camera on the network",
                        description=(
                            "A V380 camera answered a discovery broadcast that this "
                            "system had not seen before."
                        ),
                        default_armed=True,
                    ),
                ],
                tools=[
                    ToolSpec(
                        name="list_cameras",
                        description=(
                            "List every V380 camera known on the local network, with "
                            "its label, address, whether it is currently streaming, "
                            "and whether it has usable credentials."
                        ),
                        parameters={"type": "object", "properties": {}},
                    ),
                    ToolSpec(
                        name="rescan",
                        description=(
                            "Broadcast for V380 cameras on the LAN right now instead of "
                            "waiting for the next scheduled scan. Use when a camera has "
                            "just been plugged in or has changed address."
                        ),
                        parameters={"type": "object", "properties": {}},
                    ),
                    ToolSpec(
                        name="get_snapshot",
                        description=(
                            "Capture a still image from a camera and attach it to the "
                            "timeline. Returns a URL for the image. This is how you look "
                            "at what a camera can currently see."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {"camera": CAMERA_PARAM},
                            "required": ["camera"],
                        },
                    ),
                    ToolSpec(
                        name="ptz",
                        description=(
                            "Physically move a camera that supports pan and tilt. The "
                            "camera keeps moving until it is sent 'stop', so follow a "
                            "movement with a stop once it is pointed where you want."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {
                                "camera": CAMERA_PARAM,
                                "direction": {
                                    "type": "string",
                                    "enum": list(PTZ_DIRECTIONS),
                                    "description": "Which way to move, or 'stop' to halt.",
                                },
                            },
                            "required": ["camera", "direction"],
                        },
                    ),
                    ToolSpec(
                        name="set_light",
                        description=(
                            "Switch a camera's illuminator on or off, or return it to "
                            "automatic. This turns on a visible light in the room the "
                            "camera is watching."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {
                                "camera": CAMERA_PARAM,
                                "mode": {
                                    "type": "string",
                                    "enum": list(LIGHT_MODES),
                                    "description": (
                                        "'on' and 'off' force the light; 'auto' lets the "
                                        "camera decide by ambient light."
                                    ),
                                },
                            },
                            "required": ["camera", "mode"],
                        },
                    ),
                    ToolSpec(
                        name="speak",
                        description=(
                            "Say something out loud through a camera's speaker, in the "
                            "assistant's voice. Anyone near that camera hears it. Use "
                            "it to talk to a person the camera can see — a visitor at "
                            "the door, someone in the room — not to talk to the owner, "
                            "who is spoken to through the console."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {
                                "camera": CAMERA_PARAM,
                                "text": {
                                    "type": "string",
                                    "description": (
                                        "What to say. Keep it short: it is played "
                                        "through a small speaker to someone who is "
                                        "probably outdoors."
                                    ),
                                },
                            },
                            "required": ["camera", "text"],
                        },
                    ),
                    ToolSpec(
                        name="play_sound",
                        description=(
                            "Play a prepared audio clip through a camera's speaker — a "
                            "siren, a doorbell, a recorded warning. Call with no clip "
                            "name to find out which clips exist."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {
                                "camera": CAMERA_PARAM,
                                "clip": {
                                    "type": "string",
                                    "description": (
                                        "File name of a clip in the camera audio "
                                        "directory. Omit to list what is available."
                                    ),
                                },
                            },
                            "required": ["camera"],
                        },
                    ),
                    ToolSpec(
                        name="intercom",
                        description=(
                            "Open the microphone on this machine and send it live to a "
                            "camera's speaker, so the owner can talk to whoever is "
                            "there. Stops on its own after the given number of seconds."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {
                                "camera": CAMERA_PARAM,
                                "seconds": {
                                    "type": "number",
                                    "description": (
                                        "How long to keep the microphone open. "
                                        f"Defaults to {DEFAULT_INTERCOM_SECONDS:.0f}; "
                                        "the maximum is 300."
                                    ),
                                },
                            },
                            "required": ["camera"],
                        },
                    ),
                    ToolSpec(
                        name="stop_speaking",
                        description=(
                            "Immediately silence a camera's speaker, cutting off "
                            "whatever it is playing."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {"camera": CAMERA_PARAM},
                            "required": ["camera"],
                        },
                    ),
                    ToolSpec(
                        name="set_image_mode",
                        description=(
                            "Change how a camera renders its picture: full colour, "
                            "black and white (better in near darkness), automatic, or "
                            "flip the image vertically for a ceiling-mounted camera."
                        ),
                        parameters={
                            "type": "object",
                            "properties": {
                                "camera": CAMERA_PARAM,
                                "mode": {
                                    "type": "string",
                                    "enum": list(IMAGE_MODES),
                                    "description": "'flip' toggles; the others are absolute.",
                                },
                            },
                            "required": ["camera", "mode"],
                        },
                    ),
                ],
            )
        ]

    def _streams(self) -> list[StreamDescriptor]:
        if self.fleet is None or self.fleet.rtsp is None:
            return []
        return [
            StreamDescriptor(
                kind="clip",
                url=self.fleet.rtsp_url(camera.device_id) or "",
                name=camera.label,
            )
            for camera in self.fleet.cameras.values()
        ]

    # --- commands ----------------------------------------------------------

    async def handle_command(self, cmd: str, **kwargs: Any) -> Any:
        match cmd:
            case "list_cameras":
                return await self._list_cameras()
            case "rescan":
                return await self._rescan()
            case "get_snapshot":
                return await self._get_snapshot(kwargs.get("camera"))
            case "ptz":
                return await self._actuate(
                    "ptz", kwargs.get("camera"), kwargs.get("direction"), PTZ_DIRECTIONS
                )
            case "set_light":
                return await self._actuate(
                    "light", kwargs.get("camera"), kwargs.get("mode"), LIGHT_MODES
                )
            case "set_image_mode":
                return await self._actuate(
                    "image", kwargs.get("camera"), kwargs.get("mode"), IMAGE_MODES
                )
            case "speak":
                return await self._speak(kwargs.get("camera"), kwargs.get("text"))
            case "play_sound":
                return await self._play_sound(kwargs.get("camera"), kwargs.get("clip"))
            case "intercom":
                return await self._intercom(kwargs.get("camera"), kwargs.get("seconds"))
            case "stop_speaking":
                return await self._stop_speaking(kwargs.get("camera"))
        return await super().handle_command(cmd, **kwargs)

    # --- talkback ----------------------------------------------------------

    async def _speak(self, camera_ref: Any, text: Any) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            return {"error": "nothing to say: pass the words to speak as 'text'"}

        camera = self._resolve(camera_ref)
        if isinstance(camera, dict):
            return camera

        try:
            pcm = await talkback.synthesize(text)
            await self.fleet.speak(camera.device_id, talkback.from_pcm(pcm))
        except talkback.TalkbackError as exc:
            return {"error": str(exc)}

        seconds = len(pcm) / (v380_audio.PCM_RATE * v380_audio.PCM_WIDTH)
        await self._record_talkback(camera, "speech", text, seconds)
        return {"ok": True, "camera": camera.label, "seconds": round(seconds, 1)}

    async def _play_sound(self, camera_ref: Any, clip: Any) -> dict[str, Any]:
        available = settings_mod.list_clips()
        if not isinstance(clip, str) or not clip.strip():
            return {
                "clips": available,
                "directory": str(settings_mod.audio_dir()),
                "note": "call again with one of these as 'clip'",
            }

        camera = self._resolve(camera_ref)
        if isinstance(camera, dict):
            return camera

        path = settings_mod.resolve_clip(clip)
        if path is None:
            return {
                "error": f"no clip called {clip!r}",
                "clips": available,
            }

        try:
            pcm = await talkback.decode_to_pcm(path)
            await self.fleet.speak(camera.device_id, talkback.from_pcm(pcm))
        except talkback.TalkbackError as exc:
            return {"error": str(exc)}

        seconds = len(pcm) / (v380_audio.PCM_RATE * v380_audio.PCM_WIDTH)
        await self._record_talkback(camera, "clip", path.name, seconds)
        return {"ok": True, "camera": camera.label, "clip": path.name,
                "seconds": round(seconds, 1)}

    async def _intercom(self, camera_ref: Any, seconds: Any) -> dict[str, Any]:
        duration = DEFAULT_INTERCOM_SECONDS
        if seconds is not None:
            try:
                duration = float(seconds)
            except (TypeError, ValueError):
                return {"error": f"{seconds!r} is not a number of seconds"}
        if duration <= 0:
            return {"error": "the microphone has to be open for a positive time"}
        duration = min(duration, talkback.MAX_SESSION_SECONDS)

        # Checked up front: the source is a generator, so a missing sounddevice
        # would otherwise fail inside the background task, after this tool had
        # already reported that the intercom was open.
        if not talkback.microphone_available():
            return {
                "error": (
                    "no microphone available on this machine "
                    "(uv pip install 'blackice[voice]')"
                )
            }

        camera = self._resolve(camera_ref)
        if isinstance(camera, dict):
            return camera

        try:
            await self.fleet.speak(camera.device_id, talkback.microphone(duration))
        except talkback.TalkbackError as exc:
            return {"error": str(exc)}

        await self._record_talkback(camera, "intercom", "live microphone", duration)
        return {"ok": True, "camera": camera.label, "seconds": duration}

    async def _stop_speaking(self, camera_ref: Any) -> dict[str, Any]:
        camera = self._resolve(camera_ref)
        if isinstance(camera, dict):
            return camera
        stopped = await self.fleet.stop_speaking(camera.device_id)
        return {"ok": True, "camera": camera.label, "was_speaking": stopped}

    async def _record_talkback(
        self, camera, kind: str, detail: str, seconds: float
    ) -> None:
        """Audio out of a camera is audible to whoever is standing there, so it
        goes on the timeline the same way a PTZ move does."""
        assert self.ctx is not None
        await self.ctx.emit(
            Event(
                sensor_id=SENSOR_ID,
                severity=SEVERITY_LOW,
                kind="talkback",
                summary=f"Played {kind} through {camera.label}'s speaker",
                payload={
                    "device_id": camera.device_id,
                    "mode": kind,
                    "detail": detail,
                    "seconds": round(seconds, 1),
                },
            )
        )

    async def _list_cameras(self) -> dict[str, Any]:
        if self.fleet is None:
            return {"cameras": await self._cameras_from_db(), "live": False}
        return {
            "cameras": self.fleet.summaries(),
            "live": True,
            "discovery_error": self.fleet.discovery_error,
        }

    async def _rescan(self) -> dict[str, Any]:
        if self.fleet is None:
            return self._inactive()
        found = await self.fleet.scan()
        await self._persist_cameras()
        return {"found": len(found), "cameras": self.fleet.summaries()}

    async def _get_snapshot(self, camera_ref: Any) -> dict[str, Any]:
        if self.fleet is None:
            return self._inactive()
        camera = self._resolve(camera_ref)
        if isinstance(camera, dict):
            return camera

        try:
            shot = await self.fleet.snapshot(camera.device_id, force=True)
        except KeyError:
            return {"error": f"camera {camera_ref!r} is no longer present"}

        if shot is None:
            return {
                "error": (
                    f"no image available from {camera.label}: "
                    f"{camera.detail or 'the camera is not streaming'}"
                )
            }

        rel = self._write_media(camera.device_id, shot.jpeg, shot.captured_at)
        await self.ctx.db.execute(
            "INSERT INTO snapshots (device_id, path, bytes) VALUES (?, ?, ?)",
            (camera.device_id, str(rel), len(shot.jpeg)),
        )
        await self.ctx.db.commit()

        event_id = await self.ctx.emit(
            Event(
                sensor_id=SENSOR_ID,
                severity=SEVERITY_INFO,
                kind="snapshot",
                summary=f"Snapshot captured from {camera.label}",
                payload={"device_id": camera.device_id, "url": f"/media/{rel}"},
                media=[
                    MediaRef(
                        path=str(rel),
                        mime="image/jpeg",
                        bytes=len(shot.jpeg),
                        sha256=hashlib.sha256(shot.jpeg).hexdigest(),
                    )
                ],
            )
        )
        return {
            "camera": camera.label,
            "url": f"/media/{rel}",
            "bytes": len(shot.jpeg),
            "event_id": event_id,
        }

    async def _actuate(
        self, group: str, camera_ref: Any, action: Any, allowed: tuple[str, ...]
    ) -> dict[str, Any]:
        """PTZ, light, and image commands all share this path.

        A rejected argument is caller error, returned as data — the model
        guessing 'left-ish' should not mark the plugin unhealthy.
        """
        if self.fleet is None:
            return self._inactive()
        if action not in allowed:
            return {"error": f"{action!r} is not one of: {', '.join(allowed)}"}

        camera = self._resolve(camera_ref)
        if isinstance(camera, dict):
            return camera

        try:
            sent = await self.fleet.control(camera.device_id, f"{group}_{action}")
        except KeyError:
            return {"error": f"camera {camera_ref!r} is no longer present"}

        if not sent:
            return {
                "error": (
                    f"{camera.label} is not connected "
                    f"({camera.detail or camera.state}), so it cannot be controlled"
                )
            }

        # Actuation is recorded whether or not anyone asked for an alarm: these
        # commands move hardware and switch on lights in someone's home, and
        # the timeline is where that is auditable.
        await self.ctx.emit(
            Event(
                sensor_id=SENSOR_ID,
                severity=SEVERITY_LOW,
                kind="control",
                summary=f"{group.upper()} {action} sent to {camera.label}",
                payload={
                    "device_id": camera.device_id,
                    "group": group,
                    "action": action,
                },
            )
        )
        return {"ok": True, "camera": camera.label, "group": group, "action": action}

    def _inactive(self) -> dict[str, str]:
        return {
            "error": (
                "the cameras are owned by another Black Ice process; "
                "camera control is only available there"
            )
        }

    def _resolve(self, ref: Any):
        """Find a camera by label or device id, or an error to return as data."""
        if self.fleet is None:
            return self._inactive()
        if not isinstance(ref, str) or not ref.strip():
            return {"error": "which camera? Pass a label or a device id."}

        needle = ref.strip().casefold()
        for camera in self.fleet.cameras.values():
            if needle in (camera.device_id.casefold(), camera.label.casefold()):
                return camera

        known = ", ".join(c.label for c in self.fleet.cameras.values()) or "none"
        return {"error": f"no camera called {ref!r}. Known cameras: {known}"}

    # --- widgets -----------------------------------------------------------

    async def query(self, source: str, **kwargs: Any) -> Any:
        assert self.ctx is not None
        match source:
            case "online_count":
                cameras = self.fleet.summaries() if self.fleet else []
                online = sum(1 for c in cameras if c["state"] == CameraState.ONLINE)
                return {"value": online, "label": f"of {len(cameras)}"}
            case "fleet_status":
                return {"state": self._fleet_state()}
            case "cameras":
                return await self._camera_rows()
            case "latest_snapshot":
                return await self._latest_snapshot()
            case "snapshot_gallery":
                return await self._gallery()
            case "recent_changes":
                cur = await self.ctx.db.execute(
                    "SELECT ts, device_id, previous, current, detail"
                    " FROM state_changes ORDER BY id DESC LIMIT 50"
                )
                return [dict(r) for r in await cur.fetchall()]
        return await super().query(source, **kwargs)

    def _fleet_state(self) -> str:
        if self.fleet is None:
            return "degraded" if self.active else "offline"
        cameras = list(self.fleet.cameras.values())
        if not cameras:
            return "offline"
        online = sum(1 for c in cameras if c.online)
        if online == len(cameras):
            return "healthy"
        return "degraded" if online else "unhealthy"

    async def _camera_rows(self) -> list[dict[str, Any]]:
        if self.fleet is None:
            return await self._cameras_from_db()
        return [
            {
                "camera": c["label"],
                "device id": c["device_id"],
                "address": c["ip"] or "—",
                "state": c["state"],
                "codec": c["codec"],
                "frames": c["frames"],
                "viewers": c["viewers"],
                "speaker": "playing" if c["speaking"] else "idle",
                # The live-view address, here rather than in the descriptor's
                # stream list: descriptors are projected into core once at
                # start, when no camera has been discovered yet.
                "stream": self.fleet.rtsp_url(c["device_id"]) or "—",
                "note": c["detail"],
            }
            for c in self.fleet.summaries()
        ]

    async def _cameras_from_db(self) -> list[dict[str, Any]]:
        assert self.ctx is not None
        cur = await self.ctx.db.execute(
            "SELECT label, device_id, ip, state, last_seen FROM cameras ORDER BY device_id"
        )
        return [
            {
                "camera": r["label"] or r["device_id"],
                "device id": r["device_id"],
                "address": r["ip"] or "—",
                "state": r["state"],
                "last seen": r["last_seen"],
            }
            for r in await cur.fetchall()
        ]

    async def _latest_snapshot(self) -> dict[str, str]:
        assert self.ctx is not None
        cur = await self.ctx.db.execute(
            "SELECT path FROM snapshots ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return {"url": f"/media/{row['path']}"} if row else {"url": ""}

    async def _gallery(self) -> list[dict[str, str]]:
        assert self.ctx is not None
        cur = await self.ctx.db.execute(
            "SELECT path FROM snapshots ORDER BY id DESC LIMIT 12"
        )
        return [
            {"url": f"/media/{r['path']}", "thumb": f"/media/{r['path']}"}
            for r in await cur.fetchall()
        ]

    # --- state changes -----------------------------------------------------

    def _queue_state_change(self, change: StateChange) -> None:
        """Called from the fleet, which runs on the frame path — so this only
        enqueues, and the async work happens in `_drain_loop`."""
        if self._pending is not None:
            self._pending.put_nowait(change)

    async def _drain_loop(self) -> None:
        assert self._pending is not None
        while True:
            change = await self._pending.get()
            try:
                await self._record_change(change)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("failed recording camera state change")

    async def _record_change(self, change: StateChange) -> None:
        assert self.ctx is not None
        await self.ctx.db.execute(
            "INSERT INTO state_changes (device_id, previous, current, detail)"
            " VALUES (?, ?, ?, ?)",
            (change.device_id, str(change.previous), str(change.current), change.detail),
        )
        await self._persist_cameras()

        first_sighting = change.previous is CameraState.UNCONFIGURED and (
            change.current is CameraState.CONNECTING
        )
        severity = _STATE_SEVERITY.get(change.current, SEVERITY_INFO)
        summary = f"Camera {change.label} is {change.current}"
        if change.detail:
            summary = f"{summary} — {change.detail}"

        await self.ctx.emit(
            Event(
                sensor_id=SENSOR_ID,
                severity=severity,
                kind="camera_state",
                summary=summary,
                # The device id came off the network, so it is untrusted text
                # even though it looks like a number.
                sensor_text=f"device id {change.device_id}",
                payload={
                    "device_id": change.device_id,
                    "previous": str(change.previous),
                    "state": str(change.current),
                    "detail": change.detail,
                    "first_sighting": first_sighting,
                },
            )
        )

    async def _persist_cameras(self) -> None:
        """Keep the camera list in SQLite so a read-only process, and the next
        boot, still know what exists."""
        assert self.ctx is not None
        if self.fleet is None:
            return
        for camera in self.fleet.cameras.values():
            await self.ctx.db.execute(
                """INSERT INTO cameras (device_id, mac, label, ip, state, last_seen)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(device_id) DO UPDATE SET
                     mac=excluded.mac, label=excluded.label, ip=excluded.ip,
                     state=excluded.state, last_seen=excluded.last_seen""",
                (
                    camera.device_id,
                    camera.config.mac,
                    camera.label,
                    camera.config.ip,
                    str(camera.state),
                ),
            )
        await self.ctx.db.commit()

    # --- media -------------------------------------------------------------

    def _write_media(self, device_id: str, jpeg: bytes, captured_at: float) -> Path:
        """Write a snapshot under the media root and return its relative path.

        Relative, because that is what `MediaRef.path` means and what
        `/media/{path}` resolves against.
        """
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(captured_at))
        rel = Path(MEDIA_SUBDIR) / device_id / f"{stamp}.jpg"
        target = settings_mod.media_root() / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(jpeg)
        return rel
