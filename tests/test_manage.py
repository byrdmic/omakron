"""Install transactions preserve other plugins and recover exact prior code/data."""

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from omakron.manage import (
    InstallError,
    Layout,
    Manager,
    atomic_json,
    hashes,
    main,
    set_widget,
    shell_without_plugin,
    unit_text,
)
from omakron.store import Store


@pytest.fixture
def installation(tmp_path):
    layout = Layout(tmp_path / "config", tmp_path / "state", tmp_path / "data")
    shell = {
        "version": 1,
        "idle": {"lock": 5400},
        "plugins": [{"id": "other.service", "secret": "kept"}],
        "bar": {
            "layout": {
                "left": [{"id": "other.widget", "options": [1, 2]}],
                "center": [],
                "right": [{"id": "omarchy.agents", "providers": {"claude": True}}],
            }
        },
    }
    atomic_json(layout.shell, shell)
    return layout


def test_all_dry_runs_leave_files_unchanged(installation, monkeypatch):
    monkeypatch.setattr(Layout, "current", lambda: installation)
    original = hashes(installation.config.parent)
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
        assert main([action]) == 0
        assert hashes(installation.config.parent) == original


def test_widget_round_trip_preserves_unrelated_entries(installation):
    initial = json.loads(installation.shell.read_text())
    set_widget(installation.shell, True)
    enabled = json.loads(installation.shell.read_text())
    assert enabled["bar"]["layout"]["right"][1] == {"id": "omakron.routines"}
    assert shell_without_plugin(enabled) == initial
    set_widget(installation.shell, True)
    assert json.loads(installation.shell.read_text()) == enabled
    set_widget(installation.shell, False)
    assert json.loads(installation.shell.read_text()) == initial


def seed_installed(layout):
    layout.plugin.mkdir(parents=True)
    (layout.plugin / "version").write_text("old version")
    store = Store(layout.database)
    store.create_routine(
        name="Keep my routine",
        prompt="A saved prompt",
        model="claude-sonnet-5",
        cwd=str(layout.state),
        schedule_kind="manual",
    )
    store.close()
    config = layout.config / "omakron"
    config.mkdir()
    (config / "settings.json").write_text('{"deadline_s":600}')
    return hashes(layout.plugin), layout.database.read_bytes()


def test_rollback_restores_code_and_data_and_keeps_new_unrelated_settings(installation):
    code, _ = seed_installed(installation)
    commands = []
    manager = Manager(installation, command=commands.append)
    try:
        backup = manager.backup()
        backed_up = (backup / "state/omakron.db").read_bytes()
        (installation.plugin / "version").write_text("broken upgrade")
        with sqlite3.connect(installation.database) as db:
            db.execute("UPDATE routines SET prompt='broken'")
        shell = json.loads(installation.shell.read_text())
        shell["idle"]["lock"] = 9000
        atomic_json(installation.shell, shell)
        manager.rollback(backup)
        assert hashes(installation.plugin) == code
        assert installation.database.read_bytes() == backed_up
        assert json.loads(installation.shell.read_text())["idle"]["lock"] == 9000
        with sqlite3.connect(installation.database) as db:
            assert db.execute("SELECT prompt FROM routines").fetchone()[0] == "A saved prompt"
            assert (
                db.execute("SELECT value FROM schema_meta WHERE key='dispatch_enabled'").fetchone()[
                    0
                ]
                == "false"
            )
    finally:
        manager.close()


def test_changed_backup_is_rejected_before_touching_installation(installation):
    seed_installed(installation)
    manager = Manager(installation, command=lambda args: "")
    try:
        backup = manager.backup()
        before = hashes(installation.plugin)
        (backup / "plugin/version").write_text("tampered")
        with pytest.raises(InstallError, match="contents changed"):
            manager.rollback(backup)
        assert hashes(installation.plugin) == before
    finally:
        manager.close()


def test_uninstall_retains_user_data_and_configuration(installation):
    seed_installed(installation)
    set_widget(installation.shell, True)
    unrelated = shell_without_plugin(json.loads(installation.shell.read_text()))
    manager = Manager(installation, command=lambda args: "")
    try:
        backup = manager.uninstall()
        assert not installation.plugin.exists() and not installation.unit.exists()
        assert installation.database.is_file()
        assert (installation.config / "omakron/settings.json").is_file()
        assert (backup / "backup.json").is_file()
        assert json.loads(installation.shell.read_text()) == unrelated
    finally:
        manager.close()


def test_active_foreground_service_blocks_backup(service, tmp_path):
    manager = Manager(Layout(service.config.parent / "other", service.state.parent, tmp_path))
    # The harness uses state directly, whereas the installed layout adds /omakron.
    manager.layout.state.joinpath("omakron").symlink_to(service.state, target_is_directory=True)
    with pytest.raises(InstallError, match="foreground service"):
        manager.backup()
    assert service.proc.poll() is None
    assert not manager.layout.backups.exists()


def test_unit_quotes_spaces_and_percent_specifiers(tmp_path):
    layout = Layout(tmp_path / "config with % spaces", tmp_path / "state", tmp_path / "data")
    text = unit_text(layout)
    assert "%%" in text and 'ExecStart="' in text
    assert "KillMode=control-group" in text and "UMask=0077" in text


def test_install_clones_exact_revision_without_setting_up_service(installation, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run([shutil.which("git"), "init", str(source)], check=True, capture_output=True)
    for name, value in (("user.email", "test@example.invalid"), ("user.name", "Test")):
        subprocess.run([shutil.which("git"), "-C", str(source), "config", name, value], check=True)
    (source / "ui").mkdir()
    (source / "ui/Widget.qml").write_text("Item {}")
    atomic_json(source / "manifest.json", {"id": "omakron.routines"})
    subprocess.run([shutil.which("git"), "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [shutil.which("git"), "-C", str(source), "commit", "-m", "fixture"],
        check=True,
        capture_output=True,
    )
    revision = subprocess.check_output(
        [shutil.which("git"), "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    shell = installation.shell.read_bytes()
    manager = Manager(installation)
    manager.install(str(source), revision)
    assert manager.revision() == revision
    assert not installation.unit.exists() and not installation.database.exists()
    assert installation.shell.read_bytes() == shell
    with pytest.raises(InstallError, match="already exists"):
        manager.install(str(source), revision)


def test_failed_upgrade_restores_prior_data_and_records_recovery(installation, monkeypatch):
    original, _ = seed_installed(installation)
    manager = Manager(installation, command=lambda args: "")

    def install_broken(source, revision):
        installation.plugin.mkdir()
        (installation.plugin / "version").write_text("bad candidate")

    def fail_setup():
        raise InstallError("injected dependency failure")

    monkeypatch.setattr(manager, "install", install_broken)
    monkeypatch.setattr(manager, "setup_service", fail_setup)
    try:
        with pytest.raises(InstallError, match="injected dependency"):
            manager.upgrade("unused", "a" * 40)
        assert hashes(installation.plugin) == original
        record = json.loads((installation.data / "omakron/recovery.json").read_text())
        assert not record["dispatch_enabled"] and Path(record["backup"]).is_dir()
    finally:
        manager.close()


@pytest.mark.parametrize("action", ["install", "upgrade"])
def test_apply_requires_explicit_source(installation, monkeypatch, capsys, action):
    monkeypatch.setattr(Layout, "current", lambda: installation)
    monkeypatch.setattr("omakron.manage.compatibility", lambda: None)
    original = hashes(installation.config.parent)
    assert main([action, "--revision", "a" * 40, "--apply"]) == 1
    assert "--source and an exact --revision are required" in capsys.readouterr().err
    assert hashes(installation.config.parent) == original


def test_preview_names_requested_source(installation, monkeypatch, capsys):
    monkeypatch.setattr(Layout, "current", lambda: installation)
    assert main(["install", "--source", "https://example.invalid/omakron.git"]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "https://example.invalid/omakron.git"
