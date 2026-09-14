"""Claude CLI invocation profile, process supervision, and run outcome.

The invocation profile is shared by the service, tests, and smoke scripts. A
routine chooses its tools, permission mode, MCP config, and extra environment
keys; the service launches Claude Code with those choices and does not add a
permission layer of its own (see docs/PHILOSOPHY.md). :func:`supervise` runs
one process in its own process group with a wall-clock deadline, a stop check
for cancellation and shutdown, and bounded output captured to files.
:func:`classify` records how the process ended. A run succeeds when the
process exits cleanly and its result event reports no error.
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

DEFAULT_MODEL = "claude-sonnet-5"

# Flags shared by every non-interactive run. Nobody answers permission prompts
# during a scheduled run, so the permission mode decides everything.
BASE_FLAGS: tuple[str, ...] = (
    "--print",
    "--output-format",
    "stream-json",
    "--verbose",
    "--permission-prompts",
    "none",
    "--no-session-persistence",
    "--disable-slash-commands",
)

# Permission modes the CLI accepts. Runs are unattended, so the default lets
# the model act without a prompt; a routine may choose a narrower mode.
PERMISSION_MODES: tuple[str, ...] = (
    "bypassPermissions",
    "acceptEdits",
    "auto",
    "dontAsk",
    "manual",
    "plan",
)
DEFAULT_PERMISSION_MODE = "bypassPermissions"
DEFAULT_TOOLS = "default"  # the CLI's own tool set; "" disables every tool

# Environment keys every child inherits: the session basics that the CLI, its
# skills, and MCP servers commonly need. CLAUDE_CODE_* from a parent Claude
# session is never inherited. A routine names further keys in env_passthrough.
ENV_PASSTHROUGH: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TZ",
    "TERM",
    "EDITOR",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "DBUS_SESSION_BUS_ADDRESS",
    "SSH_AUTH_SOCK",
    "CLAUDE_CONFIG_DIR",
)
ENV_KEY_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"
MAX_ENV_KEYS = 64


def claude_argv(
    model: str = DEFAULT_MODEL,
    *,
    executable: str = "claude",
    tools: str = DEFAULT_TOOLS,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    mcp_config: str | None = None,
    extra: Iterable[str] = (),
) -> list[str]:
    """Build the argument list for one non-interactive run.

    ``tools`` is the CLI's ``--tools`` value: ``"default"`` for its full set,
    ``""`` for none, or a comma-separated list. ``bypassPermissions`` also needs
    the CLI's explicit opt-in flag. Prompt text is never part of argv.
    """
    if permission_mode not in PERMISSION_MODES:
        raise ValueError(f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")
    argv = [executable, *BASE_FLAGS, "--model", model, "--tools", tools]
    argv += ["--permission-mode", permission_mode]
    if permission_mode == "bypassPermissions":
        argv.append("--dangerously-skip-permissions")
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
    argv.extend(extra)
    return argv


def child_env(
    source: Mapping[str, str] | None = None, *, extra: Iterable[str] = ()
) -> dict[str, str]:
    """Environment for the child: the base passthrough plus the routine's own keys."""
    src = os.environ if source is None else source
    keys = [*ENV_PASSTHROUGH, *extra]
    return {key: src[key] for key in keys if key in src and not key.startswith("CLAUDE_CODE_")}


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

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCEEDED


def classify(
    *,
    exit_code: int | None,
    stream: ParsedStream,
    timed_out: bool = False,
    canceled: bool = False,
    interrupted: bool = False,
) -> Verdict:
    """Record how a run ended.

    Supervisor facts (cancel, deadline, shutdown) come first. After that a run
    succeeds when the process exited with status zero and its result event
    reports no error. What the model said is kept, not judged.
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
    if problems:
        return Verdict(Outcome.FAILED, tuple(problems))
    return Verdict(Outcome.SUCCEEDED, ())


# ----------------------------------------------------------------- supervision

MAX_OUTPUT_BYTES = 8 * 1024 * 1024  # a routine that uses tools can produce a long transcript
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
