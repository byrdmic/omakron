"""The repository root is the plugin folder the shell installs; its manifest must validate."""

from __future__ import annotations

import json

from omakron.plugin import validate_manifest, validate_manifest_dir

from .conftest import FIXTURES, REPO_ROOT


def good_manifest() -> dict:
    return json.loads((REPO_ROOT / "manifest.json").read_text(encoding="utf-8"))


def test_repository_manifest_is_valid():
    assert validate_manifest_dir(REPO_ROOT) == []


def test_repository_manifest_matches_the_accepted_identity():
    manifest = good_manifest()
    assert manifest["id"] == "omakron.routines"
    assert manifest["kinds"] == ["bar-widget"]
    assert manifest["barWidget"]["defaultSection"] == "right"


def test_fixture_built_to_fail_fails():
    problems = validate_manifest_dir(FIXTURES / "plugins" / "missing-entrypoint")
    assert problems == ["entry point file not found: 'ui/DoesNotExist.qml'"]


def test_reserved_namespace_is_rejected():
    manifest = good_manifest()
    manifest["id"] = "omarchy.omakron"
    assert "plugin id 'omarchy.omakron' uses the reserved omarchy.* namespace" in validate_manifest(
        manifest
    )


def test_schema_version_must_be_the_number_one():
    for bad in ("1", 2, True, None):
        manifest = good_manifest()
        manifest["schemaVersion"] = bad
        assert validate_manifest(manifest) == ["unsupported or missing schemaVersion (expected 1)"]


def test_kind_without_entry_point_is_rejected():
    manifest = good_manifest()
    manifest["entryPoints"] = {}
    assert "kind 'bar-widget' requires an 'entryPoints.barWidget' to load" in validate_manifest(
        manifest
    )


def test_unsafe_entry_points_are_rejected():
    for path, message in (
        ("/etc/passwd", "entry point must be a relative path: '/etc/passwd'"),
        ("../x.qml", "entry point may not contain '..': '../x.qml'"),
        ("a\nb", "entry point 'barWidget' may not contain a newline"),
    ):
        manifest = good_manifest()
        manifest["entryPoints"]["barWidget"] = path
        assert message in validate_manifest(manifest)


def test_symlinks_inside_the_plugin_are_rejected(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps(good_manifest()))
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "Widget.qml").write_text("// stub")
    (tmp_path / "evil").symlink_to("/etc")
    problems = validate_manifest_dir(tmp_path)
    assert problems == [f"symlinks are not allowed inside a plugin folder: {tmp_path / 'evil'}"]
