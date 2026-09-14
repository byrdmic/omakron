"""Shared fixtures: a fake clock and a fake Claude executable."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from .service_harness import ServiceHarness

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures"
FAKE_CLAUDE = TESTS_DIR / "fake_claude.py"


@dataclass
class FakeClock:
    """Controllable wall and monotonic clocks for schedule and deadline tests."""

    wall: float = 1_800_000_000.0
    mono: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds

    def jump_wall(self, seconds: float) -> None:
        """Move wall time without monotonic time, like an NTP step or timezone change."""
        self.wall += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@dataclass
class FakeClaudeRun:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str


@pytest.fixture
def fake_claude(tmp_path: Path):
    """Run the fake CLI in ``mode`` with the given argv and stdin; return the captured run."""

    def run(
        argv: list[str], *, mode: str = "ok", stdin: str = "", timeout: float = 10
    ) -> FakeClaudeRun:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "FAKE_CLAUDE_MODE": mode,
            "FAKE_CLAUDE_ARGV_FILE": str(tmp_path / "argv.json"),
        }
        proc = subprocess.run(  # noqa: PLW1510 - the exit code is the thing under test
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            timeout=timeout,
        )
        return FakeClaudeRun(argv, proc.returncode, proc.stdout, proc.stderr)

    return run


@pytest.fixture
def service(tmp_path: Path):
    """A running service with the fake CLI."""
    harness = ServiceHarness(tmp_path).start()
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture
def service_factory(tmp_path: Path):
    """Build services with non-default settings (deadline, extra environment)."""
    made: list[ServiceHarness] = []

    def make(name: str = "svc", **kwargs) -> ServiceHarness:
        harness = ServiceHarness(tmp_path / name, **kwargs)
        made.append(harness)
        return harness.start()

    try:
        yield make
    finally:
        for harness in made:
            harness.close()
