"""The run parameter is validated before anything starts; prompt text stays on stdin."""

from __future__ import annotations

import json

import pytest

from omakron import triage
from omakron.runner import DEFAULT_MODEL, claude_argv


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("DEMO-123", "DEMO-123"), (" demo-123 ", "DEMO-123"), ("A1-7", "A1-7")],
)
def test_identifier_is_normalized(raw, expected):
    assert triage.normalize_identifier(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "DEMO",
        "DEMO-",
        "-309",
        "DEMO-0",
        "DEMO 309",
        "DEMO-123; rm -rf /",
        "x" * 30,
        None,
        309,
    ],
)
def test_malformed_identifier_is_rejected(raw):
    with pytest.raises(ValueError):
        triage.normalize_identifier(raw)


def test_stdin_carries_prompt_instructions_and_the_exact_snapshot():
    snapshot = {"snapshot_kind": "linear_issue", "issue": {"identifier": "DEMO-9999"}}
    text = triage.compose_stdin("PROMPT TEXT", snapshot)
    assert text.startswith("PROMPT TEXT\n\n")
    assert triage.REPORT_FORMAT_INSTRUCTIONS in text
    body = text.split("<issue_snapshot>\n", 1)[1].split("</issue_snapshot>", 1)[0]
    assert json.loads(body) == snapshot


def test_prompt_never_reaches_argv():
    argv = claude_argv(DEFAULT_MODEL, extra=triage.system_prompt_flags())
    assert triage.PROMPT not in " ".join(argv)
    assert argv[argv.index("--system-prompt") + 1] == triage.SYSTEM_PROMPT


def test_seed_definition_is_the_accepted_routine():
    seed = triage.seed_definition("/state/workdir/triage")
    assert seed["model"] == "claude-sonnet-5"
    assert seed["schedule_kind"] == "manual"
    assert seed["enabled"] is False
    assert seed["parameter_kind"] == "linear_issue"
    assert seed["prompt"] == triage.PROMPT
