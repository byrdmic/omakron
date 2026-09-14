"""Start the real service as a subprocess against a fake ``claude`` and temp XDG dirs.

The service gets its own state, config, and socket under the test's temporary
folder. ``claude`` is a wrapper script that points the fake CLI at a mode file
the test can rewrite between runs, so one service instance can see a healthy
run, an auth failure, and a hang in sequence. Nothing here touches the real
``~/.config`` or ``~/.local/state``.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omakron import client as omakron_client

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
FAKE_CLAUDE = TESTS_DIR / "fake_claude.py"
MAX_SOCKET_PATH = 100  # AF_UNIX paths are limited to 108 bytes on Linux


def short_socket_dir(preferred: Path) -> Path:
    """Use the test folder unless its path would overflow the socket address."""
    candidate = preferred / "sock"
    if len(str(candidate / "service.sock")) <= MAX_SOCKET_PATH:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    return Path(tempfile.mkdtemp(prefix="omk-"))


def tree_digest(root: Path) -> tuple[str, list[str]]:
    h = hashlib.sha256()
    names = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root).as_posix()
        names.append(rel)
        h.update(rel.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest(), names


@dataclass
class ServiceHarness:
    root: Path
    deadline_s: float = 600.0
    extra_env: dict[str, str] = field(default_factory=dict)
    proc: subprocess.Popen | None = None

    def __post_init__(self) -> None:
        self.state = self.root / "state"
        self.config = self.root / "config"
        self.socket = short_socket_dir(self.root) / "service.sock"
        self.mode_file = self.root / "fake-mode"
        self.launch_log = self.root / "launches.jsonl"
        self.argv_file = self.root / "argv.json"
        self.wrapper = self.root / "bin" / "claude"
        self.log_file = self.root / "service.log"
        self.config.mkdir(parents=True, exist_ok=True)
        self.wrapper.parent.mkdir(parents=True, exist_ok=True)
        self.set_mode("ok")
        self.wrapper.write_text(
            "#!/bin/sh\n"
            f"FAKE_CLAUDE_MODE_FILE={self.mode_file} "
            f"FAKE_CLAUDE_ARGV_FILE={self.argv_file} "
            f"FAKE_CLAUDE_LAUNCH_LOG={self.launch_log} "
            f'exec {sys.executable} {FAKE_CLAUDE} "$@"\n',
            encoding="utf-8",
        )
        self.wrapper.chmod(0o755)
        (self.config / "settings.json").write_text(
            json.dumps({"claude_executable": str(self.wrapper), "deadline_s": self.deadline_s}),
            encoding="utf-8",
        )

    # --------------------------------------------------------------- process

    def env(self) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.root / "home"),
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "XDG_RUNTIME_DIR": str(self.socket.parent),
        }
        env.update(self.extra_env)
        return env

    def start(self, timeout_s: float = 15.0) -> ServiceHarness:
        assert self.proc is None or self.proc.poll() is not None
        (self.root / "home").mkdir(exist_ok=True)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omakron.service",
                "--state-dir",
                str(self.state),
                "--config-dir",
                str(self.config),
                "--socket",
                str(self.socket),
                "--log-level",
                "DEBUG",
            ],
            env=self.env(),
            cwd=str(self.root),
            stdout=subprocess.DEVNULL,
            stderr=open(self.log_file, "ab"),  # noqa: SIM115 - owned by the child
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"service exited early:\n{self.log_text()}")
            try:
                self.request("ping")
                return self
            except omakron_client.ServiceUnreachable:
                time.sleep(0.05)
        raise RuntimeError(f"service did not answer in {timeout_s}s:\n{self.log_text()}")

    def stop(self, timeout_s: float = 15.0) -> int:
        assert self.proc is not None
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(timeout=5)

    def kill(self) -> None:
        """SIGKILL the service only; a supervised child is in its own process group."""
        assert self.proc is not None
        self.proc.kill()
        self.proc.wait(timeout=5)

    def restart(self) -> ServiceHarness:
        self.stop()
        return self.start()

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.stop()
        # Reap any fake child still alive from a deliberately killed service.
        for run in self.all_runs_from_db():
            pid = run.get("pid")
            if pid and Path(f"/proc/{pid}").exists():
                try:
                    os.killpg(run["pgid"], signal.SIGKILL)
                except ProcessLookupError, PermissionError, TypeError:
                    pass

    def log_text(self) -> str:
        return self.log_file.read_text(encoding="utf-8", errors="replace")

    # ------------------------------------------------------------------- api

    def request(self, op: str, params: dict[str, Any] | None = None) -> Any:
        return omakron_client.request(op, params, sock=self.socket)

    def client(
        self, *args: str, timeout: float = 60, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: PLW1510 - exit code under test
            [sys.executable, "-m", "omakron.client", "--socket", str(self.socket), *args],
            capture_output=True,
            text=True,
            env=self.env(),
            cwd=str(self.root),
            input=stdin,
            timeout=timeout,
        )

    def seed_routine(self) -> dict[str, Any]:
        """The routine the service seeds."""
        routines = self.request("list_routines")["routines"]
        assert len(routines) >= 1
        return routines[0]

    def run_now(self, key: str | None = None) -> dict[str, Any]:
        """Queue one run of the seeded routine."""
        params: dict[str, Any] = {"routine_id": self.seed_routine()["id"]}
        if key is not None:
            params["idempotency_key"] = key
        return self.request("run_now", params)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self.request("get_run", {"run_id": run_id})["run"]

    def wait_run(self, run_id: str, timeout_s: float = 30.0) -> dict[str, Any]:
        run = omakron_client.wait_for_run(
            run_id, timeout_s=timeout_s, interval_s=0.1, sock=self.socket
        )
        if run["status"] not in omakron_client.TERMINAL:
            raise AssertionError(f"run {run_id} still {run['status']} after {timeout_s}s")
        return run

    def wait_status(self, run_id: str, status: str, timeout_s: float = 15.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            run = self.get_run(run_id)
            if run["status"] == status:
                return run
            if run["status"] in omakron_client.TERMINAL:
                raise AssertionError(f"run ended {run['status']} before reaching {status}: {run}")
            time.sleep(0.05)
        raise AssertionError(f"run {run_id} did not reach {status} in {timeout_s}s")

    # -------------------------------------------------------------- evidence

    def set_mode(self, mode: str) -> None:
        self.mode_file.write_text(mode + "\n", encoding="utf-8")

    def launches(self) -> list[dict[str, Any]]:
        if not self.launch_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.launch_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def recorded_argv(self) -> list[str]:
        return json.loads(self.argv_file.read_text(encoding="utf-8"))

    def all_runs_from_db(self) -> list[dict[str, Any]]:
        """Read pid/pgid straight from SQLite, for cleanup when the service is dead."""
        db = self.state / "omakron.db"
        if not db.exists():
            return []
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT id, pid, pgid, status FROM runs")]
        finally:
            conn.close()

    def run_dir(self, run_id: str) -> Path:
        return self.state / "runs" / run_id


def process_alive(pid: int | None) -> bool:
    return bool(pid) and Path(f"/proc/{pid}").exists()


def wait_until(predicate, timeout_s: float = 10.0, interval_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()
