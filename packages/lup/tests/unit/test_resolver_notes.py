"""Clearance of a concern's own notes from its leased worktree.

The resolver's whole marker story rests on these: a lease removes what it
owns and nothing else, so the worker never edits a marker and a sibling's
note never disappears as a side effect.
"""

from pathlib import Path

from lup.resolver.models import AcceptanceCriterion, Concern, ReviewNote
from lup.resolver.notes import clear_concern_notes


def concern_with(notes: list[ReviewNote], identifier: str = "dispatch") -> Concern:
    return Concern(
        id=identifier,
        title=identifier.title(),
        spec=f"Resolve {identifier}",
        criteria=[AcceptanceCriterion(id=f"{identifier}-done", description="done")],
        notes=notes,
    )


def test_a_concern_clears_its_own_note(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    module.write_text(
        "value = 1\n# lup: rework this dispatch\nresult = value\n", encoding="utf-8"
    )
    clearance = clear_concern_notes(
        tmp_path,
        concern_with(
            [ReviewNote(file=Path("module.py"), line=2, text="rework this dispatch")]
        ),
    )

    assert module.read_text(encoding="utf-8") == "value = 1\nresult = value\n"
    assert [note.text for note in clearance.cleared] == ["rework this dispatch"]
    assert clearance.missing == []


def test_a_siblings_note_in_the_same_file_survives(tmp_path: Path) -> None:
    """The cross-concern hazard: two concerns' notes on adjacent lines."""
    module = tmp_path / "module.py"
    module.write_text(
        "# lup: mine to fix\nvalue = 1\n# lup: belongs to another concern\n",
        encoding="utf-8",
    )
    clearance = clear_concern_notes(
        tmp_path,
        concern_with([ReviewNote(file=Path("module.py"), line=1, text="mine to fix")]),
    )

    remaining = module.read_text(encoding="utf-8")
    assert "belongs to another concern" in remaining
    assert "mine to fix" not in remaining
    assert len(clearance.cleared) == 1


def test_a_drifted_note_is_found_by_its_text(tmp_path: Path) -> None:
    """A dependent lease branches from its parent, so recorded lines move."""
    module = tmp_path / "module.py"
    module.write_text(
        "added = 0\nadded = 1\nvalue = 1\n# lup: rework this dispatch\n",
        encoding="utf-8",
    )
    clearance = clear_concern_notes(
        tmp_path,
        concern_with(
            [ReviewNote(file=Path("module.py"), line=2, text="rework this dispatch")]
        ),
    )

    assert "lup:" not in module.read_text(encoding="utf-8")
    assert len(clearance.cleared) == 1


def test_an_identical_text_elsewhere_picks_the_nearest(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    module.write_text(
        "# lup: same words\nvalue = 1\n# lup: same words\n", encoding="utf-8"
    )
    clearance = clear_concern_notes(
        tmp_path,
        concern_with([ReviewNote(file=Path("module.py"), line=3, text="same words")]),
    )

    assert module.read_text(encoding="utf-8") == "# lup: same words\nvalue = 1\n"
    assert len(clearance.cleared) == 1


def test_a_note_whose_code_is_gone_is_reported_not_raised(tmp_path: Path) -> None:
    """A parent concern may have deleted the code this note sat on."""
    module = tmp_path / "module.py"
    module.write_text("value = 1\n", encoding="utf-8")
    clearance = clear_concern_notes(
        tmp_path,
        concern_with([ReviewNote(file=Path("module.py"), line=2, text="long gone")]),
    )

    assert clearance.cleared == []
    assert [note.text for note in clearance.missing] == ["long gone"]


def test_an_unreadable_file_is_reported_not_raised(tmp_path: Path) -> None:
    clearance = clear_concern_notes(
        tmp_path,
        concern_with([ReviewNote(file=Path("absent.py"), line=1, text="never here")]),
    )

    assert clearance.cleared == []
    assert [note.file for note in clearance.missing] == [Path("absent.py")]


def test_a_deferred_note_is_never_cleared(tmp_path: Path) -> None:
    """Parked work outlives a clearance that did not meet its condition."""
    module = tmp_path / "module.py"
    module.write_text(
        "# lup: defer[until v2 ships]: rework the cache\nvalue = 1\n", encoding="utf-8"
    )
    clearance = clear_concern_notes(
        tmp_path,
        concern_with(
            [
                ReviewNote(
                    file=Path("module.py"),
                    line=1,
                    text="defer[until v2 ships]: rework the cache",
                )
            ]
        ),
    )

    assert "defer[until v2 ships]" in module.read_text(encoding="utf-8")
    assert clearance.cleared == []
    assert len(clearance.missing) == 1


def test_a_bare_deferred_note_is_never_cleared(tmp_path: Path) -> None:
    """Work parked behind no stated gate parks like work that named one."""
    module = tmp_path / "module.py"
    module.write_text("# lup: defer: rework the cache\nvalue = 1\n", encoding="utf-8")
    clearance = clear_concern_notes(
        tmp_path,
        concern_with(
            [
                ReviewNote(
                    file=Path("module.py"),
                    line=1,
                    text="defer: rework the cache",
                )
            ]
        ),
    )

    assert "defer: rework the cache" in module.read_text(encoding="utf-8")
    assert clearance.cleared == []
    assert len(clearance.missing) == 1


def test_an_inline_note_keeps_its_code(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    module.write_text("value = compute()  # lup: name this better\n", encoding="utf-8")
    clear_concern_notes(
        tmp_path,
        concern_with(
            [ReviewNote(file=Path("module.py"), line=1, text="name this better")]
        ),
    )

    assert module.read_text(encoding="utf-8") == "value = compute()\n"


def test_notes_across_several_files_all_clear(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("# lup: first concern note\nvalue = 1\n", encoding="utf-8")
    second.write_text("other = 2\n# lup: second concern note\n", encoding="utf-8")
    clearance = clear_concern_notes(
        tmp_path,
        concern_with(
            [
                ReviewNote(file=Path("first.py"), line=1, text="first concern note"),
                ReviewNote(file=Path("second.py"), line=2, text="second concern note"),
            ]
        ),
    )

    assert "lup:" not in first.read_text(encoding="utf-8")
    assert "lup:" not in second.read_text(encoding="utf-8")
    assert len(clearance.cleared) == 2
