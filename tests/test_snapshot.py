"""Snapshot sources name the issue and the reason when they fail, and never leak the key."""

from __future__ import annotations

import json

import pytest

from omakron.snapshot import (
    FixtureSource,
    LinearSource,
    SnapshotError,
    source_from_settings,
    validate_snapshot,
)

from .conftest import FIXTURES

SNAPSHOTS = FIXTURES / "snapshots"
SECRET = "lin_api_TESTSECRET_0123456789"  # noqa: S105 - a test canary, not a credential


def test_fixture_source_returns_the_named_issue():
    snap = FixtureSource(SNAPSHOTS).fetch("DEMO-9999")
    assert snap["issue"]["identifier"] == "DEMO-9999"
    assert snap["requested_identifier"] == "DEMO-9999"
    assert snap["source"] == "fixture"
    assert snap["team_labels"] == ["Bug", "Feature", "Improvement", "Docs"]
    assert validate_snapshot(snap) == []


def test_fixture_source_unknown_issue_names_identifier_and_time():
    with pytest.raises(SnapshotError) as info:
        FixtureSource(SNAPSHOTS).fetch("DEMO-4242")
    err = info.value
    assert err.kind == "unknown"
    assert "DEMO-4242" in str(err) and "inspected 20" in str(err)


def test_fixture_source_rejects_malformed_snapshot(tmp_path):
    (tmp_path / "DEMO-1.json").write_text('{"snapshot_kind": "linear_issue", "issue": 3}')
    with pytest.raises(SnapshotError) as info:
        FixtureSource(tmp_path).fetch("DEMO-1")
    assert info.value.kind == "malformed"
    assert "issue is not an object" in str(info.value)


def linear_payload():
    return {
        "data": {
            "issue": {
                "identifier": "DEMO-123",
                "url": "https://example.invalid/issues/DEMO-123/x",
                "title": "Set up the repository",
                "description": "Body text",
                "priorityLabel": "Medium",
                "state": {"name": "Done"},
                "labels": {"nodes": [{"name": "Improvement"}]},
                "createdAt": "2026-09-11T23:08:00.000Z",
                "updatedAt": "2026-09-12T01:00:00.000Z",
                "team": {"key": "DEMO", "labels": {"nodes": [{"name": "Bug"}, {"name": "Docs"}]}},
                "comments": {
                    "nodes": [
                        {
                            "body": "Looks good",
                            "createdAt": "2026-09-12T00:00:00Z",
                            "user": {"name": "M"},
                        }
                    ]
                },
            }
        }
    }


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "linear-api-key"
    path.write_text(SECRET + "\n")
    return path


def test_linear_source_shapes_the_issue_and_sends_the_key_only_as_a_header(key_file):
    seen = {}

    def transport(body, headers):
        seen["body"] = json.loads(body)
        seen["headers"] = dict(headers)
        return 200, json.dumps(linear_payload()).encode()

    snap = LinearSource(key_file, transport).fetch("DEMO-123")
    assert seen["headers"]["Authorization"] == SECRET
    assert seen["body"]["variables"] == {"id": "DEMO-123"}
    assert snap["issue"]["identifier"] == "DEMO-123"
    assert snap["issue"]["priority"] == "Medium"
    assert snap["issue"]["state"] == "Done"
    assert snap["issue"]["labels"] == ["Improvement"]
    assert snap["team_labels"] == ["Bug", "Docs"]
    assert snap["issue"]["comments"][0]["author"] == "M"
    assert snap["synthetic"] is False
    assert validate_snapshot(snap) == []
    assert SECRET not in json.dumps(snap), "the key must never be part of the snapshot"


def test_linear_unknown_issue_is_an_unknown_failure(key_file):
    def transport(body, headers):
        return 200, json.dumps(
            {"data": {"issue": None}, "errors": [{"message": "Entity not found"}]}
        ).encode()

    with pytest.raises(SnapshotError) as info:
        LinearSource(key_file, transport).fetch("DEMO-424242")
    assert info.value.kind == "unknown"
    assert "DEMO-424242" in str(info.value) and "Entity not found" in str(info.value)
    assert SECRET not in str(info.value)


def test_linear_auth_refusal_is_inaccessible_not_retried(key_file):
    calls = []

    def transport(body, headers):
        calls.append(1)
        return 401, b'{"errors":[{"message":"Authentication required"}]}'

    with pytest.raises(SnapshotError) as info:
        LinearSource(key_file, transport).fetch("DEMO-123")
    assert info.value.kind == "inaccessible" and "401" in str(info.value)
    assert len(calls) == 1
    assert SECRET not in str(info.value)


def test_linear_missing_key_file_is_misconfigured_before_any_request(tmp_path):
    def transport(body, headers):
        raise AssertionError("no request may be made without a key")

    with pytest.raises(SnapshotError) as info:
        LinearSource(tmp_path / "absent", transport).fetch("DEMO-123")
    assert info.value.kind == "misconfigured"
    assert "absent" in str(info.value)


def test_linear_network_error_is_inaccessible(key_file):
    def transport(body, headers):
        raise OSError("network is unreachable")

    with pytest.raises(SnapshotError) as info:
        LinearSource(key_file, transport).fetch("DEMO-123")
    assert info.value.kind == "inaccessible"


def test_source_from_settings(tmp_path):
    assert source_from_settings({}, tmp_path).kind == "linear"
    assert source_from_settings({"kind": "linear"}, tmp_path).api_key_file == (
        tmp_path / "linear-api-key"
    )
    fixture = source_from_settings({"kind": "fixture", "dir": str(SNAPSHOTS)}, tmp_path)
    assert fixture.kind == "fixture"
    with pytest.raises(ValueError):
        source_from_settings({"kind": "fixture"}, tmp_path)
    with pytest.raises(ValueError):
        source_from_settings({"kind": "carrier-pigeon"}, tmp_path)
