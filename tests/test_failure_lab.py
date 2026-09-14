"""Deterministic failures use only disposable data and the fake executable."""

import datetime as dt

import pytest

from omakron.runner import STOP_CANCEL, supervise
from omakron.store import Store
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


@pytest.mark.parametrize("age,claimed", [(299.999, True), (300, True), (300.001, False)])
def test_queue_expiry_at_exact_fake_time(tmp_path, monkeypatch, age, claimed):
    now = dt.datetime(2026, 9, 12, 12, tzinfo=dt.UTC)
    monkeypatch.setattr(
        "omakron.store.utc_now",
        lambda: now.isoformat(),  # noqa: PLW0108 - now changes below
    )
    store = Store(tmp_path / "lab.db")
    routine = store.create_routine(name="Fake", prompt="fake", model="fake", cwd=str(tmp_path))
    run, _ = store.enqueue_run(
        routine, trigger="manual", parameter=None, idempotency_key="request", deadline_s=1
    )
    now += dt.timedelta(seconds=age)
    assert bool(store.claim_next("lab")) is claimed
    assert store.get_run(run.id).status == ("claimed" if claimed else "skipped")
    store.close()


def test_duplicate_request_after_restart_returns_original_interrupted_run(tmp_path):
    path = tmp_path / "lab.db"
    store = Store(path)
    routine = store.create_routine(name="Fake", prompt="fake", model="fake", cwd=str(tmp_path))
    run, _ = store.enqueue_run(
        routine, trigger="manual", parameter=None, idempotency_key="same-request", deadline_s=1
    )
    store.claim_next("before-crash")
    store.close()
    store = Store(path)
    store.reconcile("after-crash")
    same, created = store.enqueue_run(
        routine, trigger="manual", parameter=None, idempotency_key="same-request", deadline_s=1
    )
    assert not created and same.id == run.id and same.status == "interrupted"
    assert store.claim_next("after-crash") is None
    store.close()
