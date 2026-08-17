"""Shared test fixtures.

Add fixtures here that are used across multiple test files.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from lup.gitguard import TEST_IDENTITY, guard_report, repository_state

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


@pytest.fixture(scope="session", autouse=True)
def committer_identity_armed() -> Iterator[None]:
    """Give every throwaway repository somebody to commit as, writing no file.

    Session-scoped and autouse because the git commands that need it are not
    all the suite's own: code under test runs its own ``git commit`` and
    reaches whatever the environment holds. An identity passed this way has no
    file for a misbound command to land in, which is what `git config` gives
    it. See :mod:`lup.gitguard`.
    """
    with pytest.MonkeyPatch.context() as environment:
        for name, value in TEST_IDENTITY.environment().items():
            environment.setenv(name, value)
        yield


@pytest.fixture(scope="session", autouse=True)
def enclosing_repository_untouched() -> Iterator[None]:
    """Fail the session if it wrote into the checkout it is running inside.

    The guard the library arms for its own suite, for the same reason: this
    suite builds throwaway repositories too, and one that reaches the enclosing
    checkout instead passes green while the branch the developer is standing on
    moves. Detection, where scrubbing the environment above is prevention —
    neither subsumes the other, because a fixture can misbind git without any
    help from the environment. See :mod:`lup.gitguard`.
    """
    root = Path(__file__).resolve().parents[1]
    before = repository_state(root)
    yield
    report = guard_report(before, repository_state(root))
    if report:
        pytest.fail(report, pytrace=False)
