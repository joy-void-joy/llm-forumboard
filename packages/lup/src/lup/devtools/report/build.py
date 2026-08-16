"""Where a report's items come from: the surfaces that already compute them.

Nothing here scans anything itself. Each topic is answered by the command
that owns it — the marker scan behind `dev comments`, the drift inspection
behind `harness check`, the containment walk behind `dev branches`, the run
state behind `harness resolve supervise` — and this composes their answers
into one shape. A second scanner beside those would be a second answer to
compare against theirs, which is the reason the surface composes rather than
replaces.
"""

from pathlib import Path

from lup.codescan.markers import NoteKind, find_feedback
from lup.devtools.dev.branches import unlanded_siblings
from lup.devtools.dev.comments import FoundComment, scan_tracked
from lup.devtools.harness.drift import RepositoryWriter, inspect_drift
from lup.devtools.harness.generate import NativeHarnessComposition
from lup.harness.ownership import GeneratedArtifacts, generated_artifacts
from lup.devtools.report.models import (
    CLAIMS,
    DEFERRALS,
    DRIFT,
    LEASES,
    NOTES,
    UNLANDED,
    Report,
    ReportItem,
    ReportPart,
)
from lup.resolver.state import live_lease_branches


def note_items(
    found: list[FoundComment], kind: NoteKind, owned: GeneratedArtifacts
) -> list[ReportItem]:
    """Every scanned note of one flavour this tree can act on.

    A note inside a generated artifact was written against the generator's
    source and copied here when the harness materialized, so it is the same
    note twice and only one of the two can be resolved. Reporting both would
    tell a reader there is more outstanding than there is.
    """
    return [
        ReportItem(
            where=f"{comment.file}:{comment.start_line}-{comment.end_line}",
            what=comment.text,
            gate=comment.condition or "",
        )
        for comment in found
        if comment.kind == kind and owned.owning(comment.file) is None
    ]


def drift_items(
    compositions: list[NativeHarnessComposition], writers: list[RepositoryWriter]
) -> list[ReportItem]:
    """Every generated tree the typed source has moved out from under.

    One reading answers both halves. A verdict names each stale artifact
    outside the native trees rather than only whether any is, so a reader is
    told which file is behind instead of being sent to look for it.
    """
    verdict = inspect_drift(compositions, writers)
    return [
        *[
            ReportItem(
                where=report.target,
                what="generated tree is stale — `lup-devtools harness generate all`",
            )
            for report in verdict.stale_trees
        ],
        *[
            ReportItem(
                where="repository artifacts",
                what=f"{message} — `lup-devtools harness generate all`",
            )
            for message in verdict.stale_repository
        ],
    ]


def unlanded_items() -> list[ReportItem]:
    """Every sibling branch holding work the integration branch lacks."""
    return [
        ReportItem(
            where=branch.name,
            what=(
                f"{branch.unique_commits} commit(s), "
                f"{branch.source_diff_lines} ln unlanded"
            ),
            gate=branch.worktree or "",
        )
        for branch in unlanded_siblings()
    ]


def lease_items(state_root: Path) -> list[ReportItem]:
    """Every concern a resolver run is holding a branch for."""
    return [
        ReportItem(where=held.branch, what=held.standing, gate=f"run {held.run_id}")
        for held in live_lease_branches(state_root).values()
    ]


def build_report(
    compositions: list[NativeHarnessComposition],
    repository_writers: list[RepositoryWriter],
    state_root: Path,
    root: Path,
) -> Report:
    """Ask every surface what it still has outstanding, in reading order.

    The notes scan runs once and is split three ways rather than three times,
    because walking every tracked file is what it costs and the flavour of a
    note is a field on what came back.
    """
    found = scan_tracked(find_feedback)
    owned = generated_artifacts(root)
    return Report(
        parts=[
            ReportPart(topic=NOTES, items=note_items(found, NoteKind.note, owned)),
            ReportPart(topic=DEFERRALS, items=note_items(found, NoteKind.defer, owned)),
            ReportPart(topic=CLAIMS, items=note_items(found, NoteKind.solved, owned)),
            ReportPart(
                topic=DRIFT, items=drift_items(compositions, repository_writers)
            ),
            ReportPart(topic=UNLANDED, items=unlanded_items()),
            ReportPart(topic=LEASES, items=lease_items(state_root)),
        ]
    )
