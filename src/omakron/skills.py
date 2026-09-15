"""Routines that point at a skill folder instead of carrying their own prompt.

A skill file is a ``SKILL.md``: a short YAML front matter with ``name`` and
``description``, then the prompt as Markdown. The user keeps these files in a
repository shared between machines, so the file is the prompt's source of
truth. A routine imported from one stores the file's location, and the worker
reads the file again at every launch. The run record keeps the text that was
sent. The location may also name the folder that holds the file.

Only ``name`` and ``description`` are read from the front matter. Anything
else is left to the file. There is no YAML dependency; the parser understands
plain scalars, quoted scalars, and the ``>`` and ``|`` block forms, which is
what these files use.

"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

SKILL_FILENAMES = ("SKILL.md", "skill.md")
MAX_PROMPT_CHARS = 20_000


class SkillError(ValueError):
    """The location is not a usable skill: missing, unreadable, or empty."""


@dataclass(frozen=True, slots=True)
class Skill:
    source: str  # what the routine stores: the file, or the folder holding it, resolved
    file: str  # the SKILL.md itself
    name: str
    description: str
    prompt: str  # the body after the front matter, stripped
    sha256: str  # of the file bytes, so a run can say which text it sent


def skill_file(folder: Path) -> Path | None:
    for candidate in SKILL_FILENAMES:
        path = folder / candidate
        if path.is_file():
            return path
    return None


def resolve_source(value: object) -> Path:
    """An absolute existing file or folder. Symlinks are followed so the stored path is stable."""
    if not isinstance(value, str) or not value or any(c in value for c in ("\n", "\r", "\0")):
        raise SkillError("skill file must be an absolute path")
    location = Path(value).expanduser()
    if not location.is_absolute():
        raise SkillError("skill file must be an absolute path")
    location = Path(os.path.realpath(location))
    if not location.exists():
        raise SkillError(f"skill file does not exist: {location}")
    return location


def load(value: object) -> Skill:
    """Read the skill at ``value``. Raises :class:`SkillError` with a plain reason."""
    location = resolve_source(value)
    if location.is_dir():
        path = skill_file(location)
        if path is None:
            raise SkillError(f"no SKILL.md in {location}")
    else:
        path = location
    folder = path.parent
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SkillError(f"SKILL.md could not be read: {exc}") from exc
    text = raw.decode("utf-8", errors="replace")
    meta, body = split_front_matter(text)
    prompt = body.strip()
    if not prompt:
        raise SkillError(f"SKILL.md has no prompt after its front matter: {path}")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise SkillError(f"SKILL.md is longer than {MAX_PROMPT_CHARS} characters: {path}")
    name = meta.get("name", "").strip() or folder.name
    return Skill(
        source=str(location),
        file=str(path),
        name=name[:120],
        description=meta.get("description", "").strip(),
        prompt=prompt,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """``(fields, body)``. A file without front matter is all body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return {}, text
    return _parse_fields(lines[1:end]), "\n".join(lines[end + 1 :])


def _parse_fields(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    key: str | None = None
    block: str | None = None  # ">" folds lines with spaces, "|" keeps newlines
    parts: list[str] = []

    def flush() -> None:
        if key is None:
            return
        if block == "|":
            fields[key] = "\n".join(parts).strip()
        else:
            fields[key] = " ".join(part.strip() for part in parts if part.strip()).strip()

    for line in lines:
        indented = line[:1] in (" ", "\t")
        if key is not None and (indented or (block and not line.strip())):
            parts.append(line.strip() if block != "|" else line.strip())
            continue
        flush()
        key, block, parts = None, None, []
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        raw_key, _, value = line.partition(":")
        key = raw_key.strip()
        value = value.strip()
        if value in (">", "|", ">-", "|-"):
            block = value[0]
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            parts = [value[1:-1]]
        else:
            parts = [value]
    flush()
    return fields
