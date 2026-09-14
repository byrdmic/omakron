"""The command CI runs must pass on real inputs and fail on the fixtures built to fail."""

from __future__ import annotations

import subprocess
import sys

from .conftest import FIXTURES, REPO_ROOT

BROKEN_PLUGIN = FIXTURES / "plugins" / "missing-entrypoint"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: PLW1510 - exit code under test
        [sys.executable, "-m", "omakron.checks", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": ""},
        timeout=30,
    )


def test_repository_manifest_passes():
    result = run("manifest", str(REPO_ROOT))
    assert result.returncode == 0, result.stdout + result.stderr


def test_broken_manifest_fails():
    result = run("manifest", str(BROKEN_PLUGIN))
    assert result.returncode == 1
    assert "entry point file not found" in result.stdout


def test_stream_check_records_a_clean_exit(tmp_path):
    transcript = tmp_path / "stdout.jsonl"
    transcript.write_text('{"type": "result", "is_error": false, "result": "done"}\n')
    assert run("stream", str(transcript)).returncode == 0
    failed = run("stream", str(transcript), "--exit-code", "1")
    assert failed.returncode == 1 and "exit status 1" in failed.stdout
    assert run("stream", str(tmp_path / "missing.jsonl")).stdout.startswith("cannot read")


def test_usage_error_is_distinct():
    assert run().returncode == 2
