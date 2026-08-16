"""Replay journals: identity, divergence, what a divergence means, and the store."""

from pathlib import Path

import pytest

from lup.replay.journal import (
    CellOutcome,
    JournalCell,
    JournalStore,
    ReplayJournal,
    UnreadableJournalError,
)


def journal(*outcomes: bool, claims_determinism: bool = False) -> ReplayJournal:
    return ReplayJournal(
        id="j1",
        cells=[
            JournalCell(source=f"cell {index}", ok=ok)
            for index, ok in enumerate(outcomes)
        ],
        determinism_claimed=claims_determinism,
    )


def replayed(*outcomes: bool) -> list[CellOutcome]:
    return [CellOutcome(ok=ok) for ok in outcomes]


def test_a_faithful_replay_reports_no_divergence() -> None:
    report = journal(True, True).compare(replayed(True, True))

    assert report.reproduced
    assert report.cells_replayed == 2
    assert "no divergence" in report.finding


def test_a_cell_that_replayed_differently_is_named() -> None:
    report = journal(True, True, True).compare(replayed(True, False, True))

    assert not report.reproduced
    assert [item.index for item in report.divergences] == [1]
    assert report.divergences[0].recorded_ok
    assert not report.divergences[0].replayed_ok


def test_a_divergence_carries_the_note_that_belongs_to_its_own_cell() -> None:
    # Outcome and detail travel as one value, so the note cannot slide onto
    # the wrong cell however many cells replayed cleanly before it.
    report = journal(True, True).compare(
        [CellOutcome(ok=True, detail="fine"), CellOutcome(ok=False, detail="boom")]
    )

    assert [item.detail for item in report.divergences] == ["boom"]


def test_divergence_without_a_determinism_claim_is_the_finding() -> None:
    # The environment promised nothing, so a divergence is information about
    # what the result depended on — not a broken promise.
    report = journal(True).compare(replayed(False))

    assert "which is the finding" in report.finding
    assert "not reproducible" not in report.finding


def test_divergence_against_a_determinism_claim_is_a_defect() -> None:
    report = journal(True, claims_determinism=True).compare(replayed(False))

    assert "determinism was claimed" in report.finding
    assert "nothing built on it should be treated as established" in report.finding


def test_the_finding_survives_serialization() -> None:
    # The agent holding a tool result reads the same sentence a library
    # caller does, which only holds if the finding is dumped with the report.
    dumped = journal(True).compare(replayed(False)).model_dump(mode="json")

    assert "which is the finding" in dumped["finding"]


def test_a_replay_that_stopped_early_is_compared_as_far_as_it_went() -> None:
    report = journal(True, True, True).compare(replayed(True))

    assert report.cells_replayed == 1
    assert report.reproduced


def test_identity_covers_the_cells_and_not_the_envelope() -> None:
    # A journal forked from another records the same execution, so it has to
    # digest the same however its id and lineage differ.
    original = journal(True, False)
    forked = original.model_copy(update={"id": "j2", "parent": "j1"})

    assert forked.digest() == original.digest()


def test_a_different_execution_digests_differently() -> None:
    assert journal(True, True).digest() != journal(True, False).digest()


def test_recording_a_cell_leaves_the_journal_it_was_read_from_alone() -> None:
    original = journal(True)

    grown = original.recording(JournalCell(source="cell 1", ok=False))

    assert len(original.cells) == 1
    assert [cell.ok for cell in grown.cells] == [True, False]


def test_a_store_with_no_file_yet_reads_as_an_empty_journal(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "journal.json", "sandbox-1")

    assert store.load().cells == []
    assert store.load().id == "sandbox-1"


def test_a_store_carries_its_environment_claim_into_what_it_records(
    tmp_path: Path,
) -> None:
    store = JournalStore(tmp_path / "journal.json", "lab-1", determinism_claimed=True)

    recorded = store.record(JournalCell(source="1 + 1", ok=True))

    assert recorded.determinism_claimed
    assert store.load().determinism_claimed


def test_recorded_cells_accumulate_in_order(tmp_path: Path) -> None:
    store = JournalStore(tmp_path / "journal.json", "sandbox-1")

    store.record(JournalCell(source="first", ok=True))
    store.record(JournalCell(kind="install", source='["numpy"]', ok=False))

    assert [(cell.kind, cell.source) for cell in store.load().cells] == [
        ("code", "first"),
        ("install", '["numpy"]'),
    ]


def test_an_unreadable_journal_raises_rather_than_replaying_as_empty(
    tmp_path: Path,
) -> None:
    # Starting fresh over it would let a replay of nothing report itself as a
    # replay with no divergence — a clean bill of health from an unread record.
    path = tmp_path / "journal.json"
    path.write_text("{not json", encoding="utf-8")
    store = JournalStore(path, "sandbox-1")

    with pytest.raises(UnreadableJournalError, match="does not parse"):
        store.load()
