"""A skill folder's SKILL.md is the prompt; only name and description are read from it."""

from __future__ import annotations

import pytest

from omakron import skills

FOLDED = """---
name: nightly-triage
description: >
  Triage the projects and decide per issue
  whether the implementer can build it.
---

# Routine: Triage

Work the list top to bottom.
"""


def write_skill(folder, text=FOLDED, filename="SKILL.md"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_text(text, encoding="utf-8")
    return folder


def test_front_matter_gives_name_and_folded_description_and_body_is_the_prompt(tmp_path):
    skill = skills.load(str(write_skill(tmp_path / "triage")))
    assert skill.name == "nightly-triage"
    assert skill.description == (
        "Triage the projects and decide per issue whether the implementer can build it."
    )
    assert skill.prompt == "# Routine: Triage\n\nWork the list top to bottom."
    assert skill.source == str(tmp_path / "triage")
    assert skill.file == str(tmp_path / "triage" / "SKILL.md")
    assert len(skill.sha256) == 64


def test_literal_block_quoted_scalar_and_lowercase_filename(tmp_path):
    text = '---\nname: "Weekly release"\ndescription: |\n  Line one.\n  Line two.\n---\nDo it.\n'
    skill = skills.load(str(write_skill(tmp_path / "weekly", text, filename="skill.md")))
    assert skill.name == "Weekly release"
    assert skill.description == "Line one.\nLine two."
    assert skill.prompt == "Do it."


def test_missing_front_matter_uses_the_folder_name(tmp_path):
    skill = skills.load(str(write_skill(tmp_path / "plain-folder", "Just a prompt.\n")))
    assert skill.name == "plain-folder" and skill.description == ""
    assert skill.prompt == "Just a prompt."


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("relative/path", "absolute"),
        ("", "absolute"),
        (None, "absolute"),
        ("/missing/omakron-skill", "does not exist"),
    ],
)
def test_bad_sources_are_refused_with_a_reason(value, message):
    with pytest.raises(skills.SkillError, match=message):
        skills.load(value)


def test_folder_without_skill_file_or_with_empty_body_is_refused(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(skills.SkillError, match="no SKILL.md"):
        skills.load(str(empty))
    with pytest.raises(skills.SkillError, match="no prompt"):
        skills.load(str(write_skill(tmp_path / "blank", "---\nname: x\n---\n\n")))
    with pytest.raises(skills.SkillError, match="longer than"):
        skills.load(str(write_skill(tmp_path / "long", "x" * (skills.MAX_PROMPT_CHARS + 1))))


def test_a_file_path_is_accepted_and_stored_as_given(tmp_path):
    folder = write_skill(tmp_path / "triage")
    by_file = skills.load(str(folder / "SKILL.md"))
    by_folder = skills.load(str(folder))
    assert by_file.source == str(folder / "SKILL.md") and by_folder.source == str(folder)
    assert by_file.file == by_folder.file == str(folder / "SKILL.md")
    assert by_file.prompt == by_folder.prompt and by_file.sha256 == by_folder.sha256
    (tmp_path / "notes.md").write_text("---\nname: notes\n---\nAny markdown file works.\n")
    assert skills.load(str(tmp_path / "notes.md")).name == "notes"
