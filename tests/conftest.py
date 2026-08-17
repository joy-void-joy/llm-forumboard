"""Shared test fixtures.

Add fixtures here that are used across multiple test files.
"""

import pytest

GIT_ENVIRONMENT = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
)
"""The variables naming a repository to git, ahead of any directory it is given.

``dev check`` runs from the pre-push hook, where git exports these. A helper
that bakes ``git -C <throwaway>`` changes the directory git works in but not
the repository it resolves, so a test that inherits them commits into the
checkout being pushed. Scrubbing them once for every test keeps that
impossible for helpers and for code under test alike, rather than leaving
each helper to remember its own environment.
"""


@pytest.fixture(autouse=True)
def git_repository_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave git no repository to find but the one a test names itself."""
    for variable in GIT_ENVIRONMENT:
        monkeypatch.delenv(variable, raising=False)
