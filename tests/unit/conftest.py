# Test fixtures and assertions construct these shapes deliberately.
"""Shared fixtures and fakes for unit tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from lup.workspace import paths

from forumboard.profiles import Roster, profiles_root

LUP_PROJECT_VERSION = "1.2.3"


def enrolled(root: Path, *names: str) -> None:
    """Write a roster holding exactly ``names``."""
    roster = Roster()
    for name in names:
        roster = roster.with_profile(name)
    roster.write(root)


def sign_in(root: Path, name: str) -> None:
    """Leave what a completed claude.ai login leaves behind."""
    session = profiles_root(root) / name / "claude-web"
    session.mkdir(parents=True)
    (session / "Cookies").write_text("stored")


def keep_account(root: Path, name: str) -> None:
    """Make a profile directory without a claude.ai session in it.

    An account this machine keeps for the agent to run under, which is not the
    same fact as a claude.ai session and not enrolment either.
    """
    (profiles_root(root) / name / "claude-config").mkdir(parents=True)


@pytest.fixture
def tmp_lup_project(tmp_path: Path) -> Iterator[Path]:
    """A throwaway project root wired into lup.workspace.paths, restored afterwards."""
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.lup]\nagent_version = "{LUP_PROJECT_VERSION}"\n', encoding="utf-8"
    )
    old_root = paths.project_root()
    paths.configure(root=tmp_path)
    yield tmp_path
    paths.configure(root=old_root)
