"""settings.json is validated when the service starts and when a client changes it."""

import datetime as dt
import json
import time

import pytest

from omakron import seed
from omakron.client import ServiceError
from omakron.service import Service, Settings

from .conftest import FAKE_CLAUDE
from .service_harness import short_socket_dir, wait_until


def test_queue_max_wait_defaults_to_four_hours_and_accepts_a_number(tmp_path):
    assert Settings.load(tmp_path).queue_max_wait_s == 14400.0
    (tmp_path / "settings.json").write_text(json.dumps({"queue_max_wait_s": 7200}))
    assert Settings.load(tmp_path).queue_max_wait_s == 7200.0


def test_priority_routines_defaults_to_none_and_keeps_the_listed_order(tmp_path):
    assert Settings.load(tmp_path).priority_routines == ()
    (tmp_path / "settings.json").write_text(json.dumps({"priority_routines": ["b", "a"]}))
    assert Settings.load(tmp_path).priority_routines == ("b", "a")


@pytest.mark.parametrize("value", ["routine-id", [1], [""], {"id": 1}, None])
def test_bad_priority_routines_is_rejected_at_load(tmp_path, value):
    (tmp_path / "settings.json").write_text(json.dumps({"priority_routines": value}))
    with pytest.raises(ValueError, match="priority_routines must be a list of routine ids"):
        Settings.load(tmp_path)


@pytest.mark.parametrize(
    "value",
    [0, -1, "3600", True, None, float("nan"), float("inf"), pytest.param(10**400, id="huge")],
)
def test_bad_queue_max_wait_is_rejected_at_load(tmp_path, value):
    (tmp_path / "settings.json").write_text(json.dumps({"queue_max_wait_s": value}))
    with pytest.raises(ValueError, match="queue_max_wait_s must be a positive number"):
        Settings.load(tmp_path)


# ----------------------------------------------------------- service_settings

KEYS = ("deadline_s", "queue_max_wait_s", "priority_routines")


def status_settings(svc) -> dict:
    reported = svc.request("status")["service"]
    return {key: reported[key] for key in KEYS}


def saved(svc) -> dict:
    return json.loads((svc.config / "settings.json").read_text(encoding="utf-8"))


def test_client_reads_and_changes_a_subset_and_every_other_key_is_kept(service):
    path = service.config / "settings.json"
    before = dict(saved(service), enforce_compatibility=False, future_key={"a": [1, "\u00e9"]})
    path.write_text(json.dumps(before), encoding="utf-8")
    start = {"deadline_s": 600.0, "queue_max_wait_s": 14400.0, "priority_routines": []}
    untouched = path.read_bytes()
    assert service.request("service_settings") == start
    assert path.read_bytes() == untouched, "a read writes nothing"

    reply = service.request("service_settings", {"deadline_s": 900})
    assert reply == dict(start, deadline_s=900.0) == status_settings(service)
    assert saved(service) == dict(before, deadline_s=900)

    change = {"queue_max_wait_s": 60.5, "priority_routines": [seed.ROUTINE_ID]}
    reply = service.request("service_settings", change)
    assert reply == dict(change, deadline_s=900.0) == status_settings(service)
    assert saved(service) == dict(before, deadline_s=900, **change)

    service.restart()
    assert service.request("service_settings") == reply
    cleared = service.request("service_settings", {"priority_routines": []})
    assert cleared == dict(reply, priority_routines=[]) == status_settings(service)


def test_a_missing_settings_file_is_created_with_only_the_change(service):
    (service.config / "settings.json").unlink()
    service.request("service_settings", {"deadline_s": 30})
    assert saved(service) == {"deadline_s": 30}


def test_a_refused_change_leaves_the_file_and_the_service_alone(service):
    gone = service.request(
        "create_routine", {"name": "Gone", "prompt": "p", "cwd": str(service.root)}
    )["routine"]
    service.request("delete_routine", {"routine_id": gone["id"], "expected_revision": 1})
    path = service.config / "settings.json"
    file_before, running_before = path.read_bytes(), status_settings(service)
    for params in (
        {"deadline_s": 0},
        {"deadline_s": "60"},
        {"deadline_s": True},
        {"deadline_s": None},
        {"deadline_s": float("inf")},
        {"queue_max_wait_s": -1},
        {"queue_max_wait_s": float("nan")},
        {"priority_routines": seed.ROUTINE_ID},
        {"priority_routines": None},
        {"priority_routines": [""]},
        {"priority_routines": [seed.ROUTINE_ID, "no-such-routine"]},
        {"priority_routines": [gone["id"]]},
        {"claude_executable": "/bin/true"},
        {"enforce_compatibility": True},
        {"retention_days": 1},
        {"deadline_s": 5, "unknown": 1},
        {"deadline_s": 5, "queue_max_wait_s": 0},
        {"deadline_s": 5, "priority_routines": ["no-such-routine"]},
    ):
        with pytest.raises(ServiceError) as info:
            service.request("service_settings", params)
        assert info.value.code == "bad_request", params
        assert path.read_bytes() == file_before, params
        assert status_settings(service) == running_before, params


def test_a_failed_write_leaves_the_file_and_the_service_alone(service):
    path = service.config / "settings.json"
    file_before, running_before = path.read_bytes(), status_settings(service)
    blocker = service.config / "settings.json.omakron-part"
    blocker.mkdir()
    with pytest.raises(ServiceError) as info:
        service.request("service_settings", {"deadline_s": 5})
    assert info.value.code == "io"
    assert path.read_bytes() == file_before and status_settings(service) == running_before
    blocker.rmdir()
    assert service.request("service_settings", {"deadline_s": 5})["deadline_s"] == 5.0


@pytest.mark.parametrize("text", ["{not json", "[]", '{"queue_max_wait_s": "soon"}'])
def test_a_file_the_service_could_not_start_from_is_not_rewritten(service, text):
    path = service.config / "settings.json"
    path.write_text(text, encoding="utf-8")
    running_before = status_settings(service)
    with pytest.raises(ServiceError) as info:
        service.request("service_settings", {"deadline_s": 5})
    assert info.value.code == "bad_request"
    assert path.read_text(encoding="utf-8") == text
    assert status_settings(service) == running_before


@pytest.mark.slow
def test_priority_applies_to_runs_already_waiting(service, tmp_path):
    other = service.request(
        "create_routine", {"name": "Other", "prompt": "Say hello.", "cwd": str(tmp_path)}
    )["routine"]
    service.set_mode("hang")
    active = service.request("run_now", {"routine_id": other["id"]})["run"]
    service.wait_status(active["id"], "running")
    older = service.request("run_now", {"routine_id": other["id"]})["run"]
    urgent = service.run_now()["run"]
    service.request("service_settings", {"priority_routines": [seed.ROUTINE_ID]})
    service.request("cancel_run", {"run_id": active["id"]})
    service.wait_status(urgent["id"], "running")
    assert service.get_run(older["id"])["status"] == "queued"
    service.request("cancel_run", {"run_id": older["id"]})
    service.request("cancel_run", {"run_id": urgent["id"]})
    service.wait_run(urgent["id"])


@pytest.mark.slow
def test_a_longer_queue_wait_saves_a_run_already_waiting(service_factory):
    svc = service_factory("longer", queue_max_wait_s=1.5)
    svc.set_mode("hang")
    active = svc.run_now(key="active")["run"]
    svc.wait_status(active["id"], "running")
    waiting = svc.run_now(key="waiting")["run"]
    svc.request("service_settings", {"queue_max_wait_s": 600})
    time.sleep(2.5)  # past the old limit, for the dispatcher and then for the worker's claim
    svc.set_mode("ok")
    svc.request("cancel_run", {"run_id": active["id"]})
    assert svc.wait_run(waiting["id"])["status"] == "succeeded"


@pytest.mark.slow
def test_a_shorter_queue_wait_skips_a_run_already_waiting(service):
    service.set_mode("hang")
    active = service.run_now(key="active")["run"]
    service.wait_status(active["id"], "running")
    waiting = service.run_now(key="waiting")["run"]
    service.request("service_settings", {"queue_max_wait_s": 1})
    assert service.wait_run(waiting["id"])["problems"] == [
        "waited longer than the 1 s limit (queue_max_wait_s)"
    ]
    service.request("cancel_run", {"run_id": active["id"]})
    service.wait_run(active["id"])


@pytest.mark.slow
def test_a_new_deadline_is_for_new_runs_and_queued_runs_keep_theirs(service):
    service.set_mode("hang")
    active = service.run_now(key="active")["run"]
    service.wait_status(active["id"], "running")
    queued = service.run_now(key="queued")["run"]
    service.request("service_settings", {"deadline_s": 1.5})
    later = service.run_now(key="later")["run"]
    assert later["deadline_s"] == 1.5
    assert service.get_run(active["id"])["deadline_s"] == 600.0
    assert service.get_run(queued["id"])["deadline_s"] == 600.0
    service.request("cancel_run", {"run_id": active["id"]})
    service.request("cancel_run", {"run_id": queued["id"]})
    assert service.wait_run(later["id"])["status"] == "timed_out"
    retry = service.request("retry_run", {"run_id": queued["id"], "idempotency_key": "again"})
    assert retry["run"]["deadline_s"] == 1.5
    service.wait_run(retry["run"]["id"])


def test_scheduled_runs_take_the_new_deadline(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"claude_executable": str(FAKE_CLAUDE)}))
    service = Service(
        state=tmp_path / "state", config=config, sock=short_socket_dir(tmp_path) / "service.sock"
    )
    try:
        service.start()
        routine = service.dispatch(
            "create_routine",
            {
                "name": "Nightly",
                "prompt": "p",
                "cwd": str(tmp_path),
                "schedule_kind": "cron",
                "cron": "* * * * *",
                "timezone": "UTC",
            },
        )["routine"]
        service.dispatch("service_settings", {"deadline_s": 42})
        (run_id,) = service.scheduler.tick(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=60))
        run = service.dispatch("get_run", {"run_id": run_id})["run"]
        assert run["routine_id"] == routine["id"] and run["trigger"] == "scheduled"
        assert run["deadline_s"] == 42.0
        assert wait_until(
            lambda: service.dispatch("get_run", {"run_id": run_id})["run"]["status"] == "succeeded"
        )
    finally:
        service.stop()
