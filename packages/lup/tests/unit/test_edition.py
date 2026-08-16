"""Where editing is happening, published by one process and read by another.

The writer is the hermetic hook runtime, which may not import the module that
owns the record. Nothing passes between them: each resolves the location out
of the repository they are both in, and each spells the record's shape for
itself — so these pin both ends against each other, since no type checker
spans that boundary.
"""

import json
from pathlib import Path

from lup.policy.assets.host import (
    file_diagnostics,
    publish_edition,
    shared_git_directory,
    worktree_root,
)
from lup.workspace.edition import Edition, edition_path, read_edition


def checkout(root: Path) -> Path:
    """A main checkout: `.git` is the git directory itself."""
    (root / ".git").mkdir(parents=True)
    return root


def linked(main: Path, root: Path) -> Path:
    """A worktree of *main*: `.git` is a file naming the main git directory."""
    root.mkdir(parents=True)
    (root / ".git").write_text(
        f"gitdir: {main / '.git' / 'worktrees' / root.name}\n", encoding="utf-8"
    )
    return root


def edited(root: Path, name: str = "module.py") -> Path:
    file = root / name
    file.write_text("x = 1\n", encoding="utf-8")
    return file


def test_a_file_belongs_to_the_checkout_that_holds_it(tmp_path: Path) -> None:
    work = checkout(tmp_path / "repo")
    file = work / "src" / "module.py"
    file.parent.mkdir(parents=True)
    file.write_text("x = 1\n", encoding="utf-8")

    assert worktree_root(str(file)) == str(work)


def test_a_checkout_root_answers_for_itself(tmp_path: Path) -> None:
    """Codex hands over a directory, and the walk has to admit one.

    Only the parents were candidates, so a directory that is already a
    checkout was answered for by whatever encloses it — or by nothing.
    """
    work = checkout(tmp_path / "repo")

    assert worktree_root(str(work)) == str(work)


def test_a_path_in_no_checkout_names_none(tmp_path: Path) -> None:
    loose = tmp_path / "loose.py"
    loose.write_text("x = 1\n", encoding="utf-8")

    assert worktree_root(str(loose)) == ""


def test_a_relative_path_names_none() -> None:
    """The hook is promised no working directory to resolve one against."""
    assert worktree_root("src/module.py") == ""


def test_a_worktree_resolves_to_the_directory_it_shares(tmp_path: Path) -> None:
    """The whole point: two checkouts, one place both of them can name.

    The hook runs wherever the edit landed and the reader wherever its
    server was launched. Neither can see the other's directory, so a record
    written beside the edit would be looked for somewhere it never was.
    """
    main = checkout(tmp_path / "repo")
    worktree = linked(main, tmp_path / "feature")

    assert shared_git_directory(str(edited(worktree))) == str(main / ".git")
    assert shared_git_directory(str(edited(main))) == str(main / ".git")


def test_a_repository_kept_beside_its_worktrees_resolves_too(tmp_path: Path) -> None:
    """A git directory need not sit inside a checkout, and this one does not.

    Taking the checkout above `worktrees/` assumes the standard layout. Where
    the repository is kept beside its worktrees instead, that path is not a
    checkout at all — it is whatever encloses the repository, and the record
    lands outside it. The directory they share is one level lower and is
    there in both layouts.
    """
    bare = tmp_path / "repo.git"
    (bare / "worktrees" / "feature").mkdir(parents=True)
    work = tmp_path / "tree" / "feature"
    work.mkdir(parents=True)
    (work / ".git").write_text(
        f"gitdir: {bare / 'worktrees' / 'feature'}\n", encoding="utf-8"
    )

    assert shared_git_directory(str(edited(work))) == str(bare)


def test_the_hook_and_the_library_name_one_location(tmp_path: Path) -> None:
    """Neither half can import the other, so the paths are pinned equal."""
    main = checkout(tmp_path / "repo")
    worktree = linked(main, tmp_path / "feature")
    file = edited(worktree)

    publish_edition(str(file))

    assert edition_path(worktree).is_file()
    assert edition_path(worktree) == edition_path(main)


def test_what_the_hook_writes_is_what_the_library_reads(tmp_path: Path) -> None:
    work = checkout(tmp_path / "repo")
    file = edited(work)

    publish_edition(str(file))

    assert read_edition(edition_path(work)) == Edition(workspace=work, file=file)


def test_an_edit_in_a_worktree_publishes_that_worktree(tmp_path: Path) -> None:
    """The record names where editing happened, not where it was recorded."""
    main = checkout(tmp_path / "repo")
    worktree = linked(main, tmp_path / "feature")

    publish_edition(str(edited(worktree)))

    published = read_edition(edition_path(main))
    assert published is not None and published.workspace == worktree


def test_a_later_edit_replaces_an_earlier_one(tmp_path: Path) -> None:
    main = checkout(tmp_path / "repo")
    worktree = linked(main, tmp_path / "feature")

    publish_edition(str(edited(main)))
    publish_edition(str(edited(worktree)))

    published = read_edition(edition_path(main))
    assert published is not None and published.workspace == worktree


def test_a_path_in_no_repository_publishes_nothing(tmp_path: Path) -> None:
    loose = tmp_path / "loose.py"
    loose.write_text("x = 1\n", encoding="utf-8")

    publish_edition(str(loose))

    assert list(tmp_path.glob("**/edition.json")) == []


def test_an_unwritable_destination_does_not_raise(tmp_path: Path) -> None:
    """A verdict must never turn on this, and neither must an edit.

    The permission path converts anything raised into a conservative ask.
    Publishing runs after the tool, where no verdict is left to corrupt, but
    a failure that propagated would still reach that handler and answer for
    the edit with a filesystem error.
    """
    work = checkout(tmp_path / "repo")
    (work / ".lup").write_text("occupied\n", encoding="utf-8")

    publish_edition(str(edited(work)))


def checker(root: Path, payload: str) -> list[str]:
    """A stand-in type checker emitting *payload*, so the parsing is the subject."""
    script = root / "fake-checker"
    script.write_text(f"#!/bin/sh\ncat <<'JSON'\n{payload}\nJSON\n", encoding="utf-8")
    script.chmod(0o755)
    return ["fake-checker"]


def report(file: Path, severity: str = "error", line: int = 0) -> str:
    return json.dumps(
        {
            "generalDiagnostics": [
                {
                    "file": str(file),
                    "severity": severity,
                    "range": {"start": {"line": line}},
                    "message": "something is wrong",
                }
            ]
        }
    )


def test_a_diagnostic_for_the_edited_file_is_reported(tmp_path: Path) -> None:
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = checker(work, report(file))

    assert file_diagnostics(str(file), command) == [
        "error 1: something is wrong",
    ]


def test_a_file_the_checker_cannot_read_is_not_checked(tmp_path: Path) -> None:
    """A type checker handed a manifest reports the manifest as broken.

    Every line of `pyproject.toml` is a syntax error to a Python parser, so an
    edit to one answered with the checker's whole opinion of the file — dozens
    of errors about lines the edit never touched, none of them true. Output
    that is noise on sight is output a reader learns to scroll past, including
    on the edits where it was worth reading.
    """
    work = checkout(tmp_path / "repo")
    manifest = work / "pyproject.toml"
    manifest.write_text('[project]\nname = "x"\n', encoding="utf-8")
    command = checker(work, report(manifest))

    assert file_diagnostics(str(manifest), command) == []


def test_the_readable_suffixes_are_the_callers_to_choose(tmp_path: Path) -> None:
    """The checker is the caller's, so what it reads is declared, not assumed."""
    work = checkout(tmp_path / "repo")
    file = edited(work, "module.qs")
    command = checker(work, report(file))

    assert file_diagnostics(str(file), command) == []
    assert file_diagnostics(str(file), command, suffixes=(".qs",)) == [
        "error 1: something is wrong",
    ]


def test_a_diagnostic_about_another_file_is_not(tmp_path: Path) -> None:
    """The checker resolves what the file imports, so it can see the whole tree.

    Repeating that would answer every edit with the same standing backlog,
    most of it about files this edit never touched.
    """
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = checker(work, report(work / "elsewhere.py"))

    assert file_diagnostics(str(file), command) == []


def test_an_informational_note_is_not_a_diagnostic(tmp_path: Path) -> None:
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = checker(work, report(file, severity="information"))

    assert file_diagnostics(str(file), command) == []


def test_no_declared_checker_reports_nothing(tmp_path: Path) -> None:
    """Empty declares no checker, rather than guessing at one."""
    work = checkout(tmp_path / "repo")

    assert file_diagnostics(str(edited(work)), []) == []


def test_a_checker_that_is_not_installed_reports_nothing(tmp_path: Path) -> None:
    """A missing checker is not evidence about the edit."""
    work = checkout(tmp_path / "repo")

    assert file_diagnostics(str(edited(work)), ["nowhere/pyright"]) == []


def test_a_checker_that_writes_nonsense_reports_nothing(tmp_path: Path) -> None:
    """This runs after the tool, so the alternative to silence is failing an
    edit that already happened."""
    work = checkout(tmp_path / "repo")
    file = edited(work)
    command = checker(work, "not json at all")

    assert file_diagnostics(str(file), command) == []


def test_a_corrupt_record_reads_as_none(tmp_path: Path) -> None:
    """This only refines a root the caller already has, so it may decline."""
    destination = tmp_path / "edition.json"
    destination.write_text("{ not json", encoding="utf-8")

    assert read_edition(destination) is None


def test_a_missing_record_reads_as_none(tmp_path: Path) -> None:
    assert read_edition(tmp_path / "nowhere.json") is None


def test_a_record_naming_a_removed_workspace_reads_as_none(tmp_path: Path) -> None:
    """Landing a branch deletes its worktree; the record it published stays.

    A reader that trusts the dead path resolves every relative question
    against it and answers none of them, which is worse than the fallback
    it was refining — that one is rooted somewhere that exists.
    """
    workspace = tmp_path / "gone"
    workspace.mkdir()
    destination = tmp_path / "edition.json"
    destination.write_text(
        Edition(workspace=workspace, file=workspace / "edited.py").model_dump_json(),
        encoding="utf-8",
    )
    assert read_edition(destination) is not None

    workspace.rmdir()

    assert read_edition(destination) is None


def test_the_published_bytes_are_the_declared_fields(tmp_path: Path) -> None:
    """The hook writes JSON by hand; drift here is drift in the contract."""
    work = checkout(tmp_path / "repo")

    publish_edition(str(edited(work)))

    written = json.loads(edition_path(work).read_text(encoding="utf-8"))
    assert set(written) == set(Edition.model_fields)
