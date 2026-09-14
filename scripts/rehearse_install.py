"""Rehearse U10 using real clones, dependencies, migrations, and isolated services.

systemctl calls are replaced with equivalent lifecycle calls for a disposable
foreground service. Generated units still pass systemd-analyze verify. No daily
unit or shell configuration is written. The native check uses the installed UI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from omakron.client import ServiceUnreachable, request, wait_for_run
from omakron.manage import (
    Layout,
    Manager,
    atomic_json,
    compatibility,
    hashes,
    plan,
    run,
    set_widget,
    shell_without_plugin,
)

REPO = Path(__file__).resolve().parents[1]


class Rehearsal:
    def __init__(self, root: Path, out: Path):
        self.layout = Layout(root / "config", root / "state", root / "data")
        self.runtime = root / "r"
        self.runtime.mkdir(mode=0o700)
        self.socket = self.runtime / "omakron/service.sock"
        self.proc = None
        self.out = out
        self.commands = []
        self.manager = Manager(self.layout, command=self.command)

    def command(self, argv):
        self.commands.append(argv)
        if argv[0] == "systemctl":
            if "disable" in argv:
                self.stop()
            elif "enable" in argv:
                self.start()
            return ""
        return run(argv)

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)

    def start(self):
        env = dict(
            os.environ,
            PYTHONPATH=str(self.layout.plugin / "src"),
            XDG_CONFIG_HOME=str(self.layout.config),
            XDG_STATE_HOME=str(self.layout.state),
            XDG_RUNTIME_DIR=str(self.runtime),
        )
        with (self.out / "service.log").open("a") as log:
            self.proc = subprocess.Popen(
                [str(self.layout.plugin / ".venv/bin/python"), "-m", "omakron.service"],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    "isolated service failed: " + (self.out / "service.log").read_text()
                )
            try:
                self.request("status")
                return
            except OSError, RuntimeError, ServiceUnreachable:
                time.sleep(0.1)
        raise RuntimeError("isolated service did not start")

    def request(self, op, params=None):
        return request(op, params, sock=self.socket)

    def schema(self):
        database = sqlite3.connect(self.layout.database)
        try:
            return database.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
        finally:
            database.close()

    def exercise(self, revision: str, baseline: str):  # noqa: PLR0915 - sequential recovery acceptance scenario
        original_shell = json.loads((Path.home() / ".config/omarchy/shell.json").read_text())
        atomic_json(self.layout.shell, original_shell)
        atomic_json(
            self.layout.config / "omakron/settings.json",
            {
                "claude_executable": str(REPO / "tests/fake_claude.py"),
                "snapshot_source": {
                    "kind": "fixture",
                    "dir": str(REPO / "tests/fixtures/snapshots"),
                },
            },
        )
        old = run(["git", "-C", str(REPO), "rev-parse", baseline])
        previews = []
        before = hashes(self.layout.config)
        for action in (
            "install",
            "setup-service",
            "enable-widget",
            "disable",
            "backup",
            "upgrade",
            "rollback",
            "uninstall",
        ):
            previews.append(plan(action, self.layout, revision=revision, backup=None))
            assert hashes(self.layout.config) == before
        self.manager.install(str(REPO), old)
        assert self.layout.shell.read_text() == (
            json.dumps(original_shell, indent=2, ensure_ascii=False) + "\n"
        )
        self.manager.setup_service()
        routine = self.request("list_routines")["routines"][0]
        original = self.request(
            "run_now",
            {
                "routine_id": routine["id"],
                "parameter": "DEMO-9999",
                "idempotency_key": "pre-upgrade",
            },
        )["run"]
        result = wait_for_run(original["id"], timeout_s=15, interval_s=0.1, sock=self.socket)
        assert result["status"] == "succeeded"
        baseline_schema = self.schema()
        report = result["report"]
        set_widget(self.layout.shell, True)
        assert shell_without_plugin(json.loads(self.layout.shell.read_text())) == original_shell
        backup = self.manager.upgrade(str(REPO), revision)
        assert self.schema() == "2"
        assert self.request("get_run", {"run_id": original["id"]})["run"]["report"] == report
        assert not self.request("status")["service"]["dispatch_enabled"]
        # Verify the actual cloned UI inside a copied native Omarchy shell.
        subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts/verify_popup.py"),
                "--plugin",
                str(self.layout.plugin),
                "--live-editor",
                "--out",
                str(self.out / "native"),
            ],
            check=True,
        )
        self.manager.rollback(backup)
        assert self.manager.revision() == old and self.schema() == baseline_schema
        self.manager.close()
        self.start()
        restored = self.request("get_run", {"run_id": original["id"]})["run"]
        assert restored["report"] == report
        self.stop()
        # Also exercise the automatic rollback path with a real failed checkout.
        try:
            self.manager.upgrade(str(REPO), "f" * 40)
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("invalid upgrade unexpectedly succeeded")
        assert self.manager.revision() == old and self.schema() == baseline_schema
        recovery = json.loads((self.layout.data / "omakron/recovery.json").read_text())
        self.manager.uninstall()
        assert not self.layout.plugin.exists() and not self.layout.unit.exists()
        assert self.layout.database.is_file()
        assert json.loads(self.layout.shell.read_text()) == original_shell
        (self.out / "previews.json").write_text(json.dumps(previews, indent=2) + "\n")
        (self.out / "report.json").write_text(
            json.dumps(
                {
                    "check": "installation and recovery",
                    "result": "passed",
                    "old_revision": old,
                    "candidate_revision": revision,
                    "old_schema": int(baseline_schema),
                    "new_schema": 2,
                    "restored_schema": int(baseline_schema),
                    "retained_run": original["id"],
                    "retained_report_sha256": hashlib.sha256(
                        json.dumps(report, sort_keys=True).encode()
                    ).hexdigest(),
                    "checks": [
                        "all dry runs unchanged",
                        "exact git clones",
                        "hash-pinned dependency installs",
                        "unit verification",
                        "real isolated service execution",
                        "native installed UI",
                        "upgrade migrates history",
                        "rollback restores code and data",
                        "failed upgrade automatically rolls back",
                        "uninstall retains data",
                        "unrelated shell entries unchanged",
                    ],
                    "systemd_note": (
                        "Unit validation is real. Lifecycle calls use isolated foreground "
                        "processes; no daily user unit is installed."
                    ),
                    "recovery": recovery,
                    "compatibility": compatibility(),
                    "commands": self.commands,
                },
                indent=2,
            )
            + "\n"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--baseline", help="prior compatible revision; defaults to candidate")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    compatibility()
    config = Path.home() / ".config/omarchy/shell.json"
    original = config.read_bytes()
    with tempfile.TemporaryDirectory(prefix="or", dir="/tmp") as directory:
        rehearsal = Rehearsal(Path(directory), args.out.resolve())
        try:
            rehearsal.exercise(args.revision, args.baseline or args.revision)
        finally:
            rehearsal.stop()
            rehearsal.manager.close()
    assert config.read_bytes() == original
    print("U10 installation and recovery rehearsal passed")


if __name__ == "__main__":
    main()
