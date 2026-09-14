"""Draft validation, atomic editing, and schedule previews through the real socket."""

import json

import pytest

from omakron.client import ServiceError


def draft(service):
    defaults = service.request("editor_defaults")
    return dict(
        name="Daily summary",
        prompt="Summarize the folder.",
        model=defaults["model"],
        cwd=defaults["cwd"],
        schedule_kind="cron",
        cron="0 9 * * 1-5",
        timezone="America/New_York",
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


def test_execution_choices_round_trip_and_default(service, tmp_path):
    mcp = tmp_path / "mcp.json"
    mcp.write_text("{}")
    chosen = dict(
        draft(service),
        tools="Read,Grep",
        permission_mode="plan",
        mcp_config=str(mcp),
        env_passthrough=["GH_TOKEN", "GH_TOKEN"],
    )
    saved = service.request("create_routine", chosen)["routine"]
    assert saved["tools"] == "Read,Grep" and saved["permission_mode"] == "plan"
    assert saved["mcp_config"] == str(mcp) and saved["env_passthrough"] == ["GH_TOKEN"]
    plain = service.request("create_routine", dict(draft(service), name="Plain"))["routine"]
    assert plain["tools"] == "default" and plain["permission_mode"] == "bypassPermissions"
    assert plain["mcp_config"] is None and plain["env_passthrough"] == []
    defaults = service.request("editor_defaults")
    assert defaults["permission_mode"] == "bypassPermissions"
    assert "bypassPermissions" in defaults["permission_modes"]


def test_raw_client_keeps_prompt_data_out_of_shell(service):
    request = {
        "op": "create_routine",
        "params": dict(draft(service), prompt="Literal $(touch /tmp/no) `echo hi`\nNext line."),
    }
    result = service.client("request", stdin=json.dumps(request))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["routine"]["prompt"] == request["params"]["prompt"]


SKILL_TEXT = """---
name: folder-notes
description: Write a note about the working folder.
---

Describe the working folder in one paragraph and reply with it.
"""


def skill_folder(tmp_path, text=SKILL_TEXT):
    folder = tmp_path / "skills" / "folder-notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    return folder


def test_create_from_a_skill_folder_takes_name_and_prompt_from_skill_md(service, tmp_path):
    folder = skill_folder(tmp_path)
    params = dict(draft(service), name="", prompt="", source=str(folder))
    saved = service.request("create_routine", params)["routine"]
    assert saved["source"] == str(folder)
    assert saved["name"] == "folder-notes"
    assert saved["prompt"] == "Describe the working folder in one paragraph and reply with it."
    named = service.request("create_routine", dict(params, name="My notes", prompt="ignored text"))[
        "routine"
    ]
    assert named["name"] == "My notes" and named["prompt"] == saved["prompt"]
    service.restart()
    again = service.request("get_routine", {"routine_id": saved["id"]})["routine"]
    assert again["source"] == str(folder) and again["prompt"] == saved["prompt"]


def test_read_skill_previews_a_folder_and_refuses_a_bad_one(service, tmp_path):
    folder = skill_folder(tmp_path)
    shown = service.request("read_skill", {"source": str(folder)})
    assert shown["name"] == "folder-notes"
    assert shown["description"] == "Write a note about the working folder."
    assert shown["prompt"].startswith("Describe the working folder")
    assert shown["file"] == str(folder / "SKILL.md")
    with pytest.raises(ServiceError) as error:
        service.request("read_skill", {"source": str(tmp_path)})
    assert error.value.code == "bad_request" and "no SKILL.md" in str(error.value)
    with pytest.raises(ServiceError):
        service.request("create_routine", dict(draft(service), source=str(tmp_path / "gone")))


def test_editor_defaults_list_the_skills_under_the_configured_roots(service_factory, tmp_path):
    skill_folder(tmp_path)
    other = tmp_path / "skills" / "other"
    other.mkdir()
    (other / "SKILL.md").write_text("Plain body.\n", encoding="utf-8")
    svc = service_factory("roots", settings={"skill_roots": [str(tmp_path / "skills")]})
    defaults = svc.request("editor_defaults")
    assert defaults["skill_roots"] == [str(tmp_path / "skills")]
    assert [s["name"] for s in defaults["skills"]] == ["folder-notes", "other"]
    assert defaults["skills"][0]["source"] == str(tmp_path / "skills" / "folder-notes")
