"""Compact history, explicit recovery, and conservative diagnostic retention."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from omakron.runner import MAX_OUTPUT_BYTES
from omakron.schedule import Schedule
from omakron.store import Run, Store, StoreError, parse_utc

LOG_NAMES = ("stdout.jsonl", "stderr.log")
LOG_PREVIEW_BYTES = 16 * 1024


def failure(run: Run) -> dict:
    """Explain a run that did not succeed from supervisor evidence."""
    evidence = " ".join(run.problems).lower() + " " + (run.stderr_tail or "").lower()
    kind = run.status
    help_text = ""
    if run.status == "failed":
        kind, help_text = (
            "failure",
            "Read the diagnostics, correct the cause, then retry explicitly.",
        )
        if any(
            word in evidence
            for word in ("not logged in", "/login", "unauthorized", "authentication", "401")
        ):
            kind, help_text = (
                "authentication",
                "Run claude auth login in a terminal, then retry explicitly.",
            )
        elif "model" in evidence and any(
            word in evidence
            for word in (
                "unavailable",
                "not found",
                "invalid",
                "not available",
                "unknown",
                "access",
            )
        ):
            kind, help_text = (
                "model",
                "Choose an available model in Edit, save, then retry the saved revision.",
            )
        elif any(word in evidence for word in ("permission", "forbidden")):
            kind, help_text = (
                "permission",
                "Something was refused. Check the routine's permission mode and tools, "
                "then retry explicitly.",
            )
    elif run.status == "timed_out":
        help_text = (
            "The process group was stopped at its deadline. Shorten the prompt before retrying."
        )
    elif run.status == "interrupted":
        help_text = (
            "The service stopped during this run. Review retained output before an explicit retry."
        )
    return {"kind": kind, "help": help_text}


def summary(run: Run) -> dict:
    result = {
        key: getattr(run, key)
        for key in (
            "id",
            "routine_id",
            "routine_revision",
            "status",
            "trigger",
            "enqueued_at",
            "started_at",
            "ended_at",
            "requested_model",
            "resolved_model",
            "retry_of",
        )
    }
    result.update(name=run.routine_snapshot["name"], failure=failure(run))
    return result


def detail(run: Run, runs_dir: Path) -> dict:
    result = run.to_dict()
    result["failure"] = failure(run)
    result["diagnostics"] = {}
    directory = runs_dir / run.id
    for name in LOG_NAMES:
        path = directory / name
        if path.is_symlink() or directory.is_symlink() or not path.is_file():
            result["diagnostics"][name] = {
                "available": False,
                "text": "Not retained or not produced.",
            }
            continue
        with path.open("rb") as stream:
            size = path.stat().st_size
            stream.seek(max(0, size - LOG_PREVIEW_BYTES))
            data = stream.read(LOG_PREVIEW_BYTES)
        result["diagnostics"][name] = {
            "available": True,
            "bytes": size,
            "truncated": size > len(data),
            "text": data.decode("utf-8", errors="replace"),
        }
    return result


def output_settings(store: Store) -> dict:
    row = store.conn.execute("SELECT value FROM schema_meta WHERE key='output_settings'").fetchone()
    return (
        json.loads(row[0]) if row else {"retention_days": 30, "max_output_bytes": MAX_OUTPUT_BYTES}
    )


def retention_candidates(store: Store, runs_dir: Path, days: int) -> list[Path]:
    """Only successful, expired diagnostics. Results and all failed output survive."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    candidates = []
    rows = store.conn.execute("SELECT id,ended_at FROM runs WHERE status='succeeded'")
    for row in rows:
        if not row["ended_at"] or parse_utc(row["ended_at"]) >= cutoff:
            continue
        directory = runs_dir / row["id"]
        if directory.is_symlink() or directory.parent.resolve() != runs_dir.resolve():
            continue
        for name in LOG_NAMES:
            path = directory / name
            if path.is_file() and not path.is_symlink():
                candidates.append(path)
    return candidates


class HistoryApi:
    """Service operations grouped around history and recovery."""

    def op_dashboard(self, params: dict) -> dict:
        store = self._store()
        now = dt.datetime.now(dt.UTC)
        routines, upcoming = [], []
        for routine in store.list_routines():
            item = routine.to_dict()
            item.pop("prompt")
            latest = store.list_runs(routine_id=routine.id, limit=1)
            item["latest_run"] = summary(latest[0]) if latest else None
            routines.append(item)
            if routine.enabled and routine.schedule_kind == "cron":
                schedule = Schedule(routine.cron, routine.timezone)
                for instant in schedule.preview(now):
                    local = instant.astimezone(schedule.zone).isoformat()
                    upcoming.append(
                        dict(item, utc=instant.isoformat(), local=local, day=local[:10])
                    )
        return {
            "routines": routines,
            "upcoming": sorted(upcoming, key=lambda r: r["utc"])[:100],
            "recent": [summary(run) for run in store.list_runs(limit=30)],
            "dispatch_enabled": self.scheduler.enabled,
            "output_settings": output_settings(store),
        }

    def op_delete_routine(self, params: dict) -> dict:
        routine = self._routine(params)
        revision = params.get("expected_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise StoreError("expected_revision is required")
        deleted = self._store().delete_routine(routine.id, revision)
        return {"routine": deleted.to_dict()}

    def op_retry_run(self, params: dict) -> dict:
        original = self._store().get_run(params.get("run_id", ""))
        if not original.terminal:
            raise StoreError("wait for the original run to end before retrying")
        key = params.get("idempotency_key")
        if not isinstance(key, str) or not key or len(key) > 128:
            raise StoreError("a short idempotency_key is required for an explicit retry")
        routine = self._store().get_routine(original.routine_id)
        run, created = self._store().enqueue_run(
            routine,
            trigger="manual",
            idempotency_key=key,
            deadline_s=self.settings.deadline_s,
            retry_of=original.id,
        )
        self.wake.set()
        return {"run": detail(run, self.runs_dir), "created": created}

    def op_output_settings(self, params: dict) -> dict:
        if params:
            days, limit = params.get("retention_days"), params.get("max_output_bytes")
            if (
                type(days) is not int
                or not 1 <= days <= 3650
                or type(limit) is not int
                or not 1024 <= limit <= MAX_OUTPUT_BYTES
            ):
                raise StoreError(
                    "retention_days must be 1..3650 and max_output_bytes 1024..8388608"
                )
            with self._store()._tx():
                self._store().conn.execute(
                    "INSERT INTO schema_meta(key,value) VALUES('output_settings',?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(params),),
                )
        return output_settings(self._store())

    def op_prune_output(self, params: dict) -> dict:
        candidates = retention_candidates(
            self._store(), self.runs_dir, output_settings(self._store())["retention_days"]
        )
        names = [str(path.relative_to(self.runs_dir)) for path in candidates]
        if params.get("apply") is True:
            # The caller approves the exact preview. New eligible files need a new preview.
            if params.get("files") != names:
                raise StoreError("cleanup candidates changed; preview again")
            for path in candidates:
                path.unlink()
        return {
            "files": names,
            "applied": params.get("apply") is True,
            "policy": "Expired successful diagnostics only. Results and failed output remain.",
        }
