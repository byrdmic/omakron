"""Validation shared by create and edit; rejected drafts never reach SQLite."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from omakron import triage
from omakron.runner import DEFAULT_MODEL
from omakron.schedule import Schedule, ScheduleError

FIELDS = ("name", "prompt", "model", "cwd", "schedule_kind", "cron", "timezone", "parameter_kind")


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
    kind = draft.get("schedule_kind", "manual")
    cron, zone = draft.get("cron"), draft.get("timezone")
    parameter = draft.get("parameter_kind")
    if parameter not in (None, triage.PARAMETER_KIND):
        raise ValueError("parameter_kind must be null or linear_issue")
    if kind == "manual":
        if cron or zone:
            raise ValueError("a manual routine has no cron or timezone")
        cron, zone = None, None
    elif kind == "cron":
        if parameter:
            raise ValueError(
                "issue triage requires a manual issue identifier and cannot be scheduled"
            )
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
        parameter_kind=parameter,
    )
