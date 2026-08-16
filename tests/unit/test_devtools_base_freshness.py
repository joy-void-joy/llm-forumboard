"""Behavior tests for the base-freshness probe and the two gates that read it.

A checkout cannot tell from its own contents that its base has moved, so the
probe asks the remote and every entry point that opens a session asks the
probe. These run against real repositories with a local path as origin: what
is under test is which ref a checkout answers to and what git says about it,
and a scripted double would only restate the answers.
"""

from pathlib import Path

import pytest
import sh
import typer

from lup.devtools.dev.branches import (
    BaseFreshness,
    confirm_base_freshness,
    probe_base_freshness,
    require_fresh_base,
)
from lup.harness.process import LocalProcessLauncher
from tests.unit.repos import commit_file, initialized_repo


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A repository other checkouts are cloned from and measured against."""
    work = tmp_path / "origin"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    return work


def repo_git(work: Path) -> sh.Command:
    return sh.Command("git").bake("-C", str(work), _tty_out=False)


def clone_of(origin: Path, into: Path) -> Path:
    """A clone whose checked-out branch tracks the branch it was cloned from."""
    sh.Command("git")("clone", str(origin), str(into), _tty_out=False)
    for setting, value in (("user.email", "t@example.com"), ("user.name", "T")):
        repo_git(into)("config", setting, value)
    return into


def advance(origin: Path, name: str) -> None:
    """Commit one more file on the origin, which no clone has yet."""
    commit_file(repo_git(origin), origin, name, name, f"feat: {name}")


def probe(root: Path) -> BaseFreshness:
    return probe_base_freshness(LocalProcessLauncher(), root)


def test_the_count_a_checkout_is_behind_is_reported_with_the_way_out(
    origin: Path, tmp_path: Path
) -> None:
    clone = clone_of(origin, tmp_path / "clone")
    advance(origin, "one.txt")
    advance(origin, "two.txt")

    freshness = probe(clone)

    assert freshness.tracked == "origin/main"
    assert freshness.behind == 2
    assert freshness.stale()
    assert freshness.report() == (
        "base is 2 commit(s) behind origin/main: update with `git pull --ff-only`"
    )


def test_a_checkout_holding_everything_the_remote_holds_is_current(
    origin: Path, tmp_path: Path
) -> None:
    clone = clone_of(origin, tmp_path / "clone")

    freshness = probe(clone)

    assert not freshness.stale()
    assert freshness.report() == "base is current with origin/main"


def test_a_worktree_branch_is_measured_against_the_base_it_was_cut_from(
    origin: Path, tmp_path: Path
) -> None:
    """A feature branch tracks nothing, so the recorded base is asked instead.

    Without it every worktree this project's own workflow prescribes would
    answer "no remote branch" and the gate would pass on the case it exists
    for.
    """
    clone = clone_of(origin, tmp_path / "clone")
    git = repo_git(clone)
    git("switch", "-c", "feature")
    git("config", "branch.feature.lup-base", "main")
    advance(origin, "one.txt")

    freshness = probe(clone)

    assert freshness.tracked == "origin/main"
    assert freshness.behind == 1


def test_a_checkout_answering_to_no_remote_branch_says_so(
    origin: Path, tmp_path: Path
) -> None:
    clone = clone_of(origin, tmp_path / "clone")
    repo_git(clone)("switch", "-c", "feature")

    freshness = probe(clone)

    assert freshness.report() == (
        "base freshness unknown: this checkout answers to no remote branch"
    )
    assert not freshness.stale()


def test_an_unreachable_remote_is_stated_rather_than_blocking(
    origin: Path, tmp_path: Path
) -> None:
    """Neither gate stops on an unknown answer: offline is still workable."""
    clone = clone_of(origin, tmp_path / "clone")
    repo_git(clone)("remote", "set-url", "origin", str(tmp_path / "gone"))

    freshness = probe(clone)

    assert freshness.unreachable
    assert not freshness.stale()
    assert freshness.report().startswith("base freshness unknown:")
    confirm_base_freshness(freshness, interactive=False)
    require_fresh_base(freshness)


def stale_freshness() -> BaseFreshness:
    return BaseFreshness(tracked="origin/main", behind=10)


def test_a_session_nobody_is_watching_is_refused_the_moved_base() -> None:
    with pytest.raises(typer.BadParameter, match="10 commit"):
        confirm_base_freshness(stale_freshness(), interactive=False)


def test_a_human_at_the_terminal_answers_for_the_moved_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(typer, "confirm", lambda *_, **__: True)
    confirm_base_freshness(stale_freshness(), interactive=True)

    monkeypatch.setattr(typer, "confirm", lambda *_, **__: False)
    with pytest.raises(typer.BadParameter, match="10 commit"):
        confirm_base_freshness(stale_freshness(), interactive=True)


def test_a_run_refuses_to_pin_a_base_the_remote_has_moved_past() -> None:
    """A run cuts every lease from one base, so it never starts on a stale one."""
    with pytest.raises(typer.BadParameter, match="git pull --ff-only"):
        require_fresh_base(stale_freshness())
