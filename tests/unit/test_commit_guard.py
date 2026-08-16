"""What a commit does when a generated artifact is behind its source.

The guard is a refusal, and a refusal is only worth what an attempt proves, so
these tests write a real repository, arm it, and run `git commit` — the whole
point being that reading the installed configuration would have said the guard
was fine on the day it let two stale commits through.

The miniature repository copies one source verbatim into one generated file,
which is the shape of the case that actually failed: the policy kernel modules
are copied into both plugin trees byte for byte, so rewording a comment in one
of them makes both trees stale without changing anything either does. The hook
runs the production drift verdict over that copy rather than a stand-in for it.
"""

import sys
from pathlib import Path

import pytest
import sh

from lup.devtools.dev.commit_guard import (
    DRIFT_COMMAND,
    CommitGuard,
    GuardConflict,
    arm,
    install_guard,
    read_guard,
    uninstall_guard,
)
from lup.devtools.dev.workflow import WorkflowSpec
from lup.harness.banner import REGENERATE_COMMAND
from tests.unit.repos import initialized_repo

GUARD_SCRIPT = '''"""Refuse a commit while this repository's one generated file is behind."""

import sys
from pathlib import Path

from lup.devtools.harness.drift import inspect_drift, report_stale
from lup.harness.banner import REGENERATE_COMMAND, VERBATIM_COPY
from lup.harness.materialization import write_generated_file
from lup.harness.models import Artifact

ROOT = Path(__file__).parent


def write(root: Path | None = None, *, check: bool = False) -> Path:
    """Write or verify the generated copy, whose source is copied byte for byte."""
    artifact = Artifact(
        path=Path("generated/kernel.py"),
        content=(ROOT / "canon.py").read_text(encoding="utf-8"),
        semantic_id="test.kernel",
        banner=VERBATIM_COPY,
    )
    return write_generated_file(artifact, ROOT, REGENERATE_COMMAND, check=check)


if "--generate" in sys.argv:
    write()
    sys.exit(0)

verdict = inspect_drift([], [write])
if verdict.clean:
    sys.exit(0)
report_stale(verdict)
sys.exit(1)
'''


def canon(note: str) -> str:
    """A kernel-shaped source whose only variable part is one comment."""
    return (
        '"""One kernel module, copied verbatim into the generated tree."""\n'
        "\n"
        f"# {note}\n"
        "LIMIT = 3\n"
    )


class Repository:
    """A throwaway repository with the guard armed over one generated copy."""

    def __init__(self, work: Path, git: sh.Command, script: Path) -> None:
        self.work = work
        self.git = git
        self.script = script

    def regenerate(self) -> None:
        """Bring the generated copy back in step with its source."""
        sh.Command(sys.executable)(str(self.script), "--generate")

    def commit(self, message: str) -> None:
        """Stage everything and commit, the way the failing commits were made."""
        self.git("add", "-A")
        self.git("commit", "-m", message)

    def commits(self) -> int:
        """How many commits history holds, which is what a refusal protects."""
        return int(str(self.git("rev-list", "--count", "HEAD")))


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    # `initialized_repo` passes the hooks directory per-invocation; recording it
    # is what lets the guard find the directory git will actually read.
    git("config", "core.hooksPath", str(hooks))
    script = work / "guard.py"
    script.write_text(GUARD_SCRIPT, encoding="utf-8")
    (work / "canon.py").write_text(canon("the first wording"), encoding="utf-8")
    repository = Repository(work, git, script)
    install_guard(
        CommitGuard(command=f"{sys.executable} {script}"),
        work,
    )
    repository.regenerate()
    repository.commit("a tree whose copy is current")
    return repository


def test_a_comment_only_edit_to_the_source_refuses_the_commit(
    repository: Repository,
) -> None:
    """The case that produced the stale commits: canon reworded, copy untouched."""
    (repository.work / "canon.py").write_text(
        canon("the same rule, reworded"), encoding="utf-8"
    )

    with pytest.raises(sh.ErrorReturnCode) as refusal:
        repository.commit("a reworded comment, without its regenerated copy")

    assert repository.commits() == 1
    assert REGENERATE_COMMAND in refusal.value.stderr.decode()


def test_the_same_edit_commits_once_the_copy_is_regenerated(
    repository: Repository,
) -> None:
    """The refusal is one command away from being settled, and it stays settled."""
    (repository.work / "canon.py").write_text(
        canon("the same rule, reworded"), encoding="utf-8"
    )
    repository.regenerate()

    repository.commit("a reworded comment, with its regenerated copy")

    assert repository.commits() == 2


def test_a_hand_edited_copy_refuses_the_commit(repository: Repository) -> None:
    """Drift is read over the artifact too, not only over the source."""
    (repository.work / "generated" / "kernel.py").write_text(
        canon("edited where it is generated, not where it is written"),
        encoding="utf-8",
    )

    with pytest.raises(sh.ErrorReturnCode):
        repository.commit("a hand-edited generated file")

    assert repository.commits() == 1


def test_the_guard_leaves_a_hook_it_did_not_write_alone(tmp_path: Path) -> None:
    """A repository with its own pre-commit hook is told, not overwritten."""
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    foreign = hooks / "pre-commit"
    foreign.write_text("#!/bin/sh\nexec ./scripts/mine.sh\n", encoding="utf-8")

    with pytest.raises(GuardConflict):
        install_guard(CommitGuard(), work)

    assert read_guard(CommitGuard(), work).status == "foreign"
    assert uninstall_guard(CommitGuard(), work).status == "foreign"
    assert foreign.is_file()


def test_reinstalling_refreshes_a_body_left_by_an_older_library(
    tmp_path: Path,
) -> None:
    """An armed clone that upgrades is re-armed by the same idempotent command."""
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    guard = CommitGuard()
    install_guard(CommitGuard(command="an older check"), work)

    assert read_guard(guard, work).status == "stale"
    assert install_guard(guard, work).armed


def test_the_installed_hook_runs_the_drift_check(tmp_path: Path) -> None:
    """The body these tests arm a miniature with is the drift check by default."""
    work = tmp_path / "repo"
    initialized_repo(work, tmp_path / "hooks")

    state = install_guard(CommitGuard(), work)

    assert state.path == work / ".git" / "hooks" / "pre-commit"
    assert state.path.read_text(encoding="utf-8").endswith(f"exec {DRIFT_COMMAND}\n")
    assert state.path.stat().st_mode & 0o111


def test_arming_a_checkout_it_cannot_write_reports_instead_of_failing(
    tmp_path: Path,
) -> None:
    """Worktree creation survives a sandbox that will not let the hook be written.

    The pipeline refuses the same drift on the way in, so a checkout without
    the hook is still a working checkout — failing setup over the second line
    of defence would cost more than it buys.
    """
    outside = tmp_path / "not-a-repository"
    outside.mkdir()

    assert arm(CommitGuard(), outside).startswith("commit guard not installed")


def test_the_pipeline_runs_the_command_the_hook_installs() -> None:
    """A contributor who never armed the hook meets the same command in CI."""
    assert DRIFT_COMMAND in WorkflowSpec().body()
