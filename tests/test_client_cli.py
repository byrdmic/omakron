"""The client is the surface the popup calls: argv in, JSON out, distinct exit codes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from omakron.client import socket_path

from .conftest import REPO_ROOT


def run_client(*args: str, sock: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: PLW1510 - exit code under test
        [sys.executable, "-m", "omakron.client", "--socket", str(sock), *args],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": ""},
        timeout=30,
    )


def test_unreachable_service_is_exit_three_with_json(tmp_path):
    result = run_client("status", sock=tmp_path / "nope.sock")
    assert result.returncode == 3
    assert json.loads(result.stdout)["error"]["code"] == "unreachable"


def test_usage_error_is_exit_two(tmp_path):
    assert run_client(sock=tmp_path / "x.sock").returncode == 2
    assert run_client("run-now", sock=tmp_path / "x.sock").returncode == 2


def test_socket_path_refuses_to_guess_without_runtime_dir():
    with pytest.raises(RuntimeError):
        socket_path({})
    assert socket_path({"XDG_RUNTIME_DIR": "/run/user/1"}) == Path(
        "/run/user/1/omakron/service.sock"
    )
