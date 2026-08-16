"""Behavior tests for `lup-devtools dev comments` scanning and clearing.

Runs against a throwaway git repo: `scan_tracked(find_feedback)` must report
tracked notes with their read-context window, and `clear_markers` must strip
exactly the targeted spans — inline markers keep their code, standalone
blocks vanish whole — only ever on a `resolve/*` branch, and never a `defer`
note without `--wake`, whether or not it stated a gate. The `dev check`
comments gate and the listing renderer must keep deferred notes visible in
their own section.
"""

from pathlib import Path

import pytest
import sh
import typer

from lup.codescan.markers import NoteKind, find_feedback
from lup.devtools.dev import comments
from lup.devtools.dev.check import inline_notes_lines
from tests.unit.repos import commit_file, initialized_repo

PY_SOURCE = """\
alpha = 1
beta = 2
# lup: rework this section
# it also drags in the lines below
gamma = 3
delta = 4  # lup: rename delta
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "code.py", PY_SOURCE, "chore: base")
    monkeypatch.chdir(work)
    return work


def test_scan_reports_notes_with_read_context(repo: Path) -> None:
    found = comments.scan_tracked(find_feedback)

    assert [(c.file, c.start_line, c.end_line) for c in found] == [
        ("code.py", 3, 4),
        ("code.py", 6, 6),
    ]
    block, inline = found
    # The continuation comment line is merged into the note text.
    assert block.text == "rework this section it also drags in the lines below"
    # The context window is the actual read span cut from the file.
    assert block.read_start == 1
    assert block.context.splitlines()[0] == "alpha = 1"
    assert "gamma = 3" in block.context
    assert inline.text == "rename delta"


def test_scan_ignores_untracked_files(repo: Path) -> None:
    (repo / "scratch.py").write_text("# lup: never committed\n", encoding="utf-8")
    found = comments.scan_tracked(find_feedback)
    assert all(c.file != "scratch.py" for c in found)


def test_clear_refuses_outside_a_resolve_branch(repo: Path) -> None:
    before = (repo / "code.py").read_text(encoding="utf-8")
    with pytest.raises(typer.Exit):
        comments.clear_markers(["code.py:6"])
    assert (repo / "code.py").read_text(encoding="utf-8") == before


def test_clear_inline_marker_keeps_the_code(repo: Path) -> None:
    sh.Command("git")("-C", str(repo), "checkout", "-b", "resolve/x", _tty_out=False)

    comments.clear_markers(["code.py:6"])

    text = (repo / "code.py").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[5] == "delta = 4"  # code survives, comment is gone
    assert lines[2] == "# lup: rework this section"  # untargeted note stays
    assert text.endswith("\n")  # trailing newline preserved


def test_clear_standalone_block_removes_the_whole_span(repo: Path) -> None:
    sh.Command("git")("-C", str(repo), "checkout", "-b", "resolve/x", _tty_out=False)

    comments.clear_markers(["code.py:3"])

    lines = (repo / "code.py").read_text(encoding="utf-8").splitlines()
    assert lines == [
        "alpha = 1",
        "beta = 2",
        "gamma = 3",
        "delta = 4  # lup: rename delta",
    ]


def test_clear_skips_malformed_and_missing_targets(repo: Path) -> None:
    sh.Command("git")("-C", str(repo), "checkout", "-b", "resolve/x", _tty_out=False)
    before = (repo / "code.py").read_text(encoding="utf-8")

    comments.clear_markers(["no-line-part", "code.py:99"])

    assert (repo / "code.py").read_text(encoding="utf-8") == before


CLAIMS_SOURCE = """\
alpha = 1
# lup: solved: rework this section
# across the lines below
beta = 2
# lup: still open feedback
gamma = 3
"""


def test_retire_deletes_a_claim_on_any_branch(repo: Path) -> None:
    # The verify pass runs on real checkouts, so unlike --clear this path
    # carries no resolve/* branch requirement — its safety is shape, not ref.
    (repo / "claims.py").write_text(CLAIMS_SOURCE, encoding="utf-8")

    comments.revise_claims(["claims.py:2"], retire=True)

    text = (repo / "claims.py").read_text(encoding="utf-8")
    assert "rework this section" not in text
    assert "still open feedback" in text


def test_retire_refuses_open_feedback(repo: Path) -> None:
    (repo / "claims.py").write_text(CLAIMS_SOURCE, encoding="utf-8")

    with pytest.raises(typer.Exit):
        comments.revise_claims(["claims.py:5"], retire=True)

    assert "still open feedback" in (repo / "claims.py").read_text(encoding="utf-8")


def test_restore_reopens_a_claim_with_its_words(repo: Path) -> None:
    (repo / "claims.py").write_text(CLAIMS_SOURCE, encoding="utf-8")

    comments.revise_claims(["claims.py:2"], retire=False)

    text = (repo / "claims.py").read_text(encoding="utf-8")
    assert "# lup: rework this section" in text
    assert "# across the lines below" in text


def test_restore_narrowed_keeps_only_the_outstanding_part(repo: Path) -> None:
    (repo / "claims.py").write_text(CLAIMS_SOURCE, encoding="utf-8")

    comments.revise_claims(["claims.py:2"], retire=False, narrow="the second half")

    text = (repo / "claims.py").read_text(encoding="utf-8")
    assert "# lup: the second half" in text
    assert "across the lines below" not in text


DEFER_SOURCE = """\
epsilon = 5
# lup: defer[until the cache rework lands]: revisit epsilon
zeta = 6
"""


BARE_DEFER_SOURCE = """\
epsilon = 5
# lup: defer: revisit epsilon
zeta = 6
"""

PARKED_SOURCES = [DEFER_SOURCE, BARE_DEFER_SOURCE]


def tracked_defer_file(repo: Path, source: str) -> Path:
    git = sh.Command("git").bake("-C", str(repo), _tty_out=False)
    path = repo / "parked.py"
    path.write_text(source, encoding="utf-8")
    git("add", "parked.py")
    git("commit", "-m", "chore: park work")
    git("checkout", "-b", "resolve/x")
    return path


@pytest.mark.parametrize("source", PARKED_SOURCES)
def test_clear_skips_a_deferred_note_without_wake(repo: Path, source: str) -> None:
    path = tracked_defer_file(repo, source)

    comments.clear_markers(["parked.py:2"])

    assert (
        path.read_text(encoding="utf-8") == source
    )  # parked work survives an ordinary sweep


@pytest.mark.parametrize("source", PARKED_SOURCES)
def test_clear_strips_a_deferred_note_with_wake(repo: Path, source: str) -> None:
    path = tracked_defer_file(repo, source)

    comments.clear_markers(["parked.py:2"], wake=True)

    assert path.read_text(encoding="utf-8").splitlines() == ["epsilon = 5", "zeta = 6"]


def note_at(
    file: str,
    line: int,
    text: str = "fix it",
    kind: NoteKind = NoteKind.note,
    condition: str | None = None,
) -> comments.FoundComment:
    return comments.FoundComment(
        file=file,
        start_line=line,
        end_line=line,
        read_start=max(1, line - 2),
        read_end=line + 2,
        text=text,
        kind=kind,
        condition=condition,
        context="",
    )


def test_inline_notes_list_deferred_after_unresolved() -> None:
    lines = inline_notes_lines(
        [
            note_at("a.py", 3),
            note_at(
                ".gitignore",
                9,
                kind=NoteKind.defer,
                condition="until branches merge",
                text="purge notes history",
            ),
            note_at("c.py", 5, kind=NoteKind.defer, text="parked with no gate"),
        ]
    )

    assert lines == [
        "inline notes: 1 unresolved, 2 deferred (advisory)",
        "  a.py:3-3",
        "  deferred[until branches merge] .gitignore:9-9",
        "  deferred c.py:5-5",
    ]


def test_customization_markers_are_inventory_in_the_scaffold_and_work_downstream() -> (
    None
):
    """One marker, two readings, chosen by whether this repo is still the scaffold.

    Upstream the scaffold ships these on purpose, so listing them would put a
    permanent wall of text in front of the notes somebody is actually owed.
    Downstream they are decisions the adopting domain has not made, so they
    have to say where they are.
    """
    markers = [
        note_at("a.py", 3, kind=NoteKind.template, text="pick a model tier"),
        note_at("b.py", 9, kind=NoteKind.template, text="name your outputs"),
    ]

    assert inline_notes_lines(markers, scaffold=True) == [
        "inline notes: 0 unresolved, 2 customization (advisory)"
    ]
    assert inline_notes_lines(markers, scaffold=False) == [
        "inline notes: 0 unresolved, 2 customization (advisory)",
        "  customization a.py:3-3",
        "  customization b.py:9-9",
    ]


def test_open_notes_report_without_refusing_the_tree_that_carries_them() -> None:
    """A note asks somebody for something; a branch is expected to carry open ones."""
    lines = inline_notes_lines(
        [note_at("a.py", 3, kind=NoteKind.defer, condition="until v2", text="parked")]
    )

    assert lines[0] == "inline notes: 0 unresolved, 1 deferred (advisory)"


def test_render_separates_the_deferred_section(
    capsys: pytest.CaptureFixture[str],
) -> None:
    comments.render(
        [
            note_at(
                "a.py", 3, kind=NoteKind.defer, condition="until v2", text="parked work"
            ),
            note_at("b.py", 7, text="open feedback"),
            note_at("c.py", 5, kind=NoteKind.defer, text="parked with no gate"),
        ],
        as_json=False,
        empty="none",
    )

    out = capsys.readouterr().out
    assert "Deferred — parked until explicitly woken:" in out
    assert out.index("open feedback") < out.index("defer[until v2]: parked work")
    assert "defer: parked with no gate" in out
    assert "3 comment(s) in 3 file(s) (2 deferred)" in out
