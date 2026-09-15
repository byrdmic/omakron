"""Omakron user service: the socket API, the worker, and the database.

``python -m omakron.service`` runs it in the foreground; the proposed systemd
unit in ``packaging/`` would start the same command. On start it opens the
store, seeds the example routine if the database is empty, marks runs a
previous instance left open as interrupted (and stops their orphaned
processes when they can be identified), then serves requests on a user-only
Unix socket while one worker thread executes runs.

Runs belong to the service, not to a connection: a client may disconnect the
moment its request is answered and the run continues. Stopping the service
interrupts an active run honestly instead of leaving it running unobserved.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import secrets
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omakron import __version__, history, ipc, manage, routines, seed, skills
from omakron.client import socket_path
from omakron.runner import (
    DEFAULT_MODEL,
    DEFAULT_PERMISSION_MODE,
    DEFAULT_TOOLS,
    PERMISSION_MODES,
    proc_start_ticks,
    stop_process_group,
)
from omakron.schedule import CONVENTION, Schedule
from omakron.scheduler import Scheduler
from omakron.store import Routine, Store, StoreError, config_dir, parse_utc, state_dir, utc_now
from omakron.worker import Worker, WorkerConfig

log = logging.getLogger("omakron.service")

DEFAULT_DEADLINE_S = 600.0  # accepted 2026-09-11: ten minutes per run
MAX_IDEMPOTENCY_KEY_CHARS = 128
MAX_PROMPT_CHARS = 20_000
MAX_NAME_CHARS = 120


@dataclass(frozen=True, slots=True)
class Settings:
    claude_executable: str = "claude"
    deadline_s: float = DEFAULT_DEADLINE_S
    enforce_compatibility: bool = False

    @classmethod
    def load(cls, directory: Path) -> Settings:
        path = directory / "settings.json"
        if not path.is_file():
            return cls()
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            raise ValueError(f"{path} is not a JSON object")
        executable = obj.get("claude_executable", "claude")
        deadline = obj.get("deadline_s", DEFAULT_DEADLINE_S)
        if not isinstance(executable, str) or not executable:
            raise ValueError("claude_executable must be a non-empty string")
        if isinstance(deadline, bool) or not isinstance(deadline, int | float) or deadline <= 0:
            raise ValueError("deadline_s must be a positive number")
        return cls(
            claude_executable=executable,
            deadline_s=float(deadline),
            enforce_compatibility=obj.get("enforce_compatibility", False) is True,
        )


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class Service(history.HistoryApi):
    def __init__(
        self,
        *,
        state: Path | None = None,
        config: Path | None = None,
        sock: Path | None = None,
    ):
        self.state_dir = state or state_dir()
        self.config_dir = config or config_dir()
        self.socket_path = sock or socket_path()
        self.runs_dir = self.state_dir / "runs"
        self.workdir = self.state_dir / "workdir"
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"
        self.started_at = utc_now()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.settings = Settings()
        self.api_store: Store | None = None
        self.listener: socket.socket | None = None
        self.worker_thread: threading.Thread | None = None
        self.interrupted_on_start: list[str] = []
        self.scheduler: Scheduler | None = None
        self.lock_file = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.settings = Settings.load(self.config_dir)
        if self.settings.enforce_compatibility:
            manage.compatibility()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock_file = (self.state_dir / "service.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError("another Omakron service owns this state directory") from exc
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.api_store = Store(self.state_dir / "omakron.db")
        self._bind()
        self._seed()
        self._reconcile()
        self.scheduler = Scheduler(self.api_store, self.settings.deadline_s)
        db_path = self.state_dir / "omakron.db"
        worker = Worker(
            store_factory=lambda: Store(db_path),
            config=WorkerConfig(
                claude_executable=self.settings.claude_executable,
                runs_dir=self.runs_dir,
                managed_workdir=self.workdir,
            ),
            worker_id=self.worker_id,
            wake=self.wake,
            stopping=self.stopping,
        )
        self.worker_thread = threading.Thread(
            target=worker.run_forever, name="omakron-worker", daemon=True
        )
        self.worker_thread.start()
        log.info(
            "omakron %s serving on %s as %s (claude=%s)",
            __version__,
            self.socket_path,
            self.worker_id,
            self.settings.claude_executable,
        )

    def _seed(self) -> None:
        assert self.api_store is not None
        if not self.api_store.list_routines(include_deleted=True):
            cwd = str(self.workdir / seed.FOLDER_NAME)
            self.api_store.create_routine(**seed.seed_definition(cwd))
            log.info("seeded routine %r", seed.ROUTINE_NAME)

    def _reconcile(self) -> None:
        assert self.api_store is not None
        for run in self.api_store.reconcile(self.worker_id):
            self.interrupted_on_start.append(run.id)
            note = "no process to stop"
            if run.pid and run.pgid and run.proc_start:
                if proc_start_ticks(run.pid) == run.proc_start:
                    method = stop_process_group(
                        run.pgid, grace_s=3.0, wait=lambda s, pid=run.pid: _gone_within(pid, s)
                    )
                    note = f"orphaned process group {run.pgid} stopped with {method}"
                else:
                    note = f"recorded pid {run.pid} is no longer that process"
            log.warning("run %s marked interrupted at start: %s", run.id, note)

    def _bind(self) -> None:
        directory = self.socket_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        if self.socket_path.exists():
            # A stale socket from a crashed instance is replaced; a live one is not.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.connect(str(self.socket_path))
                except OSError:
                    self.socket_path.unlink()
                else:
                    raise RuntimeError(f"another service is listening on {self.socket_path}")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        listener.listen(16)
        listener.settimeout(0.5)
        self.listener = listener

    def serve_forever(self) -> None:
        assert self.listener is not None
        while not self.stopping.is_set():
            if self.scheduler is not None:
                if self.scheduler.tick(dt.datetime.now(dt.UTC)):
                    self.wake.set()
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self.stopping.is_set():
                    break
                raise
            with conn:
                conn.settimeout(5.0)
                self._handle(conn)

    def stop(self, grace_s: float = 10.0) -> None:
        """Stop accepting, interrupt the active run if any, and wait for the worker."""
        self.stopping.set()
        self.wake.set()
        if self.worker_thread is not None:
            self.worker_thread.join(timeout=grace_s)
        if self.listener is not None:
            self.listener.close()
            self.socket_path.unlink(missing_ok=True)
            self.listener = None
        if self.api_store is not None:
            self.api_store.close()
            self.api_store = None
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None

    # ----------------------------------------------------------------- socket

    def _handle(self, conn: socket.socket) -> None:
        request_id: Any = None
        try:
            message = ipc.read_message(conn)
            request_id = message.get("id")
            op = message.get("op")
            params = message.get("params") or {}
            if not isinstance(op, str) or not isinstance(params, dict):
                raise ApiError("bad_request", "op must be a string and params an object")
            result = self.dispatch(op, params)
            response = ipc.ok_response(request_id, result)
        except ipc.ProtocolError as exc:
            response = ipc.error_response(request_id, "protocol", str(exc))
        except ApiError as exc:
            response = ipc.error_response(request_id, exc.code, str(exc))
        except StoreError as exc:
            response = ipc.error_response(request_id, "store", str(exc))
        except Exception as exc:  # the service must keep serving
            log.exception("request failed")
            response = ipc.error_response(request_id, "internal", f"{type(exc).__name__}: {exc}")
        try:
            try:
                data = ipc.encode(response, max_bytes=ipc.MAX_RESPONSE_BYTES)
            except ipc.ProtocolError:
                data = ipc.encode(
                    ipc.error_response(
                        request_id,
                        "response_limit",
                        "Result exceeds the response limit. "
                        "Read the retained output file or request fewer rows.",
                    )
                )
            conn.sendall(data)
        except (OSError, ipc.ProtocolError) as exc:
            log.warning("could not answer request %r: %s", request_id, exc)

    # -------------------------------------------------------------------- api

    def dispatch(self, op: str, params: dict[str, Any]) -> Any:
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            raise ApiError("unknown_op", f"unknown op {op!r}")
        return handler(params)

    def _store(self) -> Store:
        assert self.api_store is not None
        return self.api_store

    def _routine(self, params: dict[str, Any]) -> Routine:
        routine_id = params.get("routine_id")
        if not isinstance(routine_id, str) or not routine_id:
            raise ApiError("bad_request", "routine_id is required")
        try:
            return self._store().get_routine(routine_id)
        except StoreError as exc:
            raise ApiError("not_found", str(exc)) from exc

    def op_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "worker": self.worker_id}

    def op_status(self, params: dict[str, Any]) -> dict[str, Any]:
        store = self._store()
        active = store.active_run()
        return {
            "service": {
                "version": __version__,
                "worker": self.worker_id,
                "pid": os.getpid(),
                "started_at": self.started_at,
                "state_dir": str(self.state_dir),
                "socket": str(self.socket_path),
                "claude_executable": self.settings.claude_executable,
                "deadline_s": self.settings.deadline_s,
                "dispatch_enabled": self.scheduler.enabled if self.scheduler else False,
                "interrupted_on_start": self.interrupted_on_start,
            },
            "active_run": None if active is None else active.to_dict(),
            "routines": len(store.list_routines()),
        }

    def op_list_routines(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"routines": [r.to_dict() for r in self._store().list_routines()]}

    def op_get_routine(self, params: dict[str, Any]) -> dict[str, Any]:
        routine = self._routine(params)
        runs = self._store().list_runs(routine_id=routine.id, limit=10)
        return {
            "routine": routine.to_dict(),
            "recent_runs": [history.summary(r) for r in runs],
        }

    def _validated_draft(self, params: dict) -> dict:
        try:
            return routines.validate(params, self.workdir)
        except ValueError as exc:
            raise ApiError("bad_request", str(exc)) from exc

    def op_create_routine(self, params: dict[str, Any]) -> dict[str, Any]:
        """A routine saved with a time starts on; a manual one runs only when asked."""
        draft = self._validated_draft(params)
        routine = self._store().create_routine(**draft, enabled=draft["schedule_kind"] == "cron")
        return {"routine": routine.to_dict()}

    def op_update_routine(self, params: dict[str, Any]) -> dict[str, Any]:
        routine = self._routine(params)
        revision = params.get("expected_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ApiError("bad_request", "expected_revision is required")
        draft = self._validated_draft(params)
        try:
            updated = self._store().update_routine(routine.id, revision, draft)
        except StoreError as exc:
            raise ApiError("conflict", str(exc)) from exc
        return {"routine": updated.to_dict()}

    def op_delete_routine(self, params: dict[str, Any]) -> dict[str, Any]:
        """Remove the routine from Omakron. Recorded runs keep their saved copy of it."""
        routine = self._routine(params)
        revision = params.get("expected_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ApiError("bad_request", "expected_revision is required")
        try:
            deleted = self._store().delete_routine(routine.id, revision)
        except StoreError as exc:
            raise ApiError("conflict", str(exc)) from exc
        self.wake.set()
        return {"routine": deleted.to_dict()}

    def op_preview_schedule(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            schedule = Schedule(params.get("cron"), params.get("timezone"))
            after = parse_utc(params.get("after", utc_now()))
            times = schedule.preview(after)
        except (ValueError, TypeError) as exc:
            raise ApiError("bad_request", str(exc)) from exc
        return {
            "cron": schedule.expression,
            "timezone": schedule.timezone,
            "convention": CONVENTION,
            "occurrences": [
                {"utc": value.isoformat(), "local": value.astimezone(schedule.zone).isoformat()}
                for value in times
            ],
        }

    def op_read_skill(self, params: dict[str, Any]) -> dict[str, Any]:
        """What the editor shows once a skill folder is chosen: name, description, prompt."""
        try:
            skill = skills.load(params.get("source"))
        except skills.SkillError as exc:
            raise ApiError("bad_request", str(exc)) from exc
        return {
            "source": skill.source,
            "file": skill.file,
            "name": skill.name,
            "description": skill.description,
            "prompt": skill.prompt,
        }

    def op_editor_defaults(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "cwd": str(Path.home()),
            "model": DEFAULT_MODEL,
            "timezone": local_timezone(),
            "tools": DEFAULT_TOOLS,
            "permission_mode": DEFAULT_PERMISSION_MODE,
            "permission_modes": list(PERMISSION_MODES),
            "policy": (
                "Claude Code runs with its usual tools and no permission prompts. "
                f"{self.settings.deadline_s:g} second deadline."
            ),
            "verified_models": [DEFAULT_MODEL],
        }

    def op_set_enabled(self, params: dict[str, Any]) -> dict[str, Any]:
        routine = self._routine(params)
        enabled, revision = params.get("enabled"), params.get("expected_revision")
        if (
            not isinstance(enabled, bool)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
        ):
            raise ApiError("bad_request", "enabled and expected_revision are required")
        if enabled:
            self._validated_draft(routine.to_dict())
        try:
            routine = self._store().set_enabled(routine.id, revision, enabled)
        except StoreError as exc:
            raise ApiError("conflict", str(exc)) from exc
        self.wake.set()
        return {"routine": routine.to_dict()}

    def op_set_dispatch(self, params: dict[str, Any]) -> dict[str, Any]:
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            raise ApiError("bad_request", "enabled must be true or false")
        assert self.scheduler is not None
        self.scheduler.set_dispatch(enabled, dt.datetime.now(dt.UTC))
        return {"dispatch_enabled": self.scheduler.enabled}

    def op_schedule_gaps(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "gaps": [
                dict(row)
                for row in self._store().conn.execute(
                    "SELECT * FROM schedule_gaps ORDER BY id DESC LIMIT 100"
                )
            ]
        }

    def op_run_now(self, params: dict[str, Any]) -> dict[str, Any]:
        routine = self._routine(params)
        if routine.deleted_at is not None:
            raise ApiError("not_found", f"routine {routine.id} is deleted")
        key = params.get("idempotency_key")
        if key is not None and (
            not isinstance(key, str) or not key or len(key) > MAX_IDEMPOTENCY_KEY_CHARS
        ):
            raise ApiError("bad_request", "idempotency_key must be a short non-empty string")
        run, created = self._store().enqueue_run(
            routine,
            trigger="manual",
            idempotency_key=key,
            deadline_s=self.settings.deadline_s,
        )
        self.wake.set()
        return {"run": run.to_dict(), "created": created}

    def op_list_runs(self, params: dict[str, Any]) -> dict[str, Any]:
        routine_id = params.get("routine_id")
        limit = params.get("limit", 30)
        if routine_id is not None and not isinstance(routine_id, str):
            raise ApiError("bad_request", "routine_id must be a string")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ApiError("bad_request", "limit must be a positive integer")
        runs = self._store().list_runs(routine_id=routine_id, limit=limit)
        return {"runs": [history.summary(r) for r in runs]}

    def op_get_run(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = params.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ApiError("bad_request", "run_id is required")
        try:
            run = self._store().get_run(run_id)
        except StoreError as exc:
            raise ApiError("not_found", str(exc)) from exc
        return {"run": history.detail(run, self.runs_dir)}

    def op_cancel_run(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = params.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ApiError("bad_request", "run_id is required")
        try:
            run = self._store().request_cancel(run_id)
        except StoreError as exc:
            raise ApiError("conflict", str(exc)) from exc
        self.wake.set()
        return {"run": run.to_dict()}


def local_timezone(env: dict[str, str] | None = None) -> str:
    """The machine's zone name for new routines: ``TZ`` if set, else ``/etc/localtime``."""
    src = os.environ if env is None else env
    candidates = [src.get("TZ", "")]
    try:
        target = os.path.realpath("/etc/localtime")
        candidates.append(target.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in target else "")
    except OSError:
        pass
    for name in candidates:
        if name and not name.startswith(":"):
            try:
                ZoneInfo(name)
            except ZoneInfoNotFoundError, ValueError:
                continue
            return name
    return "UTC"


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return True
        time.sleep(0.05)
    return not Path(f"/proc/{pid}").exists()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omakron.service", description=__doc__)
    parser.add_argument("--state-dir", type=Path, help="default: $XDG_STATE_HOME/omakron")
    parser.add_argument("--config-dir", type=Path, help="default: $XDG_CONFIG_HOME/omakron")
    parser.add_argument(
        "--socket", type=Path, help="default: $XDG_RUNTIME_DIR/omakron/service.sock"
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    service = Service(state=args.state_dir, config=args.config_dir, sock=args.socket)

    def on_signal(signum, frame):  # noqa: ARG001
        log.info("signal %s: stopping", signal.Signals(signum).name)
        service.stopping.set()
        service.wake.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        service.start()
    except Exception as exc:
        log.error("cannot start: %s", exc)
        service.stop()
        return 1
    try:
        service.serve_forever()
    finally:
        service.stop()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
