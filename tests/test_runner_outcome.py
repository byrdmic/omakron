"""A run succeeds on a clean exit; what the model said is kept, not judged."""

from __future__ import annotations

import pytest

from omakron.runner import (
    BASE_FLAGS,
    Outcome,
    child_env,
    classify,
    claude_argv,
    parse_stream,
)

from .conftest import FAKE_CLAUDE


def argv_for_fake() -> list[str]:
    return claude_argv(executable=str(FAKE_CLAUDE))


def test_profile_reaches_the_executable(fake_claude, tmp_path):
    run = fake_claude(argv_for_fake(), stdin="prompt on stdin")
    recorded = (tmp_path / "argv.json").read_text()
    for flag in BASE_FLAGS:
        assert flag in recorded
    assert '"--tools", "default"' in recorded
    assert '"--permission-mode", "bypassPermissions"' in recorded
    assert "--dangerously-skip-permissions" in recorded
    for retired in ("--safe-mode", "--restricted", "--strict-mcp-config"):
        assert retired not in recorded
    assert "prompt on stdin" not in recorded
    assert run.exit_code == 0


def test_argv_carries_the_routine_choices():
    argv = claude_argv(
        "claude-opus-5", tools="Read,Edit", permission_mode="acceptEdits", mcp_config="/m.json"
    )
    assert argv[argv.index("--tools") + 1] == "Read,Edit"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--mcp-config") + 1] == "/m.json"
    assert "--dangerously-skip-permissions" not in argv
    assert "--mcp-config" not in claude_argv()
    with pytest.raises(ValueError, match="permission_mode"):
        claude_argv(permission_mode="yolo")


def test_ok_run_succeeds_and_keeps_the_result_text(fake_claude):
    run = fake_claude(argv_for_fake(), mode="ok")
    stream = parse_stream(run.stdout)
    verdict = classify(exit_code=run.exit_code, stream=stream)
    assert verdict.outcome is Outcome.SUCCEEDED and verdict.problems == ()
    assert stream.result_text.startswith("The working folder is empty")
    assert stream.resolved_models == ["claude-sonnet-5"]


def test_error_result_is_a_failure_naming_the_message(fake_claude):
    run = fake_claude(argv_for_fake(), mode="error")
    verdict = classify(exit_code=run.exit_code, stream=parse_stream(run.stdout))
    assert verdict.outcome is Outcome.FAILED
    assert "exit status 1" in verdict.problems
    assert any("Not logged in" in p for p in verdict.problems)


def test_tool_use_is_normal(fake_claude, tmp_path):
    run = fake_claude(argv_for_fake(), mode="tool")
    stream = parse_stream(run.stdout)
    assert [t["name"] for t in stream.tool_uses] == ["Write"]
    assert (tmp_path / "TOOL_WROTE.txt").is_file()
    verdict = classify(exit_code=run.exit_code, stream=stream)
    assert verdict.outcome is Outcome.SUCCEEDED


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


def test_child_env_is_the_base_list_plus_the_routine_keys():
    source = {
        "PATH": "/usr/bin",
        "HOME": "/h",
        "SSH_AUTH_SOCK": "/s",
        "CLAUDE_CODE_SSE_PORT": "1",
        "ANTHROPIC_API_KEY": "x",
        "AWS_SECRET": "y",
    }
    assert child_env(source) == {"PATH": "/usr/bin", "HOME": "/h", "SSH_AUTH_SOCK": "/s"}
    widened = child_env(source, extra=["ANTHROPIC_API_KEY", "CLAUDE_CODE_SSE_PORT", "MISSING"])
    assert widened["ANTHROPIC_API_KEY"] == "x"
    assert "CLAUDE_CODE_SSE_PORT" not in widened, "a parent Claude session never leaks in"
    assert "AWS_SECRET" not in widened
