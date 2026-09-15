"""The vertical slice with a fake CLI: seed, run now, failures, tools, interruption, storage.

Every test here starts the real service as a subprocess, talks to it over its
socket with the real client, and reads back what the store and the run folder
hold. The fake ``claude`` decides how the model behaves; the service never
knows it is fake.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import pytest

from omakron import seed
from omakron.client import ServiceError
from omakron.runner import BASE_FLAGS, ENV_PASSTHROUGH

from .service_harness import ServiceHarness, process_alive, tree_digest, wait_until

pytestmark = pytest.mark.slow


# ------------------------------------------------------------------------ U02


def test_seeded_routine_has_tools_and_runs_when_asked(service: ServiceHarness):
    routine = service.seed_routine()
    assert routine["name"] == seed.ROUTINE_NAME
    assert routine["prompt"] == seed.PROMPT
    assert routine["model"] == "claude-sonnet-5"
    assert routine["schedule_kind"] == "manual" and routine["cron"] is None
    assert routine["enabled"] is False
    assert routine["tools"] == "default"
    assert routine["permission_mode"] == "bypassPermissions"
    assert routine["mcp_config"] is None and routine["env_passthrough"] == []
    assert routine["revision"] == 1
    assert routine["cwd"] == str(service.state / "workdir" / "summary")


def test_u02_create_paused_routine_reopen_after_restart_identical(
    service: ServiceHarness, tmp_path
):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Summarize the folder.\n\nSecond paragraph; with 'quotes' and $vars.\n")
    folder = tmp_path / "work"
    folder.mkdir()
    created = service.client(
        "create-routine",
        "--name",
        "Folder summary",
        "--prompt-file",
        str(prompt_file),
        "--model",
        "claude-sonnet-5",
        "--cwd",
        str(folder),
    )
    assert created.returncode == 0, created.stdout + created.stderr
    routine = json.loads(created.stdout)["routine"]
    assert routine["enabled"] is False and routine["revision"] == 1

    service.restart()
    reopened = json.loads(service.client("routine", routine["id"]).stdout)["routine"]
    for key in ("prompt", "model", "cwd", "schedule_kind", "cron", "timezone", "name", "revision"):
        assert reopened[key] == routine[key], key
    assert reopened["prompt"] == prompt_file.read_text()
    assert reopened["cwd"] == str(folder.resolve())


def test_create_routine_validation(service: ServiceHarness, tmp_path):
    rid = service.seed_routine()["id"]
    folder = str(tmp_path)
    for params, code in (
        ({"name": "", "prompt": "p", "cwd": folder}, "bad_request"),
        ({"name": "n", "prompt": "p", "cwd": "relative/path"}, "bad_request"),
        ({"name": "n", "prompt": "p", "cwd": folder, "schedule_kind": "cron"}, "bad_request"),
        ({"name": "n", "prompt": "p", "cwd": folder, "permission_mode": "yolo"}, "bad_request"),
        ({"name": "n", "prompt": "p", "cwd": folder, "tools": ["Bash"]}, "bad_request"),
        ({"name": "n", "prompt": "p", "cwd": folder, "env_passthrough": ["1x"]}, "bad_request"),
        ({"name": "n", "prompt": "p", "cwd": folder, "mcp_config": "/no/such.json"}, "bad_request"),
    ):
        with pytest.raises(Exception) as info:
            service.request("create_routine", params)
        assert getattr(info.value, "code", None) == code, params
    with pytest.raises(Exception) as info:
        service.request("get_routine", {"routine_id": "nope"})
    assert info.value.code == "not_found"
    assert rid


# ------------------------------------------------------------------------ U05


def test_u05_run_now_yields_one_saved_result_outliving_the_client_and_a_restart(
    service: ServiceHarness,
):
    service.set_mode("slow")
    routine = service.seed_routine()

    # The popup's request: a short-lived client process that exits right away.
    result = service.client("run-now", routine["id"])
    assert result.returncode == 0, result.stdout + result.stderr
    run = json.loads(result.stdout)["run"]
    # The client process is gone; the run is still open inside the service.
    assert service.get_run(run["id"])["status"] in ("queued", "claimed", "running")

    done = service.wait_run(run["id"])
    assert done["status"] == "succeeded", done["problems"]
    assert done["routine_revision"] == routine["revision"] == 1
    assert done["routine_snapshot"]["prompt"] == seed.PROMPT
    assert done["routine_snapshot"]["model"] == "claude-sonnet-5"
    assert done["routine_snapshot"]["tools"] == "default"
    assert done["result_text"].startswith("The working folder is empty"), "kept as the model said"
    assert done["resolved_model"] == "claude-sonnet-5"
    assert done["models_used"] == ["claude-sonnet-5"]
    assert done["exit_code"] == 0
    assert done["started_at"] and done["ended_at"] and done["claimed_at"]

    run_dir = service.run_dir(run["id"])
    result_path = run_dir / "result.md"
    assert result_path.read_text() == done["result_text"]
    assert (run_dir / "stdout.jsonl").exists() and not list(run_dir.glob("*.part"))
    assert hashlib.sha256(result_path.read_bytes()).hexdigest() == done["output_sha256"]

    # The routine's choices reached the executable; the prompt did not ride on argv.
    argv = service.recorded_argv()
    for flag in BASE_FLAGS:
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == "default"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert "--system-prompt" not in argv and "--safe-mode" not in argv
    assert seed.PROMPT not in " ".join(argv)
    launches = service.launches()
    assert len(launches) == 1
    assert launches[0]["stdin_bytes"] == len(seed.PROMPT.encode()), "the prompt alone is stdin"
    assert launches[0]["cwd"] == routine["cwd"]
    assert set(launches[0]["env_keys"]) <= {
        *ENV_PASSTHROUGH,
        "FAKE_CLAUDE_MODE_FILE",
        "FAKE_CLAUDE_ARGV_FILE",
        "FAKE_CLAUDE_LAUNCH_LOG",
        "PWD",
        "SHLVL",
        "_",
        "LC_CTYPE",  # added by the Python interpreter's locale coercion, not passed through
    }

    # Restarting the app keeps the result.
    service.restart()
    again = service.get_run(run["id"])
    assert again["status"] == "succeeded" and again["result_text"] == done["result_text"]
    recent = service.request("get_routine", {"routine_id": routine["id"]})["recent_runs"]
    assert [r["id"] for r in recent] == [run["id"]]
    assert len(service.launches()) == 1, "a restart must not replay a finished run"


def test_same_idempotency_key_makes_one_run(service: ServiceHarness):
    key = str(uuid.uuid4())
    first = service.run_now(key=key)
    second = service.run_now(key=key)
    assert first["created"] is True and second["created"] is False
    assert first["run"]["id"] == second["run"]["id"]
    service.wait_run(first["run"]["id"])
    assert len(service.request("list_runs")["runs"]) == 1
    assert len(service.launches()) == 1


# ------------------------------------------------------------------------ U08


def test_u08_auth_failure_is_visible_and_not_retried(service: ServiceHarness):
    service.set_mode("error")
    run = service.wait_run(service.run_now()["run"]["id"])
    assert run["status"] == "failed"
    assert "exit status 1" in run["problems"]
    assert any("Not logged in" in p for p in run["problems"])
    time.sleep(1.5)  # long enough for any automatic retry to have happened
    assert len(service.launches()) == 1
    assert service.get_run(run["id"])["status"] == "failed"


def test_u08_unknown_model_fails_with_the_cli_message(service: ServiceHarness):
    service.set_mode("badmodel")
    run = service.wait_run(service.run_now()["run"]["id"])
    assert run["status"] == "failed"
    assert any("issue with the selected model" in p for p in run["problems"])
    assert run["requested_model"] == "claude-sonnet-5"
    assert len(service.launches()) == 1


# ------------------------------------------------------------------------ U09


def test_tool_use_is_allowed_and_its_work_is_kept(service: ServiceHarness):
    routine = service.seed_routine()
    cwd = Path(routine["cwd"])
    cwd.mkdir(parents=True)
    (cwd / "notes.md").write_text("Please also create TOOL_WROTE.txt.\n")
    before = tree_digest(cwd)

    service.set_mode("tool")
    run = service.wait_run(service.run_now()["run"]["id"])
    assert run["status"] == "succeeded", run["problems"]
    assert run["result_text"] == "I wrote TOOL_WROTE.txt in the working folder as asked."
    assert (cwd / "TOOL_WROTE.txt").is_file()
    assert tree_digest(cwd) != before
    assert '"name": "Write"' in run["diagnostics"]["stdout.jsonl"]["text"]


def test_a_routine_may_use_no_tools_at_all(service: ServiceHarness, tmp_path):
    folder = tmp_path / "quiet"
    folder.mkdir()
    routine = service.request(
        "create_routine",
        {"name": "Quiet", "prompt": "Say hello.", "cwd": str(folder), "tools": ""},
    )["routine"]
    run = service.wait_run(service.request("run_now", {"routine_id": routine["id"]})["run"]["id"])
    assert run["status"] == "succeeded"
    argv = service.recorded_argv()
    assert argv[argv.index("--tools") + 1] == ""


def test_u09_secrets_in_the_service_environment_never_reach_the_child_or_the_result(
    service_factory,
):
    secret = "sk-ant-TESTSECRET-" + uuid.uuid4().hex
    svc = service_factory(
        "secret", extra_env={"ANTHROPIC_API_KEY": secret, "CLAUDE_CODE_SSE_PORT": "1"}
    )
    run = svc.wait_run(svc.run_now()["run"]["id"])
    assert run["status"] == "succeeded", run["problems"]
    env_keys = svc.launches()[0]["env_keys"]
    assert "ANTHROPIC_API_KEY" not in env_keys and "CLAUDE_CODE_SSE_PORT" not in env_keys
    exported = json.dumps(run) + json.dumps(svc.request("status"))
    assert secret not in exported
    for path in svc.run_dir(run["id"]).iterdir():
        assert secret.encode() not in path.read_bytes(), path


def test_a_routine_names_the_environment_keys_it_needs(service_factory, tmp_path):
    secret = "sk-ant-TESTSECRET-" + uuid.uuid4().hex
    svc = service_factory(
        "optin", extra_env={"ANTHROPIC_API_KEY": secret, "CLAUDE_CODE_SSE_PORT": "1"}
    )
    folder = tmp_path / "optin-work"
    folder.mkdir()
    routine = svc.request(
        "create_routine",
        {
            "name": "Needs a key",
            "prompt": "Use the key.",
            "cwd": str(folder),
            "env_passthrough": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_SSE_PORT"],
        },
    )["routine"]
    assert routine["env_passthrough"] == ["ANTHROPIC_API_KEY", "CLAUDE_CODE_SSE_PORT"]
    run = svc.wait_run(svc.request("run_now", {"routine_id": routine["id"]})["run"]["id"])
    assert run["status"] == "succeeded", run["problems"]
    env_keys = svc.launches()[0]["env_keys"]
    assert "ANTHROPIC_API_KEY" in env_keys
    assert "CLAUDE_CODE_SSE_PORT" not in env_keys, "a parent Claude session never leaks in"
    assert secret not in json.dumps(run)


# ---------------------------------------------------------- cancel and deadline


def test_cancel_running_run_stops_the_process_and_records_the_reason(service: ServiceHarness):
    service.set_mode("hang")
    run = service.run_now()["run"]
    running = service.wait_status(run["id"], "running")
    child = service.all_runs_from_db()[0]
    assert process_alive(child["pid"])

    canceled = service.client("cancel", run["id"])
    assert canceled.returncode == 0, canceled.stdout
    done = service.wait_run(run["id"])
    assert done["status"] == "canceled" and done["problems"] == ["canceled by user"]
    assert wait_until(lambda: not process_alive(child["pid"]))
    assert done["result_text"] is None and running["status"] == "running"


def test_cancel_queued_run_ends_it_before_start(service: ServiceHarness):
    service.set_mode("hang")
    first = service.run_now(key="first")["run"]
    service.wait_status(first["id"], "running")
    second = service.run_now(key="second")["run"]
    assert second["status"] == "queued"
    canceled = service.request("cancel_run", {"run_id": second["id"]})["run"]
    assert canceled["status"] == "canceled"
    assert canceled["problems"] == ["canceled by user before start"]
    service.request("cancel_run", {"run_id": first["id"]})
    service.wait_run(first["id"])
    assert len(service.launches()) == 1


def test_deadline_turns_a_hang_into_timed_out(service_factory):
    svc = service_factory("deadline", deadline_s=1.0)
    svc.set_mode("hang")
    run = svc.wait_run(svc.run_now()["run"]["id"], timeout_s=20)
    assert run["status"] == "timed_out"
    assert run["problems"] == ["deadline reached before the process exited"]
    assert run["deadline_s"] == 1.0
    pid = svc.all_runs_from_db()[0]["pid"]
    assert wait_until(lambda: not process_alive(pid))


# ----------------------------------------------------- interruption and storage


def test_interrupted_run_stays_visible_is_not_replayed_and_its_orphan_is_stopped(
    service: ServiceHarness,
):
    service.set_mode("hang")
    run = service.run_now()["run"]
    service.wait_status(run["id"], "running")
    child = service.all_runs_from_db()[0]
    assert process_alive(child["pid"])

    service.kill()  # the service dies; the supervised child is orphaned in its own group
    assert process_alive(child["pid"]), "precondition: the orphan outlived the service"

    service.start()
    after = service.get_run(run["id"])
    assert after["status"] == "interrupted"
    assert "not observed" in after["problems"][0] and "not replayed" in after["problems"][0]
    assert service.request("status")["service"]["interrupted_on_start"] == [run["id"]]
    assert wait_until(lambda: not process_alive(child["pid"]), timeout_s=10)
    time.sleep(1.5)
    assert len(service.launches()) == 1, "an interrupted run must not be launched again"
    assert service.get_run(run["id"])["status"] == "interrupted"
    assert service.request("status")["active_run"] is None


def test_graceful_stop_mid_run_records_interrupted_not_success(service: ServiceHarness):
    service.set_mode("hang")
    run = service.run_now()["run"]
    service.wait_status(run["id"], "running")
    child = service.all_runs_from_db()[0]
    assert service.stop() == 0
    assert wait_until(lambda: not process_alive(child["pid"]))
    service.start()
    after = service.get_run(run["id"])
    assert after["status"] == "interrupted"
    assert after["problems"] == ["service stopped before the process exited"]
    assert service.request("status")["service"]["interrupted_on_start"] == []


def test_output_storage_failure_is_reported_as_failed(service: ServiceHarness):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    service.set_mode("slow")
    run = service.run_now()["run"]
    service.wait_status(run["id"], "running")
    run_dir = service.run_dir(run["id"])
    run_dir.chmod(0o500)  # the model will finish fine; the result cannot be stored
    try:
        done = service.wait_run(run["id"])
    finally:
        run_dir.chmod(0o700)
    assert done["status"] == "failed"
    assert done["problems"][0].startswith("output could not be stored")
    assert done["result_text"] is None
    assert not (run_dir / "result.md").exists()


def test_run_folder_that_cannot_be_created_fails_before_launch(service: ServiceHarness):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    runs = service.state / "runs"
    runs.chmod(0o500)
    try:
        done = service.wait_run(service.run_now()["run"]["id"])
    finally:
        runs.chmod(0o700)
    assert done["status"] == "failed"
    assert done["problems"][0].startswith("output could not be stored")
    assert service.launches() == []


# -------------------------------------------------------------------- surface


def test_status_and_runs_listing(service: ServiceHarness):
    status = service.request("status")
    assert status["routines"] == 1 and status["active_run"] is None
    assert status["service"]["claude_executable"].endswith("/bin/claude")
    run = service.wait_run(service.run_now()["run"]["id"])
    listed = json.loads(service.client("runs", "--limit", "5").stdout)["runs"]
    assert [r["id"] for r in listed] == [run["id"]]
    assert "result_text" not in listed[0], "listings stay small; the detail carries the result"
    detail = json.loads(service.client("run", run["id"]).stdout)["run"]
    assert detail["result_text"] == run["result_text"]
    waited = json.loads(service.client("wait", run["id"], "--timeout", "5").stdout)["run"]
    assert waited["status"] == "succeeded"


@pytest.mark.slow
def test_a_skill_folder_routine_reads_skill_md_at_every_launch(service: ServiceHarness, tmp_path):
    folder = tmp_path / "skills" / "notes"
    folder.mkdir(parents=True)
    skill = folder / "SKILL.md"
    skill.write_text("---\nname: notes\n---\nFirst version of the prompt.\n", encoding="utf-8")
    defaults = service.request("editor_defaults")
    params = {"source": str(folder), "cwd": defaults["cwd"], "model": defaults["model"]}
    routine = service.request("create_routine", params)["routine"]
    assert routine["name"] == "notes" and routine["prompt"] == "First version of the prompt."

    first = service.request("run_now", {"routine_id": routine["id"]})["run"]
    done = service.wait_run(first["id"])
    assert done["status"] == "succeeded", done["problems"]
    assert done["routine_snapshot"]["prompt"] == "First version of the prompt."
    assert done["routine_snapshot"]["source"] == str(folder)

    # Edit the file in place, as a pull on the shared repository would.
    skill.write_text("---\nname: notes\n---\nSecond version, longer than before.\n")
    second = service.request("run_now", {"routine_id": routine["id"]})["run"]
    done = service.wait_run(second["id"])
    assert done["status"] == "succeeded", done["problems"]
    assert done["routine_snapshot"]["prompt"] == "Second version, longer than before."
    digest = hashlib.sha256(skill.read_bytes()).hexdigest()
    assert done["routine_snapshot"]["source_sha256"] == digest
    assert done["routine_revision"] == 1, "reading the file is not a routine edit"
    launches = service.launches()
    assert [launch["stdin_bytes"] for launch in launches[-2:]] == [
        len(b"First version of the prompt."),
        len(b"Second version, longer than before."),
    ]
    # The stored routine still shows the text seen at import; the file is authoritative.
    saved = service.request("get_routine", {"routine_id": routine["id"]})["routine"]
    assert saved["prompt"] == "First version of the prompt."

    # A missing file is a failed run with the reason, not a silent fallback.
    skill.unlink()
    third = service.request("run_now", {"routine_id": routine["id"]})["run"]
    done = service.wait_run(third["id"])
    assert done["status"] == "failed"
    assert any("no SKILL.md" in problem for problem in done["problems"])
    assert len(service.launches()) == len(launches), "nothing was launched without a prompt"


@pytest.mark.slow
def test_cli_create_routine_from_a_source_folder(service: ServiceHarness, tmp_path):
    folder = tmp_path / "skills" / "cli"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: from-cli\n---\nBody.\n", encoding="utf-8")
    cwd = service.request("editor_defaults")["cwd"]
    result = service.client("create-routine", "--source", str(folder), "--cwd", cwd)
    assert result.returncode == 0, result.stdout + result.stderr
    routine = json.loads(result.stdout)["routine"]
    assert routine["name"] == "from-cli" and routine["source"] == str(folder)
    missing = service.client("create-routine", "--prompt", "x", "--cwd", cwd)
    assert missing.returncode != 0 and "--name is required" in missing.stderr


@pytest.mark.slow
def test_delete_routine_removes_it_from_omakron_and_refuses_later_work(service: ServiceHarness):
    routine = service.seed_routine()
    stale = service.client("delete-routine", routine["id"], "--revision", "99")
    assert stale.returncode == 1
    assert json.loads(stale.stdout)["error"]["code"] == "conflict"
    assert service.seed_routine()["id"] == routine["id"], "a stale revision deletes nothing"

    result = service.client("delete-routine", routine["id"])
    assert result.returncode == 0, result.stdout + result.stderr
    deleted = json.loads(result.stdout)["routine"]
    assert deleted["id"] == routine["id"] and deleted["deleted_at"]

    assert service.request("list_routines")["routines"] == []
    assert service.request("dashboard")["routines"] == []
    assert service.request("status")["routines"] == 0
    with pytest.raises(ServiceError, match="deleted"):
        service.request("run_now", {"routine_id": routine["id"]})
    with pytest.raises(ServiceError, match="reload"):
        service.request(
            "set_enabled",
            dict(routine_id=routine["id"], expected_revision=deleted["revision"], enabled=True),
        )
    with pytest.raises(ServiceError, match="reload"):
        service.request(
            "update_routine",
            dict(routine, routine_id=routine["id"], expected_revision=deleted["revision"]),
        )
    again = service.client("delete-routine", routine["id"])
    assert again.returncode == 1 and json.loads(again.stdout)["error"]["code"] == "conflict"
    service.restart()
    assert service.request("list_routines")["routines"] == [], "a restart does not reseed"
