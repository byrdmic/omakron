"""The service reports the commit it started from, and starts without one."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from omakron.service import running_code

from .service_harness import REPO_ROOT, ServiceHarness


def git(folder: Path, *args: str) -> str:
    return subprocess.check_output(
        [shutil.which("git"), "-C", str(folder), *args], text=True
    ).strip()


def make_repo(folder: Path) -> Path:
    git(folder.parent, "init", "-q", str(folder))
    for name, value in (("user.email", "test@example.invalid"), ("user.name", "Test")):
        git(folder, "config", name, value)
    (folder / "tracked.txt").write_text("one\n")
    git(folder, "add", ".")
    git(folder, "commit", "-qm", "fixture")
    return folder


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


def test_a_clean_checkout_reports_its_commit(repo):
    head = git(repo, "rev-parse", "HEAD")
    assert len(head) == 40
    assert running_code(repo) == (head, False)


def test_only_tracked_files_count_as_modified(repo):
    head = git(repo, "rev-parse", "HEAD")
    (repo / "untracked.txt").write_text("new\n")
    assert running_code(repo) == (head, False)
    (repo / "tracked.txt").write_text("two\n")
    assert running_code(repo) == (head, True)
    git(repo, "add", "tracked.txt")
    assert running_code(repo) == (head, True)


def test_a_touched_but_equal_file_is_not_modified_and_the_index_is_left_alone(repo):
    os.utime(repo / "tracked.txt", (1, 1))
    index = (repo / ".git/index").read_bytes()
    assert running_code(repo)[1] is False
    assert (repo / ".git/index").read_bytes() == index


def test_a_linked_checkout_and_a_worktree_are_top_levels(repo, tmp_path):
    head = git(repo, "rev-parse", "HEAD")
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    assert running_code(link) == (head, False)
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(worktree))
    assert running_code(worktree) == (head, False)


def test_unknown_when_git_cannot_speak_for_this_folder(repo, tmp_path, monkeypatch):
    inside = repo / "plugin"
    inside.mkdir()
    assert running_code(inside) == (None, None), "a folder inside another repository"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert running_code(plain) == (None, None), "no repository"
    assert running_code(tmp_path / "missing") == (None, None)
    git(tmp_path, "init", "-q", str(tmp_path / "unborn"))
    assert running_code(tmp_path / "unborn") == (None, None), "no commit yet"
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    assert running_code(plain) == (None, None), "GIT_DIR must not answer for another folder"


def test_unknown_when_git_fails_after_naming_the_commit_hangs_or_is_missing(
    repo, tmp_path, monkeypatch
):
    real = shutil.which("git")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    monkeypatch.setenv("PATH", str(fake_bin))
    assert running_code(repo) == (None, None), "git missing"
    fake = fake_bin / "git"
    fake.write_text(f'#!/bin/sh\ncase "$*" in *status*) exit 1;; esac\nexec {real} "$@"\n')
    fake.chmod(0o755)
    assert running_code(repo) == (None, None), "status failed"
    monkeypatch.setattr("omakron.service.GIT_TIMEOUT_S", 0.2)
    fake.write_text("#!/bin/sh\nexec /bin/sleep 5\n")
    assert running_code(repo) == (None, None), "git hung"


@pytest.mark.slow
def test_status_keeps_the_values_from_start(tmp_path):
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    shutil.copytree(REPO_ROOT / "src", plugin / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (plugin / ".gitignore").write_text("__pycache__/\n")
    make_repo(plugin)
    first = git(plugin, "rev-parse", "HEAD")
    harness = ServiceHarness(tmp_path / "svc", extra_env={"PYTHONPATH": str(plugin / "src")})
    try:
        harness.start()
        seen = harness.request("status")["service"]
        assert (seen["commit"], seen["modified"]) == (first, False)

        (plugin / "tracked.txt").write_text("two\n")
        git(plugin, "commit", "-qam", "second")
        (plugin / "tracked.txt").write_text("three\n")
        seen = harness.request("status")["service"]
        assert (seen["commit"], seen["modified"]) == (first, False)

        harness.restart()
        seen = harness.request("status")["service"]
        assert (seen["commit"], seen["modified"]) == (git(plugin, "rev-parse", "HEAD"), True)
    finally:
        harness.close()


@pytest.mark.slow
def test_the_service_starts_without_git(service_factory, tmp_path):
    empty = tmp_path / "no-git"
    empty.mkdir()
    seen = service_factory(extra_env={"PATH": str(empty)}).request("status")["service"]
    assert seen["commit"] is None and seen["modified"] is None
