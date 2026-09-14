"""Claude CLI invocation profile and run outcome classification.

The invocation profile is shared by the service, tests, and verification scripts.
:func:`supervise` runs one process in its own process group with a wall-clock
deadline, a stop check for cancellation and shutdown, and bounded output
captured to files. :func:`classify` judges the finished process; that rule
does not depend on who launched it.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from omakron.report import extract_json_object, validate_report

DEFAULT_MODEL = "claude-sonnet-5"

# Flags shared by every restricted invocation.
BASE_FLAGS: tuple[str, ...] = (
    "--print",
    "--output-format",
    "stream-json",
    "--verbose",
    "--safe-mode",  # no CLAUDE.md, skills, plugins, hooks, MCP, custom agents
    "--restricted",  # no code-running tools; settings ignored
    "--strict-mcp-config",  # only MCP servers passed via --mcp-config (none)
    "--permission-prompts",
    "none",  # anything that would prompt is denied
    "--no-session-persistence",
    "--disable-slash-commands",
)

# Environment keys passed through to the child. Everything else, including
# CLAUDE_CODE_* from a parent Claude session, is dropped.
ENV_PASSTHROUGH: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "XDG_RUNTIME_DIR", "TZ")


def claude_argv(
    model: str = DEFAULT_MODEL,
    *,
    executable: str = "claude",
    tools: Iterable[str] = (),
    extra: Iterable[str] = (),
) -> list[str]:
    """Build the argument list for one restricted, non-interactive run.

    ``tools`` is empty for the report-only triage routine: the model receives
    the issue snapshot on stdin and has nothing to call. Prompt text is never
    part of argv.
    """
    return [executable, *BASE_FLAGS, "--model", model, "--tools", ",".join(tools), *extra]


def child_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Filtered environment for the child process."""
    src = os.environ if source is None else source
    return {key: src[key] for key in ENV_PASSTHROUGH if key in src}


class Outcome(enum.StrEnum):
    """Terminal run states the UI derives from supervisor evidence."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"


@dataclass(slots=True)
class ParsedStream:
    """What the supervisor keeps from a ``stream-json`` transcript."""

    init: dict | None = None
    result: dict | None = None
    tool_uses: list[dict] = field(default_factory=list)
    assistant_text: list[str] = field(default_factory=list)
    unparsed_lines: int = 0

    @property
    def result_text(self) -> str:
        value = (self.result or {}).get("result")
        return value if isinstance(value, str) else ""

    @property
    def resolved_models(self) -> list[str]:
        usage = (self.result or {}).get("modelUsage") or {}
        return sorted(usage) if isinstance(usage, dict) else []


def parse_stream(stdout: str) -> ParsedStream:
    """Parse the newline-delimited JSON events emitted by ``--output-format stream-json``."""
    parsed = ParsedStream()
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parsed.unparsed_lines += 1
            continue
        if not isinstance(event, dict):
            parsed.unparsed_lines += 1
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            parsed.init = event
        elif kind == "result":
            parsed.result = event
        elif kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    parsed.tool_uses.append(
                        {"name": block.get("name"), "input": block.get("input")}
                    )
                elif block.get("type") == "text":
                    parsed.assistant_text.append(block.get("text", ""))
    return parsed


@dataclass(frozen=True, slots=True)
class Verdict:
    """Outcome plus the reasons, so a failure is explainable in the run detail."""

    outcome: Outcome
    problems: tuple[str, ...] = ()
    report: dict | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCEEDED


def classify(
    *,
    exit_code: int | None,
    stream: ParsedStream,
    team_labels: Iterable[str] | None = None,
    timed_out: bool = False,
    canceled: bool = False,
    interrupted: bool = False,
    expected_issue_id: str | None = None,
) -> Verdict:
    """Judge a finished run.

    The order matters: supervisor facts (cancel, deadline, shutdown, exit
    status) come before anything the model said, and a report is only accepted
    when it parses, passes the contract, and is about the issue that was
    asked for. An exit status of zero with unusable output is a failure, never
    a success with a caveat.
    """
    if canceled:
        return Verdict(Outcome.CANCELED, ("canceled by user",))
    if timed_out:
        return Verdict(Outcome.TIMED_OUT, ("deadline reached before the process exited",))
    if interrupted:
        return Verdict(Outcome.INTERRUPTED, ("service stopped before the process exited",))
    if exit_code is None:
        return Verdict(Outcome.INTERRUPTED, ("process end was not observed",))

    problems: list[str] = []
    if exit_code != 0:
        problems.append(f"exit status {exit_code}")
    if stream.result is None:
        problems.append("no result event in output")
    elif stream.result.get("is_error"):
        problems.append(f"result reported an error: {stream.result_text[:200]!r}")
    if stream.tool_uses:
        problems.append(
            f"forbidden tool use observed: {sorted({t['name'] for t in stream.tool_uses})}"
        )
    if problems:
        return Verdict(Outcome.FAILED, tuple(problems))

    report = extract_json_object(stream.result_text)
    if report is None:
        return Verdict(Outcome.FAILED, ("result text is not a JSON object",))
    contract = validate_report(report, team_labels)
    if contract:
        return Verdict(Outcome.FAILED, tuple(contract), report)
    if expected_issue_id is not None and report.get("issue_id") != expected_issue_id:
        return Verdict(
            Outcome.FAILED,
            (f"report is about {report.get('issue_id')!r}, not {expected_issue_id}",),
            report,
        )
    return Verdict(Outcome.SUCCEEDED, (), report)


# ----------------------------------------------------------------- supervision

MAX_OUTPUT_BYTES = 8 * 1024 * 1024  # stream-json for a report-only run is a few KiB
STDERR_TAIL_CHARS = 2000
STOP_CANCEL = "cancel"
STOP_SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class Launch:
    """Everything needed to start one run. Prompt text is stdin, never argv."""

    argv: list[str]
    cwd: Path
    env: dict[str, str]
    stdin_text: str
    deadline_s: float
    grace_s: float = 3.0


@dataclass(slots=True)
class Supervised:
    """What the supervisor observed. Nothing here comes from the model's text."""

    launch_error: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    stop_reason: str | None = None  # STOP_CANCEL or STOP_SHUTDOWN when stopped by us
    output_limit_exceeded: bool = False
    stop_method: str | None = None
    elapsed_s: float = 0.0
    pid: int | None = None
    pgid: int | None = None
    proc_start: str | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    stdout_bytes: int = 0
    stderr_tail: str = ""
    storage_error: str | None = None  # the captures could not be named or read back

    @property
    def canceled(self) -> bool:
        return self.stop_reason == STOP_CANCEL

    @property
    def interrupted(self) -> bool:
        return self.stop_reason == STOP_SHUTDOWN


def proc_start_ticks(pid: int) -> str | None:
    """Field 22 of ``/proc/<pid>/stat``: start time in clock ticks since boot.

    Stored with the pid so a later instance can tell the same process from a
    reused pid before it stops an orphan.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    rest = stat[stat.rindex(")") + 2 :].split()
    return rest[19] if len(rest) > 19 else None


def stop_process_group(pgid: int, *, grace_s: float, wait: Callable[[float], bool]) -> str:
    """SIGTERM the group, then SIGKILL it if ``wait(grace_s)`` reports it is still alive."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "already gone"
    if wait(grace_s):
        return "SIGTERM"
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    wait(grace_s)
    return "SIGKILL"


def _wait(proc: subprocess.Popen, seconds: float) -> bool:
    try:
        proc.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_durably(path: Path, data: bytes) -> str:
    """Temp file, fsync, rename, fsync the directory. Returns the SHA-256 of ``data``."""
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)
    return hashlib.sha256(data).hexdigest()


def _watch(
    proc: subprocess.Popen,
    launch: Launch,
    result: Supervised,
    *,
    sizes: Callable[[], tuple[int, int]],
    stop_check: Callable[[], str | None],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    poll_s: float,
    max_output_bytes: int,
    t0: float,
) -> str | None:
    """Poll until exit or a reason to stop; returns that reason or ``None`` on exit."""
    while proc.poll() is None:
        requested = stop_check()
        if requested in (STOP_CANCEL, STOP_SHUTDOWN):
            result.stop_reason = requested
            return requested
        if monotonic() - t0 >= launch.deadline_s:
            result.timed_out = True
            return "deadline"
        out_size, err_size = sizes()
        if out_size > max_output_bytes or err_size > max_output_bytes:
            result.output_limit_exceeded = True
            return "output"
        sleep(poll_s)
    return None


def supervise(
    launch: Launch,
    *,
    out_dir: Path,
    stop_check: Callable[[], str | None] = lambda: None,
    on_started: Callable[[int, int, str | None], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 0.1,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> Supervised:
    """Run ``launch`` to completion, deadline, cancellation, or shutdown.

    The child gets its own session and process group; stopping always targets
    the group so helpers cannot outlive the run. stdout and stderr are written
    to ``.part`` files in ``out_dir`` and renamed into place when the process
    ends, so a partial capture is never mistaken for a complete one.
    ``stop_check`` returns ``STOP_CANCEL`` or ``STOP_SHUTDOWN`` to stop early.
    ``on_started`` receives pid, pgid, and the /proc start ticks right after
    launch so the service can record them before anything else happens.
    """
    result = Supervised()
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_part = out_dir / "stdout.jsonl.part"
    stderr_part = out_dir / "stderr.log.part"
    t0 = monotonic()
    try:
        with open(stdout_part, "wb") as out_fh, open(stderr_part, "wb") as err_fh:
            os.fchmod(out_fh.fileno(), 0o600)
            os.fchmod(err_fh.fileno(), 0o600)
            try:
                proc = subprocess.Popen(
                    launch.argv,
                    cwd=str(launch.cwd),
                    env=launch.env,
                    stdin=subprocess.PIPE,
                    stdout=out_fh,
                    stderr=err_fh,
                    start_new_session=True,
                )
            except OSError as exc:
                result.launch_error = f"could not start {launch.argv[0]!r}: {exc}"
                result.elapsed_s = monotonic() - t0
                return result

            result.pid = proc.pid
            result.pgid = os.getpgid(proc.pid)
            result.proc_start = proc_start_ticks(proc.pid)
            try:
                if on_started is not None:
                    on_started(result.pid, result.pgid, result.proc_start)
            except BaseException:
                # Bookkeeping failed after launch: do not leave the child running.
                stop_process_group(
                    result.pgid, grace_s=launch.grace_s, wait=lambda s: _wait(proc, s)
                )
                raise

            def feed_stdin() -> None:
                try:
                    proc.stdin.write(launch.stdin_text.encode("utf-8"))
                    proc.stdin.close()
                except BrokenPipeError, OSError:
                    pass

            threading.Thread(target=feed_stdin, name="omakron-stdin", daemon=True).start()

            def wait(seconds: float) -> bool:
                return _wait(proc, seconds)

            stop_for = _watch(
                proc,
                launch,
                result,
                sizes=lambda: (
                    os.fstat(out_fh.fileno()).st_size,
                    os.fstat(err_fh.fileno()).st_size,
                ),
                stop_check=stop_check,
                monotonic=monotonic,
                sleep=sleep,
                poll_s=poll_s,
                max_output_bytes=max_output_bytes,
                t0=t0,
            )
            if stop_for is not None:
                result.stop_method = stop_process_group(
                    result.pgid, grace_s=launch.grace_s, wait=wait
                )
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            for capture in (out_fh, err_fh):
                if os.fstat(capture.fileno()).st_size > max_output_bytes:
                    result.output_limit_exceeded = True
                    capture.truncate(max_output_bytes)
                capture.flush()
                os.fsync(capture.fileno())
            if not (result.timed_out or result.stop_reason or result.output_limit_exceeded):
                result.exit_code = proc.returncode
            result.elapsed_s = monotonic() - t0
    finally:
        _finalize_captures(result, out_dir, stdout_part, stderr_part)
    return result


def _finalize_captures(
    result: Supervised, out_dir: Path, stdout_part: Path, stderr_part: Path
) -> None:
    """Name the complete captures and read the stderr tail.

    The process has ended, so the ``.part`` files are complete. A failure here
    is a storage failure the caller must report; the run is not allowed to look
    successful because the model finished.
    """
    try:
        for part, final in ((stdout_part, "stdout.jsonl"), (stderr_part, "stderr.log")):
            if part.exists():
                os.replace(part, part.with_name(final))
        _fsync_dir(out_dir)
    except OSError as exc:
        result.storage_error = f"output could not be stored: {exc}"
    result.stdout_path = out_dir / "stdout.jsonl"
    result.stderr_path = out_dir / "stderr.log"
    try:
        result.stdout_bytes = result.stdout_path.stat().st_size
        tail = result.stderr_path.read_bytes()[-4 * STDERR_TAIL_CHARS :]
        result.stderr_tail = tail.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:]
    except OSError as exc:
        result.storage_error = result.storage_error or f"output could not be read back: {exc}"
