"""Compact history, explicit recovery, and conservative diagnostic retention.

Every run leaves a folder: the raw ``stream-json`` transcript, the model's
result, the last of stderr, and ``log.md``, a readable account of what
happened and when. The folder lives under the state directory unless the
person chose a log folder in the settings, in which case every new run goes
there. Retention may delete the raw transcript and stderr of old successful
runs; ``result.md`` and ``log.md`` are never deleted by the service.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from omakron.runner import MAX_OUTPUT_BYTES, ParsedStream
from omakron.schedule import Schedule
from omakron.store import Run, Store, StoreError, parse_utc

LOG_NAMES = ("stdout.jsonl", "stderr.log")  # retention may delete these
LOG_PREVIEW_BYTES = 16 * 1024
MAX_LOG_TEXT = 64 * 1024


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


def duration_s(run: Run) -> float | None:
    if not run.started_at or not run.ended_at:
        return None
    return round((parse_utc(run.ended_at) - parse_utc(run.started_at)).total_seconds(), 1)


def run_folder(run: Run, runs_dir: Path) -> Path:
    """Where this run's files are: the folder recorded at launch, else the default place."""
    return Path(run.output_dir) if run.output_dir else runs_dir / run.id


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
    result["duration_s"] = duration_s(run)
    result["problems"] = list(run.problems)
    return result


def detail(run: Run, runs_dir: Path) -> dict:
    result = run.to_dict()
    result["name"] = run.routine_snapshot["name"]
    result["failure"] = failure(run)
    result["duration_s"] = duration_s(run)
    result["diagnostics"] = {}
    directory = run_folder(run, runs_dir)
    result["folder"] = str(directory)
    log_path = directory / "log.md"
    result["log"] = None
    if log_path.is_file() and not log_path.is_symlink() and not directory.is_symlink():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        result["log"] = text[-MAX_LOG_TEXT:]
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
    value = json.loads(row[0]) if row else {}
    return {
        "retention_days": value.get("retention_days", 30),
        "max_output_bytes": value.get("max_output_bytes", MAX_OUTPUT_BYTES),
        "log_dir": value.get("log_dir") or None,
    }


def resolve_log_dir(value: object) -> str | None:
    """An absolute, writable folder for run logs, created if missing. Empty means the default."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or any(c in value for c in ("\n", "\r", "\0")):
        raise StoreError("log_dir must be an absolute folder path")
    folder = Path(value).expanduser()
    if not folder.is_absolute():
        raise StoreError("log_dir must be an absolute folder path")
    folder = Path(os.path.realpath(folder))
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise StoreError(f"log folder could not be created: {exc}") from exc
    if not folder.is_dir() or not os.access(folder, os.W_OK | os.X_OK):
        raise StoreError(f"log folder is not writable: {folder}")
    return str(folder)


def render_log(run: Run, stream: ParsedStream | None, stderr_tail: str | None) -> str:
    """A readable account of one run, written as ``log.md`` beside the raw transcript."""
    snapshot = run.routine_snapshot
    lines = [f"# {snapshot['name']}", ""]
    facts = [
        ("Run", run.id),
        ("Routine", f"{run.routine_id} (revision {run.routine_revision})"),
        ("Trigger", run.trigger),
        ("Queued", run.enqueued_at),
        ("Started", run.started_at or "never"),
        ("Ended", run.ended_at or "not yet"),
        ("Duration", f"{duration_s(run)} s" if duration_s(run) is not None else "n/a"),
        ("Status", run.status),
        ("Exit code", str(run.exit_code) if run.exit_code is not None else "n/a"),
        ("Model", run.resolved_model or run.requested_model),
        ("Working folder", snapshot.get("cwd", "")),
        ("Tools", snapshot.get("tools", "default")),
        ("Permission mode", snapshot.get("permission_mode", "")),
    ]
    if snapshot.get("source"):
        facts.append(("Skill file", snapshot["source"]))
        if snapshot.get("source_sha256"):
            facts.append(("Skill file SHA-256", snapshot["source_sha256"]))
    lines += [f"- {label}: {value}" for label, value in facts]
    if run.problems:
        lines += ["", "## Problems", ""] + [f"- {problem}" for problem in run.problems]
    lines += ["", "## Prompt", "", snapshot.get("prompt", ""), "", "## Transcript", ""]
    if stream is None:
        lines.append("No transcript was produced.")
    else:
        if stream.init:
            lines.append(f"- Session started with model {stream.init.get('model', '?')}.")
        for text in stream.assistant_text:
            lines += [""] + ["> " + line for line in text.strip().splitlines()]
        for use in stream.tool_uses:
            name = use.get("name", "tool") if isinstance(use, dict) else "tool"
            args = use.get("input", {}) if isinstance(use, dict) else use
            shown = json.dumps(args, sort_keys=True) if not isinstance(args, str) else args
            lines.append(f"- Tool `{name}`: {shown[:300]}")
        if not stream.assistant_text and not stream.tool_uses:
            lines.append("The model produced no text or tool calls before the result.")
        if stream.unparsed_lines:
            lines.append(f"- {stream.unparsed_lines} transcript line(s) could not be parsed.")
    lines += ["", "## Result", "", run.result_text or "(no result text)"]
    if stderr_tail:
        lines += ["", "## Stderr (last part)", "", "```", stderr_tail.rstrip(), "```"]
    return "\n".join(lines) + "\n"


def retention_candidates(
    store: Store, runs_dir: Path, days: int, log_dir: str | None = None
) -> list[Path]:
    """Only successful, expired diagnostics. Results, logs, and all failed output survive."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    allowed = {runs_dir.resolve()}
    if log_dir:
        allowed.add(Path(log_dir).resolve())
    candidates = []
    rows = store.conn.execute("SELECT id,ended_at,output_dir FROM runs WHERE status='succeeded'")
    for row in rows:
        if not row["ended_at"] or parse_utc(row["ended_at"]) >= cutoff:
            continue
        directory = Path(row["output_dir"]) if row["output_dir"] else runs_dir / row["id"]
        if directory.is_symlink() or directory.parent.resolve() not in allowed:
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
        active = store.active_run()
        return {
            "routines": routines,
            "active_run": summary(active) if active else None,
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
        """Read, or with params replace, retention, the output limit, and the log folder."""
        if params:
            current = output_settings(self._store())
            days = params.get("retention_days", current["retention_days"])
            limit = params.get("max_output_bytes", current["max_output_bytes"])
            if (
                type(days) is not int
                or not 1 <= days <= 3650
                or type(limit) is not int
                or not 1024 <= limit <= MAX_OUTPUT_BYTES
            ):
                raise StoreError(
                    "retention_days must be 1..3650 and max_output_bytes 1024..8388608"
                )
            log_dir = resolve_log_dir(params.get("log_dir", current["log_dir"]))
            value = {"retention_days": days, "max_output_bytes": limit, "log_dir": log_dir}
            with self._store()._tx():
                self._store().conn.execute(
                    "INSERT INTO schema_meta(key,value) VALUES('output_settings',?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(value),),
                )
        return output_settings(self._store())

    def op_prune_output(self, params: dict) -> dict:
        settings = output_settings(self._store())
        candidates = retention_candidates(
            self._store(), self.runs_dir, settings["retention_days"], settings["log_dir"]
        )
        names = [str(path) for path in candidates]
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
