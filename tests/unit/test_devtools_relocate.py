"""Behavior tests for `lup-devtools dev relocate`.

The rewrite exists for the ordinary relocation — a flat module moving under a
subpackage — which changes the module path's depth. Pinned here is that a
depth change is applied rather than declined, that everything outside the
spliced span survives byte for byte, and that the two things which look like
module paths but are not — an imported symbol after `from`, and a mention in
prose — are left alone.
"""

from pathlib import Path

from lup.devtools.dev.relocate import (
    Relocation,
    name_parts,
    relocate,
    surviving_mentions,
)


def moved(old: str, new: str) -> Relocation:
    """One relocation, spelled the way the CLI's flag grammar spells it."""
    return Relocation(old=name_parts(old) or [], new=name_parts(new) or [])


DEEPER = [moved("lup.paths", "lup.workspace.paths")]
"""The move the command exists for: two names becoming three."""


def test_relocate_applies_a_move_that_deepens_the_path(tmp_path: Path) -> None:
    """A flat module moving under a subpackage is rewritten, not skipped."""
    source = tmp_path / "site.py"
    source.write_text("from lup.paths import sessions_dir\n", encoding="utf-8")

    edits = relocate([tmp_path], DEEPER)

    assert [edit.imports for edit in edits] == [1]
    assert source.read_text(encoding="utf-8") == (
        "from lup.workspace.paths import sessions_dir\n"
    )


def test_relocate_applies_a_move_that_shortens_the_path(tmp_path: Path) -> None:
    """The reverse move splices just as well: three names becoming two."""
    source = tmp_path / "site.py"
    source.write_text("import lup.workspace.paths as paths\n", encoding="utf-8")

    relocate([tmp_path], [moved("lup.workspace.paths", "lup.paths")])

    assert source.read_text(encoding="utf-8") == "import lup.paths as paths\n"


def test_relocate_leaves_everything_outside_the_span_alone(tmp_path: Path) -> None:
    """Spacing, continuations, comments, and unrelated code survive intact."""
    source = tmp_path / "site.py"
    source.write_text(
        "from lup.paths import (  # the trailing comment\n"
        "    sessions_dir,\n"
        "    traces_path,\n"
        ")\n"
        "from lup.pathsy import untouched\n"
        "\n"
        "VALUE = {'lup.paths': 1}\n",
        encoding="utf-8",
    )

    relocate([tmp_path], DEEPER)

    assert source.read_text(encoding="utf-8") == (
        "from lup.workspace.paths import (  # the trailing comment\n"
        "    sessions_dir,\n"
        "    traces_path,\n"
        ")\n"
        "from lup.pathsy import untouched\n"
        "\n"
        "VALUE = {'lup.paths': 1}\n"
    )


def test_relocate_rewrites_two_imports_sharing_one_line(tmp_path: Path) -> None:
    """Rightmost first, so the second splice does not shift the first."""
    source = tmp_path / "site.py"
    source.write_text("import lup.paths, lup.paths.inner\n", encoding="utf-8")

    relocate([tmp_path], DEEPER)

    assert source.read_text(encoding="utf-8") == (
        "import lup.workspace.paths, lup.workspace.paths.inner\n"
    )


def test_relocate_carries_submodules_of_a_moved_package(tmp_path: Path) -> None:
    """Relocating a package moves what sits beneath it without declaring each."""
    source = tmp_path / "site.py"
    source.write_text("from lup.paths.inner import deep\n", encoding="utf-8")

    relocate([tmp_path], DEEPER)

    assert source.read_text(encoding="utf-8") == (
        "from lup.workspace.paths.inner import deep\n"
    )


def test_relocate_leaves_an_imported_symbol_alone(tmp_path: Path) -> None:
    """Names after `import` in a `from` statement are symbols, not modules."""
    source = tmp_path / "site.py"
    source.write_text("from lup import paths\n", encoding="utf-8")

    assert relocate([tmp_path], DEEPER) == []
    assert source.read_text(encoding="utf-8") == "from lup import paths\n"


def test_relocate_reports_untouched_files_as_unchanged(tmp_path: Path) -> None:
    """A file naming no mover is not rewritten and not reported."""
    source = tmp_path / "site.py"
    source.write_text("from lup.telemetry.trace import TraceLogger\n", encoding="utf-8")

    assert relocate([tmp_path], DEEPER) == []


def test_surviving_mentions_reports_prose_the_rewrite_cannot_reach(
    tmp_path: Path,
) -> None:
    """A docstring naming the old home is reported for a human to judge."""
    source = tmp_path / "site.py"
    source.write_text('"""Reads through lup.paths."""\n', encoding="utf-8")

    mentions = surviving_mentions([tmp_path], DEEPER)

    assert [mention.split(": ", 1)[1] for mention in mentions] == [
        '"""Reads through lup.paths."""'
    ]
