"""Taking a project's tracker issues as evidence a run can plan from.

An issue is already the repository's structured, reviewable record of what is
wrong. Before this it had to be transcribed into a `# lup:` note before a run
could act on it, and friction a run found died with the run — so evidence made
a round trip through a human because the two surfaces could not talk.
"""

from pathlib import Path

import pytest

import lup.devtools.utils as utils
from lup.devtools.dev.issues import IssueLabel, IssueRow
from lup.devtools.utils import slug_from_remote
from lup.resolver.core import planned_evidence
from lup.resolver.models import (
    InventoryNote,
    IssueEvidence,
    ResolveRequest,
    SourceSnapshot,
)


def issue(number: int) -> IssueEvidence:
    return IssueEvidence(
        number=number,
        url=f"https://example.test/issues/{number}",
        title=f"Issue {number}",
        body="what goes wrong",
    )


def request(notes: int, statements: int, issues: int) -> ResolveRequest:
    return ResolveRequest(
        source=SourceSnapshot(branch="dev", commit="a" * 40),
        notes=[
            InventoryNote(file=Path("a.py"), line=index + 1, text="note", context="ctx")
            for index in range(notes)
        ],
        statements=[f"statement {index}" for index in range(statements)],
        issues=[issue(index + 1) for index in range(issues)],
    )


@pytest.mark.parametrize(
    ("remote", "slug"),
    [
        # An ssh alias, the shape that broke every `gh` query when reported.
        ("alias:owner/name.git", "owner/name"),
        ("git@github.com:owner/name.git", "owner/name"),
        ("https://github.com/owner/name.git", "owner/name"),
        ("https://github.com/owner/name", "owner/name"),
        ("ssh://git@github.com/owner/name.git", "owner/name"),
        ("/a/local/path.git", "local/path"),
        ("name", ""),
    ],
)
def test_a_remote_names_its_repository_whatever_shape_it_is_written_in(
    remote: str, slug: str
) -> None:
    assert slug_from_remote(remote) == slug


def test_a_query_names_the_repository_it_means(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named rather than inferred, because inference is what an alias defeats."""
    monkeypatch.setattr(utils, "repository_slug", lambda: "owner/name")

    assert utils.repository_arguments() == ["--repo", "owner/name"]


def test_a_checkout_with_no_readable_forge_names_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty slug is a project with no forge, not a broken flag to pass on."""
    monkeypatch.setattr(utils, "repository_slug", lambda: "")

    assert utils.repository_arguments() == []


def test_an_excluded_label_withholds_an_issue() -> None:
    row = IssueRow(
        number=1,
        url="https://example.test/1",
        title="Something",
        labels=[IssueLabel(name="resolver-skip")],
    )

    assert row.excluded_by("resolver-skip")
    assert not row.excluded_by("other")


def test_issue_positions_continue_past_the_notes_and_statements() -> None:
    # Appended rather than inserted: the indexes a planner already wrote are
    # persisted in run state, and a resumed run must read them as it meant them.
    evidence = request(notes=2, statements=1, issues=2)

    assert evidence.evidence_count() == 5

    cited = planned_evidence(evidence, [1, 2, 4])

    assert [note.line for note in cited.notes] == [2]
    assert cited.evidence == "statement 0"
    assert [item.number for item in cited.issues] == [2]


def test_a_run_can_be_planned_from_issues_alone() -> None:
    evidence = request(notes=0, statements=0, issues=1)

    assert evidence.evidence_count() == 1
    assert planned_evidence(evidence, [0]).issues == [issue(1)]


def test_evidence_of_no_kind_at_all_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one piece of evidence"):
        ResolveRequest(source=SourceSnapshot(branch="dev", commit="a" * 40))
