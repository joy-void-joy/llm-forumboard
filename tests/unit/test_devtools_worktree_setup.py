"""Behavior tests for a worktree that exists without being ready.

Creation registers the worktree in one git call and makes it usable in
several more, so any failure between them leaves a directory whose existence
says nothing about its state. What is pinned here is that the difference is
observable and acted on: a re-run finishes what was left, and a run that
cannot finish says which steps did not, rather than reporting the success
that sends an agent off diagnosing phantom errors in correct code.
"""

from pathlib import Path

import pytest
import sh
import typer

from lup.devtools.dev import worktree
from lup.devtools.harness.launch import relocation_hint
from tests.unit.repos import commit_file, initialized_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    return work


@pytest.fixture
def tree_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The sibling directory worktrees are created in, as `create` resolves it."""
    tree = tmp_path / "tree"
    tree.mkdir()
    monkeypatch.setattr(worktree, "get_tree_dir", lambda: tree)
    return tree


def repo_git(work: Path) -> sh.Command:
    return sh.Command("git").bake("-C", str(work), _tty_out=False)


def create(name: str, no_sync: bool = True, no_copy_data: bool = True) -> None:
    """Create a worktree the way the CLI does, without the slow steps."""
    worktree.create(
        name,
        no_sync=no_sync,
        no_copy_data=no_copy_data,
        base_branch=None,
        launcher=relocation_hint,
    )


def interrupted_creation(repo: Path, tree_dir: Path, name: str) -> Path:
    """The exact half-made worktree an interrupted config write leaves.

    Registered by `worktree add`, then abandoned before anything recorded a
    base or built an environment — which is what a config lock, or any other
    failure between the git call and the rest, leaves on disk.
    """
    repo_git(repo)("worktree", "add", str(tree_dir / name), "-b", name)
    return tree_dir / name


@pytest.mark.usefixtures("tree_dir")
def test_a_finished_worktree_reports_itself_active(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`already active` is kept for the case where it is true."""
    monkeypatch.chdir(repo)
    create("topic")

    with pytest.raises(typer.Exit) as exit_info:
        create("topic")

    assert exit_info.value.exit_code == 0


def test_a_half_made_worktree_is_finished_by_re_running(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The failure the success message used to hide: setup that never ran.

    The base is the observable half — `uv sync` is not run in tests — and it
    is exactly the record whose config write fails under a held lock.
    """
    path = interrupted_creation(repo, tree_dir, "topic")
    monkeypatch.chdir(repo)
    assert path.exists()

    create("topic")

    recorded = repo_git(repo)("config", "--get", "branch.topic.lup-base")
    assert str(recorded).strip() == "main"
    assert "setup never finished" in capsys.readouterr().out


def test_a_half_made_worktree_does_not_report_success(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unfinishable step exits non-zero rather than claiming the worktree is ready.

    The environment stands in for every step that can fail: `uv sync` is
    stubbed to leave no `.venv`, which is precisely the state that makes
    pyright report errors in code nobody touched.
    """
    interrupted_creation(repo, tree_dir, "topic")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "sync_dependencies", lambda _path: None)

    with pytest.raises(typer.Exit) as exit_info:
        create("topic", no_sync=False)

    assert exit_info.value.exit_code == 1


def test_the_steps_that_did_not_run_are_named(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Naming the missing step is what costs less than the diagnosis it prevents."""
    interrupted_creation(repo, tree_dir, "topic")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "sync_dependencies", lambda _path: None)

    with pytest.raises(typer.Exit):
        create("topic", no_sync=False)

    reported = capsys.readouterr().out
    assert "not ready" in reported
    assert "the synced environment (.venv)" in reported


def test_an_environment_that_was_built_is_not_rebuilt(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness is read off the worktree, so a finished step is not repeated."""
    (interrupted_creation(repo, tree_dir, "topic") / ".venv").mkdir()
    monkeypatch.chdir(repo)

    def refuse(_worktree_path: Path) -> None:
        raise AssertionError("a synced environment was rebuilt")

    monkeypatch.setattr(worktree, "sync_dependencies", refuse)

    create("topic", no_sync=False)


def test_an_opted_out_step_is_not_owed(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-sync` removes the step, so its absence cannot make a worktree unready."""
    interrupted_creation(repo, tree_dir, "topic")
    monkeypatch.chdir(repo)

    create("topic", no_sync=True)

    assert not (tree_dir / "topic" / ".venv").exists()


def test_a_recorded_base_is_left_as_it_was(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finishing a worktree completes what is missing and rewrites nothing."""
    interrupted_creation(repo, tree_dir, "topic")
    repo_git(repo)("config", "branch.topic.lup-base", "deliberate")
    monkeypatch.chdir(repo)

    create("topic")

    recorded = repo_git(repo)("config", "--get", "branch.topic.lup-base")
    assert str(recorded).strip() == "deliberate"


def test_the_gitignored_extras_are_finished_too(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree missing the source tree's local settings is not ready either."""
    path = interrupted_creation(repo, tree_dir, "topic")
    (repo / ".env.local").write_text("SECRET=1\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    create("topic", no_copy_data=False)

    assert (path / ".env.local").read_text(encoding="utf-8") == "SECRET=1\n"
