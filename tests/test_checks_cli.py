"""The command CI runs must pass on real inputs and fail on the fixtures built to fail."""

from __future__ import annotations

import subprocess
import sys

from .conftest import FIXTURES, REPO_ROOT

VALID_REPORT = FIXTURES / "reports" / "valid-triage-report.json"
BROKEN_REPORT = FIXTURES / "reports" / "exit-zero-malformed.json"
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


def test_valid_report_passes():
    result = run("report", str(VALID_REPORT), "--team-labels", "Bug,Feature,Improvement,Docs")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "report: ok"


def test_broken_report_fails_with_problems_listed():
    result = run("report", str(BROKEN_REPORT), "--team-labels", "Bug,Feature,Improvement,Docs")
    assert result.returncode == 1
    assert "report: FAIL (5 problems)" in result.stdout
    assert "missing missing_information" in result.stdout


def test_repository_manifest_passes():
    result = run("manifest", str(REPO_ROOT))
    assert result.returncode == 0, result.stdout + result.stderr


def test_broken_manifest_fails():
    result = run("manifest", str(BROKEN_PLUGIN))
    assert result.returncode == 1
    assert "entry point file not found" in result.stdout


def test_unreadable_report_is_a_failure_not_a_crash():
    result = run("report", str(FIXTURES / "does-not-exist.json"))
    assert result.returncode == 1
    assert result.stdout.startswith("cannot read report:")


def test_usage_error_is_distinct():
    assert run().returncode == 2
