-- 001: initial schema (DESIGN.md §5)

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sources (
    name            TEXT PRIMARY KEY,
    enabled         INTEGER NOT NULL DEFAULT 1,
    cursor          TEXT,
    last_poll       TEXT,
    error_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    muted           INTEGER NOT NULL DEFAULT 0,
    score           REAL NOT NULL DEFAULT 1.0,
    last_item_at    TEXT
);

CREATE TABLE IF NOT EXISTS authors (
    source              TEXT NOT NULL,
    handle              TEXT NOT NULL,
    score               REAL NOT NULL DEFAULT 1.0,
    muted               INTEGER NOT NULL DEFAULT 0,
    consecutive_dead    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, handle)
);

CREATE TABLE IF NOT EXISTS links (
    code        TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new',   -- new | alerted | dead | worked
    feedback    TEXT
);

CREATE TABLE IF NOT EXISTS sightings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL REFERENCES links(code),
    source      TEXT NOT NULL,
    author      TEXT NOT NULL,
    permalink   TEXT,
    posted_at   TEXT,
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sightings_code ON sightings(code);
CREATE INDEX IF NOT EXISTS idx_sightings_source ON sightings(source);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL REFERENCES links(code),
    sent_at     TEXT NOT NULL,
    message_id  INTEGER,
    silent      INTEGER NOT NULL DEFAULT 0,
    confidence  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_code ON alerts(code);

CREATE TABLE IF NOT EXISTS votes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL REFERENCES links(code),
    vote        TEXT NOT NULL,                 -- worked | dead
    voted_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_votes_code ON votes(code);

-- Chat lock: the single owner chat the bot is allowed to interact with.
CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
