#!/usr/bin/env python3
"""Manual execution smoke test: the vertical slice with the real ``claude`` CLI.

Starts the real service in a disposable state/config/socket under ``--out``,
queues one Run now of the seeded routine through the real client, lets the
client exit, waits for the run, checks that the model did what the prompt
asked (a ``SUMMARY.md`` in the working folder) and that its result was stored,
restarts the service, and checks the result is still there. One bounded model
call is made under the existing login, with the routine's default tools and
permission mode. Nothing under the real ``~/.config`` or ``~/.local/state`` is
touched.

The verification result is written to ``--evidence`` (default
``<out>/evidence``): ``report.md``, ``report.json``, and the model's
``result.md``. Exit status is non-zero when any check fails, including a
missing login (the run then fails with the CLI's own message).

Usage:
    PYTHONPATH=src python scripts/smoke_vertical_slice.py --out /path/to/scratch
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from omakron import client as omakron_client  # noqa: E402
from omakron import seed  # noqa: E402

SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"lin_api_[A-Za-z0-9]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),  # JWT-shaped
]


class Smoke:
    def __init__(self, out: Path, evidence: Path, claude: str, deadline_s: float):
        self.out = out
        self.evidence = evidence
        self.claude = claude
        self.deadline_s = deadline_s
        self.state = out / "state"
        self.config = out / "config"
        sock_dir = out / "sock"
        if len(str(sock_dir / "service.sock")) > 100:
            sock_dir = Path("/tmp") / f"omk-smoke-{os.getpid()}"  # noqa: S108 - AF_UNIX path limit
        self.sock = sock_dir / "service.sock"
        self.log_file = out / "service.log"
        self.proc: subprocess.Popen | None = None
        self.checks: list[dict] = []
        self.data: dict = {
            "check": "manual execution",
            "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "claude_version": None,
            "service_instances": [],
        }

    # ----------------------------------------------------------------- helpers

    def check(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")

    def env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "XDG_RUNTIME_DIR": str(self.sock.parent),
        }

    def start_service(self) -> None:
        self.sock.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omakron.service",
                "--state-dir",
                str(self.state),
                "--config-dir",
                str(self.config),
                "--socket",
                str(self.sock),
            ],
            env=self.env(),
            cwd=str(self.out),
            stdout=subprocess.DEVNULL,
            stderr=open(self.log_file, "ab"),  # noqa: SIM115 - owned by the child
            start_new_session=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit(f"service exited early:\n{self.log_file.read_text()}")
            try:
                self.request("ping")
                break
            except omakron_client.ServiceUnreachable:
                time.sleep(0.05)
        status = self.request("status")["service"]
        self.data["service_instances"].append(
            {"worker": status["worker"], "started_at": status["started_at"]}
        )

    def stop_service(self) -> int:
        assert self.proc is not None
        self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(timeout=5)

    def request(self, op: str, params: dict | None = None):
        return omakron_client.request(op, params, sock=self.sock)

    def client(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: PLW1510 - exit code is inspected
            [sys.executable, "-m", "omakron.client", "--socket", str(self.sock), *args],
            capture_output=True,
            text=True,
            env=self.env(),
            timeout=60,
        )

    # -------------------------------------------------------------------- run

    def run(self) -> bool:
        if self.out.exists():
            shutil.rmtree(self.out)
        self.config.mkdir(parents=True)
        (self.config / "settings.json").write_text(
            json.dumps({"claude_executable": self.claude, "deadline_s": self.deadline_s}, indent=2)
        )
        version = subprocess.run(
            [self.claude, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
        self.data["claude_version"] = version.stdout.strip() or version.stderr.strip()
        self.check("claude_cli_present", version.returncode == 0, self.data["claude_version"])

        self.start_service()
        routines = self.request("list_routines")["routines"]
        routine = routines[0]
        self.check(
            "seeded_routine",
            len(routines) == 1
            and routine["name"] == seed.ROUTINE_NAME
            and routine["tools"] == "default",
            f"name={routine['name']!r} model={routine['model']} revision={routine['revision']} "
            f"tools={routine['tools']!r} permission_mode={routine['permission_mode']}",
        )
        self.data["routine"] = {
            k: routine[k]
            for k in (
                "id",
                "name",
                "model",
                "schedule_kind",
                "enabled",
                "revision",
                "tools",
                "permission_mode",
            )
        }

        # Run now through the real client; the client exits before the run ends.
        t0 = time.monotonic()
        result = self.client("run-now", routine["id"])
        client_elapsed = time.monotonic() - t0
        run = json.loads(result.stdout)["run"] if result.returncode == 0 else None
        self.check(
            "client_queued_run_and_exited",
            result.returncode == 0 and run is not None,
            f"exit={result.returncode} elapsed={client_elapsed:.2f}s "
            f"status_at_answer={run and run['status']}",
        )
        if run is None:
            print(result.stdout, result.stderr, file=sys.stderr)
            return self.finish()
        after_client = self.request("get_run", {"run_id": run["id"]})["run"]
        self.check(
            "run_alive_after_client_exit",
            after_client["status"] in ("queued", "claimed", "running"),
            f"status={after_client['status']}",
        )

        done = omakron_client.wait_for_run(
            run["id"], timeout_s=self.deadline_s + 30, interval_s=0.5, sock=self.sock
        )
        self.data["run"] = {
            k: done.get(k)
            for k in (
                "id",
                "status",
                "problems",
                "routine_revision",
                "requested_model",
                "resolved_model",
                "models_used",
                "exit_code",
                "enqueued_at",
                "claimed_at",
                "started_at",
                "ended_at",
                "deadline_s",
                "output_sha256",
            )
        }
        self.check(
            "run_succeeded",
            done["status"] == "succeeded",
            f"status={done['status']} problems={done['problems']} exit={done['exit_code']}",
        )
        self.check(
            "resolved_model_is_requested_model",
            done["resolved_model"] == routine["model"]
            and routine["model"] in (done["models_used"] or []),
            f"requested={routine['model']} resolved={done['resolved_model']} "
            f"models_used={done['models_used']}",
        )
        result_text = done.get("result_text") or ""
        summary = Path(routine["cwd"]) / "SUMMARY.md"
        self.check(
            "model_did_what_the_prompt_asked",
            summary.is_file() and summary.stat().st_size > 0 and bool(result_text.strip()),
            f"summary_exists={summary.is_file()} result_chars={len(result_text)}",
        )
        run_dir = Path(done["output_dir"]) if done.get("output_dir") else None
        on_disk = run_dir is not None and (run_dir / "result.md").is_file()
        digest_ok = False
        if on_disk:
            digest_ok = (
                hashlib.sha256((run_dir / "result.md").read_bytes()).hexdigest()
                == done["output_sha256"]
            )
        self.check(
            "result_stored_durably_with_matching_digest",
            on_disk and digest_ok and not list(run_dir.glob("*.part")),
            f"files={sorted(p.name for p in run_dir.iterdir()) if run_dir else None}",
        )
        if result_text:
            self.data["saved_result"] = result_text

        # Restart the app: the result must still be there, and nothing is replayed.
        exit_code = self.stop_service()
        self.check("service_stopped_cleanly", exit_code == 0, f"exit={exit_code}")
        self.start_service()
        again = self.request("get_run", {"run_id": run["id"]})["run"]
        status = self.request("status")
        self.check(
            "result_survives_restart",
            again["status"] == "succeeded"
            and again["result_text"] == result_text
            and again["output_sha256"] == done["output_sha256"],
            f"status={again['status']} same_result={again['result_text'] == result_text}",
        )
        self.check(
            "restart_replays_nothing",
            status["service"]["interrupted_on_start"] == []
            and status["active_run"] is None
            and len(self.request("list_runs")["runs"]) == 1,
            f"interrupted_on_start={status['service']['interrupted_on_start']} "
            f"runs={len(self.request('list_runs')['runs'])}",
        )
        self.stop_service()
        return self.finish()

    # --------------------------------------------------------------- evidence

    def finish(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            self.stop_service()
        self.data["checks"] = self.checks
        text = json.dumps(self.data, indent=2, sort_keys=True)
        home = os.environ.get("HOME", "")
        if home:
            text = text.replace(home, "~")
        text = text.replace(str(self.out), "<out>")
        leaks = sorted({m.group(0)[:12] + "…" for p in SECRET_PATTERNS for m in p.finditer(text)})
        self.check(
            "no_credentials_or_home_paths_in_output",
            not leaks and home not in text,
            f"leaks={leaks}",
        )
        self.data["checks"] = self.checks
        text = (
            json.dumps(self.data, indent=2, sort_keys=True)
            .replace(home, "~")
            .replace(str(self.out), "<out>")
        )
        ok = all(c["ok"] for c in self.checks)

        self.evidence.mkdir(parents=True, exist_ok=True)
        (self.evidence / "report.json").write_text(text + "\n")
        if "saved_result" in self.data:
            (self.evidence / "result.md").write_text(self.data["saved_result"].rstrip() + "\n")
        (self.evidence / "report.md").write_text(self.markdown(ok))
        print(f"\n{'all checks passed' if ok else 'CHECKS FAILED'}: {len(self.checks)} checks")
        print(f"evidence written to {self.evidence}")
        return ok

    def markdown(self, ok: bool) -> str:
        passed = sum(1 for c in self.checks if c["ok"])
        lines = [
            "# Manual execution smoke test (real CLI)",
            "",
            f"Generated {self.data['generated_at']} by `scripts/smoke_vertical_slice.py`. "
            f"Checks passed: {passed}/{len(self.checks)}. Result: **{'pass' if ok else 'FAIL'}**.",
            "",
            "One Run now of the seeded routine in its managed working folder, through the "
            "real service, the real client, and the real `claude` CLI under the existing "
            "login, with the routine's tools and permission mode.",
            "",
            "| Item | Value |",
            "| --- | --- |",
            f"| Claude Code | `{self.data.get('claude_version')}` |",
            f"| Routine | `{self.data.get('routine', {}).get('name')}` revision "
            f"{self.data.get('routine', {}).get('revision')}, model "
            f"`{self.data.get('routine', {}).get('model')}` |",
        ]
        run = self.data.get("run") or {}
        if run:
            lines += [
                f"| Run status | `{run.get('status')}` (exit {run.get('exit_code')}) |",
                f"| Resolved model | `{run.get('resolved_model')}`; models used "
                f"`{run.get('models_used')}` |",
                f"| Saved result SHA-256 | `{run.get('output_sha256')}` |",
                f"| Enqueued / started / ended | {run.get('enqueued_at')} / "
                f"{run.get('started_at')} / {run.get('ended_at')} |",
            ]
        lines += [
            f"| Service instances | {len(self.data.get('service_instances', []))} "
            "(stopped and restarted once) |",
            "",
            "## Checks",
            "",
            "| Result | Check | Detail |",
            "| --- | --- | --- |",
        ]
        for c in self.checks:
            detail = c["detail"].replace("|", "\\|")
            lines.append(f"| {'PASS' if c['ok'] else 'FAIL'} | `{c['name']}` | {detail} |")
        if "saved_result" in self.data:
            lines += ["", "## Saved result (`result.md`)", "", self.data["saved_result"].rstrip()]
        lines += [
            "",
            "The raw service log and run folder stay under the scratch directory; "
            "they are not committed.",
            "",
        ]
        return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="scratch directory (recreated)")
    parser.add_argument(
        "--evidence", type=Path, help="private evidence folder; defaults to <out>/evidence"
    )
    parser.add_argument("--claude", default=shutil.which("claude") or "claude")
    parser.add_argument("--deadline", type=float, default=600.0)
    args = parser.parse_args()
    smoke = Smoke(
        args.out.resolve(),
        (args.evidence or args.out / "evidence").resolve(),
        args.claude,
        args.deadline,
    )
    return 0 if smoke.run() else 1


if __name__ == "__main__":
    sys.exit(main())
