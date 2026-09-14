"""Omarchy plugin manifest contract.

Mirrors the checks in the shell's ``PluginRegistry.validateManifest`` and the
``omarchy plugin validate`` command, so CI can refuse a manifest the running
shell would reject, on a machine without Omarchy installed. The installed
command remains the reference; when the two disagree, the shell wins and this
module is fixed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REQUIRED_FIELDS = ("id", "name", "version", "kinds", "entryPoints")
SECTIONS = ("left", "center", "right")
KIND_ENTRY_POINTS = {
    "bar": "bar",
    "bar-widget": "barWidget",
    "menu": "menu",
    "overlay": "overlay",
    "panel": "panel",
    "service": "service",
}
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_manifest_dir(plugin_dir: Path) -> list[str]:
    """Return the problems with the plugin folder at ``plugin_dir`` (empty means valid)."""
    plugin_dir = Path(plugin_dir)
    if not plugin_dir.is_dir():
        return [f"plugin folder not found: {plugin_dir}"]
    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.is_file():
        return [f"missing manifest.json in {plugin_dir}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [f"manifest.json is not valid JSON: {exc}"]
    problems = validate_manifest(manifest, plugin_dir)
    problems.extend(_symlink_problems(plugin_dir))
    return problems


def validate_manifest(manifest: object, plugin_dir: Path | None = None) -> list[str]:
    """Validate manifest content. With ``plugin_dir`` also require entry files to exist."""
    if not isinstance(manifest, dict):
        return ["manifest is not an object"]
    # `1` and `True` compare equal in Python; the shell requires the number 1.
    version = manifest.get("schemaVersion")
    if isinstance(version, bool) or version != 1:
        return ["unsupported or missing schemaVersion (expected 1)"]

    problems = [
        f"manifest missing required field '{key}'" for key in REQUIRED_FIELDS if key not in manifest
    ]
    if problems:
        return problems

    plugin_id = str(manifest["id"])
    if not plugin_id:
        problems.append("manifest 'id' is empty")
    elif not ID_PATTERN.match(plugin_id) or ".." in plugin_id:
        problems.append(f"invalid plugin id '{plugin_id}'")
    elif plugin_id.startswith("omarchy."):
        problems.append(f"plugin id '{plugin_id}' uses the reserved omarchy.* namespace")

    kinds = manifest["kinds"]
    if not isinstance(kinds, list) or not kinds:
        problems.append("'kinds' must be a non-empty array")
        kinds = []

    entry_points = manifest["entryPoints"]
    if not isinstance(entry_points, dict):
        problems.append("'entryPoints' must be an object")
        entry_points = {}

    bar_widget = manifest.get("barWidget")
    if isinstance(bar_widget, dict) and "defaultSection" in bar_widget:
        section = bar_widget["defaultSection"]
        if not isinstance(section, str) or section not in SECTIONS:
            problems.append("'barWidget.defaultSection' must be left, center, or right")

    for key, value in entry_points.items():
        path = value if isinstance(value, str) else ""
        if not path:
            problems.append(f"entry point '{key}' path is empty")
        elif "\n" in path:
            problems.append(f"entry point '{key}' may not contain a newline")
        elif path.startswith("/"):
            problems.append(f"entry point must be a relative path: '{path}'")
        elif ".." in path:
            problems.append(f"entry point may not contain '..': '{path}'")
        elif plugin_dir is not None and not (Path(plugin_dir) / path).is_file():
            problems.append(f"entry point file not found: '{path}'")

    for kind in kinds:
        needed = KIND_ENTRY_POINTS.get(str(kind))
        if needed and needed not in entry_points:
            problems.append(f"kind '{kind}' requires an 'entryPoints.{needed}' to load")
    return problems


SCAN_SKIP = frozenset({".git", ".venv"})  # never installed; the venv is the documented dev setup


def _symlink_problems(plugin_dir: Path) -> list[str]:
    """Symlinks could point an installed plugin at arbitrary files.

    ``.git`` and ``.venv`` are skipped: neither is part of what the shell
    installs, and the documented development setup creates ``.venv`` in the
    repository root.
    """
    for path in sorted(plugin_dir.rglob("*")):
        if SCAN_SKIP & set(path.relative_to(plugin_dir).parts):
            continue
        if path.is_symlink():
            return [f"symlinks are not allowed inside a plugin folder: {path}"]
    return []
