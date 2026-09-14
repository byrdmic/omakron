"""Draft validation, atomic editing, and schedule previews through the real socket."""

import json

import pytest

from omakron.client import ServiceError


def draft(service):
    defaults = service.request("editor_defaults")
    return dict(
        name="Daily report",
        prompt="Return the report.",
        model=defaults["model"],
        cwd=defaults["cwd"],
        schedule_kind="cron",
        cron="0 9 * * 1-5",
        timezone="America/New_York",
        parameter_kind=None,
    )


def test_create_and_reopen_schedule_identically(service):
    expected = draft(service)
    routine = service.request("create_routine", expected)["routine"]
    service.restart()
    saved = service.request("get_routine", {"routine_id": routine["id"]})["routine"]
    assert not saved["enabled"] and saved["revision"] == 1
    assert {key: saved[key] for key in expected} == expected


@pytest.mark.parametrize(
    "bad",
    [
        {"cron": "0 90 * * *"},
        {"timezone": "Nowhere"},
        {"cwd": "/missing/omakron-folder"},
        {"model": "--bad-model"},
        {"prompt": ""},
        {"schedule_kind": "unknown"},
    ],
)
def test_invalid_edit_preserves_accepted_revision(service, bad):
    original = draft(service)
    saved = service.request("create_routine", original)["routine"]
    with pytest.raises(ServiceError):
        service.request(
            "update_routine", dict(original, **bad, routine_id=saved["id"], expected_revision=1)
        )
    assert service.request("get_routine", {"routine_id": saved["id"]})["routine"] == saved


def test_conflicting_edit_preserves_latest_accepted_revision(service):
    original = draft(service)
    saved = service.request("create_routine", original)["routine"]
    updated = service.request(
        "update_routine",
        dict(original, name="Accepted", routine_id=saved["id"], expected_revision=1),
    )["routine"]
    with pytest.raises(ServiceError) as error:
        service.request(
            "update_routine",
            dict(original, name="Stale draft", routine_id=saved["id"], expected_revision=1),
        )
    assert error.value.code == "conflict"
    assert updated["revision"] == 2 and not updated["enabled"]
    assert service.request("get_routine", {"routine_id": saved["id"]})["routine"] == updated


def test_preview_uses_the_lab_evaluator_for_dst(service):
    result = service.request(
        "preview_schedule",
        {"cron": "30 1 * * *", "timezone": "America/New_York", "after": "2026-11-01T04:00:00Z"},
    )
    assert len(result["occurrences"]) == 5
    assert result["occurrences"][0]["utc"] == "2026-11-01T05:30:00+00:00"
    assert result["occurrences"][1]["utc"] == "2026-11-02T06:30:00+00:00"
    assert "OR" in result["convention"]


def test_manual_triage_cannot_accidentally_be_scheduled(service):
    saved = service.triage_routine()
    with pytest.raises(ServiceError, match="manual issue identifier"):
        service.request(
            "update_routine",
            dict(
                saved,
                routine_id=saved["id"],
                expected_revision=1,
                schedule_kind="cron",
                cron="0 9 * * *",
                timezone="UTC",
            ),
        )


def test_raw_client_keeps_prompt_data_out_of_shell(service):
    request = {
        "op": "create_routine",
        "params": dict(draft(service), prompt="Literal $(touch /tmp/no) `echo hi`\nNext line."),
    }
    result = service.client("request", stdin=json.dumps(request))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["routine"]["prompt"] == request["params"]["prompt"]
