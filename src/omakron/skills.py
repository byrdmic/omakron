"""Routines that point at a skill folder instead of carrying their own prompt.

A skill folder holds a ``SKILL.md``: a short YAML front matter with ``name``
and ``description``, then the prompt as Markdown. The user keeps these folders
in a repository shared between machines, so the file is the prompt's source of
truth. A routine created from one stores the folder path, and the worker reads
the file again at every launch. The run record keeps the text that was sent.

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
MAX_DISCOVERED = 200
DEFAULT_ROOTS = ("~/.claude/scheduled-tasks", "~/.claude/skills")


class SkillError(ValueError):
    """The folder is not a usable skill: missing, unreadable, or empty."""


@dataclass(frozen=True, slots=True)
class Skill:
    source: str  # the folder, absolute and resolved
    file: str  # the SKILL.md inside it
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
    """An absolute existing folder. Symlinks are followed so the stored path is stable."""
    if not isinstance(value, str) or not value or any(c in value for c in ("\n", "\r", "\0")):
        raise SkillError("skill folder must be an absolute path")
    folder = Path(value).expanduser()
    if not folder.is_absolute():
        raise SkillError("skill folder must be an absolute path")
    folder = Path(os.path.realpath(folder))
    if not folder.is_dir():
        raise SkillError(f"skill folder does not exist: {folder}")
    return folder


def load(value: object) -> Skill:
    """Read the skill in ``value``. Raises :class:`SkillError` with a plain reason."""
    folder = resolve_source(value)
    path = skill_file(folder)
    if path is None:
        raise SkillError(f"no SKILL.md in {folder}")
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
        source=str(folder),
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


def discover(roots: list[str] | tuple[str, ...]) -> list[dict[str, str]]:
    """Skill folders directly under each root, for the editor's picker.

    A root that does not exist is skipped. Folders whose SKILL.md cannot be
    read are listed with the problem as their description, so the person sees
    them and knows why they will not work.
    """
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        try:
            children = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            if skill_file(child) is None:
                continue
            resolved = os.path.realpath(child)
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                skill = load(resolved)
                entry = {"source": resolved, "name": skill.name, "description": skill.description}
            except SkillError as exc:
                entry = {"source": resolved, "name": child.name, "description": str(exc)}
            entry["root"] = str(base)
            found.append(entry)
            if len(found) >= MAX_DISCOVERED:
                return found
    return found
