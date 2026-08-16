"""Who is enrolled, and everything the plugin remembers about sightings.

This is the file that holds biometric data about people in someone's house,
including visitors, so it is worth being explicit about what is and is not
here:

* `embeddings` holds L2-normalised float vectors and nothing else. No images.
* `sightings` holds what was decided and where the crop went, so the widgets
  and `who_was_seen` have something to answer from. The crop itself lives under
  the media root, attached to its event, and is pruned by the existing media
  retention rather than by this plugin.
* `recent_tracks` is a short-lived cache of the vectors for tracks seen in the
  last hour. It exists for two reasons: so `enrol_person(track=...)` has
  something to name, and so a faceless track on one camera can be matched to a
  track on another. It is pruned on every write.

`forget_person` deletes the person row; the embeddings and cached track vectors
go with it by foreign key, and the name is scrubbed from past sightings. That
is a real deletion of the biometrics, not a hidden flag.
"""

from __future__ import annotations

import time
from typing import Any

import aiosqlite
import numpy as np

from .embeddings import VectorIndex, from_blob, mean_vector, to_blob, usable

FACE = "face"
BODY = "body"

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    note       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS embeddings (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    modality   TEXT NOT NULL,
    vec        BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS embeddings_by_person ON embeddings (person_id, modality);

CREATE TABLE IF NOT EXISTS sightings (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,
    device_id  TEXT NOT NULL,
    camera     TEXT NOT NULL DEFAULT '',
    track_id   INTEGER NOT NULL,
    person_id  INTEGER REFERENCES people (id) ON DELETE SET NULL,
    identity   TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    modality   TEXT NOT NULL DEFAULT '',
    severity   INTEGER NOT NULL DEFAULT 0,
    kind       TEXT NOT NULL DEFAULT 'recognition',
    media_path TEXT NOT NULL DEFAULT '',
    event_id   INTEGER
);
CREATE INDEX IF NOT EXISTS sightings_by_time ON sightings (ts DESC);
CREATE INDEX IF NOT EXISTS sightings_by_camera ON sightings (device_id, ts DESC);
CREATE INDEX IF NOT EXISTS sightings_by_person ON sightings (person_id, ts DESC);

CREATE TABLE IF NOT EXISTS recent_tracks (
    track_key  TEXT PRIMARY KEY,
    device_id  TEXT NOT NULL,
    camera     TEXT NOT NULL DEFAULT '',
    track_id   INTEGER NOT NULL,
    ts         REAL NOT NULL,
    face       BLOB,
    body       BLOB,
    person_id  INTEGER REFERENCES people (id) ON DELETE SET NULL,
    identity   TEXT,
    media_path TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS recent_tracks_by_time ON recent_tracks (ts DESC);

CREATE TABLE IF NOT EXISTS prefs (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: How long a track's vectors stay available to `enrol_person(track=...)`.
#: An hour is long enough to notice an event and name the person in it, short
#: enough that the plugin is not quietly accumulating biometrics on strangers.
RECENT_TRACK_TTL = 3600.0


class Gallery:
    """The enrolled people, and the indexes used to match against them.

    Matching is synchronous and in memory: it runs once per analysed frame per
    person, and awaiting SQLite there would put database latency on the
    recognition path. The database is the durable copy; these indexes are
    rebuilt from it whenever it changes.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db
        self.faces = VectorIndex()
        self.bodies = VectorIndex()
        self.names: dict[int, str] = {}

    async def create_schema(self) -> None:
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    # --- indexes -----------------------------------------------------------

    async def reload(self) -> None:
        """Rebuild the in-memory indexes from the database."""
        cur = await self.db.execute("SELECT id, name FROM people")
        self.names = {r["id"]: r["name"] for r in await cur.fetchall()}

        cur = await self.db.execute("SELECT person_id, modality, vec FROM embeddings")
        face_entries: list[tuple[int, np.ndarray]] = []
        body_entries: list[tuple[int, np.ndarray]] = []
        for row in await cur.fetchall():
            target = face_entries if row["modality"] == FACE else body_entries
            target.append((row["person_id"], from_blob(row["vec"])))
        self.faces.build(face_entries)
        self.bodies.build(body_entries)

    def match(self, modality: str, vec: np.ndarray | None) -> tuple[int, str, float] | None:
        """Closest enrolled person for a vector, with their similarity.

        Threshold-free on purpose: the resolver owns the thresholds, and it
        wants the score even when it is below the bar so the event can report
        how close the call was.
        """
        if vec is None or not usable(vec):
            return None
        index = self.faces if modality == FACE else self.bodies
        hit = index.best(vec)
        if hit is None:
            return None
        person_id, score = hit
        return person_id, self.names.get(person_id, str(person_id)), score

    def name_of(self, person_id: int | None) -> str | None:
        return None if person_id is None else self.names.get(person_id)

    def id_of(self, name: str) -> int | None:
        needle = name.strip().casefold()
        for person_id, known in self.names.items():
            if known.casefold() == needle:
                return person_id
        return None

    # --- enrolment ---------------------------------------------------------

    async def enrol(
        self, name: str, vectors: list[tuple[str, np.ndarray]], source: str
    ) -> dict[str, Any]:
        """Add or extend a person.

        Several shots of one face collapse into one prototype vector rather
        than N rows: averaging normalised embeddings is what makes enrolment
        from a handful of photos better than enrolment from the best one.
        """
        clean = name.strip()
        cur = await self.db.execute(
            "INSERT INTO people (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (clean,)
        )
        _ = cur
        row = await (
            await self.db.execute("SELECT id FROM people WHERE name = ?", (clean,))
        ).fetchone()
        person_id = int(row["id"])

        added = {FACE: 0, BODY: 0}
        for modality in (FACE, BODY):
            prototype = mean_vector([v for m, v in vectors if m == modality])
            if prototype is None:
                continue
            await self.db.execute(
                "INSERT INTO embeddings (person_id, modality, vec, dim, source)"
                " VALUES (?, ?, ?, ?, ?)",
                (person_id, modality, to_blob(prototype), int(prototype.size), source),
            )
            added[modality] += 1
        await self.db.commit()
        await self.reload()
        return {"person_id": person_id, "name": clean, "added": added}

    async def rename(self, old: str, new: str) -> dict[str, Any] | None:
        person_id = self.id_of(old)
        if person_id is None:
            return None
        await self.db.execute(
            "UPDATE people SET name = ? WHERE id = ?", (new.strip(), person_id)
        )
        # Past sightings carry the name as text so the widgets do not need a
        # join; renaming has to reach them or the log keeps saying the old name.
        await self.db.execute(
            "UPDATE sightings SET identity = ? WHERE person_id = ?", (new.strip(), person_id)
        )
        await self.db.execute(
            "UPDATE recent_tracks SET identity = ? WHERE person_id = ?",
            (new.strip(), person_id),
        )
        await self.db.commit()
        await self.reload()
        return {"person_id": person_id, "name": new.strip()}

    async def forget(self, name: str) -> dict[str, Any] | None:
        """Delete a person and their biometrics.

        The embeddings go by cascade, and the name is scrubbed from this
        plugin's own history. Events already on the timeline keep whatever they
        said at the time — this plugin cannot rewrite core tables, and the
        caller is told so.
        """
        person_id = self.id_of(name)
        if person_id is None:
            return None

        row = await (
            await self.db.execute(
                "SELECT count(*) AS n FROM embeddings WHERE person_id = ?", (person_id,)
            )
        ).fetchone()
        embeddings = int(row["n"]) if row else 0
        row = await (
            await self.db.execute(
                "SELECT count(*) AS n FROM sightings WHERE person_id = ?", (person_id,)
            )
        ).fetchone()
        sightings = int(row["n"]) if row else 0

        await self.db.execute("UPDATE sightings SET identity = NULL WHERE person_id = ?",
                              (person_id,))
        await self.db.execute("UPDATE recent_tracks SET identity = NULL WHERE person_id = ?",
                              (person_id,))
        await self.db.execute("DELETE FROM embeddings WHERE person_id = ?", (person_id,))
        await self.db.execute("DELETE FROM people WHERE id = ?", (person_id,))
        await self.db.commit()
        await self.reload()
        return {
            "person_id": person_id,
            "embeddings_deleted": embeddings,
            "sightings_scrubbed": sightings,
        }

    async def people(self) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            """SELECT p.id, p.name, p.created_at,
                      sum(e.modality = 'face') AS faces,
                      sum(e.modality = 'body') AS bodies
               FROM people p LEFT JOIN embeddings e ON e.person_id = p.id
               GROUP BY p.id ORDER BY p.name COLLATE NOCASE"""
        )
        rows = [dict(r) for r in await cur.fetchall()]
        for row in rows:
            last = await (
                await self.db.execute(
                    "SELECT ts, camera FROM sightings WHERE person_id = ?"
                    " ORDER BY ts DESC LIMIT 1",
                    (row["id"],),
                )
            ).fetchone()
            row["faces"] = int(row["faces"] or 0)
            row["bodies"] = int(row["bodies"] or 0)
            row["last_seen"] = last["ts"] if last else None
            row["last_camera"] = last["camera"] if last else None
        return rows

    # --- sightings ---------------------------------------------------------

    async def record_sighting(self, **fields: Any) -> int:
        cur = await self.db.execute(
            """INSERT INTO sightings
                 (ts, device_id, camera, track_id, person_id, identity, confidence,
                  modality, severity, kind, media_path, event_id)
               VALUES (:ts, :device_id, :camera, :track_id, :person_id, :identity,
                       :confidence, :modality, :severity, :kind, :media_path, :event_id)""",
            {
                "ts": fields.get("ts", time.time()),
                "device_id": fields.get("device_id", ""),
                "camera": fields.get("camera", ""),
                "track_id": int(fields.get("track_id", 0)),
                "person_id": fields.get("person_id"),
                "identity": fields.get("identity"),
                "confidence": float(fields.get("confidence", 0.0)),
                "modality": fields.get("modality", ""),
                "severity": int(fields.get("severity", 0)),
                "kind": fields.get("kind", "recognition"),
                "media_path": fields.get("media_path", ""),
                "event_id": fields.get("event_id"),
            },
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def seen_on_camera(self, person_id: int, device_id: str) -> bool:
        """Whether this person has ever been resolved on this camera before.

        Drives the "somewhere they have never been" severity bump, so it is
        asked before the current sighting is written.
        """
        row = await (
            await self.db.execute(
                "SELECT 1 FROM sightings WHERE person_id = ? AND device_id = ? LIMIT 1",
                (person_id, device_id),
            )
        ).fetchone()
        return row is not None

    # --- the recent-track cache -------------------------------------------

    async def remember_track(self, **fields: Any) -> None:
        now = float(fields.get("ts", time.time()))
        face = fields.get("face")
        body = fields.get("body")
        await self.db.execute(
            """INSERT INTO recent_tracks
                 (track_key, device_id, camera, track_id, ts, face, body,
                  person_id, identity, media_path)
               VALUES (:key, :device_id, :camera, :track_id, :ts, :face, :body,
                       :person_id, :identity, :media_path)
               ON CONFLICT(track_key) DO UPDATE SET
                 ts=excluded.ts,
                 face=COALESCE(excluded.face, recent_tracks.face),
                 body=COALESCE(excluded.body, recent_tracks.body),
                 person_id=excluded.person_id, identity=excluded.identity,
                 media_path=excluded.media_path""",
            {
                "key": fields["key"],
                "device_id": fields.get("device_id", ""),
                "camera": fields.get("camera", ""),
                "track_id": int(fields.get("track_id", 0)),
                "ts": now,
                "face": to_blob(face) if usable(face) else None,
                "body": to_blob(body) if usable(body) else None,
                "person_id": fields.get("person_id"),
                "identity": fields.get("identity"),
                "media_path": fields.get("media_path", ""),
            },
        )
        await self.db.execute(
            "DELETE FROM recent_tracks WHERE ts < ?", (now - RECENT_TRACK_TTL,)
        )
        await self.db.commit()

    async def recent_tracks(self, since: float, exclude_device: str = "") -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT * FROM recent_tracks WHERE ts >= ? AND device_id != ? ORDER BY ts DESC",
            (since, exclude_device),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def track(self, key: str) -> dict[str, Any] | None:
        row = await (
            await self.db.execute("SELECT * FROM recent_tracks WHERE track_key = ?", (key,))
        ).fetchone()
        return dict(row) if row else None

    # --- preferences -------------------------------------------------------

    async def get_pref(self, key: str) -> str | None:
        row = await (
            await self.db.execute("SELECT value FROM prefs WHERE key = ?", (key,))
        ).fetchone()
        return row["value"] if row else None

    async def set_pref(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO prefs (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()
