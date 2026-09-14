"""Run claims are transactional, snapshots are written once, and nothing is replayed."""

from __future__ import annotations

import sqlite3

import pytest

from omakron.store import QUEUE_EXPIRY_S, Store, StoreError


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def routine(store, tmp_path):
    return store.create_routine(
        name="Triage",
        prompt="Review the issue.",
        model="claude-sonnet-5",
        cwd=str(tmp_path / "work"),
        parameter_kind="linear_issue",
    )


def test_new_routine_is_paused_manual_revision_one(routine):
    assert routine.revision == 1
    assert routine.enabled is False
    assert routine.schedule_kind == "manual"
    assert routine.cron is None and routine.timezone is None


def test_manual_routine_rejects_cron_fields(store):
    with pytest.raises(StoreError):
        store.create_routine(
            name="x", prompt="y", model="m", cwd="/", cron="* * * * *", timezone="UTC"
        )
    with pytest.raises(StoreError, match="cron schedule"):
        store.create_routine(name="x", prompt="y", model="m", cwd="/", schedule_kind="cron")


def test_enqueue_is_idempotent_per_key(store, routine):
    first, created = store.enqueue_run(
        routine, trigger="manual", parameter="DEMO-1", idempotency_key="k1", deadline_s=10
    )
    again, created_again = store.enqueue_run(
        routine, trigger="manual", parameter="DEMO-1", idempotency_key="k1", deadline_s=10
    )
    assert created and not created_again
    assert again.id == first.id
    assert first.routine_snapshot["prompt"] == "Review the issue."
    assert first.routine_snapshot["revision"] == 1
    assert len(store.list_runs()) == 1


def test_claim_is_exclusive_and_global_concurrency_is_one(store, routine):
    a, _ = store.enqueue_run(
        routine, trigger="manual", parameter="DEMO-1", idempotency_key="a", deadline_s=10
    )
    b, _ = store.enqueue_run(
        routine, trigger="manual", parameter="DEMO-2", idempotency_key="b", deadline_s=10
    )
    claimed = store.claim_next("w1")
    assert claimed.id == a.id and claimed.status == "claimed" and claimed.worker == "w1"
    assert store.claim_next("w2") is None, "a second claim must wait for the active run"
    store.finish_run(a.id, status="failed", problems=["x"])
    assert store.claim_next("w2").id == b.id


def test_stale_queued_run_is_skipped_not_started(store, routine):
    run, _ = store.enqueue_run(
        routine, trigger="manual", parameter="DEMO-1", idempotency_key=None, deadline_s=10
    )
    old = "2020-01-01T00:00:00.000Z"
    store.conn.execute("UPDATE runs SET enqueued_at = ? WHERE id = ?", (old, run.id))
    assert store.claim_next("w") is None
    skipped = store.get_run(run.id)
    assert skipped.status == "skipped"
    assert skipped.problems == [f"not started within {int(QUEUE_EXPIRY_S)} s of queueing"]


def test_input_snapshot_is_written_exactly_once(store, routine):
    run, _ = store.enqueue_run(
        routine, trigger="manual", parameter="DEMO-1", idempotency_key=None, deadline_s=10
    )
    store.set_input_snapshot(run.id, {"issue": 1}, "abc")
    with pytest.raises(StoreError):
        store.set_input_snapshot(run.id, {"issue": 2}, "def")
    assert store.get_run(run.id).input_snapshot == {"issue": 1}


def test_finish_requires_an_open_run_and_a_terminal_status(store, routine):
    run, _ = store.enqueue_run(
        routine, trigger="manual", parameter="DEMO-1", idempotency_key=None, deadline_s=10
    )
    with pytest.raises(StoreError):
        store.finish_run(run.id, status="running", problems=[])
    store.finish_run(run.id, status="succeeded", problems=[], report={"issue_id": "DEMO-1"})
    with pytest.raises(StoreError):
        store.finish_run(run.id, status="failed", problems=["late"])
    assert store.get_run(run.id).status == "succeeded"


def test_cancel_queued_ends_now_cancel_running_only_flags(store, routine):
    queued, _ = store.enqueue_run(
        routine, trigger="manual", parameter="DEMO-1", idempotency_key="q", deadline_s=10
    )
    running, _ = store.enqueue_run(
        routine, trigger="manual", parameter="DEMO-2", idempotency_key="r", deadline_s=10
    )
    store.claim_next("w")  # claims `queued` (oldest) -- so make the other the queued one
    assert store.request_cancel(running.id).status == "canceled"
    flagged = store.request_cancel(queued.id)
    assert flagged.status == "claimed" and flagged.cancel_requested
    assert store.cancel_requested(queued.id)
    store.finish_run(queued.id, status="canceled", problems=["canceled by user"])
    with pytest.raises(StoreError, match="already ended"):
        store.request_cancel(queued.id)


def test_reconcile_marks_other_workers_runs_interrupted_without_replay(store, routine):
    run, _ = store.enqueue_run(
        routine, trigger="manual", parameter="DEMO-1", idempotency_key=None, deadline_s=10
    )
    store.claim_next("old-worker")
    store.mark_running(run.id, pid=4242, pgid=4242, proc_start="99", output_dir="/x")
    interrupted = store.reconcile("new-worker")
    assert [r.id for r in interrupted] == [run.id]
    assert interrupted[0].pid == 4242
    after = store.get_run(run.id)
    assert after.status == "interrupted"
    assert "not replayed" in after.problems[0]
    assert store.claim_next("new-worker") is None, "an interrupted run never re-enters the queue"


def test_reconcile_leaves_own_runs_alone(store, routine):
    run, _ = store.enqueue_run(
        routine, trigger="manual", parameter="DEMO-1", idempotency_key=None, deadline_s=10
    )
    store.claim_next("me")
    assert store.reconcile("me") == []
    assert store.get_run(run.id).status == "claimed"


def test_schema_version_mismatch_refuses_to_open(tmp_path):
    path = tmp_path / "v.db"
    Store(path).close()
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="schema version 999"):
        Store(path)


def test_routine_snapshot_survives_later_routine_state(store, routine):
    run, _ = store.enqueue_run(
        routine, trigger="manual", parameter="DEMO-1", idempotency_key=None, deadline_s=10
    )
    store.conn.execute(
        "UPDATE routines SET prompt = 'changed', revision = 2 WHERE id = ?", (routine.id,)
    )
    assert store.get_run(run.id).routine_snapshot["prompt"] == "Review the issue."
    assert store.get_run(run.id).routine_revision == 1
