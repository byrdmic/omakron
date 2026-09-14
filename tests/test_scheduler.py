"""U06/U07 run against a real store and a deterministic wall clock."""

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from omakron.scheduler import Scheduler, stamp
from omakron.store import Store, StoreError


@pytest.fixture
def lab(tmp_path, monkeypatch):
    now = [dt.datetime(2026, 9, 12, 8, 59, tzinfo=dt.UTC)]
    monkeypatch.setattr("omakron.store.utc_now", lambda: stamp(now[0]))
    store = Store(tmp_path / "lab.db")
    scheduler = Scheduler(store, 10)
    yield store, scheduler, now
    store.close()


def routine(store, name="Report", cron="* * * * *", timezone="UTC"):
    return store.create_routine(
        name=name,
        prompt="Original",
        model="fake",
        cwd=str(store.path.parent),
        schedule_kind="cron",
        cron=cron,
        timezone=timezone,
        enabled=True,
    )


def test_preview_matches_dispatch_and_slots_survive_restart(lab):
    store, scheduler, now = lab
    r = routine(store)
    scheduler.tick(now[0])
    first = store.list_runs()[0]
    store.finish_run(first.id, status="succeeded", problems=[])
    now[0] += dt.timedelta(minutes=1)
    ids = scheduler.tick(now[0])
    assert len(ids) == 1
    run = store.get_run(ids[0])
    assert run.scheduled_at == "2026-09-12T09:00:00.000Z"
    assert run.routine_snapshot["cron"] == "* * * * *"
    reopened = Store(store.path)
    assert Scheduler(reopened, 10).tick(now[0]) == []
    assert len(reopened.list_runs(routine_id=r.id)) == 2
    reopened.close()


def test_same_routine_overlap_is_saved_as_skipped(lab):
    store, scheduler, now = lab
    routine(store)
    first = scheduler.tick(now[0])[0]
    assert store.claim_next("worker").id == first
    now[0] += dt.timedelta(minutes=1)
    skipped = scheduler.tick(now[0])[0]
    assert store.get_run(skipped).status == "skipped"
    assert store.get_run(skipped).problems == ["same-routine overlap"]
    assert store.get_run(first).status == "claimed"


def test_different_routine_waits_and_expires_while_worker_busy(lab):
    store, scheduler, now = lab
    routine(store, "A")
    routine(store, "B")
    scheduler.tick(now[0])
    first = store.claim_next("worker")
    other = [r for r in store.list_runs() if r.id != first.id][0]
    now[0] += dt.timedelta(seconds=300.001)
    scheduler.tick(now[0])
    assert store.get_run(other.id).status == "skipped"
    assert "not started within 300 s" in store.get_run(other.id).problems[0]
    assert store.get_run(first.id).status == "claimed"


def test_pause_removes_queued_schedule_but_leaves_active_run(lab):
    store, scheduler, now = lab
    r = routine(store)
    first = scheduler.tick(now[0])[0]
    store.claim_next("worker")
    paused = store.set_enabled(r.id, r.revision, False)
    assert store.get_run(first).status == "claimed"
    now[0] += dt.timedelta(days=7)
    assert scheduler.tick(now[0]) == []
    resumed = store.set_enabled(r.id, paused.revision, True)
    assert resumed.enabled
    assert scheduler.tick(now[0]) == []
    store.finish_run(first, status="succeeded", problems=[])
    now[0] += dt.timedelta(minutes=1)
    assert len(scheduler.tick(now[0])) == 1


def test_pause_before_claim_skips_the_queue(lab):
    store, scheduler, now = lab
    r = routine(store)
    queued = scheduler.tick(now[0])[0]
    store.set_enabled(r.id, r.revision, False)
    assert store.get_run(queued).status == "skipped"
    assert store.claim_next("worker") is None


def test_edit_running_keeps_snapshot_and_next_run_uses_new_revision(lab):
    store, scheduler, now = lab
    r = routine(store)
    active = scheduler.tick(now[0])[0]
    store.claim_next("worker")
    changed = store.update_routine(r.id, r.revision, dict(r.to_dict(), prompt="Changed"))
    assert not changed.enabled
    assert store.get_run(active).routine_snapshot["prompt"] == "Original"
    resumed = store.set_enabled(r.id, changed.revision, True)
    store.finish_run(active, status="succeeded", problems=[])
    now[0] += dt.timedelta(minutes=1)
    next_run = store.get_run(scheduler.tick(now[0])[0])
    assert next_run.routine_revision == resumed.revision
    assert next_run.routine_snapshot["prompt"] == "Changed"


def test_offline_period_summarizes_gap_without_catchup_burst(lab):
    store, scheduler, now = lab
    routine(store)
    first = scheduler.tick(now[0])[0]
    store.finish_run(first, status="succeeded", problems=[])
    now[0] += dt.timedelta(days=14, seconds=5)
    assert len(scheduler.tick(now[0])) == 1
    assert scheduler.tick(now[0]) == []
    gaps = store.conn.execute("SELECT * FROM schedule_gaps").fetchall()
    assert gaps[-1]["reason"].endswith("missed occurrences skipped")
    assert len(store.list_runs()) == 2


def test_backward_jump_and_edit_cannot_relaunch_same_slot(lab):
    store, scheduler, now = lab
    r = routine(store)
    first = scheduler.tick(now[0])[0]
    store.finish_run(first, status="succeeded", problems=[])
    now[0] -= dt.timedelta(seconds=30)
    changed = store.update_routine(r.id, 1, dict(r.to_dict(), prompt="Changed"))
    store.set_enabled(r.id, changed.revision, True)
    assert scheduler.tick(now[0]) == []
    now[0] += dt.timedelta(seconds=30)
    assert scheduler.tick(now[0]) == []


def test_autumn_repeated_minute_and_schedule_edit_never_replay(lab):
    store, scheduler, now = lab
    r = routine(store, cron="30 1 * * *", timezone="America/New_York")
    now[0] = dt.datetime(2026, 11, 1, 5, 30, tzinfo=dt.UTC)
    first = scheduler.tick(now[0])[0]
    store.finish_run(first, status="succeeded", problems=[])
    changed = store.update_routine(r.id, 1, dict(r.to_dict(), prompt="Changed"))
    store.set_enabled(r.id, changed.revision, True)
    now[0] += dt.timedelta(hours=1)
    assert scheduler.tick(now[0]) == []
    assert len(store.list_runs()) == 1


def test_global_dispatch_disable_persists_and_resume_is_future_only(lab):
    store, scheduler, now = lab
    routine(store)
    first = scheduler.tick(now[0])[0]
    scheduler.set_dispatch(False, now[0])
    assert store.get_run(first).status == "skipped"
    reopened = Store(store.path)
    other = Scheduler(reopened, 10)
    assert not other.enabled
    now[0] += dt.timedelta(days=1)
    assert other.tick(now[0]) == []
    other.set_dispatch(True, now[0])
    assert other.tick(now[0]) == []
    now[0] += dt.timedelta(minutes=1)
    assert len(other.tick(now[0])) == 1
    reopened.close()


def test_crash_during_dispatch_rolls_back_slot_queue_and_checkpoint(lab, monkeypatch):
    store, scheduler, now = lab
    routine(store)
    original = scheduler._checkpoint

    def crash(*args):
        original(*args)
        raise OSError("simulated crash before commit")

    monkeypatch.setattr(scheduler, "_checkpoint", crash)
    with pytest.raises(OSError):
        scheduler.tick(now[0])
    assert store.list_runs() == []
    assert store.conn.execute("SELECT count(*) FROM schedule_slots").fetchone()[0] == 0
    monkeypatch.setattr(scheduler, "_checkpoint", original)
    assert len(scheduler.tick(now[0])) == 1


def test_newer_schema_is_refused_without_writing(tmp_path):
    path = tmp_path / "future.db"
    Store(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_meta SET value='999' WHERE key='schema_version'")
    before = path.read_bytes()
    with pytest.raises(StoreError, match="schema version 999"):
        Store(path)
    assert path.read_bytes() == before


def test_v1_upgrade_preserves_data_and_creates_readable_backup(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.executescript((Path(__file__).parent / "fixtures/schema-v1.sql").read_text())
        connection.execute("INSERT INTO schema_meta VALUES('schema_version','1')")
        connection.execute(
            "INSERT INTO routines(id,revision,name,prompt,model,cwd,"
            "schedule_kind,created_at,updated_at)"
            " VALUES('r',1,'Kept','Original','fake','/','manual','2026','2026')"
        )
    store = Store(path)
    assert store.get_routine("r").prompt == "Original"
    assert store.get_routine("r").tools == "default"
    assert store.get_routine("r").source is None
    assert (
        store.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        == "4"
    )
    backup = next(tmp_path.glob("*.backup"))
    with sqlite3.connect(backup) as connection:
        assert (
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            == "1"
        )
        assert connection.execute("SELECT prompt FROM routines").fetchone()[0] == "Original"
    store.close()
