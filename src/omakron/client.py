"""Local client the QML popup (and a person at a terminal) uses to talk to the service.

``python -m omakron.client <command> ...`` sends one bounded JSON request over
the user-only Unix socket and prints the JSON answer. Every value the caller
supplies travels as an argv element and then as JSON; nothing is ever
interpolated into a shell command. Exit status: 0 success, 1 the service
answered with an error, 2 usage, 3 the service is not reachable.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from omakron import ipc

CONNECT_TIMEOUT_S = 5.0
TERMINAL = ("succeeded", "failed", "timed_out", "canceled", "interrupted", "skipped")


def socket_path(env: dict[str, str] | None = None) -> Path:
    """``$XDG_RUNTIME_DIR/omakron/service.sock``; fails loudly without a runtime dir."""
    src = os.environ if env is None else env
    runtime = src.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise RuntimeError("XDG_RUNTIME_DIR is not set; refusing to guess a socket location")
    return Path(runtime) / "omakron" / "service.sock"


class ServiceUnreachable(Exception):
    pass


class ServiceError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def request(op: str, params: dict[str, Any] | None = None, *, sock: Path | None = None) -> Any:
    """Send one request and return its ``result``; raise on transport or service errors."""
    path = sock or socket_path()
    message = {"id": str(uuid.uuid4()), "op": op, "params": params or {}}
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(CONNECT_TIMEOUT_S)
    try:
        conn.connect(str(path))
        conn.sendall(ipc.encode(message))
        conn.settimeout(30.0)
        response = ipc.read_message(conn, max_bytes=ipc.MAX_RESPONSE_BYTES)
    except (OSError, ipc.ProtocolError) as exc:
        raise ServiceUnreachable(f"omakron service not reachable at {path}: {exc}") from exc
    finally:
        conn.close()
    if response.get("id") != message["id"]:
        raise ServiceUnreachable("service answered a different request")
    if not response.get("ok"):
        error = response.get("error") or {}
        raise ServiceError(str(error.get("code", "error")), str(error.get("message", "")))
    return response.get("result")


def wait_for_run(
    run_id: str, *, timeout_s: float, interval_s: float = 0.5, sock: Path | None = None
) -> dict[str, Any]:
    """Poll until the run is terminal or ``timeout_s`` passes; returns the last run seen."""
    deadline = time.monotonic() + timeout_s
    while True:
        run = request("get_run", {"run_id": run_id}, sock=sock)["run"]
        if run["status"] in TERMINAL or time.monotonic() >= deadline:
            return run
        time.sleep(interval_s)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m omakron.client", description=__doc__)
    parser.add_argument("--socket", type=Path, help="override the socket location")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("request", help="one JSON operation from stdin, for the native UI")
    sub.add_parser("status", help="service identity, active run, routine count")
    sub.add_parser("routines", help="list routines")

    routine = sub.add_parser("routine", help="one routine with its recent runs")
    routine.add_argument("routine_id")

    create = sub.add_parser("create-routine", help="create a paused manual routine")
    create.add_argument("--name", required=True)
    group = create.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt")
    group.add_argument("--prompt-file", type=Path)
    create.add_argument("--model", default="claude-sonnet-5")
    create.add_argument("--cwd", required=True)
    create.add_argument("--enabled", action="store_true")
    create.add_argument("--parameter-kind", choices=["linear_issue"])

    run_now = sub.add_parser("run-now", help="queue one run of a routine")
    run_now.add_argument("routine_id")
    run_now.add_argument("--issue", help="Linear issue identifier to triage, e.g. DEMO-123")
    run_now.add_argument("--key", help="idempotency key; default: a fresh UUID")
    run_now.add_argument("--wait", type=float, metavar="SECONDS", help="wait for the run to end")

    runs = sub.add_parser("runs", help="recent runs, newest first")
    runs.add_argument("--routine")
    runs.add_argument("--limit", type=int, default=30)

    run = sub.add_parser("run", help="one run with its input snapshot and report")
    run.add_argument("run_id")

    cancel = sub.add_parser("cancel", help="cancel a queued or running run")
    cancel.add_argument("run_id")

    wait = sub.add_parser("wait", help="poll a run until it ends")
    wait.add_argument("run_id")
    wait.add_argument("--timeout", type=float, default=660.0)
    wait.add_argument("--interval", type=float, default=0.5)
    return parser


def _dispatch(args: argparse.Namespace) -> Any:
    sock = args.socket
    if args.command == "request":
        raw = sys.stdin.buffer.read(ipc.MAX_MESSAGE_BYTES + 1)
        if len(raw) > ipc.MAX_MESSAGE_BYTES:
            raise ServiceError("bad_request", "request exceeds size limit")
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ServiceError("bad_request", "request must be JSON") from exc
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("op"), str)
            or not isinstance(message.get("params", {}), dict)
        ):
            raise ServiceError("bad_request", "request needs an op and a params object")
        return request(message["op"], message.get("params", {}), sock=sock)
    if args.command == "status":
        return request("status", sock=sock)
    if args.command == "routines":
        return request("list_routines", sock=sock)
    if args.command == "routine":
        return request("get_routine", {"routine_id": args.routine_id}, sock=sock)
    if args.command == "create-routine":
        prompt = args.prompt
        if args.prompt_file is not None:
            prompt = args.prompt_file.read_text(encoding="utf-8")
        params = {
            "name": args.name,
            "prompt": prompt,
            "model": args.model,
            "cwd": args.cwd,
            "enabled": args.enabled,
            "parameter_kind": args.parameter_kind,
        }
        return request("create_routine", params, sock=sock)
    if args.command == "run-now":
        params = {
            "routine_id": args.routine_id,
            "parameter": args.issue,
            "idempotency_key": args.key or str(uuid.uuid4()),
        }
        result = request("run_now", params, sock=sock)
        if args.wait is not None:
            result["run"] = wait_for_run(result["run"]["id"], timeout_s=args.wait, sock=sock)
        return result
    if args.command == "runs":
        return request("list_runs", {"routine_id": args.routine, "limit": args.limit}, sock=sock)
    if args.command == "run":
        return request("get_run", {"run_id": args.run_id}, sock=sock)
    if args.command == "cancel":
        return request("cancel_run", {"run_id": args.run_id}, sock=sock)
    if args.command == "wait":
        return {
            "run": wait_for_run(
                args.run_id, timeout_s=args.timeout, interval_s=args.interval, sock=sock
            )
        }
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _dispatch(args)
    except ServiceUnreachable as exc:
        print(json.dumps({"error": {"code": "unreachable", "message": str(exc)}}))
        return 3
    except ServiceError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}))
        return 1
    except OSError as exc:
        print(json.dumps({"error": {"code": "io", "message": str(exc)}}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
