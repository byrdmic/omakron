"""Recovery preserves evidence and never treats refresh as authorization to run."""

import datetime as dt
from contextlib import closing
from dataclasses import replace

import pytest

from omakron.client import ServiceError
from omakron.history import failure
from omakron.store import Store
from tests.test_editor import draft


def test_retry_is_explicit_linked_and_deduplicated(service):
    service.set_mode("error")
    original = service.wait_run(service.run_now()["run"]["id"])
    assert original["failure"]["kind"] == "authentication"
    for _ in range(3):
        assert service.request("dashboard")["recent"][0]["id"] == original["id"]
    assert len(service.launches()) == 1
    service.set_mode("ok")
    params = {"run_id": original["id"], "idempotency_key": "explicit-retry"}
    retried = service.request("retry_run", params)
    again = service.request("retry_run", params)
    assert not again["created"] and retried["created"]
    run = service.wait_run(retried["run"]["id"])
    assert run["status"] == "succeeded" and run["retry_of"] == original["id"]
    assert len(service.launches()) == 2


def test_history_survives_edit_delete_restart_and_clock_rollback(service):
    run_id = service.run_now()["run"]["id"]
    original = service.wait_run(run_id)
    routine = service.seed_routine()
    updated = service.request(
        "update_routine",
        dict(
            routine, name="Renamed", routine_id=routine["id"], expected_revision=routine["revision"]
        ),
    )["routine"]
    with closing(Store(service.state / "omakron.db")) as store:
        # Creation order is authoritative even across backwards wall-clock movement.
        newer, _ = store.enqueue_run(
            store.get_routine(routine["id"]),
            trigger="manual",
            idempotency_key="newer",
            deadline_s=600,
            enqueued_at="2000-01-01T00:00:00Z",
        )
        store.conn.execute("UPDATE runs SET ended_at='2099-01-01T00:00:00Z' WHERE id=?", (run_id,))
        assert store.list_runs(routine_id=routine["id"])[0].id == newer.id
    service.request(
        "delete_routine", {"routine_id": routine["id"], "expected_revision": updated["revision"]}
    )
    service.restart()
    assert service.request("dashboard")["routines"] == []
    kept = service.get_run(run_id)
    assert kept["routine_snapshot"] == original["routine_snapshot"]
    assert kept["result_text"] == original["result_text"]
    assert kept["diagnostics"]["stdout.jsonl"]["available"]
    with pytest.raises(ServiceError, match="deleted"):
        service.request("retry_run", {"run_id": run_id, "idempotency_key": "deleted-retry"})


def test_retention_preview_keeps_failed_output_and_results(service):
    success = service.wait_run(service.run_now()["run"]["id"])
    service.set_mode("error")
    failed = service.wait_run(service.run_now()["run"]["id"])
    with closing(Store(service.state / "omakron.db")) as store:
        store.conn.execute("UPDATE runs SET ended_at='2000-01-01T00:00:00Z'")
    service.request("output_settings", {"retention_days": 1, "max_output_bytes": 4096})
    preview = service.request("prune_output")
    assert len(preview["files"]) == 2
    assert all(name.startswith(success["id"]) for name in preview["files"])
    assert (service.run_dir(success["id"]) / "stdout.jsonl").is_file()
    with pytest.raises(ServiceError, match="preview again"):
        service.request("prune_output", {"apply": True, "files": []})
    service.request("prune_output", {"apply": True, "files": preview["files"]})
    assert (service.run_dir(success["id"]) / "result.md").is_file()
    assert (service.run_dir(failed["id"]) / "stdout.jsonl").is_file()
    assert not service.get_run(success["id"])["diagnostics"]["stdout.jsonl"]["available"]
    service.restart()
    assert service.request("output_settings")["max_output_bytes"] == 4096


def test_upcoming_is_chronological_and_all_includes_paused(service):
    routine = service.request("create_routine", draft(service))["routine"]
    dashboard = service.request("dashboard")
    assert len(dashboard["routines"]) == 2 and not dashboard["upcoming"]
    service.request(
        "set_enabled", {"routine_id": routine["id"], "expected_revision": 1, "enabled": True}
    )
    upcoming = service.request("dashboard")["upcoming"]
    assert len(upcoming) == 5
    assert upcoming == sorted(upcoming, key=lambda row: row["utc"])
    assert all(dt.datetime.fromisoformat(row["utc"]).tzinfo for row in upcoming)


@pytest.mark.parametrize(
    "status,problems,kind",
    [
        ("failed", ["model unavailable"], "model"),
        ("failed", ["permission denied"], "permission"),
        ("failed", ["exit status 2"], "failure"),
        ("timed_out", ["deadline"], "timed_out"),
        ("interrupted", [], "interrupted"),
    ],
)
def test_failure_explanation_uses_supervisor_facts(service, status, problems, kind):
    original = service.wait_run(service.run_now()["run"]["id"])
    with closing(Store(service.state / "omakron.db")) as store:
        run = replace(store.get_run(original["id"]), status=status, problems=problems)
        assert failure(run)["kind"] == kind


def test_output_limit_is_bounded_even_when_child_exits_between_polls(service):
    service.request("output_settings", {"retention_days": 30, "max_output_bytes": 1024})
    service.set_mode("chatty")
    result = service.wait_run(service.run_now()["run"]["id"])
    assert result["status"] == "failed"
    assert "size limit" in " ".join(result["problems"])
    assert (service.run_dir(result["id"]) / "stdout.jsonl").stat().st_size <= 1024


def test_oversized_response_is_an_honest_error_and_dashboard_stays_usable(service):
    definition = dict(
        draft(service), schedule_kind="manual", cron=None, timezone=None, prompt="x" * 20000
    )
    for index in range(55):
        service.request("create_routine", dict(definition, name=f"Routine {index}"))
    with pytest.raises(ServiceError) as error:
        service.request("list_routines")
    assert error.value.code == "response_limit"
    assert len(service.request("dashboard")["routines"]) == 56
