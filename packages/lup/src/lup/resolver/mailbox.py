# lup: ignore[constant-declaration]
# The constants here name the mailbox's own on-disk layout, which a writer and
# a reader in different processes must agree on to find each other's files at
# all — an identity of this format rather than a choice a caller can make.
"""The persisted question mailbox every answer door writes through.

A question is a :class:`~lup.channels.slot.Slot`: declared once by whoever
asks, offered to by any door, and settled exactly once. Messages ride a
:class:`~lup.channels.stream.Stream` instead, and that split is the point.
A run parks while any *slot* it needs is unsettled; nothing parks on a
stream. So volunteering information to a worker cannot stall the run, which
it could when both rode the same structure.

Doors write ``offered``, which is correctable — a mistyped free-text answer
can be replaced right up until it counts, and an offer may arrive before its
question exists, which is what lets a flag answer a question the run has not
asked yet. Exactly one writer promotes offers into ``settled``, taking the
earliest valid one, so "first answer wins" is a deterministic decision
rather than a race between whoever reached the filesystem first.
"""

import asyncio
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from lup.channels.models import (
    ChannelConflictError,
    ChannelCorruptionError,
    Door,
    DoorPolicy,
    utc_now,
)
from lup.channels.cursor import StreamCursors
from lup.channels.slot import Slot, SlotSet
from lup.channels.stream import Stream
from lup.channels.wait import POLL_SECONDS, wait_until
from lup.resolver.models import (
    ActorRef,
    MaterialQuestion,
    QuestionAnswer,
)

QUESTION_DIR = "questions"
MESSAGE_FILE = "messages.jsonl"
DELIVERY_DIR = "delivery"
PARK_DIR = "park"
DRAIN_DIR = "drain"
RESUME_DIR = "resume"
ANSWER_POLL_SECONDS = POLL_SECONDS

# The resolver's own names for the channel package's shapes, so a caller
# reads one vocabulary rather than two.
MailboxConflictError = ChannelConflictError
MailboxCorruptionError = ChannelCorruptionError
AnswerDoor = Door


class PendingQuestion(BaseModel, frozen=True):
    """One question a run is waiting on, written once by whoever asked."""

    run_id: str
    question: MaterialQuestion
    asked_by: str
    asked_at: datetime


class AnswerOffer(BaseModel, frozen=True):
    """One door's proposed answer, correctable until it is promoted."""

    run_id: str
    question_id: str
    value: str
    door: AnswerDoor
    offered_at: datetime


class RecordedAnswer(BaseModel, frozen=True):
    """The promoted answer to one question. Written once, never revised."""

    run_id: str
    answer: QuestionAnswer
    door: AnswerDoor
    answered_at: datetime


class ParkRequest(BaseModel, frozen=True):
    """A door asking every open wait in this run to give up now."""

    run_id: str
    reason: str


class ActorMessage(BaseModel, frozen=True):
    """One thing a door told an actor. This never settles anything.

    A message is not a question, and the type is where that is enforced: a
    stream has no unsettled state for a run to wait on, so no amount of
    messaging can park anything.

    ``redirect`` separates telling an actor something from stopping it. An
    ordinary message rides in front of the actor's next tool call and it
    keeps going; a redirect refuses that call and hands back the text as the
    reason, so the actor cannot carry on with what it was doing without
    first reading why it was stopped. Both are the same record on the same
    stream, because an intervention belongs in order beside what it
    interrupted.
    """

    run_id: str
    to_actor: str
    text: str
    door: AnswerDoor
    sent_at: datetime
    in_reply_to: str = ""
    redirect: bool = False


class ActorDelivery(BaseModel, frozen=True):
    """What one actor has waiting, and the position that consumes exactly it.

    The position is carried rather than taken again at the moment of
    delivery, because a message posted between reading and handing over
    would otherwise be committed past without ever being read.
    """

    messages: list[ActorMessage]
    through: int

    def redirects(self) -> list[ActorMessage]:
        return [message for message in self.messages if message.redirect]


class MailboxWait(BaseModel, frozen=True):
    """How one wait ended. ``reason`` is empty only on a complete answer."""

    answered: list[RecordedAnswer]
    unanswered: list[str]
    reason: str = ""


type MailboxRecord = PendingQuestion | AnswerOffer | RecordedAnswer | ParkRequest


class MailboxSlotRecord(BaseModel, frozen=True):
    """One question slot's payload at whichever of its three states it holds.

    A slot stores one model, and a question's declaration, its correctable
    offer, and its settled answer are three different shapes. Carrying all
    three optionally keeps the slot generic over a single type without
    collapsing what the three of them mean.
    """

    pending: PendingQuestion | None = None
    offer: AnswerOffer | None = None
    answer: RecordedAnswer | None = None


class QuestionMailbox:
    """File-backed question, answer, and message exchange for one run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.slots: SlotSet[MailboxSlotRecord] = SlotSet(
            root / QUESTION_DIR, MailboxSlotRecord
        )
        self.stream: Stream[ActorMessage] = Stream(
            root / MESSAGE_FILE, TypeAdapter(ActorMessage)
        )
        self.cursors = StreamCursors(root / DELIVERY_DIR)
        self.park_slot: Slot[ParkRequest] = Slot(root / PARK_DIR, ParkRequest)
        # Two verbs, because they are two requests. Park ends every open
        # *wait*, which reaches a run sitting on an answer and no other; a
        # worker inside a model turn is not waiting on anything, so a busy
        # run was unaffected by it and killing was the only way to stop one.
        # Draining ends the *work*, at the next boundary where stopping
        # costs nothing. One verb meaning both would surprise whoever
        # wanted the first.
        self.drain_slot: Slot[ParkRequest] = Slot(root / DRAIN_DIR, ParkRequest)
        # Pause and resume are asymmetric on purpose. Pausing is a directive
        # any door may issue; resuming is a decision, and excluding AGENT
        # makes it one the orchestrator physically cannot take for itself.
        self.resume_slot: Slot[ParkRequest] = Slot(
            root / RESUME_DIR, ParkRequest, DoorPolicy(excluded=[Door.AGENT])
        )

    def queue(self, pending: PendingQuestion) -> None:
        """Record a question once; re-asking it re-renders what has moved.

        Identical is a no-op, so the first asking keeps its timestamp. A
        restatement takes, because the facts a gate quotes go stale while
        the run is parked on it. A moved answer domain is refused, which is
        the case this guard was built for: an actor redefining one id.
        """
        slot = self.slots.slot(pending.question.id)
        existing = slot.declared()
        if existing is not None and existing.pending is not None:
            if not pending.question.restates(existing.pending.question):
                raise MailboxConflictError(
                    f"question {pending.question.id!r} is already asked differently"
                )
            if pending.question != existing.pending.question:
                slot.redeclare(MailboxSlotRecord(pending=pending))
            return
        slot.declare(MailboxSlotRecord(pending=pending))

    def settled_answer(self, question_id: str) -> RecordedAnswer | None:
        """The promoted answer to one question, where a promoter took one."""
        record = self.slots.slot(question_id).settled()
        return None if record is None else record.answer

    def offer(self, offer: AnswerOffer) -> None:
        """Propose an answer, replacing any earlier proposal for that question.

        A promoted answer is never revised, so an offer reaching a settled
        question cannot take. Re-offering the value that settled is how a
        rerun recipe resumes, and passes as the no-op it already is. A
        *different* value is a correction, and recording one silently left
        the run advancing under exactly the value its author meant to
        replace — a concern the human had rejected was leased for work. It
        is refused at the one point every door writes through, so no door
        can be the one that still does this quietly.
        """
        settled = self.settled_answer(offer.question_id)
        if settled is None:
            self.slots.slot(offer.question_id).offer(
                MailboxSlotRecord(offer=offer), offer.door
            )
            return
        if settled.answer.value != offer.value:
            raise MailboxConflictError(
                f"question {offer.question_id!r} is already settled as "
                f"{settled.answer.value!r}, a promoted answer is not revisable, "
                f"so the offered {offer.value!r} would not take"
            )

    def record(self, answer: RecordedAnswer) -> bool:
        """Promote one answer, or report that another door already won."""
        return self.slots.slot(answer.answer.question_id).settle(
            MailboxSlotRecord(answer=answer), answer.door
        )

    def questions(self) -> list[PendingQuestion]:
        return [
            record.pending
            for record in self.slots.declared()
            if record.pending is not None
        ]

    def offers(self) -> list[AnswerOffer]:
        return [
            record.offer for record in self.slots.offered() if record.offer is not None
        ]

    def answers(self) -> list[RecordedAnswer]:
        return [
            record.answer
            for record in self.slots.settled()
            if record.answer is not None
        ]

    def answered_ids(self) -> list[str]:
        return [record.answer.question_id for record in self.answers()]

    def send(self, message: ActorMessage) -> None:
        """Tell an actor something. This never settles and never parks a run."""
        self.stream.append(message)

    def waiting(self, actor: ActorRef) -> ActorDelivery:
        """Everything queued for one actor, consuming none of it.

        From the position that actor was last *delivered* to, which is a
        file rather than a number some session happens to hold. Starting
        each session at the stream head instead meant a message posted while
        the previous turn was in flight was skipped rather than queued: the
        window a turn opened began after it, in every round, so it reached
        nobody ever. Reading is separated from consuming so that asking what
        an actor has waiting — which is how a sender learns whether anything
        was read — cannot itself be what makes it disappear.
        """
        position = self.cursors.offset(actor.conversation())
        found = self.stream.read_from(position)
        reaching = actor.addresses()
        return ActorDelivery(
            messages=[pair.item for pair in found if pair.item.to_actor in reaching],
            through=found[-1].commit_offset if found else position,
        )

    def delivered(self, actor: ActorRef, through: int) -> None:
        """Record that one actor has been handed everything through ``through``.

        The whole region rather than the last matching message, because a
        reader filtering by actor must still skip past the ones addressed
        elsewhere or it re-reads them on every turn.
        """
        self.cursors.commit(actor.conversation(), through)

    def park(self, request: ParkRequest) -> None:
        self.park_slot.clear()
        self.park_slot.settle(request)

    def parked(self) -> ParkRequest | None:
        return self.park_slot.settled()

    def clear_park(self) -> None:
        """Drop a stale park marker so a resumed run can wait again."""
        self.park_slot.clear()

    def drain(self, request: ParkRequest) -> None:
        """Ask this run to stop taking new work at its next safe boundary."""
        self.drain_slot.clear()
        self.drain_slot.settle(request)

    def draining(self) -> ParkRequest | None:
        return self.drain_slot.settled()

    def clear_drain(self) -> None:
        """Drop a satisfied drain marker so a resumed run works again."""
        self.drain_slot.clear()

    def request_resume(self, request: ParkRequest, door: AnswerDoor) -> bool:
        """Release a paused run. Refused for ``AGENT`` by the slot's own policy."""
        return self.resume_slot.settle(request, door)

    def resumed(self) -> ParkRequest | None:
        return self.resume_slot.settled()


async def wait_for_answers(
    mailbox: QuestionMailbox,
    question_ids: list[str],
    *,
    wait_seconds: float,
    poll_interval_seconds: float = ANSWER_POLL_SECONDS,
    wake: asyncio.Event | None = None,
) -> MailboxWait:
    """Wait for every named answer, a park request, or the deadline.

    The tick bounds how late an out-of-process answer can be noticed; the
    optional event lets an in-process promoter deliver one with no delay at
    all. Both surfaces observe the same promotion, so there is one mechanism
    rather than a fast path and a slow path that can disagree.
    """
    wanted = list(dict.fromkeys(question_ids))

    def recorded() -> dict[str, RecordedAnswer]:
        return {record.answer.question_id: record for record in mailbox.answers()}

    def resolved() -> MailboxWait | None:
        found = recorded()
        answered = [found[name] for name in wanted if name in found]
        outstanding = [name for name in wanted if name not in found]
        if not outstanding:
            return MailboxWait(answered=answered, unanswered=[])
        request = mailbox.parked()
        if request is not None:
            return MailboxWait(
                answered=answered, unanswered=outstanding, reason=request.reason
            )
        return None

    settled = await wait_until(
        resolved,
        wait_seconds=wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        wake=wake,
    )
    if settled is not None:
        return settled
    found = recorded()
    return MailboxWait(
        answered=[found[name] for name in wanted if name in found],
        unanswered=[name for name in wanted if name not in found],
        reason="no answer arrived before the wait elapsed",
    )


def new_message(
    run_id: str,
    to_actor: str,
    text: str,
    door: AnswerDoor,
    in_reply_to: str = "",
    redirect: bool = False,
) -> ActorMessage:
    """Build a message stamped now, so callers do not each reach for a clock."""
    return ActorMessage(
        run_id=run_id,
        to_actor=to_actor,
        text=text,
        door=door,
        sent_at=utc_now(),
        in_reply_to=in_reply_to,
        redirect=redirect,
    )
