"""Explicit connector imports have honest provenance, freshness, and issue scope."""

import datetime as dt
import json
from pathlib import Path

import pytest

from omakron.client import ServiceError
from omakron.snapshot import InboxSource, SnapshotError


def snapshot(identifier="DEMO-9999"):
    value = json.loads((Path(__file__).parent / "fixtures/snapshots/DEMO-9999.json").read_text())
    value.update(
        synthetic=False, source="linear_connector", fetched_at=dt.datetime.now(dt.UTC).isoformat()
    )
    value["issue"]["identifier"] = identifier
    return value


def test_explicit_import_runs_once_and_keeps_original_fetch_time(service_factory):
    service = service_factory(snapshot_source={"kind": "inbox"})
    value = snapshot()
    imported = service.request("import_snapshot", {"snapshot": value})
    assert imported["identifier"] == "DEMO-9999" and not service.launches()
    result = service.wait_run(service.run_now("DEMO-9999")["run"]["id"])
    assert result["status"] == "succeeded"
    assert result["input_snapshot"]["fetched_at"] == value["fetched_at"]
    assert result["input_snapshot"]["source"] == "linear_connector"
    assert len(service.launches()) == 1
    assert (service.config / "inbox/DEMO-9999.json").stat().st_mode & 0o777 == 0o600


def test_missing_and_stale_import_fail_without_a_model_call(service_factory):
    service = service_factory(snapshot_source={"kind": "inbox"})
    result = service.wait_run(service.run_now("DEMO-9999")["run"]["id"])
    assert result["status"] == "failed" and "no imported snapshot" in " ".join(result["problems"])
    value = snapshot()
    value["fetched_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(ServiceError, match="stale"):
        service.request("import_snapshot", {"snapshot": value})
    assert not service.launches()


def test_import_expiry_is_checked_again_at_run_time(tmp_path):
    inbox = InboxSource(tmp_path)
    inbox.accept(snapshot())
    path = tmp_path / "DEMO-9999.json"
    value = json.loads(path.read_text())
    value["fetched_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(value))
    with pytest.raises(SnapshotError, match="stale"):
        inbox.fetch("DEMO-9999")


@pytest.mark.parametrize(
    "patch", [{"synthetic": True}, {"source": "fixture"}, {"fetched_at": "bad"}]
)
def test_invalid_import_does_not_replace_accepted_snapshot(tmp_path, patch):
    inbox = InboxSource(tmp_path)
    inbox.accept(snapshot())
    original = (tmp_path / "DEMO-9999.json").read_bytes()
    with pytest.raises((ValueError, SnapshotError)):
        inbox.accept(dict(snapshot(), **patch))
    assert (tmp_path / "DEMO-9999.json").read_bytes() == original
