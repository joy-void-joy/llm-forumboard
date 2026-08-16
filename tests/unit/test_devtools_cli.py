# lup: ignore[string-replace, tuple-shape]
# Test fixtures and assertions construct these shapes deliberately.
"""Smoke and behavior tests for the lup-devtools CLI.

The smoke test walks the root typer app's full command tree (sub-apps and
nested groups) and invokes ``--help`` on every command. This catches
import-time crashes (e.g. a module-level ``sh.Command`` for a missing
binary) and option wiring errors, entirely offline.

The pr tests pin output behavior with a stubbed gh: ``pr merge`` must
print its MergeResult even when the tree-dir lookup raises ``typer.Exit``.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
import sh
import typer
from typer.testing import CliRunner

from lup.devtools.dev import pr
from lup_template.devtools.main import app
from lup.devtools.sync import load_json

# Typer renders usage errors through Rich, which styles option tokens whenever
# it believes it is writing to a terminal. That splits a flag name from the
# prose beside it with escape codes, so output assertions only hold when the
# console is plain: FORCE_COLOR is cleared and a dumb terminal is declared.
PLAIN_CONSOLE = {"FORCE_COLOR": None, "NO_COLOR": "1", "TERM": "dumb"}

runner = CliRunner(env=PLAIN_CONSOLE)


def iter_command_paths(
    current: typer.Typer, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[str, ...]]:
    """Yield the CLI path of every group and command, depth-first."""
    yield prefix
    for command in current.registered_commands:
        if command.name:
            name = command.name
        elif command.callback:
            name = command.callback.__name__.lower().replace("_", "-")
        else:
            continue
        yield (*prefix, name)
    for group in current.registered_groups:
        sub_app = group.typer_instance
        if sub_app is None or not isinstance(group.name, str):
            continue
        yield from iter_command_paths(sub_app, (*prefix, group.name))


COMMAND_PATHS = sorted(iter_command_paths(app))


def test_command_tree_is_walked() -> None:
    flattened = {" ".join(path) for path in COMMAND_PATHS}
    assert "trace list" in flattened
    assert "dev pr merge" in flattened
    assert "dev worktree create" in flattened


@pytest.mark.parametrize(
    "path", COMMAND_PATHS, ids=[" ".join(p) or "(root)" for p in COMMAND_PATHS]
)
def test_help_succeeds_for_every_command(path: tuple[str, ...]) -> None:
    result = runner.invoke(app, [*path, "--help"])
    assert result.exit_code == 0, result.output


READONLY_COMMANDS: list[list[str]] = [
    ["version"],
    ["version", "changelog"],
    ["trace", "list"],
    ["feedback", "status"],
    ["setup", "status"],
    ["setup", "profile", "list"],
    ["sync", "status"],
]


@pytest.mark.parametrize("args", READONLY_COMMANDS, ids=lambda args: " ".join(args))
def test_readonly_command_exits_cleanly(args: list[str]) -> None:
    """--help only proves wiring; a runtime crash in a callback (the version
    sub-app once crashed on every real invocation) needs a real run."""
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output


class FakeGh:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        return ""


def raise_typer_exit() -> Path:
    raise typer.Exit(1)


@pytest.fixture
def merge_stubs(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh()
    monkeypatch.setattr(pr, "gh", fake)
    monkeypatch.setattr(pr, "get_integration_branch", lambda: "dev")
    monkeypatch.setattr(pr, "get_tree_dir", raise_typer_exit)
    return fake


def test_merge_prints_json_result_when_tree_dir_lookup_exits(
    merge_stubs: FakeGh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pr.merge(42, dry_run=False, as_json=True)

    out = capsys.readouterr().out
    assert '"pr_number": 42' in out
    assert '"merged": true' in out
    assert '"pulled": false' in out
    assert merge_stubs.calls[0][:2] == ("pr", "merge")


def test_merge_prints_text_result_when_tree_dir_lookup_exits(
    merge_stubs: FakeGh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pr.merge(7, dry_run=False, as_json=False)

    out = capsys.readouterr().out
    assert "merged: True" in out
    assert "integration_branch: dev" in out


def test_merge_defaults_to_a_merge_commit(merge_stubs: FakeGh) -> None:
    """The branch's own commits stay reachable unless a caller says otherwise."""
    pr.merge(42, dry_run=False)

    assert "--merge" in merge_stubs.calls[0]
    assert "--squash" not in merge_stubs.calls[0]


def test_merge_takes_the_method_it_is_given(merge_stubs: FakeGh) -> None:
    pr.merge(42, dry_run=False, method=pr.MergeMethod.squash)

    assert "--squash" in merge_stubs.calls[0]
    assert "--merge" not in merge_stubs.calls[0]


def test_merge_hands_further_flags_to_gh_untouched(merge_stubs: FakeGh) -> None:
    """What this signature does not name still has a way through."""
    pr.merge(42, dry_run=False, gh_args=("--admin",))

    assert "--admin" in merge_stubs.calls[0]


class GhExitOne(sh.ErrorReturnCode):
    """A concrete non-zero exit; the sh base leaves ``exit_code`` to subclasses."""

    exit_code = 1


class FailingGh:
    """gh exiting non-zero, over a PR left in *state*."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        raise GhExitOne(
            "gh pr merge", b"", b"failed to delete local branch: used by worktree"
        )

    def out(self, *args: str) -> str:
        self.calls.append(args)
        return f'{{"state": "{self.state}"}}'


def test_a_cleanup_that_failed_is_not_a_merge_that_failed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """gh deletes the branch after merging, and reports the second as the whole.

    A worktree holding the branch is enough to fail that step, which leaves a
    landed PR reported as an unlanded one — and a caller who believes it
    merges again, or hand-lands work that is already in.
    """
    monkeypatch.setattr(pr, "gh", FailingGh("MERGED"))
    monkeypatch.setattr(pr, "get_integration_branch", lambda: "dev")
    monkeypatch.setattr(pr, "get_tree_dir", raise_typer_exit)

    pr.merge(42, dry_run=False)

    assert "cleanup did not finish" in capsys.readouterr().err


def test_a_merge_that_did_not_happen_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pr, "gh", FailingGh("OPEN"))
    monkeypatch.setattr(pr, "get_integration_branch", lambda: "dev")
    monkeypatch.setattr(pr, "get_tree_dir", raise_typer_exit)

    with pytest.raises(typer.Exit):
        pr.merge(42, dry_run=False)


def test_annotated_downstream_config_raises_a_typed_recovery_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "downstream.json"
    assert load_json(path) == {"projects": []}

    path.write_text('{"projects": []}\n# a trailing annotation\n', encoding="utf-8")

    with pytest.raises(typer.BadParameter, match="not valid JSON"):
        load_json(path)


def test_ending_a_run_needs_no_adapter_but_driving_one_still_does() -> None:
    """An abort takes no turn, so the flag that picks a runtime is not its own.

    Ending a run reads recorded state and frees worktrees; nothing renders a
    skill invocation, so demanding the adapter refused the one operation a
    run in trouble most needs.
    """
    ended = runner.invoke(
        app, ["harness", "resolve", "--abort", "reason", "--run-id", "absent-run"]
    )
    assert "--adapter is required" not in ended.output
    assert "no resolver run 'absent-run' to abort" in ended.output

    driven = runner.invoke(app, ["harness", "resolve", "--run-id", "absent-run"])
    assert "--adapter is required" in driven.output
