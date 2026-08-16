"""Reminder storage and scheduling.

Kept apart from the plugin class so the three fiddly parts can be read and
tested on their own: turning a spoken time into an instant, rolling a repeat
forward without the wall-clock time drifting across a DST boundary, and
claiming a due reminder exactly once when two processes share the file.

Times are stored as UTC in SQLite's own `YYYY-MM-DD HH:MM:SS` text format, so
they sort correctly and compare directly against `datetime('now')`. The
timezone a reminder was set in is stored beside it, because "every day at 7am"
means 7am where the owner lives, not a fixed number of hours.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, time, timedelta
from typing import Any

from .reading import spoken_time, zone

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    reason TEXT NOT NULL,
    due_at TEXT NOT NULL,               -- UTC, 'YYYY-MM-DD HH:MM:SS'
    timezone TEXT NOT NULL DEFAULT '',  -- IANA name; '' means machine local
    repeat TEXT,                        -- NULL | daily | weekdays | weekly | monthly
    status TEXT NOT NULL DEFAULT 'scheduled',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    claimed_at TEXT,
    last_fired_at TEXT,
    -- When it reached a terminal state. A cancelled reminder is as old as its
    -- cancellation, not as old as the future date it was pointing at.
    finished_at TEXT,
    occurrences INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders (status, due_at);
"""

REPEATS = ("daily", "weekdays", "weekly", "monthly")
STATUSES = ("scheduled", "fired", "missed", "cancelled")
CLAIMED = "firing"  # transient: one process owns this row right now

SQL_TIME = "%Y-%m-%d %H:%M:%S"

#: How long after its due time a reminder may still be announced. Anything
#: older was missed while the service was down, and shouting it hours late is
#: worse than not shouting it at all.
DEFAULT_GRACE_MINUTES = 60

#: A process that dies mid-fire leaves its row claimed. Reclaim after this.
STUCK_SECONDS = 300


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_sql(when: datetime) -> str:
    return when.astimezone(UTC).strftime(SQL_TIME)


def from_sql(text: str) -> datetime:
    return datetime.strptime(text, SQL_TIME).replace(tzinfo=UTC)


def parse_when(text: str, tz: Any, now: datetime | None = None) -> datetime:
    """Read a time the assistant supplied. Returns an aware UTC datetime.

    Accepts a full ISO 8601 datetime, a bare date, or a bare `HH:MM` — which
    means the next time that clock time comes round, since "remind me at seven"
    never means seven o'clock this morning when it is already noon.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("a time is required, for example '2026-08-17T07:00' or '07:00'")

    local_now = (now or utc_now()).astimezone(tz)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            clock = time.fromisoformat(raw)
        except ValueError:
            raise ValueError(
                f"could not read {text!r} as a time; use ISO 8601 like"
                " '2026-08-17T07:00', or 'HH:MM' for the next time it comes round"
            ) from None
        parsed = datetime.combine(local_now.date(), clock)
        if parsed <= local_now.replace(tzinfo=None):
            parsed += timedelta(days=1)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


def next_occurrence(when: datetime, repeat: str, tz: Any) -> datetime:
    """The occurrence after `when`, keeping the same local wall-clock time.

    The arithmetic is done on the naive local time and then re-localised, so a
    daily 07:00 reminder stays at 07:00 through a DST change instead of
    sliding to 06:00 or 08:00.
    """
    local = when.astimezone(tz).replace(tzinfo=None)

    if repeat == "daily":
        local += timedelta(days=1)
    elif repeat == "weekly":
        local += timedelta(days=7)
    elif repeat == "weekdays":
        local += timedelta(days=1)
        while local.weekday() >= 5:  # Saturday, Sunday
            local += timedelta(days=1)
    elif repeat == "monthly":
        month = local.month + 1
        year = local.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        # The 31st of a 30-day month becomes the 30th, and stays there.
        day = min(local.day, calendar.monthrange(year, month)[1])
        local = local.replace(year=year, month=month, day=day)
    else:
        raise ValueError(f"repeat must be one of {', '.join(REPEATS)}")

    return local.replace(tzinfo=tz).astimezone(UTC)


def advance_past(when: datetime, repeat: str, tz: Any, now: datetime) -> datetime:
    """Roll a repeat forward until it is in the future.

    A daily reminder that went unheard for a week should ring tomorrow morning,
    not seven times at startup.
    """
    for _ in range(1000):
        when = next_occurrence(when, repeat, tz)
        if when > now:
            return when
    raise ValueError(f"could not find a future occurrence for repeat {repeat!r}")


class ReminderStore:
    """Every reminder operation, over one plugin-private connection."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def setup(self) -> None:
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    # --- reads -------------------------------------------------------------

    async def get(self, reminder_id: int) -> dict[str, Any] | None:
        cur = await self.db.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list(self, status: str | None = "scheduled", limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM reminders"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY due_at LIMIT ?"
        params.append(limit)
        cur = await self.db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]

    # --- writes ------------------------------------------------------------

    async def create(
        self, reason: str, at: str, repeat: str | None = None,
        timezone: str | None = None, now: datetime | None = None,
    ) -> dict[str, Any]:
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("a reminder needs a reason — what should I remind you of?")
        if repeat and repeat not in REPEATS:
            raise ValueError(f"repeat must be one of {', '.join(REPEATS)}")

        tz = zone(timezone)  # raises on an unknown name
        now = now or utc_now()
        due = parse_when(at, tz, now)
        if due <= now - timedelta(minutes=1):
            raise ValueError(
                f"{describe_local(due, tz)} is in the past — did you mean a later time?"
            )

        cur = await self.db.execute(
            """INSERT INTO reminders (reason, due_at, timezone, repeat)
               VALUES (?, ?, ?, ?)""",
            (reason, to_sql(due), (timezone or "").strip(), repeat),
        )
        await self.db.commit()
        return await self.get(cur.lastrowid)

    async def edit(
        self, reminder_id: int, reason: str | None = None, at: str | None = None,
        repeat: str | None = None, status: str | None = None,
        timezone: str | None = None, now: datetime | None = None,
    ) -> dict[str, Any]:
        row = await self.get(reminder_id)
        if row is None:
            raise ValueError(f"no reminder with id {reminder_id}")

        sets: dict[str, Any] = {}
        if reason is not None:
            if not reason.strip():
                raise ValueError("a reminder needs a reason")
            sets["reason"] = reason.strip()
        if timezone is not None:
            zone(timezone)  # validate before storing
            sets["timezone"] = timezone.strip()
        if repeat is not None:
            # "none" is how the assistant says "stop repeating this".
            if repeat in ("", "none", "never"):
                sets["repeat"] = None
            elif repeat in REPEATS:
                sets["repeat"] = repeat
            else:
                raise ValueError(f"repeat must be one of {', '.join(REPEATS)}, or 'none'")
        if status is not None:
            if status not in STATUSES:
                raise ValueError(f"status must be one of {', '.join(STATUSES)}")
            sets["status"] = status
        if at is not None:
            tz = zone(sets.get("timezone", row["timezone"]))
            now = now or utc_now()
            due = parse_when(at, tz, now)
            if due <= now - timedelta(minutes=1):
                raise ValueError(
                    f"{describe_local(due, tz)} is in the past — did you mean a later time?"
                )
            sets["due_at"] = to_sql(due)
            # Rescheduling a reminder that already went off revives it.
            sets.setdefault("status", "scheduled")

        if not sets:
            raise ValueError("nothing to change — say what to alter")

        assignments = ", ".join(f"{k} = ?" for k in sets)
        await self.db.execute(
            f"UPDATE reminders SET {assignments} WHERE id = ?",
            (*sets.values(), reminder_id),
        )
        await self.db.commit()
        return await self.get(reminder_id)

    async def cancel(self, reminder_id: int) -> dict[str, Any]:
        """Cancelling keeps the row, so "no, put it back" is one edit away."""
        row = await self.get(reminder_id)
        if row is None:
            raise ValueError(f"no reminder with id {reminder_id}")
        if row["status"] == "cancelled":
            return row
        await self.db.execute(
            "UPDATE reminders SET status = 'cancelled', finished_at = ? WHERE id = ?",
            (to_sql(utc_now()), reminder_id),
        )
        await self.db.commit()
        return await self.get(reminder_id)

    async def purge(self, older_than_days: int = 30, now: datetime | None = None) -> int:
        """Delete finished reminders for good. Never touches a scheduled one."""
        if older_than_days < 0:
            raise ValueError("older_than_days cannot be negative")
        cutoff = (now or utc_now()) - timedelta(days=older_than_days)
        cur = await self.db.execute(
            """DELETE FROM reminders
                WHERE status IN ('cancelled', 'fired', 'missed')
                  AND finished_at IS NOT NULL AND finished_at <= ?""",
            (to_sql(cutoff),),
        )
        await self.db.commit()
        return cur.rowcount

    # --- scheduling --------------------------------------------------------

    async def due(self, now: datetime, grace_minutes: int) -> list[dict[str, Any]]:
        """Scheduled reminders whose time has come and is still worth saying."""
        cur = await self.db.execute(
            """SELECT * FROM reminders
                WHERE status = 'scheduled' AND due_at <= ? AND due_at > ?
                ORDER BY due_at""",
            (to_sql(now), to_sql(now - timedelta(minutes=grace_minutes))),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def stale(self, now: datetime, grace_minutes: int) -> list[dict[str, Any]]:
        """Scheduled reminders that went past their grace window unheard."""
        cur = await self.db.execute(
            """SELECT * FROM reminders
                WHERE status = 'scheduled' AND due_at <= ?
                ORDER BY due_at""",
            (to_sql(now - timedelta(minutes=grace_minutes)),),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def claim(self, reminder_id: int, now: datetime) -> bool:
        """Take ownership of a due reminder. True if we got it, False if not.

        `blackice serve` and `blackice voice` each run their own scheduler over
        this same file, so this compare-and-set is what stops a reminder being
        announced twice. SQLite makes the UPDATE atomic; the loser sees zero
        rows changed and moves on.
        """
        cur = await self.db.execute(
            """UPDATE reminders SET status = ?, claimed_at = ?
                WHERE id = ? AND status = 'scheduled'""",
            (CLAIMED, to_sql(now), reminder_id),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def finish(self, row: dict[str, Any], now: datetime) -> dict[str, Any]:
        """Retire a fired reminder, or set the repeat's next occurrence."""
        if row["repeat"]:
            tz = zone(row["timezone"])
            due = advance_past(from_sql(row["due_at"]), row["repeat"], tz, now)
            await self.db.execute(
                """UPDATE reminders
                      SET status = 'scheduled', due_at = ?, last_fired_at = ?,
                          claimed_at = NULL, occurrences = occurrences + 1
                    WHERE id = ?""",
                (to_sql(due), to_sql(now), row["id"]),
            )
        else:
            await self.db.execute(
                """UPDATE reminders
                      SET status = 'fired', last_fired_at = ?, finished_at = ?,
                          claimed_at = NULL, occurrences = occurrences + 1
                    WHERE id = ?""",
                (to_sql(now), to_sql(now), row["id"]),
            )
        await self.db.commit()
        return await self.get(row["id"])

    async def miss(self, row: dict[str, Any], now: datetime) -> dict[str, Any]:
        """Give up on an occurrence nobody heard.

        A repeating reminder survives it — three days of downtime should not
        end a daily alarm, only skip the mornings that already went by.
        """
        if row["repeat"]:
            tz = zone(row["timezone"])
            due = advance_past(from_sql(row["due_at"]), row["repeat"], tz, now)
            # Back to scheduled: the sweep claimed this row, and a claim that is
            # not released is a reminder that never rings again.
            await self.db.execute(
                """UPDATE reminders SET due_at = ?, status = 'scheduled',
                          claimed_at = NULL
                    WHERE id = ?""",
                (to_sql(due), row["id"]),
            )
        else:
            await self.db.execute(
                """UPDATE reminders SET status = 'missed', finished_at = ?,
                          claimed_at = NULL
                    WHERE id = ?""",
                (to_sql(now), row["id"]),
            )
        await self.db.commit()
        return await self.get(row["id"])

    async def recover_stuck(self, now: datetime) -> int:
        """Release rows claimed by a process that died before firing them."""
        cur = await self.db.execute(
            """UPDATE reminders SET status = 'scheduled', claimed_at = NULL
                WHERE status = ? AND claimed_at < ?""",
            (CLAIMED, to_sql(now - timedelta(seconds=STUCK_SECONDS))),
        )
        await self.db.commit()
        return cur.rowcount


# --- how a reminder is read back -------------------------------------------

def describe_local(when: datetime, tz: Any) -> str:
    """"Monday, August 17 at 7:00 AM" — a phrase, not a timestamp."""
    local = when.astimezone(tz)
    return f"{local.strftime('%A')}, {local.strftime('%B')} {local.day} at {spoken_time(local)}"


def describe(row: dict[str, Any]) -> dict[str, Any]:
    """A reminder as the assistant should read it out and the dashboard shows it."""
    tz = zone(row["timezone"])
    due = from_sql(row["due_at"])
    local = due.astimezone(tz)
    every = f", repeating {row['repeat']}" if row["repeat"] else ""
    return {
        "id": row["id"],
        "reason": row["reason"],
        "when": describe_local(due, tz),
        "due_at": local.isoformat(timespec="minutes"),
        "time": spoken_time(local),
        "date": local.strftime("%Y-%m-%d"),
        "repeat": row["repeat"],
        "status": row["status"],
        "timezone": row["timezone"] or str(local.tzname() or ""),
        "spoken": f"{row['reason']}, {describe_local(due, tz)}{every}",
    }
