# Test fixtures and assertions construct these shapes deliberately.
"""Shared fixtures and fakes for unit tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from lup.workspace import paths

LUP_PROJECT_VERSION = "1.2.3"


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
