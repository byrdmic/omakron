"""The supervisor owns the process group, the deadline, the stop reasons, and the captures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from omakron.runner import STOP_CANCEL, STOP_SHUTDOWN, Launch, claude_argv, supervise

from .conftest import FAKE_CLAUDE


def launch(tmp_path: Path, mode: str, deadline_s: float = 30.0) -> Launch:
    return Launch(
        argv=claude_argv(executable=str(FAKE_CLAUDE)),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "FAKE_CLAUDE_MODE": mode},
        stdin_text="prompt on stdin",
        deadline_s=deadline_s,
        grace_s=1.0,
    )


def group_alive(pgid: int | None) -> bool:
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def test_ok_run_exits_zero_and_names_complete_captures(tmp_path):
    out = tmp_path / "out"
    result = supervise(launch(tmp_path, "ok"), out_dir=out)
    assert result.exit_code == 0 and not result.timed_out and result.stop_reason is None
    assert (out / "stdout.jsonl").exists() and (out / "stderr.log").exists()
    assert not list(out.glob("*.part"))
    assert b'"type": "result"' in (out / "stdout.jsonl").read_bytes()
    assert result.pid and result.proc_start


def test_deadline_stops_the_group_and_reports_timed_out(tmp_path):
    result = supervise(launch(tmp_path, "hang", deadline_s=0.5), out_dir=tmp_path / "out")
    assert result.timed_out and result.exit_code is None
    assert result.stop_method in ("SIGTERM", "SIGKILL")
    assert not group_alive(result.pgid)


def test_stop_check_cancel_and_shutdown_are_distinct(tmp_path):
    canceled = supervise(
        launch(tmp_path, "hang"), out_dir=tmp_path / "a", stop_check=lambda: STOP_CANCEL
    )
    assert canceled.canceled and not canceled.interrupted and not canceled.timed_out
    assert not group_alive(canceled.pgid)
    stopped = supervise(
        launch(tmp_path, "hang"), out_dir=tmp_path / "b", stop_check=lambda: STOP_SHUTDOWN
    )
    assert stopped.interrupted and not stopped.canceled


def test_missing_executable_is_a_launch_error_not_a_crash(tmp_path):
    bad = Launch(
        argv=[str(tmp_path / "no-such-claude")],
        cwd=tmp_path,
        env={},
        stdin_text="",
        deadline_s=5,
    )
    result = supervise(bad, out_dir=tmp_path / "out")
    assert result.launch_error and "no-such-claude" in result.launch_error
    assert result.exit_code is None and result.pid is None


def test_output_limit_stops_the_process(tmp_path):
    result = supervise(launch(tmp_path, "slow"), out_dir=tmp_path / "out", max_output_bytes=10)
    assert result.output_limit_exceeded
    assert not group_alive(result.pgid)


def test_on_started_sees_the_pid_before_the_process_ends(tmp_path):
    seen = []
    supervise(
        launch(tmp_path, "ok"), out_dir=tmp_path / "out", on_started=lambda *a: seen.append(a)
    )
    assert len(seen) == 1
    pid, pgid, start = seen[0]
    assert pid == pgid, "the child leads its own process group"
    assert start


def test_read_only_output_folder_is_a_storage_error(tmp_path):
    if os.geteuid() == 0:
        return  # root ignores directory permissions; the rule is tested as a user
    out = tmp_path / "out"
    out.mkdir()
    fake_env = {"PATH": os.environ.get("PATH", ""), "FAKE_CLAUDE_MODE": "ok"}
    # A fake that makes the folder read-only while running, so the final rename fails.
    script = tmp_path / "lock-then-ok.sh"
    script.write_text(f'#!/bin/sh\nchmod 0500 {out}\nexec {sys.executable} {FAKE_CLAUDE} "$@"\n')
    script.chmod(0o755)
    try:
        result = supervise(
            Launch(argv=[str(script)], cwd=tmp_path, env=fake_env, stdin_text="", deadline_s=30),
            out_dir=out,
        )
    finally:
        out.chmod(0o700)
    assert result.exit_code == 0
    assert result.storage_error and "output could not be stored" in result.storage_error
