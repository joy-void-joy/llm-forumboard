"""Behavior tests for `lup-devtools dev resolve-branch`.

The /lup:resolve editor creates its branch through this command (allowlisted as
`uv run lup-devtools`) instead of a raw `git checkout -b`, so the bash hook needs
no editor special case. These pin the slug guard and that a valid id lands the
worktree on resolve/<id>.
"""

from pathlib import Path

import pytest
import sh
import typer

from lup.devtools.dev import branches
from tests.unit.repos import commit_file, initialized_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    return work


def current_branch(work: Path) -> str:
    out = sh.Command("git")("-C", str(work), "branch", "--show-current", _tty_out=False)
    return str(out).strip()


def test_creates_resolve_branch(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    branches.create_resolve_branch("backend-abc")
    assert current_branch(repo) == "resolve/backend-abc"


def test_rejects_bad_ids(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    for bad in ["", "  ", "../escape", "has space", ".dotfirst", "a/b"]:
        with pytest.raises(typer.Exit):
            branches.create_resolve_branch(bad)
    assert current_branch(repo) == "main"  # never left the starting branch
