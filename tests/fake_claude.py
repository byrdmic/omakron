#!/usr/bin/env python3
"""A fake ``claude`` executable for tests.

Speaks just enough of ``claude -p --output-format stream-json`` for the runner
to be exercised without a login, a network, or a model. The behavior is chosen
by ``FAKE_CLAUDE_MODE``:

- ``ok``         exit 0, ``is_error: false``, result text is the valid fixture report
- ``malformed``  exit 0, ``is_error: false``, result text is prose (exit zero must not pass)
- ``contract``   exit 0, result parses as JSON but breaks the report contract
- ``error``      exit 1, ``is_error: true`` with an auth-style message
- ``tool``       exit 0, valid report but an assistant tool_use event appears first
- ``hang``       never emits a result; sleeps until killed (deadline/cancel tests)
- ``slow``       like ``ok`` after a 3 s pause (a client can disconnect meanwhile)
- ``badmodel``   exit 1, ``is_error: true`` with the CLI's unknown-model message

The mode may also come from the file named by ``FAKE_CLAUDE_MODE_FILE``, which
wins over the variable; the service passes a filtered environment to its child,
so a wrapper script points the fake at a mode file a test can rewrite between
runs. Argv is recorded to ``FAKE_CLAUDE_ARGV_FILE`` when set so tests can
assert the profile that reached the executable, and one line per launch is
appended to ``FAKE_CLAUDE_LAUNCH_LOG`` so tests can prove there was no retry.
Stdin is read fully, as the real CLI does, and its byte count is logged.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "reports"


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main() -> int:
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    mode_file = os.environ.get("FAKE_CLAUDE_MODE_FILE")
    if mode_file and Path(mode_file).is_file():
        mode = Path(mode_file).read_text(encoding="utf-8").strip() or mode
    argv_file = os.environ.get("FAKE_CLAUDE_ARGV_FILE")
    if argv_file:
        Path(argv_file).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
    stdin_text = sys.stdin.read()
    launch_log = os.environ.get("FAKE_CLAUDE_LAUNCH_LOG")
    if launch_log:
        with open(launch_log, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "mode": mode,
                        "stdin_bytes": len(stdin_text.encode()),
                        "env_keys": sorted(os.environ),
                        "cwd": os.getcwd(),
                    }
                )
                + "\n"
            )

    model = "fake-model"
    if "--model" in sys.argv:
        model = sys.argv[sys.argv.index("--model") + 1]
    emit({"type": "system", "subtype": "init", "model": model, "tools": [], "mcp_servers": []})

    if mode == "hang":
        while True:
            time.sleep(1)
    if mode == "slow":
        time.sleep(3.0)

    if mode == "badmodel":
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": (
                    f"There's an issue with the selected model ({model}). It may not exist or "
                    "you may not have access to it. Run --model to pick a different model."
                ),
            }
        )
        return 1

    if mode == "error":
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": "Not logged in · Please run /login",
            }
        )
        return 1

    if mode == "malformed":
        text = "Here is my triage: it looks like a Medium priority bug. Next: ask for logs."
    elif mode == "contract":
        text = (FIXTURES / "exit-zero-malformed.json").read_text(encoding="utf-8")
    else:
        text = (FIXTURES / "valid-triage-report.json").read_text(encoding="utf-8")

    if mode == "tool":
        emit(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "x"}}]
                },
            }
        )
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 1,
            "result": text,
            "modelUsage": {model: {"canonicalModel": model}},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
