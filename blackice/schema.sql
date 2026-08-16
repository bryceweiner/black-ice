-- Core entities -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sensors (
    id          TEXT PRIMARY KEY,
    plugin      TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'generic',
    state       TEXT NOT NULL DEFAULT 'unknown',
    descriptor  TEXT NOT NULL DEFAULT '{}',
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT
);

CREATE TABLE IF NOT EXISTS sensor_groups (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    collapsed  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sensor_group_members (
    group_id  INTEGER NOT NULL REFERENCES sensor_groups(id) ON DELETE CASCADE,
    sensor_id TEXT    NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, sensor_id)
);

-- Events --------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    sensor_id  TEXT NOT NULL,
    plugin     TEXT NOT NULL,
    ts         TEXT NOT NULL DEFAULT (datetime('now')),
    severity   INTEGER NOT NULL DEFAULT 0,
    kind       TEXT NOT NULL DEFAULT 'generic',
    summary    TEXT NOT NULL DEFAULT '',
    -- Sensor-supplied free text. Attacker-influenceable: never eligible to
    -- become a durable memory fact. See blackice/memory/consolidate.py.
    sensor_text TEXT,
    payload    TEXT NOT NULL DEFAULT '{}',
    tier       TEXT,
    verdict    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_sensor ON events(sensor_id, ts DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    summary, sensor_text, content='events', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, summary, sensor_text)
    VALUES (new.id, new.summary, new.sensor_text);
END;

CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, summary, sensor_text)
    VALUES ('delete', old.id, old.summary, old.sensor_text);
END;

CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, summary, sensor_text)
    VALUES ('delete', old.id, old.summary, old.sensor_text);
    INSERT INTO events_fts(rowid, summary, sensor_text)
    VALUES (new.id, new.summary, new.sensor_text);
END;

CREATE TABLE IF NOT EXISTS event_media (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    mime        TEXT NOT NULL,
    sha256      TEXT,
    bytes       INTEGER,
    duration_ms INTEGER,
    pinned      INTEGER NOT NULL DEFAULT 0,
    pruned_at   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_media_event ON event_media(event_id);

-- Escalations ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS escalations (
    id               INTEGER PRIMARY KEY,
    event_id         INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    ts               TEXT NOT NULL DEFAULT (datetime('now')),
    threat_level     TEXT NOT NULL DEFAULT 'unknown',
    classification   TEXT NOT NULL DEFAULT '',
    reasoning        TEXT NOT NULL DEFAULT '',
    suggested_action TEXT NOT NULL DEFAULT '',
    model            TEXT,
    prompt_version   INTEGER,
    status           TEXT NOT NULL DEFAULT 'open',
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_escalations_ts ON escalations(ts DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS escalations_fts USING fts5(
    classification, reasoning, suggested_action,
    content='escalations', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS escalations_ai AFTER INSERT ON escalations BEGIN
    INSERT INTO escalations_fts(rowid, classification, reasoning, suggested_action)
    VALUES (new.id, new.classification, new.reasoning, new.suggested_action);
END;

CREATE TRIGGER IF NOT EXISTS escalations_ad AFTER DELETE ON escalations BEGIN
    INSERT INTO escalations_fts(escalations_fts, rowid, classification, reasoning, suggested_action)
    VALUES ('delete', old.id, old.classification, old.reasoning, old.suggested_action);
END;

CREATE TRIGGER IF NOT EXISTS escalations_au AFTER UPDATE ON escalations BEGIN
    INSERT INTO escalations_fts(escalations_fts, rowid, classification, reasoning, suggested_action)
    VALUES ('delete', old.id, old.classification, old.reasoning, old.suggested_action);
    INSERT INTO escalations_fts(rowid, classification, reasoning, suggested_action)
    VALUES (new.id, new.classification, new.reasoning, new.suggested_action);
END;

-- Alarms --------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS alarm_rules (
    id          INTEGER PRIMARY KEY,
    plugin      TEXT NOT NULL,
    key         TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sensor_id   TEXT,
    spec        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (plugin, key)
);

CREATE TABLE IF NOT EXISTS alarm_state (
    rule_id    INTEGER PRIMARY KEY REFERENCES alarm_rules(id) ON DELETE CASCADE,
    armed      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by TEXT NOT NULL DEFAULT 'system'
);

-- LLM / voice / guard logging ----------------------------------------------

CREATE TABLE IF NOT EXISTS llm_turns (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    channel     TEXT NOT NULL,
    role        TEXT NOT NULL,
    model       TEXT,
    content     TEXT,
    tool_name   TEXT,
    tool_args   TEXT,
    tool_result TEXT,
    latency_ms  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_llm_session ON llm_turns(session_id, id);

CREATE TABLE IF NOT EXISTS voice_turns (
    id             INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL DEFAULT (datetime('now')),
    raw_transcript TEXT,
    normalized     TEXT,
    guard_verdict  TEXT,
    guard_score    REAL,
    woke           INTEGER NOT NULL DEFAULT 0,
    reply          TEXT,
    session_id     TEXT
);

CREATE TABLE IF NOT EXISTS guard_log (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL DEFAULT (datetime('now')),
    channel   TEXT NOT NULL,
    trust     TEXT NOT NULL,
    score     REAL,
    verdict   TEXT NOT NULL,
    action    TEXT NOT NULL,
    raw_text  TEXT,
    norm_text TEXT
);

CREATE TABLE IF NOT EXISTS plugin_health (
    plugin     TEXT PRIMARY KEY,
    state      TEXT NOT NULL DEFAULT 'unknown',
    last_error TEXT,
    last_ok    TEXT,
    restarts   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Triage ---------------------------------------------------------------------

-- Tier-1 knobs, per sensor. The row with sensor_id '*' is the default.
-- These are what the RSI proposal layer tunes, which is why they live in the
-- database rather than in config.
CREATE TABLE IF NOT EXISTS triage_config (
    sensor_id           TEXT PRIMARY KEY,
    dedup_seconds       INTEGER NOT NULL DEFAULT 60,
    rate_limit_per_hour INTEGER NOT NULL DEFAULT 120,
    -- Floor of 1 keeps pure INFO chatter (heartbeats, keepalives) off the
    -- models. It is still recorded; it just does not cost inference.
    severity_floor      INTEGER NOT NULL DEFAULT 1,
    quiet_start         TEXT,
    quiet_end           TEXT,
    quiet_severity_floor INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by          TEXT NOT NULL DEFAULT 'system'
);

INSERT OR IGNORE INTO triage_config (sensor_id) VALUES ('*');

-- Memory and RSI ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memory_ops (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL DEFAULT (datetime('now')),
    op         TEXT NOT NULL,
    category   TEXT,
    key        TEXT,
    value      TEXT,
    source     TEXT,
    confidence REAL,
    fact_id    TEXT,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS verdicts (
    id            INTEGER PRIMARY KEY,
    escalation_id INTEGER NOT NULL REFERENCES escalations(id) ON DELETE CASCADE,
    ts            TEXT NOT NULL DEFAULT (datetime('now')),
    verdict       TEXT NOT NULL,
    note          TEXT,
    author        TEXT NOT NULL DEFAULT 'user'
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    parent_id  INTEGER REFERENCES prompt_versions(id),
    rationale  TEXT,
    author     TEXT NOT NULL DEFAULT 'human',
    active     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS rsi_proposals (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL DEFAULT (datetime('now')),
    kind       TEXT NOT NULL,
    target     TEXT NOT NULL,
    current    TEXT NOT NULL DEFAULT '{}',
    proposed   TEXT NOT NULL DEFAULT '{}',
    evidence   TEXT NOT NULL DEFAULT '{}',
    rationale  TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',
    decided_at TEXT,
    decided_by TEXT
);

CREATE TABLE IF NOT EXISTS regression_runs (
    id                  INTEGER PRIMARY KEY,
    ts                  TEXT NOT NULL DEFAULT (datetime('now')),
    candidate_id        INTEGER REFERENCES prompt_versions(id),
    incumbent_id        INTEGER REFERENCES prompt_versions(id),
    golden_set_size     INTEGER NOT NULL DEFAULT 0,
    candidate_score     REAL,
    incumbent_score     REAL,
    passed              INTEGER NOT NULL DEFAULT 0,
    detail              TEXT
);

-- Periodic jobs -------------------------------------------------------------

-- Last successful run per job, so a restart neither repeats nor skips a day.
CREATE TABLE IF NOT EXISTS job_runs (
    job        TEXT PRIMARY KEY,
    last_run   TEXT NOT NULL,
    detail     TEXT
);
