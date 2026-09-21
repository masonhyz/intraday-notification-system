-- Schema for the intraday notification system.
--
-- SQLite here so the whole thing runs with `python -m app.replay` and no
-- infrastructure, but the shape is the one we would take to Postgres: every
-- table is keyed by org_id first (multi-tenancy is out of scope for this
-- exercise, but the key is in place rather than retrofitted), and the hot
-- tables are keyed by subject so they shard by (org_id, subject_id).

-- Raw event log. Append-only, PRIMARY KEY on event_id gives us idempotent
-- ingest: an at-least-once producer can redeliver freely.
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL,
    ts           TEXT NOT NULL,        -- when it happened (producer clock)
    received_at  TEXT NOT NULL,        -- when we saw it (our clock)
    type         TEXT NOT NULL,
    subject_type TEXT,
    subject_id   TEXT,
    status       TEXT NOT NULL,        -- applied | stale | rejected
    note         TEXT,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_subject ON events (org_id, subject_type, subject_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_received ON events (org_id, received_at);

-- Current state, one row per subject. This is what rules read; it stays small
-- no matter how many events flow through.
CREATE TABLE IF NOT EXISTS queue_state (
    org_id                   TEXT NOT NULL,
    queue_id                 TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    tickets_waiting          INTEGER,
    longest_wait_sec         INTEGER,
    sla_target_sec           INTEGER,
    agents_available         INTEGER,
    agents_on_call           INTEGER,
    volume_last_15m          INTEGER,
    volume_forecast_next_15m INTEGER,
    PRIMARY KEY (org_id, queue_id)
);

CREATE TABLE IF NOT EXISTS agent_state (
    org_id               TEXT NOT NULL,
    agent_id             TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    state                TEXT,
    state_since          TEXT,
    state_updated_at     TEXT,
    queue_ids            TEXT NOT NULL DEFAULT '[]',
    scheduled_state      TEXT,
    actual_state         TEXT,
    in_violation         INTEGER NOT NULL DEFAULT 0,
    violation_started_at TEXT,
    adherence_updated_at TEXT,
    PRIMARY KEY (org_id, agent_id)
);

-- People who can receive notifications. Auth and org plumbing are out of
-- scope; this is the routing directory.
CREATE TABLE IF NOT EXISTS users (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    name                TEXT NOT NULL,
    role                TEXT NOT NULL,                     -- agent | team_lead | head_of_support
    agent_id            TEXT,                              -- set when this user *is* an agent
    queue_ids           TEXT NOT NULL DEFAULT '[]',        -- queues this user is responsible for
    channel             TEXT NOT NULL DEFAULT 'slack',
    -- Noise control that belongs to the person, not the rule: anything below
    -- this severity is rolled up into a periodic digest instead of pinging.
    digest_min_severity TEXT NOT NULL DEFAULT 'info',
    digest_interval_sec INTEGER NOT NULL DEFAULT 900
);
CREATE INDEX IF NOT EXISTS idx_users_org_role ON users (org_id, role);
CREATE INDEX IF NOT EXISTS idx_users_agent ON users (org_id, agent_id);

CREATE TABLE IF NOT EXISTS rules (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL,
    name              TEXT NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1,
    subject_type      TEXT NOT NULL,          -- queue | agent
    scope             TEXT NOT NULL,          -- {"mode": all|ids|queues, "ids": [...]}
    metric            TEXT NOT NULL,
    operator          TEXT NOT NULL,
    threshold         REAL NOT NULL,
    state_filter      TEXT,
    for_sec           INTEGER NOT NULL DEFAULT 0,   -- must hold this long before firing
    clear_after_sec   INTEGER NOT NULL DEFAULT 0,   -- must be false this long before resolving
    renotify_sec      INTEGER NOT NULL DEFAULT 0,   -- 0 = never repeat while firing
    notify_on_resolve INTEGER NOT NULL DEFAULT 1,
    severity          TEXT NOT NULL,
    audience          TEXT NOT NULL,          -- [{"type": ..., "value": ...}]
    created_by        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
-- Evaluation looks rules up by (org, subject type); everything enabled for a
-- subject type is a small set we keep indexed in memory.
CREATE INDEX IF NOT EXISTS idx_rules_lookup ON rules (org_id, subject_type, enabled);

-- The engine's keyed state: one row per (rule, subject) that is or was firing.
-- This is the part that would live in a keyed store (Redis/RocksDB) in
-- production - it is what makes "sustained for 10 minutes" and "resolved"
-- possible without re-reading history.
CREATE TABLE IF NOT EXISTS rule_subject_state (
    rule_id         TEXT NOT NULL,
    org_id          TEXT NOT NULL,
    subject_id      TEXT NOT NULL,
    status          TEXT NOT NULL,        -- ok | pending | firing | clearing
    condition_since TEXT,                 -- first moment the condition held
    clear_since     TEXT,                 -- first moment it stopped holding
    incident_id     TEXT,
    opened_at       TEXT,
    last_value      REAL,
    last_eval_at    TEXT,
    last_notified_at TEXT,
    notify_seq      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (rule_id, subject_id)
);
CREATE INDEX IF NOT EXISTS idx_rss_open ON rule_subject_state (org_id, status);

-- Delivered notifications. Append-only; `dedupe_key` makes delivery
-- idempotent even if evaluation runs twice for the same tick.
CREATE TABLE IF NOT EXISTS notifications (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL,
    dedupe_key   TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,          -- event time, not wall clock
    rule_id      TEXT,
    rule_name    TEXT,
    incident_id  TEXT,
    kind         TEXT NOT NULL,          -- fire | reminder | resolve | digest
    severity     TEXT NOT NULL,
    subject_type TEXT,
    subject_id   TEXT,
    recipient_id TEXT NOT NULL,
    channel      TEXT NOT NULL,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    context      TEXT NOT NULL DEFAULT '{}',
    delivery     TEXT NOT NULL,          -- immediate | digest
    status       TEXT NOT NULL,          -- delivered | buffered | rolled_up
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notif_recipient ON notifications (org_id, recipient_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notif_created ON notifications (org_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notif_buffered ON notifications (org_id, recipient_id, status);
