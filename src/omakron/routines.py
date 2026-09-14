"""Validation shared by create and edit; rejected drafts never reach SQLite."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from omakron.runner import (
    DEFAULT_MODEL,
    DEFAULT_PERMISSION_MODE,
    DEFAULT_TOOLS,
    ENV_KEY_PATTERN,
    MAX_ENV_KEYS,
    PERMISSION_MODES,
)
from omakron.schedule import Schedule, ScheduleError

FIELDS = (
    "name",
    "prompt",
    "model",
    "cwd",
    "schedule_kind",
    "cron",
    "timezone",
    "tools",
    "permission_mode",
    "mcp_config",
    "env_passthrough",
)
MAX_TOOLS_CHARS = 2000


def validate(draft: dict[str, Any], managed_workdir: Path) -> dict[str, Any]:
    name, prompt = draft.get("name"), draft.get("prompt")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("name must be 1-120 characters")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20_000:
        raise ValueError("prompt must be 1-20000 characters")
    model = draft.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not re.fullmatch(r"claude-[a-z0-9][a-z0-9.-]{0,100}", model):
        raise ValueError("model must be a Claude model selector, such as claude-sonnet-5")
    cwd = draft.get("cwd")
    if not isinstance(cwd, str) or not cwd or any(c in cwd for c in ("\n", "\r", "\0")):
        raise ValueError("working folder must be an absolute path")
    folder = Path(cwd).expanduser()
    if not folder.is_absolute():
        raise ValueError("working folder must be an absolute path")
    folder = Path(os.path.realpath(folder))
    if not folder.is_dir() and not folder.is_relative_to(managed_workdir.resolve()):
        raise ValueError(f"working folder does not exist: {folder}")
    if folder.exists() and not folder.is_dir():
        raise ValueError(f"working folder is not a directory: {folder}")
    if folder.is_dir() and not os.access(folder, os.R_OK | os.X_OK):
        raise ValueError(f"working folder cannot be read: {folder}")
    execution = _validate_execution(draft)
    kind = draft.get("schedule_kind", "manual")
    cron, zone = draft.get("cron"), draft.get("timezone")
    if kind == "manual":
        if cron or zone:
            raise ValueError("a manual routine has no cron or timezone")
        cron, zone = None, None
    elif kind == "cron":
        schedule = Schedule(cron, zone)
        cron, zone = schedule.expression, schedule.timezone
    else:
        raise ScheduleError("schedule_kind must be manual or cron")
    return dict(
        name=name.strip(),
        prompt=prompt,
        model=model,
        cwd=str(folder),
        schedule_kind=kind,
        cron=cron,
        timezone=zone,
        **execution,
    )


def _validate_execution(draft: dict[str, Any]) -> dict[str, Any]:
    """The run-time choices: tools, permission mode, MCP config, and env keys."""
    tools = draft.get("tools", DEFAULT_TOOLS)
    if tools is None:
        tools = DEFAULT_TOOLS
    if (
        not isinstance(tools, str)
        or len(tools) > MAX_TOOLS_CHARS
        or any(c in tools for c in ("\n", "\r", "\0"))
    ):
        raise ValueError('tools must be "default", "", or a comma-separated list of tool names')
    mode = draft.get("permission_mode", DEFAULT_PERMISSION_MODE)
    if mode is None:
        mode = DEFAULT_PERMISSION_MODE
    if mode not in PERMISSION_MODES:
        raise ValueError(f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")
    mcp_config = draft.get("mcp_config")
    if mcp_config == "":
        mcp_config = None
    if mcp_config is not None:
        if not isinstance(mcp_config, str) or any(c in mcp_config for c in ("\n", "\r", "\0")):
            raise ValueError("mcp_config must be an absolute path to a JSON file")
        mcp_path = Path(mcp_config).expanduser()
        if not mcp_path.is_absolute() or not mcp_path.is_file():
            raise ValueError(f"mcp_config file does not exist: {mcp_path}")
        mcp_config = str(mcp_path)
    env_keys = draft.get("env_passthrough") or []
    if (
        not isinstance(env_keys, list)
        or len(env_keys) > MAX_ENV_KEYS
        or not all(isinstance(k, str) and re.fullmatch(ENV_KEY_PATTERN, k) for k in env_keys)
    ):
        raise ValueError(f"env_passthrough must be a list of up to {MAX_ENV_KEYS} variable names")
    env_keys = sorted(set(env_keys))
    return dict(tools=tools, permission_mode=mode, mcp_config=mcp_config, env_passthrough=env_keys)
