"""What the report surface accounts for, and what it refuses to double-count.

`lup.devtools.report` answers "what is left to implement" by composing the
surfaces that already answer one topic each. These pin the parts that are its
own judgement rather than a surface's: which scanned notes this tree can act
on, and how a topic reads when nothing is outstanding. The skill's prose is
generated from the same declaration, so the roster is pinned here too.
"""

from pathlib import Path

from lup.adapters.harness import claude_prompt_renderer
from lup.codescan.markers import NoteKind
from lup.devtools.dev.comments import FoundComment
from lup.devtools.harness.content.skills.report import SKILL
from lup.devtools.report.build import note_items
from lup.devtools.report.models import (
    DEFAULT_REPORT_PATH,
    NOTES,
    REPORT_TOPICS,
    Report,
    ReportItem,
    ReportPart,
    topic_bullets,
)
from lup.harness.ownership import GeneratedArtifacts, OwnedArtifact


def found(file: str, text: str, kind: str = "note") -> FoundComment:
    """One scanned note, as the marker scan hands it to the report."""
    return FoundComment.model_validate(
        {
            "file": file,
            "context": text,
            "start_line": 1,
            "end_line": 2,
            "read_start": 1,
            "read_end": 3,
            "text": text,
            "kind": kind,
        }
    )


def owning(*paths: str) -> GeneratedArtifacts:
    """An ownership answer naming exactly ``paths`` as generated."""
    return GeneratedArtifacts(
        by_path={
            path: OwnedArtifact(
                path=Path(path),
                category="generated",
                sha256="0" * 64,
                semantic_id=path,
            )
            for path in paths
        }
    )


def test_a_note_copied_into_a_generated_tree_is_not_counted_twice() -> None:
    """The same note in source and in its artifact is one outstanding thing.

    A note inside a generated tree was written against the generator's source
    and copied there when the harness materialized. Counting both tells a
    reader there is more left than there is, and points half the work at a
    file where resolving it would be overwritten by the next generation.
    """
    scanned = [
        found("packages/lup/src/lup/policy/kernel/edit.py", "make the gate refuse"),
        found(".claude/plugins/lup/hooks/runtime/kernel/edit.py", "make the gate"),
    ]

    items = note_items(
        scanned,
        NoteKind.note,
        owning(".claude/plugins/lup/hooks/runtime/kernel/edit.py"),
    )

    assert [item.where for item in items] == [
        "packages/lup/src/lup/policy/kernel/edit.py:1-2"
    ]


def test_each_flavour_of_note_answers_only_its_own_topic() -> None:
    """One scan is split by kind, so a deferral is not reported as a request."""
    scanned = [
        found("a.py", "asking for something"),
        found("b.py", "parked", kind=NoteKind.defer),
        found("c.py", "claimed", kind=NoteKind.solved),
    ]
    nothing_owned = owning()

    assert len(note_items(scanned, NoteKind.note, nothing_owned)) == 1
    assert len(note_items(scanned, NoteKind.defer, nothing_owned)) == 1
    assert len(note_items(scanned, NoteKind.solved, nothing_owned)) == 1


def test_an_empty_topic_still_prints_its_section() -> None:
    """ "Nothing outstanding" is an answer; a vanished section is not.

    A topic that disappeared when it found nothing reads as one nobody looked
    at, which is the reading that lets a stale report pass for a clean tree.
    """
    rendered = ReportPart(topic=NOTES, items=[]).markdown()

    assert NOTES.title in rendered
    assert "Nothing outstanding." in rendered


def test_a_topic_counts_what_it_found_in_its_own_heading() -> None:
    part = ReportPart(
        topic=NOTES, items=[ReportItem(where="a.py:1", what="do the thing")]
    )

    rendered = part.markdown()

    assert f"## {NOTES.title} (1)" in rendered
    assert "- `a.py:1` — do the thing\n" in rendered


def test_an_item_carrying_a_gate_says_so() -> None:
    """A deferral's gate is what tells a reader it is parked rather than open."""
    gated = ReportItem(where="a.py:1", what="parked", gate="until the v2 API ships")

    assert gated.line() == "`a.py:1` [until the v2 API ships] — parked"
    assert ReportItem(where="a.py:1", what="open").line() == "`a.py:1` — open"


def test_a_report_totals_every_topic_it_holds() -> None:
    report = Report(
        parts=[
            ReportPart(topic=topic, items=[ReportItem(where="a.py:1", what="x")])
            for topic in REPORT_TOPICS
        ]
    )

    assert report.outstanding() == len(REPORT_TOPICS)
    assert f"{len(REPORT_TOPICS)} outstanding item(s)" in report.markdown()
    for topic in REPORT_TOPICS:
        assert f"## {topic.title}" in report.markdown()


def test_the_skill_recites_the_roster_the_command_fills_in() -> None:
    """The skill's own prose carries these bullets, not a second roster.

    Asserting over ``topic_bullets`` alone would hold just as well against a
    skill that hard-coded a roster of its own and drifted, which is the one
    failure this is here to catch. So the skill's declaration is what gets
    read: a topic added here reaches that prose by regeneration, and a skill
    that stopped interpolating stops passing.
    """
    prose = claude_prompt_renderer().render(SKILL.prompt)

    assert topic_bullets() in prose
    for topic in REPORT_TOPICS:
        assert f"- **{topic.title}** — {topic.guidance}\n" in prose
    assert str(DEFAULT_REPORT_PATH) in prose


def test_a_written_report_lands_in_scratch() -> None:
    """Scratch is the whole reason writing one is not a tracking file."""
    assert DEFAULT_REPORT_PATH.parts[0] == "tmp"
    assert not DEFAULT_REPORT_PATH.is_absolute()
