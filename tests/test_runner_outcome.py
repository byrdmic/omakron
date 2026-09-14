"""Exit zero alone is insufficient: outcomes come from supervisor facts plus the contract."""

from __future__ import annotations

from omakron.runner import (
    BASE_FLAGS,
    Outcome,
    child_env,
    classify,
    claude_argv,
    parse_stream,
)

from .conftest import FAKE_CLAUDE, TEAM_LABELS


def argv_for_fake() -> list[str]:
    return claude_argv(executable=str(FAKE_CLAUDE))


def test_profile_reaches_the_executable(fake_claude, tmp_path):
    run = fake_claude(argv_for_fake(), stdin="prompt on stdin")
    recorded = (tmp_path / "argv.json").read_text()
    for flag in BASE_FLAGS:
        assert flag in recorded
    assert '"--tools", ""' in recorded
    assert "prompt on stdin" not in recorded
    assert run.exit_code == 0


def test_ok_run_succeeds_with_report(fake_claude):
    run = fake_claude(argv_for_fake(), mode="ok")
    verdict = classify(
        exit_code=run.exit_code, stream=parse_stream(run.stdout), team_labels=TEAM_LABELS
    )
    assert verdict.outcome is Outcome.SUCCEEDED
    assert verdict.report["issue_id"] == "DEMO-9999"
    assert parse_stream(run.stdout).resolved_models == ["claude-sonnet-5"]


def test_exit_zero_with_prose_is_a_failure(fake_claude):
    run = fake_claude(argv_for_fake(), mode="malformed")
    assert run.exit_code == 0
    verdict = classify(
        exit_code=run.exit_code, stream=parse_stream(run.stdout), team_labels=TEAM_LABELS
    )
    assert verdict.outcome is Outcome.FAILED
    assert verdict.problems == ("result text is not a JSON object",)


def test_exit_zero_with_contract_breach_is_a_failure(fake_claude):
    run = fake_claude(argv_for_fake(), mode="contract")
    assert run.exit_code == 0
    verdict = classify(
        exit_code=run.exit_code, stream=parse_stream(run.stdout), team_labels=TEAM_LABELS
    )
    assert verdict.outcome is Outcome.FAILED
    assert "next_step too short to be actionable" in verdict.problems
    assert verdict.report is not None  # kept so the run detail can show what was rejected


def test_error_result_is_a_failure_naming_the_message(fake_claude):
    run = fake_claude(argv_for_fake(), mode="error")
    verdict = classify(exit_code=run.exit_code, stream=parse_stream(run.stdout))
    assert verdict.outcome is Outcome.FAILED
    assert "exit status 1" in verdict.problems
    assert any("Not logged in" in p for p in verdict.problems)


def test_tool_use_is_a_failure_even_with_a_valid_report(fake_claude):
    run = fake_claude(argv_for_fake(), mode="tool")
    verdict = classify(
        exit_code=run.exit_code, stream=parse_stream(run.stdout), team_labels=TEAM_LABELS
    )
    assert verdict.outcome is Outcome.FAILED
    assert verdict.problems == ("forbidden tool use observed: ['Write']",)


def test_supervisor_facts_win_over_model_output():
    stream = parse_stream("")
    assert classify(exit_code=0, stream=stream, canceled=True).outcome is Outcome.CANCELED
    assert classify(exit_code=0, stream=stream, timed_out=True).outcome is Outcome.TIMED_OUT
    assert classify(exit_code=None, stream=stream).outcome is Outcome.INTERRUPTED
    assert classify(exit_code=0, stream=stream).problems == ("no result event in output",)


def test_parse_stream_counts_garbage_instead_of_raising():
    parsed = parse_stream('not json\n{"type": "result", "result": "x"}\n[1,2]\n')
    assert parsed.unparsed_lines == 2
    assert parsed.result_text == "x"


def test_child_env_drops_everything_but_the_passthrough():
    env = child_env(
        {"PATH": "/usr/bin", "HOME": "/h", "CLAUDE_CODE_SSE_PORT": "1", "AWS_SECRET": "x"}
    )
    assert env == {"PATH": "/usr/bin", "HOME": "/h"}
