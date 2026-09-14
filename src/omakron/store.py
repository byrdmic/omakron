"""SQLite persistence for routines, runs, queue claims, and settings.

Only the service process writes. Every run is claimed in one ``BEGIN
IMMEDIATE`` transaction and carries an immutable snapshot of the routine
revision it was launched from. Terminal status is written only after output is
durably stored, so a row that says ``succeeded`` always points at the model's
result on disk.

Location rules live in :func:`state_dir` and :func:`config_dir`; the schema
lives here with its version so a later migration has a starting point.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omakron.schedule import Schedule, ScheduleError

SCHEMA_VERSION = 3
MAX_RESULT_TEXT_CHARS = 64 * 1024  # the run row keeps this much; stdout.jsonl keeps it all

# Non-terminal statuses. Terminal ones are the runner's ``Outcome`` values.
QUEUED = "queued"
CLAIMED = "claimed"
RUNNING = "running"
ACTIVE_STATUSES = (CLAIMED, RUNNING)
OPEN_STATUSES = (QUEUED, CLAIMED, RUNNING)

QUEUE_EXPIRY_S = 300.0  # a queued run not started within five minutes is skipped

SCHEMA = """
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
"""


def state_dir(env: dict[str, str] | None = None) -> Path:
    """``$XDG_STATE_HOME/omakron`` (default ``~/.local/state/omakron``)."""
    src = os.environ if env is None else env
    base = src.get("XDG_STATE_HOME") or str(
        Path(src.get("HOME", "~")).expanduser() / ".local" / "state"
    )
    return Path(base) / "omakron"


def config_dir(env: dict[str, str] | None = None) -> Path:
    """``$XDG_CONFIG_HOME/omakron`` (default ``~/.config/omakron``)."""
    src = os.environ if env is None else env
    base = src.get("XDG_CONFIG_HOME") or str(Path(src.get("HOME", "~")).expanduser() / ".config")
    return Path(base) / "omakron"


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


class StoreError(Exception):
    """A persistence rule was violated (unknown id, stale revision, bad state)."""


@dataclass(frozen=True, slots=True)
class Routine:
    id: str
    revision: int
    name: str
    prompt: str
    model: str
    cwd: str
    schedule_kind: str
    cron: str | None
    timezone: str | None
    enabled: bool
    policy_version: int
    tools: str
    permission_mode: str
    mcp_config: str | None
    env_passthrough: list[str]
    created_at: str
    updated_at: str
    deleted_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "revision": self.revision,
            "name": self.name,
            "prompt": self.prompt,
            "model": self.model,
            "cwd": self.cwd,
            "schedule_kind": self.schedule_kind,
            "cron": self.cron,
            "timezone": self.timezone,
            "enabled": self.enabled,
            "policy_version": self.policy_version,
            "tools": self.tools,
            "permission_mode": self.permission_mode,
            "mcp_config": self.mcp_config,
            "env_passthrough": list(self.env_passthrough),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deleted_at": self.deleted_at,
        }

    def snapshot(self) -> dict[str, Any]:
        """The part of a revision a run must keep even if the routine changes later."""
        return {
            "routine_id": self.id,
            "revision": self.revision,
            "name": self.name,
            "prompt": self.prompt,
            "model": self.model,
            "cwd": self.cwd,
            "policy_version": self.policy_version,
            "tools": self.tools,
            "permission_mode": self.permission_mode,
            "mcp_config": self.mcp_config,
            "env_passthrough": list(self.env_passthrough),
            "schedule_kind": self.schedule_kind,
            "cron": self.cron,
            "timezone": self.timezone,
        }


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    routine_id: str
    routine_revision: int
    trigger: str
    idempotency_key: str | None
    routine_snapshot: dict[str, Any]
    status: str
    problems: list[str]
    enqueued_at: str
    claimed_at: str | None
    started_at: str | None
    ended_at: str | None
    deadline_s: float
    worker: str | None
    pid: int | None
    pgid: int | None
    proc_start: str | None
    requested_model: str
    resolved_model: str | None
    models_used: list[str]
    exit_code: int | None
    result_text: str | None
    output_dir: str | None
    output_sha256: str | None
    stderr_tail: str | None
    cancel_requested: bool
    retry_of: str | None
    scheduled_at: str | None
    local_slot: str | None

    @property
    def terminal(self) -> bool:
        return self.status not in OPEN_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "routine_id": self.routine_id,
            "routine_revision": self.routine_revision,
            "trigger": self.trigger,
            "idempotency_key": self.idempotency_key,
            "routine_snapshot": self.routine_snapshot,
            "status": self.status,
            "problems": self.problems,
            "enqueued_at": self.enqueued_at,
            "claimed_at": self.claimed_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "deadline_s": self.deadline_s,
            "worker": self.worker,
            "requested_model": self.requested_model,
            "resolved_model": self.resolved_model,
            "models_used": self.models_used,
            "exit_code": self.exit_code,
            "result_text": self.result_text,
            "output_dir": self.output_dir,
            "output_sha256": self.output_sha256,
            "stderr_tail": self.stderr_tail,
            "cancel_requested": self.cancel_requested,
            "retry_of": self.retry_of,
            "scheduled_at": self.scheduled_at,
            "local_slot": self.local_slot,
        }


def _json_or(value: str | None, default: Any) -> Any:
    return default if value is None else json.loads(value)


def _routine(row: sqlite3.Row) -> Routine:
    return Routine(
        id=row["id"],
        revision=row["revision"],
        name=row["name"],
        prompt=row["prompt"],
        model=row["model"],
        cwd=row["cwd"],
        schedule_kind=row["schedule_kind"],
        cron=row["cron"],
        timezone=row["timezone"],
        enabled=bool(row["enabled"]),
        policy_version=row["policy_version"],
        tools=row["tools"],
        permission_mode=row["permission_mode"],
        mcp_config=row["mcp_config"],
        env_passthrough=_json_or(row["env_passthrough"], []),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


def _run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        routine_id=row["routine_id"],
        routine_revision=row["routine_revision"],
        trigger=row["trigger"],
        idempotency_key=row["idempotency_key"],
        routine_snapshot=json.loads(row["routine_snapshot"]),
        status=row["status"],
        problems=json.loads(row["problems"]),
        enqueued_at=row["enqueued_at"],
        claimed_at=row["claimed_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        deadline_s=row["deadline_s"],
        worker=row["worker"],
        pid=row["pid"],
        pgid=row["pgid"],
        proc_start=row["proc_start"],
        requested_model=row["requested_model"],
        resolved_model=row["resolved_model"],
        models_used=_json_or(row["models_used"], []),
        exit_code=row["exit_code"],
        result_text=row["result_text"],
        output_dir=row["output_dir"],
        output_sha256=row["output_sha256"],
        stderr_tail=row["stderr_tail"],
        cancel_requested=bool(row["cancel_requested"]),
        retry_of=row["retry_of"],
        scheduled_at=row["scheduled_at"],
        local_slot=row["local_slot"],
    )


class Store:
    """One SQLite connection. Open one per thread; SQLite serializes writers."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._migrate()
        os.chmod(self.path, 0o600)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        version = None
        if exists:
            row = self.conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            version = int(row["value"]) if row else None
            if version not in (1, 2, SCHEMA_VERSION):
                self.conn.close()
                raise StoreError(
                    f"database schema version {version} is not {SCHEMA_VERSION}; refusing to open"
                )
        if version in (1, 2):
            backup_path = self.path.with_name(
                self.path.name + f".v{version}-" + uuid.uuid4().hex + ".backup"
            )
            backup = sqlite3.connect(backup_path)
            try:
                os.chmod(backup_path, 0o600)
                self.conn.backup(backup)
            finally:
                backup.close()
            steps = ["BEGIN IMMEDIATE;"]
            if version == 1:
                steps += [
                    "ALTER TABLE runs ADD COLUMN scheduled_at TEXT;",
                    "ALTER TABLE runs ADD COLUMN local_slot TEXT;",
                ]
            # Version 3: a routine chooses its tools, permission mode, MCP config,
            # and extra environment keys, and a run keeps the model's result text.
            # The report-only issue triage columns go away with the feature.
            steps += [
                "ALTER TABLE routines ADD COLUMN tools TEXT NOT NULL DEFAULT 'default';",
                "ALTER TABLE routines ADD COLUMN permission_mode TEXT NOT NULL"
                " DEFAULT 'bypassPermissions';",
                "ALTER TABLE routines ADD COLUMN mcp_config TEXT;",
                "ALTER TABLE routines ADD COLUMN env_passthrough TEXT NOT NULL DEFAULT '[]';",
                "ALTER TABLE routines DROP COLUMN parameter_kind;",
                "ALTER TABLE runs ADD COLUMN result_text TEXT;",
                "ALTER TABLE runs DROP COLUMN parameter;",
                "ALTER TABLE runs DROP COLUMN input_snapshot;",
                "ALTER TABLE runs DROP COLUMN input_sha256;",
                "ALTER TABLE runs DROP COLUMN report;",
                SCHEMA,
                "UPDATE schema_meta SET value='3' WHERE key='schema_version';",
                "COMMIT;",
            ]
            self.conn.executescript("".join(steps))
        else:
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key,value) VALUES ('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )

    class _Tx:
        def __init__(self, conn: sqlite3.Connection):
            self.conn = conn

        def __enter__(self):
            self.nested = self.conn.in_transaction
            self.savepoint = "tx_" + uuid.uuid4().hex
            self.conn.execute(f"SAVEPOINT {self.savepoint}" if self.nested else "BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            if self.nested:
                if exc_type is not None:
                    self.conn.execute(f"ROLLBACK TO {self.savepoint}")
                self.conn.execute(f"RELEASE {self.savepoint}")
            else:
                self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
            return False

    def _tx(self) -> Store._Tx:
        return Store._Tx(self.conn)

    # ----------------------------------------------------------------- routines

    def create_routine(
        self,
        *,
        name: str,
        prompt: str,
        model: str,
        cwd: str,
        schedule_kind: str = "manual",
        cron: str | None = None,
        timezone: str | None = None,
        enabled: bool = False,
        tools: str = "default",
        permission_mode: str = "bypassPermissions",
        mcp_config: str | None = None,
        env_passthrough: list[str] | None = None,
        routine_id: str | None = None,
    ) -> Routine:
        """Create a routine at revision 1. New routines start paused by default."""
        if schedule_kind == "manual" and (cron or timezone):
            raise StoreError("a manual routine has no cron expression or timezone")
        if schedule_kind == "cron":
            try:
                Schedule(cron, timezone)
            except ScheduleError as exc:
                raise StoreError(f"cron schedule: {exc}") from exc
        elif schedule_kind != "manual":
            raise StoreError("unknown schedule kind")
        if not name.strip():
            raise StoreError("routine name is empty")
        if not prompt.strip():
            raise StoreError("routine prompt is empty")
        now = utc_now()
        rid = routine_id or str(uuid.uuid4())
        with self._tx():
            self.conn.execute(
                "INSERT INTO routines (id, revision, name, prompt, model, cwd, schedule_kind, cron,"
                " timezone, enabled, policy_version, tools, permission_mode,"
                " mcp_config, env_passthrough, created_at, updated_at)"
                " VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    name,
                    prompt,
                    model,
                    cwd,
                    schedule_kind,
                    cron,
                    timezone,
                    int(enabled),
                    tools,
                    permission_mode,
                    mcp_config,
                    json.dumps(list(env_passthrough or [])),
                    now,
                    now,
                ),
            )
        return self.get_routine(rid)

    def update_routine(self, routine_id: str, expected_revision: int, draft: dict) -> Routine:
        """Accept the whole validated draft only if the caller's revision is current.

        Saving pauses future dispatch. Existing run snapshots remain unchanged.
        """
        fields = (
            "name",
            "prompt",
            "model",
            "cwd",
            "schedule_kind",
            "cron",
            "timezone",
            "tools",
            "permission_mode",
            "mcp_config",
            "env_passthrough",
        )
        values = dict(draft)
        values["env_passthrough"] = json.dumps(list(draft.get("env_passthrough") or []))
        values.setdefault("tools", "default")
        values.setdefault("permission_mode", "bypassPermissions")
        values.setdefault("mcp_config", None)
        with self._tx():
            current = self.get_routine(routine_id)
            if current.deleted_at or current.revision != expected_revision:
                raise StoreError(
                    "routine changed; reload the accepted revision or reapply your draft"
                )
            self.skip_scheduled_queue(routine_id, "routine edited before start")
            assignments = ", ".join(f"{field} = ?" for field in fields)
            self.conn.execute(
                f"UPDATE routines SET {assignments}, revision = revision + 1,"  # noqa: S608 - fixed field names
                " enabled = 0, updated_at = ? WHERE id = ? AND revision = ?",
                (*[values[field] for field in fields], utc_now(), routine_id, expected_revision),
            )
        return self.get_routine(routine_id)

    def delete_routine(self, routine_id: str, expected_revision: int) -> Routine:
        """Hide the routine, stop waiting work, and retain all historical snapshots."""
        with self._tx():
            current = self.get_routine(routine_id)
            if current.deleted_at or current.revision != expected_revision:
                raise StoreError("routine changed; reload before deleting")
            now = utc_now()
            self.conn.execute(
                "UPDATE routines SET deleted_at=?, enabled=0, revision=revision+1,"
                " updated_at=? WHERE id=?",
                (now, now, routine_id),
            )
            self.conn.execute(
                "UPDATE runs SET status='canceled', ended_at=?, problems=?"
                " WHERE routine_id=? AND status='queued'",
                (now, json.dumps(["routine deleted before start"]), routine_id),
            )
        return self.get_routine(routine_id)

    def get_routine(self, routine_id: str) -> Routine:
        row = self.conn.execute("SELECT * FROM routines WHERE id = ?", (routine_id,)).fetchone()
        if row is None:
            raise StoreError(f"unknown routine {routine_id}")
        return _routine(row)

    def list_routines(self, *, include_deleted: bool = False) -> list[Routine]:
        sql = "SELECT * FROM routines"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        sql += " ORDER BY created_at, id"
        return [_routine(r) for r in self.conn.execute(sql)]

    def skip_scheduled_queue(self, routine_id: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE runs SET status='skipped', ended_at=?, problems=?"
            " WHERE routine_id=? AND trigger='scheduled' AND status='queued'",
            (utc_now(), json.dumps([reason]), routine_id),
        )

    def set_enabled(self, routine_id: str, expected_revision: int, enabled: bool) -> Routine:
        with self._tx():
            current = self.get_routine(routine_id)
            if current.deleted_at or current.revision != expected_revision:
                raise StoreError("routine changed; reload before changing its enabled state")
            if enabled and current.schedule_kind != "cron":
                raise StoreError("a manual routine cannot be enabled for scheduling")
            now = utc_now()
            self.skip_scheduled_queue(routine_id, "routine paused or resumed before start")
            self.conn.execute(
                "UPDATE routines SET enabled=?, revision=revision+1, updated_at=? WHERE id=?",
                (int(enabled), now, routine_id),
            )
            previous = self.conn.execute(
                "SELECT checkpoint FROM schedule_state WHERE routine_id=?", (routine_id,)
            ).fetchone()
            checkpoint = now
            if previous:
                checkpoint = max(previous["checkpoint"], now)
                if enabled and previous["checkpoint"] < now:
                    self.conn.execute(
                        "INSERT INTO schedule_gaps(routine_id,start_at,end_at,reason)"
                        " VALUES(?,?,?,?)",
                        (
                            routine_id,
                            previous["checkpoint"],
                            now,
                            "resume skips missed occurrences",
                        ),
                    )
            self.conn.execute(
                "INSERT INTO schedule_state(routine_id,checkpoint) VALUES(?,?)"
                " ON CONFLICT(routine_id) DO UPDATE SET checkpoint=excluded.checkpoint",
                (routine_id, checkpoint),
            )
        return self.get_routine(routine_id)

    # --------------------------------------------------------------------- runs

    def enqueue_run(
        self,
        routine: Routine,
        *,
        trigger: str,
        idempotency_key: str | None,
        deadline_s: float,
        enqueued_at: str | None = None,
        retry_of: str | None = None,
    ) -> tuple[Run, bool]:
        """Queue a run of ``routine`` at its current revision.

        Returns ``(run, created)``. A repeated idempotency key returns the
        existing run unchanged, so a double click or a reconnect never makes a
        second run.
        """
        if routine.deleted_at is not None:
            raise StoreError(f"routine {routine.id} is deleted")
        now = enqueued_at or utc_now()
        run_id = str(uuid.uuid4())
        with self._tx():
            if idempotency_key is not None:
                existing = self.conn.execute(
                    "SELECT * FROM runs WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    prior = _run(existing)
                    if (prior.routine_id, prior.retry_of) != (routine.id, retry_of):
                        raise StoreError("idempotency key belongs to a different request")
                    return prior, False
            self.conn.execute(
                "INSERT INTO runs (id, routine_id, routine_revision, trigger,"
                " idempotency_key, routine_snapshot, status, enqueued_at, deadline_s,"
                " requested_model, retry_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    routine.id,
                    routine.revision,
                    trigger,
                    idempotency_key,
                    json.dumps(routine.snapshot(), sort_keys=True),
                    QUEUED,
                    now,
                    deadline_s,
                    routine.model,
                    retry_of,
                ),
            )
        return self.get_run(run_id), True

    def expire_queue(self, now: str) -> None:
        with self._tx():
            for row in self.conn.execute(
                "SELECT id,enqueued_at FROM runs WHERE status='queued'"
            ).fetchall():
                if (
                    parse_utc(now) - parse_utc(row["enqueued_at"])
                ).total_seconds() > QUEUE_EXPIRY_S:
                    self.conn.execute(
                        "UPDATE runs SET status='skipped', ended_at=?, problems=? WHERE id=?",
                        (
                            now,
                            json.dumps([f"not started within {int(QUEUE_EXPIRY_S)} s of queueing"]),
                            row["id"],
                        ),
                    )

    def claim_next(self, worker: str) -> Run | None:
        """Claim the oldest queued run for ``worker`` in one transaction.

        Global concurrency is one: nothing is claimed while another run is
        claimed or running. Queued runs older than :data:`QUEUE_EXPIRY_S` are
        marked skipped instead of started late.
        """
        now = utc_now()
        with self._tx():
            self.expire_queue(now)
            active = self.conn.execute(
                "SELECT 1 FROM runs WHERE status IN (?, ?) LIMIT 1", ACTIVE_STATUSES
            ).fetchone()
            if active is not None:
                return None
            for row in self.conn.execute(
                "SELECT id, enqueued_at FROM runs WHERE status = ? ORDER BY rowid",
                (QUEUED,),
            ).fetchall():
                cur = self.conn.execute(
                    "UPDATE runs SET status = ?, claimed_at = ?, worker = ?"
                    " WHERE id = ? AND status = ?",
                    (CLAIMED, now, worker, row["id"], QUEUED),
                )
                if cur.rowcount == 1:
                    run_id = row["id"]
                    break
            else:
                return None
        return self.get_run(run_id)

    def mark_running(
        self, run_id: str, *, pid: int, pgid: int, proc_start: str, output_dir: str
    ) -> None:
        with self._tx():
            cur = self.conn.execute(
                "UPDATE runs SET status = ?, started_at = ?, pid = ?, pgid = ?, proc_start = ?,"
                " output_dir = ? WHERE id = ? AND status = ?",
                (RUNNING, utc_now(), pid, pgid, proc_start, output_dir, run_id, CLAIMED),
            )
            if cur.rowcount != 1:
                raise StoreError(f"run {run_id} is not claimed")

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        problems: list[str],
        exit_code: int | None = None,
        resolved_model: str | None = None,
        models_used: list[str] | None = None,
        result_text: str | None = None,
        output_dir: str | None = None,
        output_sha256: str | None = None,
        stderr_tail: str | None = None,
    ) -> Run:
        """Write the terminal status. Only an open run can finish, and only once."""
        if status in OPEN_STATUSES:
            raise StoreError(f"{status} is not a terminal status")
        if result_text is not None and len(result_text) > MAX_RESULT_TEXT_CHARS:
            result_text = result_text[:MAX_RESULT_TEXT_CHARS]
        with self._tx():
            cur = self.conn.execute(
                "UPDATE runs SET status = ?, problems = ?, ended_at = ?, exit_code = ?,"
                " resolved_model = ?, models_used = ?, result_text = ?,"
                " output_dir = COALESCE(?, output_dir), output_sha256 = ?, stderr_tail = ?"
                " WHERE id = ? AND status IN ('queued', 'claimed', 'running')",
                (
                    status,
                    json.dumps(problems),
                    utc_now(),
                    exit_code,
                    resolved_model,
                    json.dumps(models_used or []),
                    result_text,
                    output_dir,
                    output_sha256,
                    stderr_tail,
                    run_id,
                ),
            )
            if cur.rowcount != 1:
                raise StoreError(f"run {run_id} is not open")
        return self.get_run(run_id)

    def request_cancel(self, run_id: str) -> Run:
        """Cancel a run. A queued run ends now; a claimed or running one is flagged."""
        with self._tx():
            row = self.conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise StoreError(f"unknown run {run_id}")
            if row["status"] == QUEUED:
                self.conn.execute(
                    "UPDATE runs SET status = 'canceled', cancel_requested = 1, ended_at = ?,"
                    " problems = ? WHERE id = ? AND status = ?",
                    (utc_now(), json.dumps(["canceled by user before start"]), run_id, QUEUED),
                )
            elif row["status"] in ACTIVE_STATUSES:
                self.conn.execute("UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,))
            else:
                raise StoreError(f"run {run_id} already ended with status {row['status']}")
        return self.get_run(run_id)

    def cancel_requested(self, run_id: str) -> bool:
        row = self.conn.execute(
            "SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return bool(row and row["cancel_requested"])

    def get_run(self, run_id: str) -> Run:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise StoreError(f"unknown run {run_id}")
        return _run(row)

    def list_runs(self, *, routine_id: str | None = None, limit: int = 30) -> list[Run]:
        """Newest first."""
        limit = max(1, min(int(limit), 500))
        if routine_id is None:
            rows = self.conn.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT ?", (limit,))
        else:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE routine_id = ? ORDER BY rowid DESC LIMIT ?",
                (routine_id, limit),
            )
        return [_run(r) for r in rows]

    def active_run(self) -> Run | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE status IN (?, ?) ORDER BY claimed_at LIMIT 1",
            ACTIVE_STATUSES,
        ).fetchone()
        return None if row is None else _run(row)

    def reconcile(self, worker: str) -> list[Run]:
        """Mark runs another worker left claimed or running as interrupted.

        Called once at service start. The process end of such a run was never
        observed, so it is neither replayed nor guessed at; the returned runs
        carry the pid/pgid/proc_start needed to stop an orphaned process.
        """
        with self._tx():
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE status IN (?, ?) AND (worker IS NULL OR worker != ?)",
                (*ACTIVE_STATUSES, worker),
            ).fetchall()
            interrupted = [_run(r) for r in rows]
            now = utc_now()
            for run in interrupted:
                self.conn.execute(
                    "UPDATE runs SET status = 'interrupted', ended_at = ?, problems = ?"
                    " WHERE id = ?",
                    (
                        now,
                        json.dumps(
                            [
                                "process end was not observed: the service stopped while this run"
                                f" was {run.status}; not replayed"
                            ]
                        ),
                        run.id,
                    ),
                )
        return interrupted
