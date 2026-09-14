"""The run executor: claim, snapshot, launch, judge, store, in that order.

One worker thread per service. It claims the oldest queued run in a
transaction, fetches the issue snapshot and records it before launch, runs the
verified Claude profile under :func:`omakron.runner.supervise`, classifies the
result from supervisor facts plus the report contract, stores the report
durably, and only then writes the terminal status. Any step that cannot store
its output turns the run into a failure, whatever the model said.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from omakron import history, triage
from omakron.runner import (
    STOP_CANCEL,
    STOP_SHUTDOWN,
    Launch,
    Outcome,
    Supervised,
    Verdict,
    child_env,
    classify,
    claude_argv,
    parse_stream,
    supervise,
    write_durably,
)
from omakron.snapshot import SnapshotError, snapshot_digest
from omakron.store import Run, Store, StoreError

log = logging.getLogger("omakron.worker")


class SnapshotSource(Protocol):
    kind: str

    def fetch(self, identifier: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    claude_executable: str
    runs_dir: Path
    managed_workdir: Path  # the only place the worker will create a working folder


class Worker:
    """Owns one store connection, opened on the thread that runs the loop."""

    def __init__(
        self,
        *,
        store_factory: Callable[[], Store],
        source: SnapshotSource,
        config: WorkerConfig,
        worker_id: str,
        wake: threading.Event,
        stopping: threading.Event,
    ):
        self._store_factory = store_factory
        self._store: Store | None = None
        self.source = source
        self.config = config
        self.worker_id = worker_id
        self.wake = wake
        self.stopping = stopping

    # ------------------------------------------------------------------- loop

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    def run_forever(self, idle_wait_s: float = 1.0) -> None:
        while not self.stopping.is_set():
            try:
                worked = self.run_once()
            except Exception:
                log.exception("worker loop error")
                worked = False
            if not worked:
                self.wake.wait(idle_wait_s)
                self.wake.clear()

    def run_once(self) -> bool:
        run = self.store.claim_next(self.worker_id)
        if run is None:
            return False
        log.info("claimed run %s (%s)", run.id, run.parameter)
        self.execute(run)
        return True

    # ---------------------------------------------------------------- execute

    def _fail(self, run: Run, problems: list[str], **extra: Any) -> None:
        log.warning("run %s failed: %s", run.id, problems)
        self.store.finish_run(run.id, status=Outcome.FAILED, problems=problems, **extra)

    def execute(self, run: Run) -> None:
        out_dir = self.config.runs_dir / run.id
        try:
            out_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        except OSError as exc:
            self._fail(run, [f"output could not be stored: {exc}"])
            return
        prepared = self._prepare_input(run, out_dir)
        if prepared is None:
            return
        stdin_text, team_labels, extra_flags = prepared
        cwd = self._ensure_cwd(run, Path(run.routine_snapshot["cwd"]))
        if cwd is None:
            return

        launch = Launch(
            argv=claude_argv(
                run.routine_snapshot["model"],
                executable=self.config.claude_executable,
                extra=extra_flags,
            ),
            cwd=cwd,
            env=child_env(),
            stdin_text=stdin_text,
            deadline_s=run.deadline_s,
        )

        def stop_check() -> str | None:
            if self.stopping.is_set():
                return STOP_SHUTDOWN
            if self.store.cancel_requested(run.id):
                return STOP_CANCEL
            return None

        def on_started(pid: int, pgid: int, proc_start: str | None) -> None:
            self.store.mark_running(
                run.id, pid=pid, pgid=pgid, proc_start=proc_start or "", output_dir=str(out_dir)
            )

        supervised = supervise(
            launch,
            out_dir=out_dir,
            stop_check=stop_check,
            on_started=on_started,
            max_output_bytes=history.output_settings(self.store)["max_output_bytes"],
        )
        if supervised.launch_error:
            self._fail(run, [supervised.launch_error], output_dir=str(out_dir))
            return
        if supervised.storage_error:
            self._fail(run, [supervised.storage_error], output_dir=str(out_dir))
            return
        self._judge_and_store(run, supervised, out_dir, team_labels)

    def _prepare_input(
        self, run: Run, out_dir: Path
    ) -> tuple[str, list[str] | None, list[str]] | None:
        """Fetch and store the issue snapshot; compose stdin. ``None`` means the run failed."""
        prompt = run.routine_snapshot["prompt"]
        if run.routine_snapshot.get("parameter_kind") != triage.PARAMETER_KIND:
            return prompt, None, []
        identifier = run.parameter
        if not identifier:
            self._fail(run, ["issue identifier is required"], output_dir=str(out_dir))
            return None
        try:
            snapshot = self.source.fetch(identifier)
        except SnapshotError as exc:
            self._fail(run, [str(exc)], output_dir=str(out_dir))
            return None
        try:
            write_durably(
                out_dir / "snapshot.json",
                (json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            self.store.set_input_snapshot(run.id, snapshot, snapshot_digest(snapshot))
        except (OSError, StoreError) as exc:
            self._fail(run, [f"input snapshot could not be stored: {exc}"], output_dir=str(out_dir))
            return None
        team_labels = list(snapshot.get("team_labels") or [])
        return triage.compose_stdin(prompt, snapshot), team_labels, triage.system_prompt_flags()

    def _ensure_cwd(self, run: Run, cwd: Path) -> Path | None:
        """Only folders under the managed workdir are created; others must already exist."""
        if cwd.is_dir():
            return cwd
        if not cwd.is_relative_to(self.config.managed_workdir):
            self._fail(run, [f"working folder does not exist: {cwd}"])
            return None
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._fail(run, [f"working folder could not be created: {exc}"])
            return None
        return cwd

    def _judge_and_store(
        self, run: Run, supervised: Supervised, out_dir: Path, team_labels: list[str] | None
    ) -> None:
        try:
            stdout_text = supervised.stdout_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self._fail(run, [f"output could not be read back: {exc}"])
            return
        stream = parse_stream(stdout_text)

        if supervised.output_limit_exceeded:
            verdict = Verdict(Outcome.FAILED, ("output exceeded the size limit; process stopped",))
        else:
            verdict = classify(
                exit_code=supervised.exit_code,
                stream=stream,
                team_labels=team_labels,
                timed_out=supervised.timed_out,
                canceled=supervised.canceled,
                interrupted=supervised.interrupted,
                expected_issue_id=run.parameter,
            )

        report_sha: str | None = None
        if verdict.ok:
            try:
                report_sha = write_durably(
                    out_dir / "report.json",
                    (json.dumps(verdict.report, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                )
            except OSError as exc:
                verdict = Verdict(
                    Outcome.FAILED, (f"output could not be stored: {exc}",), verdict.report
                )

        self.store.finish_run(
            run.id,
            status=verdict.outcome,
            problems=list(verdict.problems),
            exit_code=supervised.exit_code,
            resolved_model=(stream.init or {}).get("model"),
            models_used=stream.resolved_models,
            report=verdict.report,
            output_dir=str(out_dir),
            output_sha256=report_sha,
            stderr_tail=supervised.stderr_tail or None,
        )
        log.info("run %s ended %s", run.id, verdict.outcome)
