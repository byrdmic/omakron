"""Deterministic failures use only disposable data and the fake executable."""

import datetime as dt
import threading

import pytest

from omakron.runner import STOP_CANCEL, supervise
from omakron.scheduler import stamp
from omakron.store import DEFAULT_QUEUE_MAX_WAIT_S, Store
from omakron.worker import Worker, WorkerConfig
from tests.conftest import FAKE_CLAUDE
from tests.test_supervise import group_alive, launch


@pytest.mark.parametrize("wall_jump", [-86400, 86400])
def test_deadline_uses_monotonic_time_during_wall_clock_jump(tmp_path, fake_clock, wall_jump):
    def sleep(seconds):
        fake_clock.advance(seconds)
        fake_clock.jump_wall(wall_jump)

    result = supervise(
        launch(tmp_path, "hang", deadline_s=0.5),
        out_dir=tmp_path / "output",
        monotonic=fake_clock.monotonic,
        sleep=sleep,
    )
    assert result.timed_out
    assert 0.5 <= result.elapsed_s <= 0.7
    assert not group_alive(result.pgid)


def test_cancellation_at_known_fake_clock_tick(tmp_path, fake_clock):
    result = supervise(
        launch(tmp_path, "hang"),
        out_dir=tmp_path / "output",
        monotonic=fake_clock.monotonic,
        sleep=fake_clock.sleep,
        stop_check=lambda: STOP_CANCEL if fake_clock.monotonic() >= 1000.25 else None,
    )
    assert result.canceled and not result.timed_out
    assert 0.25 <= result.elapsed_s < 0.5
    assert not group_alive(result.pgid)


@pytest.mark.parametrize("past_limit,claimed", [(-0.001, True), (0, True), (0.001, False)])
def test_queue_wait_limit_at_exact_fake_time(tmp_path, monkeypatch, past_limit, claimed):
    now = dt.datetime(2026, 9, 12, 12, tzinfo=dt.UTC)
    monkeypatch.setattr(
        "omakron.store.utc_now",
        lambda: now.isoformat(),  # noqa: PLW0108 - now changes below
    )
    store = Store(tmp_path / "lab.db")
    routine = store.create_routine(name="Fake", prompt="fake", model="fake", cwd=str(tmp_path))
    run, _ = store.enqueue_run(routine, trigger="manual", idempotency_key="request", deadline_s=1)
    now += dt.timedelta(seconds=DEFAULT_QUEUE_MAX_WAIT_S + past_limit)
    assert bool(store.claim_next("lab")) is claimed
    assert store.get_run(run.id).status == ("claimed" if claimed else "skipped")
    store.close()


def test_time_spent_waiting_does_not_count_against_the_deadline(tmp_path):
    store = Store(tmp_path / "lab.db")
    routine = store.create_routine(name="Fake", prompt="fake", model="fake", cwd=str(tmp_path))
    run, _ = store.enqueue_run(routine, trigger="manual", idempotency_key=None, deadline_s=30)
    three_hours_ago = stamp(dt.datetime.now(dt.UTC) - dt.timedelta(hours=3))
    store.conn.execute("UPDATE runs SET enqueued_at=? WHERE id=?", (three_hours_ago, run.id))
    worker = Worker(
        store_factory=lambda: store,
        config=WorkerConfig(str(FAKE_CLAUDE), tmp_path / "runs", tmp_path / "work"),
        worker_id="lab",
        wake=threading.Event(),
        stopping=threading.Event(),
    )
    assert worker.run_once()
    assert store.get_run(run.id).status == "succeeded"
    store.close()


def test_duplicate_request_after_restart_returns_original_interrupted_run(tmp_path):
    path = tmp_path / "lab.db"
    store = Store(path)
    routine = store.create_routine(name="Fake", prompt="fake", model="fake", cwd=str(tmp_path))
    run, _ = store.enqueue_run(
        routine, trigger="manual", idempotency_key="same-request", deadline_s=1
    )
    store.claim_next("before-crash")
    store.close()
    store = Store(path)
    store.reconcile("after-crash")
    same, created = store.enqueue_run(
        routine, trigger="manual", idempotency_key="same-request", deadline_s=1
    )
    assert not created and same.id == run.id and same.status == "interrupted"
    assert store.claim_next("after-crash") is None
    store.close()
