"""The report contract accepts synthetic valid data and rejects invalid fixtures."""

from __future__ import annotations

import json

import pytest

from omakron.report import REQUIRED_FIELDS, extract_json_object, validate_report

from .conftest import FIXTURES, TEAM_LABELS

VALID = FIXTURES / "reports" / "valid-triage-report.json"
BROKEN = FIXTURES / "reports" / "exit-zero-malformed.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_valid_synthetic_report_passes():
    assert validate_report(load(VALID), TEAM_LABELS) == []


def test_fixture_built_to_fail_fails_with_named_problems():
    problems = validate_report(load(BROKEN), TEAM_LABELS)
    assert "missing missing_information" in problems
    assert any(p.startswith("proposed.priority 'Critical'") for p in problems)
    assert "proposed.labels ['Needs Triage'] not in team labels" in problems
    assert "next_step too short to be actionable" in problems
    assert "missing_information not a list" in problems


def test_broken_fixture_still_fails_without_label_list():
    """Structure alone must reject it; the label rule is not the only line of defense."""
    assert validate_report(load(BROKEN), None) != []


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_each_required_field_is_required(field):
    obj = load(VALID)
    del obj[field]
    assert f"missing {field}" in validate_report(obj, TEAM_LABELS)


def test_non_object_is_rejected():
    assert validate_report(["not", "an", "object"]) == ["not a JSON object"]
    assert validate_report(None) == ["not a JSON object"]


def test_extract_tolerates_fence_and_prose():
    text = "Sure, here it is:\n```json\n" + VALID.read_text() + "\n```\nLet me know."
    assert extract_json_object(text) == load(VALID)


def test_extract_rejects_non_objects():
    assert extract_json_object("no json here") is None
    assert extract_json_object("[1, 2, 3]") is None
    assert extract_json_object('{"unterminated": ') is None
