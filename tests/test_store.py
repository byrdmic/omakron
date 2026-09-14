"""Run claims are transactional, revisions are immutable, and nothing is replayed."""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
        name="Summary",
        prompt="Summarize the folder.",
        model="claude-sonnet-5",
        cwd=str(tmp_path / "work"),
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
        routine, trigger="manual", idempotency_key="k1", deadline_s=10
    )
    again, created_again = store.enqueue_run(
        routine, trigger="manual", idempotency_key="k1", deadline_s=10
    )
    assert created and not created_again
    assert again.id == first.id
    assert first.routine_snapshot["prompt"] == "Summarize the folder."
    assert first.routine_snapshot["revision"] == 1
    assert len(store.list_runs()) == 1


def test_claim_is_exclusive_and_global_concurrency_is_one(store, routine):
    a, _ = store.enqueue_run(routine, trigger="manual", idempotency_key="a", deadline_s=10)
    b, _ = store.enqueue_run(routine, trigger="manual", idempotency_key="b", deadline_s=10)
    claimed = store.claim_next("w1")
    assert claimed.id == a.id and claimed.status == "claimed" and claimed.worker == "w1"
    assert store.claim_next("w2") is None, "a second claim must wait for the active run"
    store.finish_run(a.id, status="failed", problems=["x"])
    assert store.claim_next("w2").id == b.id


def test_stale_queued_run_is_skipped_not_started(store, routine):
    run, _ = store.enqueue_run(routine, trigger="manual", idempotency_key=None, deadline_s=10)
    old = "2020-01-01T00:00:00.000Z"
    store.conn.execute("UPDATE runs SET enqueued_at = ? WHERE id = ?", (old, run.id))
    assert store.claim_next("w") is None
    skipped = store.get_run(run.id)
    assert skipped.status == "skipped"
    assert skipped.problems == [f"not started within {int(QUEUE_EXPIRY_S)} s of queueing"]


def test_finish_requires_an_open_run_and_a_terminal_status(store, routine):
    run, _ = store.enqueue_run(routine, trigger="manual", idempotency_key=None, deadline_s=10)
    with pytest.raises(StoreError):
        store.finish_run(run.id, status="running", problems=[])
    store.finish_run(run.id, status="succeeded", problems=[], result_text="done")
    with pytest.raises(StoreError):
        store.finish_run(run.id, status="failed", problems=["late"])
    assert store.get_run(run.id).status == "succeeded"


def test_cancel_queued_ends_now_cancel_running_only_flags(store, routine):
    queued, _ = store.enqueue_run(routine, trigger="manual", idempotency_key="q", deadline_s=10)
    running, _ = store.enqueue_run(routine, trigger="manual", idempotency_key="r", deadline_s=10)
    store.claim_next("w")  # claims `queued` (oldest) -- so make the other the queued one
    assert store.request_cancel(running.id).status == "canceled"
    flagged = store.request_cancel(queued.id)
    assert flagged.status == "claimed" and flagged.cancel_requested
    assert store.cancel_requested(queued.id)
    store.finish_run(queued.id, status="canceled", problems=["canceled by user"])
    with pytest.raises(StoreError, match="already ended"):
        store.request_cancel(queued.id)


def test_reconcile_marks_other_workers_runs_interrupted_without_replay(store, routine):
    run, _ = store.enqueue_run(routine, trigger="manual", idempotency_key=None, deadline_s=10)
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
    run, _ = store.enqueue_run(routine, trigger="manual", idempotency_key=None, deadline_s=10)
    store.claim_next("me")
    assert store.reconcile("me") == []
    assert store.get_run(run.id).status == "claimed"


def test_v2_upgrade_adds_the_execution_columns_and_drops_the_triage_ones(tmp_path):
    path = tmp_path / "v2.db"
    with sqlite3.connect(path) as connection:
        connection.executescript((Path(__file__).parent / "fixtures/schema-v2.sql").read_text())
        connection.execute("INSERT INTO schema_meta VALUES('schema_version','2')")
        connection.execute(
            "INSERT INTO routines(id,revision,name,prompt,model,cwd,schedule_kind,"
            "parameter_kind,created_at,updated_at)"
            " VALUES('r',1,'n','p','fake','/','manual','linear_issue','2026','2026')"
        )
    store = Store(path)
    routine = store.get_routine("r")
    assert routine.tools == "default" and routine.permission_mode == "bypassPermissions"
    assert routine.env_passthrough == [] and routine.mcp_config is None
    columns = {row[1] for row in store.conn.execute("PRAGMA table_info(routines)")}
    assert "parameter_kind" not in columns
    run_columns = {row[1] for row in store.conn.execute("PRAGMA table_info(runs)")}
    assert {"parameter", "input_snapshot", "input_sha256", "report"}.isdisjoint(run_columns)
    assert "result_text" in run_columns
    assert next(tmp_path.glob("*.v2-*.backup")).is_file()
    store.close()


def test_execution_choices_are_saved_and_snapshotted(store, tmp_path):
    routine = store.create_routine(
        name="Coder",
        prompt="Implement the ready card.",
        model="claude-sonnet-5",
        cwd=str(tmp_path),
        tools="Bash,Edit,Read",
        permission_mode="acceptEdits",
        env_passthrough=["GH_TOKEN"],
    )
    run, _ = store.enqueue_run(routine, trigger="manual", idempotency_key=None, deadline_s=10)
    snapshot = run.routine_snapshot
    assert snapshot["tools"] == "Bash,Edit,Read"
    assert snapshot["permission_mode"] == "acceptEdits"
    assert snapshot["env_passthrough"] == ["GH_TOKEN"]
    changed = store.update_routine(routine.id, routine.revision, dict(routine.to_dict(), tools=""))
    assert changed.tools == ""
    assert store.get_run(run.id).routine_snapshot["tools"] == "Bash,Edit,Read"


def test_result_text_is_kept_and_bounded(store, routine):
    run, _ = store.enqueue_run(routine, trigger="manual", idempotency_key=None, deadline_s=10)
    store.finish_run(run.id, status="succeeded", problems=[], result_text="x" * 70_000)
    assert len(store.get_run(run.id).result_text) == 64 * 1024


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
    run, _ = store.enqueue_run(routine, trigger="manual", idempotency_key=None, deadline_s=10)
    store.conn.execute(
        "UPDATE routines SET prompt = 'changed', revision = 2 WHERE id = ?", (routine.id,)
    )
    assert store.get_run(run.id).routine_snapshot["prompt"] == "Summarize the folder."
    assert store.get_run(run.id).routine_revision == 1
