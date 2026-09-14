CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routines (
    id             TEXT PRIMARY KEY,
    revision       INTEGER NOT NULL,
    name           TEXT NOT NULL,
    prompt         TEXT NOT NULL,
    model          TEXT NOT NULL,
    cwd            TEXT NOT NULL,
    schedule_kind  TEXT NOT NULL CHECK (schedule_kind IN ('manual', 'cron')),
    cron           TEXT,
    timezone       TEXT,
    enabled        INTEGER NOT NULL DEFAULT 0,
    policy_version INTEGER NOT NULL DEFAULT 1,
    tools          TEXT NOT NULL DEFAULT 'default',
    permission_mode TEXT NOT NULL DEFAULT 'bypassPermissions',
    mcp_config     TEXT,
    env_passthrough TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    deleted_at     TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id               TEXT PRIMARY KEY,
    routine_id       TEXT NOT NULL REFERENCES routines(id),
    routine_revision INTEGER NOT NULL,
    trigger          TEXT NOT NULL,
    idempotency_key  TEXT UNIQUE,
    routine_snapshot TEXT NOT NULL,
    status           TEXT NOT NULL,
    problems         TEXT NOT NULL DEFAULT '[]',
    enqueued_at      TEXT NOT NULL,
    claimed_at       TEXT,
    started_at       TEXT,
    ended_at         TEXT,
    deadline_s       REAL NOT NULL,
    worker           TEXT,
    pid              INTEGER,
    pgid             INTEGER,
    proc_start       TEXT,
    requested_model  TEXT NOT NULL,
    resolved_model   TEXT,
    models_used      TEXT,
    exit_code        INTEGER,
    result_text      TEXT,
    output_dir       TEXT,
    output_sha256    TEXT,
    stderr_tail      TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    retry_of         TEXT,
    scheduled_at     TEXT,
    local_slot       TEXT
);
CREATE INDEX IF NOT EXISTS runs_routine_enqueued ON runs (routine_id, enqueued_at);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status);
CREATE TABLE IF NOT EXISTS schedule_state (
    routine_id TEXT PRIMARY KEY REFERENCES routines(id),
    checkpoint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_slots (
    routine_id TEXT NOT NULL REFERENCES routines(id),
    local_slot TEXT NOT NULL,
    revision INTEGER NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id),
    PRIMARY KEY (routine_id, local_slot)
);
CREATE TABLE IF NOT EXISTS schedule_gaps (
    id INTEGER PRIMARY KEY,
    routine_id TEXT NOT NULL REFERENCES routines(id),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    reason TEXT NOT NULL
);
