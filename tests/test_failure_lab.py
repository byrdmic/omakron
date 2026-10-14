"""Deterministic failures use only disposable data and the fake executable."""

import datetime as dt
import threading

import pytest

from omakron.runner import STOP_CANCEL, supervise
from omakron.scheduler import stamp
from omakron.store import DEFAULT_QUEUE_MAX_WAIT_S, Store
from omakron.worker import Worker, WorkerConfig, notify_desktop
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


def _run_worker(tmp_path, mode: str, notify) -> str:
    store = Store(tmp_path / "lab.db")
    routine = store.create_routine(
        name="Fake",
        prompt="fake",
        model="fake",
        cwd=str(tmp_path),
        env_passthrough=["FAKE_CLAUDE_MODE"],
    )
    run, _ = store.enqueue_run(routine, trigger="manual", idempotency_key=None, deadline_s=30)
    worker = Worker(
        store_factory=lambda: store,
        config=WorkerConfig(str(FAKE_CLAUDE), tmp_path / "runs", tmp_path / "work", notify=notify),
        worker_id="lab",
        wake=threading.Event(),
        stopping=threading.Event(),
    )
    assert worker.run_once()
    status = store.get_run(run.id).status
    store.close()
    return status


def test_failed_run_sends_one_desktop_notification(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    sent: list[tuple[str, str]] = []
    assert _run_worker(tmp_path, "error", lambda t, b: sent.append((t, b))) == "failed"
    assert len(sent) == 1
    title, body = sent[0]
    assert title == "Fake: run failed"
    assert body.startswith("exit status 1")
    assert "retry explicitly" in body


def test_succeeded_run_sends_no_notification(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    sent: list[tuple[str, str]] = []
    assert _run_worker(tmp_path, "ok", lambda t, b: sent.append((t, b))) == "succeeded"
    assert sent == []


def test_notification_failure_does_not_change_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")

    def broken(title, body):
        raise RuntimeError("no session bus")

    assert _run_worker(tmp_path, "error", broken) == "failed"


def test_notify_desktop_passes_text_as_arguments(tmp_path, monkeypatch):
    fake = tmp_path / "notify-send"
    fake.write_text('#!/bin/sh\nprintf "%s\\0" "$@" > "$0.args"\n')
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    notify_desktop("Fake: run failed", "exit status 1\nretry")
    args = (tmp_path / "notify-send.args").read_text().split("\0")
    assert args[:-1] == [
        "--app-name=Omakron",
        "--urgency=critical",
        "--",
        "Fake: run failed",
        "exit status 1\nretry",
    ]


def test_notify_desktop_without_notify_send_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    notify_desktop("title", "body")
