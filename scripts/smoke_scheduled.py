"""Scheduled dispatch check: one bounded scheduled model call, then a restart without replay."""

import argparse
import datetime as dt
import json
import shutil
import time
from pathlib import Path

from omakron import triage
from omakron.report import validate_report
from omakron.runner import DEFAULT_MODEL
from smoke_vertical_slice import SECRET_PATTERNS, SNAPSHOTS, Smoke


def smoke(out: Path, evidence: Path, claude: str):  # noqa: PLR0915 - one sequential smoke scenario
    out.mkdir(parents=True, exist_ok=False)
    evidence.mkdir(parents=True, exist_ok=True)
    session = Smoke(out, evidence, claude, 120)
    session.config.mkdir()
    (session.config / "settings.json").write_text(
        json.dumps(
            {
                "claude_executable": claude,
                "deadline_s": 120,
                "snapshot_source": {"kind": "fixture", "dir": str(SNAPSHOTS)},
            }
        )
    )
    session.data["check"] = "scheduled dispatch"
    result = {"check": "scheduled dispatch", "result": "failed"}
    try:
        session.start_service()
        now = dt.datetime.now(dt.UTC)
        due = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        snapshot = json.loads((SNAPSHOTS / "DEMO-9999.json").read_text())
        folder = out / "fixture"
        folder.mkdir()
        (folder / "untouched.txt").write_text("This fixture must remain unchanged.\n")
        before = (folder / "untouched.txt").read_bytes()
        routine = session.request(
            "create_routine",
            {
                "name": "Scheduled synthetic triage smoke",
                "prompt": triage.compose_stdin(triage.PROMPT, snapshot),
                "cwd": str(folder),
                "model": DEFAULT_MODEL,
                "schedule_kind": "cron",
                "cron": f"{due.minute} {due.hour} * * *",
                "timezone": "UTC",
            },
        )["routine"]
        enabled = session.request(
            "set_enabled", {"routine_id": routine["id"], "expected_revision": 1, "enabled": True}
        )["routine"]
        print(f"Waiting for one scheduled occurrence at {due.isoformat()}", flush=True)
        deadline = time.monotonic() + 210
        found = None
        paused = False
        while time.monotonic() < deadline:
            runs = session.request("list_runs", {"routine_id": routine["id"]})["runs"]
            if runs:
                assert len(runs) == 1, "unexpected second run"
                found = session.request("get_run", {"run_id": runs[0]["id"]})["run"]
                if not paused and found["status"] != "queued":
                    session.request(
                        "set_enabled",
                        {
                            "routine_id": routine["id"],
                            "expected_revision": enabled["revision"],
                            "enabled": False,
                        },
                    )
                    paused = True
                    print("Scheduled run exists; future dispatch is paused.", flush=True)
                if found["status"] in (
                    "succeeded",
                    "failed",
                    "interrupted",
                    "timed_out",
                    "canceled",
                    "skipped",
                ):
                    break
            time.sleep(0.5)
        assert found is not None, "no scheduled run appeared"
        result["run"] = found
        assert found["status"] == "succeeded", found["problems"]
        assert dt.datetime.fromisoformat(found["scheduled_at"].replace("Z", "+00:00")) == due
        assert not validate_report(
            found["report"], team_labels=["Bug", "Feature", "Improvement", "Docs"]
        )
        assert (folder / "untouched.txt").read_bytes() == before
        assert sorted(p.name for p in folder.iterdir()) == ["untouched.txt"]
        session.stop_service()
        session.start_service()
        after = session.request("get_run", {"run_id": found["id"]})["run"]
        assert after == found
        assert len(session.request("list_runs", {"routine_id": routine["id"]})["runs"]) == 1
        assert not session.request("get_routine", {"routine_id": routine["id"]})["routine"][
            "enabled"
        ]
        result.update(
            result="passed",
            checks=[
                "one scheduled result",
                "exact due instant",
                "report validation",
                "fixture unchanged",
                "restart retains result without replay",
            ],
        )
        (evidence / "triage-report.json").write_text(json.dumps(found["report"], indent=2) + "\n")
        print("Scheduled smoke passed; saved result survived restart.", flush=True)
    finally:
        if session.proc and session.proc.poll() is None:
            session.stop_service()
        encoded = json.dumps(result, indent=2) + "\n"
        assert not any(pattern.search(encoded) for pattern in SECRET_PATTERNS)
        (evidence / "report.json").write_text(encoded)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--claude", default=shutil.which("claude"))
    args = parser.parse_args()
    smoke(args.out.resolve(), args.evidence.resolve(), args.claude)
