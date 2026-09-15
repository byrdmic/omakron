"""Preview-first installation and recovery. Uses only the Python standard library.

Cloning the plugin does not install dependencies, enable a user unit, or change
bar layout. Service setup and enabling the widget are separate actions.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

PLUGIN_ID = "omakron.routines"
UNIT = "omakron.service"
# The oldest versions this release was verified on. Anything newer passes:
# the CLI and the shell update themselves often, and an exact pin would stop
# the service after every one of those updates.
MINIMUM = {"omarchy": "4.0.2", "claude": "2.1.270", "qs": "0.3.1"}


class InstallError(Exception):
    """The requested operation cannot preserve the installation contract."""


def run(argv: list[str]) -> str:
    result = subprocess.run(argv, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".omakron-part")
    with temporary.open("w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True)
class Layout:
    config: Path
    state: Path
    data: Path

    @classmethod
    def current(cls) -> Layout:
        user_home = Path.home()
        return cls(
            Path(os.environ.get("XDG_CONFIG_HOME", user_home / ".config")),
            Path(os.environ.get("XDG_STATE_HOME", user_home / ".local/state")),
            Path(os.environ.get("XDG_DATA_HOME", user_home / ".local/share")),
        )

    @property
    def plugin(self) -> Path:
        return self.config / "omarchy/plugins" / PLUGIN_ID

    @property
    def shell(self) -> Path:
        return self.config / "omarchy/shell.json"

    @property
    def unit(self) -> Path:
        return self.config / "systemd/user" / UNIT

    @property
    def database(self) -> Path:
        return self.state / "omakron/omakron.db"

    @property
    def backups(self) -> Path:
        return self.data / "omakron/backups"


def version_key(text: str) -> tuple[int, ...]:
    """``"4.0.2-1"`` -> ``(4, 0, 2, 1)``: every run of digits, in order."""
    return tuple(int(part) for part in re.findall(r"\d+", text))


def compatibility() -> dict:
    """The platform versions, or :class:`InstallError` if any is older than verified."""
    if sys.version_info[:2] < (3, 14):  # noqa: UP036 - guards a foreign interpreter
        raise InstallError("this release needs Python 3.14 or newer")
    versions = {
        "omarchy": run(["omarchy", "version"]),
        "claude": run(["claude", "--version"]).split()[0],
        "qs": run(["qs", "--version"]).split()[1],
    }
    for name, found in versions.items():
        if not version_key(found) or version_key(found) < version_key(MINIMUM[name]):
            raise InstallError(
                f"unsupported platform: {name} {found!r} is older than {MINIMUM[name]};"
                f" found {versions}"
            )
    return versions


def shell_without_plugin(value: dict) -> dict:
    result = copy.deepcopy(value)
    layout = result.get("bar", {}).get("layout", {})
    for section in ("left", "center", "right"):
        if section in layout:
            layout[section] = [
                entry
                for entry in layout[section]
                if (entry.get("id") if isinstance(entry, dict) else entry) != PLUGIN_ID
            ]
    if "plugins" in result:
        result["plugins"] = [
            entry
            for entry in result["plugins"]
            if (entry.get("id") if isinstance(entry, dict) else entry) != PLUGIN_ID
        ]
    return result


def set_widget(path: Path, enabled: bool) -> None:
    before_bytes = path.read_bytes()
    before = json.loads(before_bytes)
    after = shell_without_plugin(before)
    if enabled:
        layout = after.setdefault("bar", {}).setdefault("layout", {})
        right = layout.setdefault("right", [])
        index = next(
            (
                i + 1
                for i, entry in enumerate(right)
                if isinstance(entry, dict) and entry.get("id") == "omarchy.agents"
            ),
            len(right),
        )
        right.insert(index, {"id": PLUGIN_ID})
    if shell_without_plugin(after) != shell_without_plugin(before):
        raise InstallError("unrelated shell entries would change")
    if path.read_bytes() != before_bytes:
        raise InstallError("shell configuration changed; preview again")
    if before != after:
        atomic_json(path, after)


def unit_text(layout: Layout) -> str:
    # systemd quoting is distinct from shell quoting. Reject newlines and escape specifiers.
    def quote(value: Path | str) -> str:
        text = str(value)
        if any(ch in text for ch in "\n\r\x00"):
            raise InstallError("installation paths cannot contain control characters")
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    return "\n".join(
        [
            "[Unit]",
            "Description=Omakron scheduled Claude Code routines",
            "",
            "[Service]",
            "Type=simple",
            "Environment=" + quote(Path("PYTHONPATH=" + str(layout.plugin / "src"))),
            "Environment=" + quote(Path("XDG_CONFIG_HOME=" + str(layout.config))),
            "Environment=" + quote(Path("XDG_STATE_HOME=" + str(layout.state))),
            "Environment="
            + quote("PATH=" + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")),
            "ExecStart=" + quote(layout.plugin / ".venv/bin/python") + " -m omakron.service",
            "Restart=on-failure",
            "RestartSec=5",
            "KillMode=control-group",
            "TimeoutStopSec=30",
            "UMask=0077",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


class Manager:
    def __init__(self, layout: Layout, command=run):
        self.layout = layout
        self.command = command
        self.lock_file = None

    def close(self) -> None:
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None

    def stop(self) -> None:
        if self.layout.unit.exists():
            self.command(["systemctl", "--user", "disable", "--now", UNIT])
        # A foreground instance must stop before backup or replacement too.
        directory = self.layout.state / "omakron"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.lock_file is None:
            self.lock_file = (directory / "service.lock").open("a")
            try:
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self.close()
                raise InstallError("a foreground service owns the state; stop it first") from exc
        self.disable_dispatch()

    def disable_dispatch(self) -> None:
        if self.layout.database.exists():
            with sqlite3.connect(self.layout.database) as database:
                database.execute(
                    "INSERT INTO schema_meta(key,value) VALUES('dispatch_enabled','false')"
                    " ON CONFLICT(key) DO UPDATE SET value='false'"
                )

    def backup(self) -> Path:
        self.stop()
        target = self.layout.backups / uuid.uuid4().hex
        target.mkdir(parents=True, mode=0o700)
        sources = {
            "plugin": self.layout.plugin,
            "state": self.layout.state / "omakron",
            "config": self.layout.config / "omakron",
        }
        for name, source in sources.items():
            if source.exists():
                shutil.copytree(source, target / name, symlinks=True)
        if self.layout.unit.exists():
            shutil.copy2(self.layout.unit, target / "unit")
        if self.layout.shell.exists():
            shutil.copy2(self.layout.shell, target / "shell.json")
        # Backups never include unrelated configuration or credentials outside Omakron.
        atomic_json(
            target / "backup.json",
            {
                "format": 1,
                "plugin": str(self.layout.plugin),
                "revision": self.revision(),
                "files": hashes(target),
            },
        )
        return target

    def revision(self) -> str | None:
        if not (self.layout.plugin / ".git").exists():
            return None
        return self.command(["git", "-C", str(self.layout.plugin), "rev-parse", "HEAD"])

    def install(self, source: str, revision: str) -> None:
        if self.layout.plugin.exists():
            raise InstallError("plugin already exists; use upgrade")
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise InstallError("revision must be the exact 40-character commit SHA")
        self.layout.plugin.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".omakron-stage-", dir=self.layout.plugin.parent
        ) as stage:
            clone = Path(stage) / "plugin"
            self.command(["git", "clone", "--no-hardlinks", "--", source, str(clone)])
            self.command(["git", "-C", str(clone), "checkout", "--detach", revision])
            actual = self.command(["git", "-C", str(clone), "rev-parse", "HEAD"])
            if actual != revision:
                raise InstallError("clone did not resolve to the accepted commit")
            manifest = json.loads((clone / "manifest.json").read_text())
            if manifest.get("id") != PLUGIN_ID or not (clone / "ui/Widget.qml").is_file():
                raise InstallError("clone is not an Omakron plugin")
            os.replace(clone, self.layout.plugin)

    def setup_service(self) -> None:
        if not self.layout.plugin.is_dir():
            raise InstallError("install the plugin before service setup")
        self.stop()
        python = self.layout.plugin / ".venv/bin/python"
        if not python.exists():
            self.command([sys.executable, "-m", "venv", str(python.parent.parent)])
        self.command(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "-r",
                str(self.layout.plugin / "requirements/runtime.txt"),
            ]
        )
        self.command([str(python), "-m", "pip", "check"])
        # Initialize using the installed release's own schema before setting dispatch off.
        self.command(
            [
                str(python),
                "-c",
                f"import sys; sys.path.insert(0, {str(self.layout.plugin / 'src')!r}); "
                f"from omakron.store import Store; s=Store({str(self.layout.database)!r}); "
                "s.close()",
            ]
        )
        self.disable_dispatch()
        settings_path = self.layout.config / "omakron/settings.json"
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        settings.setdefault("claude_executable", shutil.which("claude") or "claude")
        settings["enforce_compatibility"] = True
        atomic_json(settings_path, settings)
        self.layout.unit.parent.mkdir(parents=True, exist_ok=True)
        self.layout.unit.write_text(unit_text(self.layout))
        self.command(["systemd-analyze", "--user", "verify", str(self.layout.unit)])
        self.command(["systemctl", "--user", "daemon-reload"])
        self.close()
        self.command(["systemctl", "--user", "enable", "--now", UNIT])

    def rollback(self, backup: Path) -> None:
        metadata = json.loads((backup / "backup.json").read_text())
        if metadata.get("format") != 1 or metadata.get("plugin") != str(self.layout.plugin):
            raise InstallError("backup belongs to a different installation")
        if metadata["files"] != hashes(backup):
            raise InstallError("backup contents changed; refusing an unverified restore")
        self.stop()
        for name, destination in (
            ("plugin", self.layout.plugin),
            ("state", self.layout.state / "omakron"),
            ("config", self.layout.config / "omakron"),
        ):
            if destination.exists():
                shutil.rmtree(destination)
            if (backup / name).exists():
                shutil.copytree(backup / name, destination, symlinks=True)
        self.layout.unit.unlink(missing_ok=True)
        if (backup / "unit").exists():
            self.layout.unit.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / "unit", self.layout.unit)
        self.close()
        self.stop()
        # Keep current unrelated shell edits. Only remove our widget until explicitly enabled.
        if self.layout.shell.exists():
            set_widget(self.layout.shell, False)
        self.command(["systemctl", "--user", "daemon-reload"])

    def upgrade(self, source: str, revision: str) -> Path:
        backup = self.backup()
        try:
            shutil.rmtree(self.layout.plugin)
            self.install(source, revision)
            self.setup_service()
        except Exception as exc:
            self.rollback(backup)
            record = self.layout.data / "omakron/recovery.json"
            atomic_json(
                record,
                {
                    "backup": str(backup),
                    "restored_revision": self.revision(),
                    "dispatch_enabled": False,
                    "failure": str(exc),
                },
            )
            print(f"Upgrade rolled back. Recovery evidence: {record}", file=sys.stderr)
            raise
        return backup

    def uninstall(self) -> Path:
        backup = self.backup()
        if self.layout.shell.exists():
            set_widget(self.layout.shell, False)
        if self.layout.plugin.exists():
            shutil.rmtree(self.layout.plugin)
        self.layout.unit.unlink(missing_ok=True)
        self.command(["systemctl", "--user", "daemon-reload"])
        return backup


def hashes(directory: Path) -> dict:
    result = {}
    for path in sorted(directory.rglob("*")):
        if path == directory / "backup.json":
            continue
        key = str(path.relative_to(directory))
        if path.is_symlink():
            result[key] = "link:" + os.readlink(path)
        elif path.is_file():
            result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def plan(
    action: str,
    layout: Layout,
    *,
    revision: str | None,
    backup: Path | None,
    source: str | None = None,
) -> dict:
    actions = {
        "install": [
            "Clone the exact revision into the user plugin folder.",
            "Leave service and shell unchanged.",
        ],
        "setup-service": [
            "Check platform versions. Stop dispatch and the service.",
            "Install hash-pinned dependencies in the plugin virtual environment.",
            "Write and validate the user unit. Enable and start it with dispatch disabled.",
        ],
        "enable-widget": [
            "Insert Omakron after Agents in the right bar. Preserve all unrelated entries."
        ],
        "disable": [
            "Disable dispatch and stop the user service. Remove only the Omakron bar entry."
        ],
        "backup": [
            "Disable dispatch and stop the service.",
            "Copy Omakron code, data, config, and unit to a private backup.",
        ],
        "upgrade": [
            "Stop dispatch and back up the current version and data.",
            "Clone the exact new revision and set up the service.",
            "On failure, restore the backup with dispatch disabled and the widget hidden.",
        ],
        "rollback": [
            "Verify the backup. Stop service and replace Omakron code, state, config, and unit.",
            "Keep unrelated current shell settings. Leave dispatch disabled and service stopped.",
        ],
        "uninstall": [
            "Stop dispatch and create a recoverable backup.",
            "Remove Omakron plugin, unit, and bar entry. Retain user data, config, and backups.",
        ],
    }
    return {
        "action": action,
        "requires_at_least": MINIMUM,
        "plugin": str(layout.plugin),
        "unit": str(layout.unit),
        "state": str(layout.state / "omakron"),
        "backup_root": str(layout.backups),
        "revision": revision,
        "source": source,
        "backup": str(backup) if backup else None,
        "steps": actions[action],
        "apply": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "install",
            "setup-service",
            "enable-widget",
            "disable",
            "backup",
            "upgrade",
            "rollback",
            "uninstall",
        ),
    )
    parser.add_argument("--source", help="Git repository URL or local path for install/upgrade")
    parser.add_argument("--revision", help="exact accepted commit SHA for install or upgrade")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true", help="execute the printed plan")
    args = parser.parse_args(argv)
    layout = Layout.current()
    print(
        json.dumps(
            plan(
                args.action, layout, revision=args.revision, backup=args.backup, source=args.source
            ),
            indent=2,
        ),
        flush=True,
    )
    if not args.apply:
        return 0
    manager = Manager(layout)
    try:
        if args.action in ("install", "upgrade", "setup-service", "enable-widget"):
            compatibility()
        if args.action in ("install", "upgrade"):
            if not args.revision or not args.source:
                raise InstallError("--source and an exact --revision are required")
            result = getattr(manager, args.action)(args.source, args.revision)
        elif args.action == "enable-widget":
            if not layout.plugin.is_dir():
                raise InstallError("install the plugin first")
            set_widget(layout.shell, True)
            result = None
        elif args.action == "disable":
            manager.stop()
            set_widget(layout.shell, False)
            result = None
        elif args.action == "rollback":
            if not args.backup:
                raise InstallError("--backup is required")
            result = manager.rollback(args.backup.resolve())
        else:
            result = getattr(manager, args.action.replace("-", "_"))()
        print(json.dumps({"applied": True, "backup": str(result) if result else None}))
    except (InstallError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Omakron operation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        manager.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
