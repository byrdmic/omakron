"""The routine an empty database starts with.

It is a plain example of what a routine is: a prompt, a model, a working
folder, and a schedule. It has tools, runs only when asked, and starts paused
like every routine. Nothing about it names a project or an outside service;
a person edits the prompt or creates their own.
"""

from __future__ import annotations

import uuid

from omakron.runner import DEFAULT_MODEL, DEFAULT_PERMISSION_MODE, DEFAULT_TOOLS

ROUTINE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:omakron:folder-summary"))
ROUTINE_NAME = "Folder summary"
FOLDER_NAME = "summary"  # created under the service's managed working folder

PROMPT = (
    "Look at the working folder you started in. Describe what is there and what "
    "changed most recently, in a few short paragraphs. If the folder is empty, say so. "
    "Write that description to a file named SUMMARY.md in the working folder, replacing "
    "any earlier version, then reply with the same description."
)


def seed_definition(cwd: str) -> dict:
    """Keyword arguments for ``Store.create_routine`` for the seeded routine."""
    return {
        "routine_id": ROUTINE_ID,
        "name": ROUTINE_NAME,
        "prompt": PROMPT,
        "model": DEFAULT_MODEL,
        "cwd": cwd,
        "schedule_kind": "manual",
        "enabled": False,
        "tools": DEFAULT_TOOLS,
        "permission_mode": DEFAULT_PERMISSION_MODE,
        "mcp_config": None,
        "env_passthrough": [],
    }
