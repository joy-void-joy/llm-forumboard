"""One ordered record of everything every actor in a run did.

A run's actors each hold their own session, and until now nothing outside
those sessions could see what happened inside one. The journal is that
record: one append-only file per run, one writer, every entry naming the
actor it belongs to.

Ordering needs no coordination. A run holds its state lock for its entire
life, so there is exactly one writer and the sequence number is simply the
count of what came before.

The per-actor view and the merged view are the same sequence, filtered or
not. That is deliberate — a merged view assembled from separate per-actor
logs would have to invent an ordering between them, and knowing what
actually happened first is the one thing a reader wants from a merged view.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter

from lup.channels.models import utc_now
from lup.channels.stream import Stream
from lup.resolver.models import (
    ActorRef,
    CarriedParent,
    ConcernProgress,
    MaterialQuestion,
    QuestionAnswer,
    ResolvePhase,
)
from lup.runtime.models import TurnEvent

# lup: ignore[constant-declaration] — the journal's own file name, which the
# writing run and every reader of it must spell alike to meet at all
JOURNAL_FILE = "journal.jsonl"


class PhaseChangedEvent(BaseModel, frozen=True):
    """The run moved to a new phase."""

    type: Literal["phase_changed"] = "phase_changed"
    phase: ResolvePhase


class ConcernProgressedEvent(BaseModel, frozen=True):
    """One concern reached a new status."""

    type: Literal["concern_progressed"] = "concern_progressed"
    progress: ConcernProgress


class QuestionAskedEvent(BaseModel, frozen=True):
    """An actor put a material question to the humans."""

    type: Literal["question_asked"] = "question_asked"
    question: MaterialQuestion
    asked_by: str


class AnswerSettledEvent(BaseModel, frozen=True):
    """A question took its answer, from whichever door supplied it."""

    type: Literal["answer_settled"] = "answer_settled"
    answer: QuestionAnswer
    door: str


class MessagePostedEvent(BaseModel, frozen=True):
    """A door volunteered something to an actor, or an actor replied.

    An intervention belongs in the record beside what it interrupted. A
    reader scrolling one actor's trace sees the moment someone redirected
    it, in order, against what it was doing — which is the difference
    between a trace and an audit filed somewhere else.
    """

    type: Literal["message_posted"] = "message_posted"
    text: str
    door: str
    in_reply_to: str | None = None
    redirect: bool = False


class MessageOutstandingEvent(BaseModel, frozen=True):
    """A message still queued for an actor whose session is being closed.

    Recorded because the sender was told the message was sent, and the
    stream alone cannot say whether anyone read it. On a park this is a
    message that will land at the head of the resumed turn; on a run that
    ended it is one that reached nobody, and a redirect nobody read is the
    failure of an operation somebody performed to stop something.
    """

    type: Literal["message_outstanding"] = "message_outstanding"
    text: str
    door: str
    redirect: bool = False


class JoinCompletedEvent(BaseModel, frozen=True):
    """One parent was joined, and whether git had to be adjudicated for it."""

    type: Literal["join_completed"] = "join_completed"
    parent: str
    commit: str
    conflicted: bool
    broke: list[str] = []


class JoinPlannedEvent(BaseModel, frozen=True):
    """Which parents have to be merged, and which already ride inside one.

    Concerns are cut from their dependencies' commits, so the branches stack:
    a parent contained in another is already in the tree once that one lands,
    and merging it costs a verification and can cost a merger turn to
    conclude that nothing happened. Named here rather than left to the skip
    inside the loop, so what the run declined to merge is on the record.
    """

    type: Literal["join_planned"] = "join_planned"
    tips: list[str]
    carried: list["CarriedParent"] = []


class JoinRenderedEvent(BaseModel, frozen=True):
    """A join disagreed only in artifacts, and the generator settled it.

    Worth recording because it is the difference between a join that cost a
    merger turn and one that cost a second: every lease touching a catalog
    re-renders both plugin trees, so a conflict there says nothing about the
    work and everything about when each branch last generated.
    """

    type: Literal["join_rendered"] = "join_rendered"
    parent: str


class RecheckReusedEvent(BaseModel, frozen=True):
    """A resume took these concerns' re-checks from the pass that ran them."""

    type: Literal["recheck_reused"] = "recheck_reused"
    concerns: list[str]
    commit: str


class JoinAuditEvent(BaseModel, frozen=True):
    """The finished tree was re-checked against every parent that built it."""

    type: Literal["join_audit"] = "join_audit"
    parents: list[str]
    outstanding: int
    commit: str


class ReviewResidualEvent(BaseModel, frozen=True):
    """Observations an accepting review recorded beside its verdict.

    A residual on a rejected review re-enters the worker's feedback; on an
    accepted one it previously reached nobody, and real findings sat unread
    in the round records. The journal is where they surface — for the CLI,
    the supervisor, and whoever files the follow-up work.
    """

    type: Literal["review_residual"] = "review_residual"
    concern_id: str
    round: int
    residual: list[str]


class CriteriaCarriedEvent(BaseModel, frozen=True):
    """A human took an acceptance over criteria the reviewer left unmet.

    The bar is theirs, so waiving part of it is theirs too, and the waiver
    is worth more than the verdict it produced: whoever lands this concern
    inherits work that does not meet everything it was admitted against,
    and only this says which parts and on whose word.
    """

    type: Literal["criteria_carried"] = "criteria_carried"
    concern_id: str
    round: int
    criteria: list[str]


class ForeignCriteriaEvent(BaseModel, frozen=True):
    """An accepted review credited ids the concern never declared.

    Recorded rather than acted on. Every declared criterion was accounted
    for, so nothing passed unchecked and the verdict stands; the stray label
    is the reviewer's bookkeeping, and turning an acceptance back over it
    spent a revision round re-deriving the same verdict until the budget
    ran out. Journalled so a reviewer that keeps miscrediting is still
    visible to whoever reads the run.
    """

    type: Literal["foreign_criteria"] = "foreign_criteria"
    concern_id: str
    round: int
    labels: list[str]


class VerificationFailedEvent(BaseModel, frozen=True):
    """What one gate saw at the moment it decided a concern's round.

    A run's record held no verification event of any kind: the check ran,
    produced a verdict that decided a concern's fate, and left nothing
    behind. Journalling it puts what the gate saw beside the turns it
    decided about, which is the one place a later session can read it — the
    lease worktree the check ran in is usually still held by the run.
    """

    type: Literal["verification_failed"] = "verification_failed"
    concern_id: str
    round: int
    name: str
    exit_code: int
    output: str


class RecheckRepeatedEvent(BaseModel, frozen=True):
    """A re-check reproduced a standing finding already put to the humans.

    The same lost-criteria set for the same concern asks once; a later join
    that reproduces it is recorded here instead of re-raising an identical
    question per join.
    """

    type: Literal["recheck_repeated"] = "recheck_repeated"
    concern_id: str
    occasion: str
    criteria: list[str]


class BaseRefreshedEvent(BaseModel, frozen=True):
    """A lease made from here starts from the branch as it stands now.

    A run pinned to the commit it was created at cannot see a fix made to
    unblock it, and its workers reason about code that has already been
    replaced — reaching careful conclusions that contradict decisions the
    repository has already taken. Recorded whether it moved or not: a
    refresh that could not be made cleanly is the reason the leases beside
    it are still where they were.
    """

    type: Literal["base_refreshed"] = "base_refreshed"
    branch: str
    was: str
    commit: str
    conflicts: list[str] = []
    reason: str = ""


class LeaseRefreshedEvent(BaseModel, frozen=True):
    """What bringing the refreshed base into one lease did, or what stopped it.

    The base event above says where the run now starts; it says nothing
    about which branches actually took it. That answer reached stdout once
    and was never written down, so a detached run — whose output goes to a
    file nobody is watching — left no record that three of its leases were
    refused. A lease still on the old base is the reason its worker reads
    replaced code, which is the failure the refresh exists to prevent.
    """

    type: Literal["lease_refreshed"] = "lease_refreshed"
    concern_id: str
    commit: str
    applied: bool
    conflicts: list[str] = []
    uncommitted: list[str] = []
    reason: str = ""


class LeaseDriftEvent(BaseModel, frozen=True):
    """An abandoned concern's tree does not hold the commit last recorded.

    Recorded at restore rather than raised. The concern failed, so nothing
    in this run reads that tree again — but the work is still on its branch,
    and a reader salvaging it wants both commits named.
    """

    type: Literal["lease_drift"] = "lease_drift"
    concern_id: str
    expected: str
    found: str


class RunFailedEvent(BaseModel, frozen=True):
    """The run reached a terminal failure."""

    type: Literal["run_failed"] = "run_failed"
    reason: str


type RunEvent = (
    PhaseChangedEvent
    | ConcernProgressedEvent
    | QuestionAskedEvent
    | AnswerSettledEvent
    | MessagePostedEvent
    | MessageOutstandingEvent
    | JoinPlannedEvent
    | JoinRenderedEvent
    | JoinCompletedEvent
    | JoinAuditEvent
    | RecheckReusedEvent
    | ReviewResidualEvent
    | CriteriaCarriedEvent
    | ForeignCriteriaEvent
    | VerificationFailedEvent
    | RecheckRepeatedEvent
    | BaseRefreshedEvent
    | LeaseRefreshedEvent
    | LeaseDriftEvent
    | RunFailedEvent
)
"""What the run did, as opposed to what one actor's session did.

These share the sequence with turn events rather than living in a file of
their own, because the one thing a reader wants from a merged view is to
know what actually happened first — and an ordering invented between two
logs cannot answer that.
"""


class JournalEntry(BaseModel, frozen=True):
    """One event, stamped and attributed."""

    seq: int
    at: datetime
    actor: ActorRef
    event: TurnEvent | RunEvent


ENTRY_ADAPTER: TypeAdapter[JournalEntry] = TypeAdapter(JournalEntry)


class JournalTail(BaseModel, frozen=True):
    """What one follower read, and where it should resume."""

    entries: list[JournalEntry]
    offset: int


class Journal:
    """The run's single ordered record, appended by one writer."""

    def __init__(self, root: Path) -> None:
        self.stream: Stream[JournalEntry] = Stream(root / JOURNAL_FILE, ENTRY_ADAPTER)
        self.next_seq = len(self.stream.read_all())
        self.run = ActorRef(kind="run", id=root.name)

    def append(self, actor: ActorRef, event: TurnEvent | RunEvent) -> JournalEntry:
        """Record one event.

        Appending reaches no await, so concurrent actors draining their own
        sessions into the same journal interleave between entries and never
        within one. That is what keeps the single-writer claim true once
        every actor holds a live session instead of taking turns.
        """
        entry = JournalEntry(seq=self.next_seq, at=utc_now(), actor=actor, event=event)
        self.stream.append(entry)
        self.next_seq += 1
        return entry

    def record(self, event: RunEvent) -> JournalEntry:
        """Record something the run did rather than something an actor did."""
        return self.append(self.run, event)

    def read(self, after_seq: int = -1) -> list[JournalEntry]:
        """Every entry after ``after_seq``, which is what an SSE resume wants.

        Reconnecting replays from ``seq + 1`` rather than from zero, so a
        page open all run does not re-render the whole run whenever its
        connection blinks.
        """
        return [entry for entry in self.stream.read_all() if entry.seq > after_seq]

    def tail(self, offset: int) -> JournalTail:
        """Whatever arrived after ``offset``, and where to resume.

        A follower reads by byte offset rather than by sequence number so
        that watching a run costs the size of what is new. Filtering by
        ``seq`` re-parses the whole file on every poll, which turns a long
        run into quadratic work exactly as it becomes interesting.
        """
        found = self.stream.read_from(offset)
        if not found:
            return JournalTail(entries=[], offset=offset)
        return JournalTail(
            entries=[pair.item for pair in found], offset=found[-1].commit_offset
        )

    def entry(self, seq: int) -> JournalEntry | None:
        """One entry whole, for a reader expanding a truncated block."""
        return next((item for item in self.stream.read_all() if item.seq == seq), None)

    def before(self, seq: int, count: int) -> list[JournalEntry]:
        """The ``count`` entries just before ``seq``, oldest first.

        A fresh follower starts from a bounded tail of the record, so the
        run older than that tail is reached from here one page at a time —
        on demand, rather than by replaying the whole run into the reader
        the bound exists to protect.
        """
        if count <= 0:
            return []
        earlier = [entry for entry in self.stream.read_all() if entry.seq < seq]
        return earlier[-count:]

    def actors(self) -> list[ActorRef]:
        """Every actor that has produced an entry, in first-seen order."""
        return list(dict.fromkeys(entry.actor for entry in self.stream.read_all()))

    def for_actor(self, actor: ActorRef, after_seq: int = -1) -> list[JournalEntry]:
        """One actor's slice of the record, which is its whole trace."""
        return [entry for entry in self.read(after_seq) if entry.actor == actor]


async def record_turn(
    journal: Journal, actor: ActorRef, events: AsyncIterator[TurnEvent]
) -> None:
    """Drain one turn's durable events into the journal as they arrive.

    Taking the durable view rather than the live one keeps the journal a
    record of what happened rather than of what was being typed.
    """
    async for event in events:
        journal.append(actor, event)


def journal_tail(root: Path) -> JournalEntry | None:
    """A run's most recent entry, without paying for its whole journal.

    `Journal` counts every record on construction to know its next
    sequence, which a writer needs and a reader does not. A status view
    asks this each time it runs against a file that reaches tens of
    megabytes in one run, so it reads the stream directly.
    """
    return Stream(root / JOURNAL_FILE, ENTRY_ADAPTER).last()
