"""One durable session per actor, opened once and kept while the run moves.

Every resolver turn used to go through :func:`lup.runtime.query.query`, which
opens a session, takes one turn and closes it. Nine separate symptoms sat
downstream of that one fact: a park discarded the whole turn, a reviewer
re-read its concern cold each round, a merger never saw the parent it joined
last, and the same question was answered four times because each turn
re-derived it under an id no recorded answer matched.

An actor here is addressed rather than constructed per turn. It holds its
session across every turn it takes, drains what it does into the journal as
it happens, and is reattached after a park from its persisted identity. The
multi-turn shape is not new — :class:`lup.runtime.background.BackgroundAgent`
already holds one session open across many turns — the resolver simply
reached for the one-shot convenience instead.

``query()`` stays in the library. It is the legitimate one-shot convenience
and ``examples/one_shot.py`` uses it; what changed is that no resolver turn
calls it.
"""

import asyncio
import hashlib
import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from lup.channels.models import publish_atomic
from lup.hooks import (
    LupHookInput,
    LupHookMatcher,
    LupHookOutput,
    LupHooksConfig,
)
from lup.resolver.journal import (
    Journal,
    MessageOutstandingEvent,
    MessagePostedEvent,
    record_turn,
)
from lup.resolver.mailbox import ActorDelivery, ActorMessage, QuestionMailbox
from lup.resolver.models import ActorRef
from lup.runtime.errors import ProviderTurnError
from lup.runtime.factory import SessionFactory
from lup.runtime.models import (
    SessionHandle,
    SessionId,
    TurnHandle,
    TurnRequest,
    TurnResult,
)

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — the run directory's own layout, which a
# resumed run must spell exactly as the run that wrote it
SESSION_DIR = "sessions"


class ActorSchemaChangedError(RuntimeError):
    """A resumed actor expects a different submission schema than it left with."""


class ActorRecord(BaseModel, frozen=True):
    """What one actor needs to be reattached after a park.

    The digest is recorded here rather than pushed into the runtime because
    only this side knows both halves of the comparison. A provider that
    restores a resumed thread's tools from its own metadata never says what
    it restored, so the answerable question is whether *we* expect the same
    schema now that we expected before the park.
    """

    actor: ActorRef
    session: SessionId | None = None
    schema_digests: dict[str, str] = {}  # lup: ignore[dict-str-payload]
    """The digest this actor last used for each submission type it was asked for.

    Keyed by type rather than one per actor, because one actor is legitimately
    asked for more than one: the merger drives a whole join and reports a
    `JoinReport`, then adjudicates the finished tree and reports a
    `MergeReport`. A single digest read that second ask as the first schema
    having changed, and refused a conversation whose history is exactly what
    the second ask needs.
    """


RECORD_ADAPTER: TypeAdapter[ActorRecord] = TypeAdapter(ActorRecord)


def schema_digest(output_type: type[BaseModel] | type[None] | None) -> str | None:
    """Digest the submission schema an actor's turns are bound to."""
    if output_type is None or not issubclass(output_type, BaseModel):
        return None
    schema = json.dumps(TypeAdapter(output_type).json_schema(), sort_keys=True)
    return hashlib.sha256(schema.encode("utf-8")).hexdigest()


class ActorInbox:
    """One conversation's mail, delivered once by whichever path reaches it.

    Two paths put a message in front of an actor — the hook that interrupts
    a live turn, and the collection that heads the next one — and they each
    held their own in-memory position over the same stream. Two positions
    over one stream can only agree by luck: both started at whatever the
    head was when they were constructed, so a message posted while a turn
    was in flight was already behind both of them, and the run reported it
    sent.

    One inbox per conversation, holding the round it is on, is what lets the
    hook record a delivery against the actor that actually received it while
    the position it commits is the one the next turn resumes from.
    """

    def __init__(
        self, mailbox: QuestionMailbox, journal: Journal, actor: ActorRef
    ) -> None:
        self.mailbox = mailbox
        self.journal = journal
        self.actor = actor

    def waiting(self) -> ActorDelivery:
        """What this conversation has queued, without consuming any of it."""
        return self.mailbox.waiting(self.actor)

    def commit(self, delivery: ActorDelivery) -> None:
        """Record one delivery as handed over, and resume after it next time.

        Journaled here rather than at either call site, so a mid-turn
        delivery and a between-turns one leave the same record. The record
        being written on only one path is what let a redirect vanish twice
        over: nothing was delivered, and nothing said so.

        Separate from reading because the two are separated by however long
        it takes to open a session, and the run this exists for was
        interrupted by a spend limit. Committing on the read would have
        consumed a message the interrupted turn never carried.
        """
        for message in delivery.messages:
            self.journal.append(
                self.actor,
                MessagePostedEvent(
                    text=message.text,
                    door=message.door,
                    in_reply_to=message.in_reply_to,
                    redirect=message.redirect,
                ),
            )
        self.mailbox.delivered(self.actor, delivery.through)

    def take(self) -> list[ActorMessage]:
        """Take everything queued, for a caller delivering it here and now."""
        delivery = self.waiting()
        self.commit(delivery)
        return delivery.messages

    def record_outstanding(self) -> None:
        """Record whatever is still queued as this conversation closes."""
        for message in self.waiting().messages:
            self.journal.append(
                self.actor,
                MessageOutstandingEvent(
                    text=message.text, door=message.door, redirect=message.redirect
                ),
            )


def create_inbox_hooks(inbox: ActorInbox) -> LupHooksConfig:
    """Put anything said to this actor in front of it, mid-turn.

    Non-cooperative by construction. The actor calls any tool at all and the
    message is in its context — it never chooses to check, so it cannot fail
    to. Waiting for the next turn would mean a directive sits unread for as
    long as the current one runs, which on a resolver turn is most of the
    run.

    Telling and stopping are different acts and get different verdicts. A
    message rides alongside the call and the actor keeps going. A redirect
    denies the call and hands back the text as the reason, so an actor going
    the wrong way cannot take one more step down it — which is the whole
    difference between being informed and being redirected. Nothing here
    spends an interrupt: a turn that ends mid-report is a turn whose typed
    submission never arrives, and the actor is answering a refused tool call
    either way.

    The inbox is the actor's own rather than one opened here, so what this
    delivers the next turn does not deliver again, and what it delivers is
    recorded. Built from a target of its own, it matched the bare concern id
    while the console printed and accepted ``worker:some-concern#1`` — so a
    redirect sent to the address the console gave reached neither path.
    """

    async def deliver(_input: LupHookInput) -> LupHookOutput:
        arrived = inbox.take()
        if not arrived:
            return LupHookOutput(decision="allow")
        # Everything that arrived is carried either way. The position has
        # already moved past all of it, so a message batched alongside a
        # redirect has this one delivery and no other.
        delivered = "\n".join(
            f"[{'redirected' if message.redirect else 'message'} by {message.door}] "
            f"{message.text}"
            for message in arrived
        )
        if any(message.redirect for message in arrived):
            return LupHookOutput(
                decision="deny",
                reason=delivered
                + "\n\nStop what this call was part of and act on the above.",
            )
        return LupHookOutput(decision="allow", additional_context=delivered)

    return LupHooksConfig(pre_tool_use=[LupHookMatcher(hook=deliver, tag="inbox")])


class ActorSession:
    """One actor's conversation, held open across every turn it takes."""

    def __init__(
        self,
        actor: ActorRef,
        factory: SessionFactory,
        journal: Journal,
        record: ActorRecord | None = None,
        inbox: ActorInbox | None = None,
    ) -> None:
        self.actor = actor
        self.factory = factory
        self.journal = journal
        self.inbox = inbox
        self.record = record or ActorRecord(actor=actor)
        self.stack = AsyncExitStack()
        self.handle: SessionHandle | None = None
        self.pending: list[str] = []
        self.collected: ActorDelivery | None = None

    async def opened(self) -> SessionHandle:
        """Open this actor's session once, resuming where one was persisted."""
        if self.handle is None:
            self.handle = await self.stack.enter_async_context(
                self.factory.open(resume=self.record.session)
            )
        return self.handle

    async def started[T: BaseModel | None](
        self, handle: SessionHandle, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        """Begin a turn, forgetting a conversation the provider no longer has.

        A recorded session is a claim that the provider still holds that
        conversation, and neither runtime guarantees it: a transcript can be
        pruned, and a runtime that does not persist one never wrote it. Ending
        a run over lost context rather than over the work is the worse
        failure, so the actor reopens without the resume and takes its turn on
        a fresh conversation — the loss recorded rather than passed off as
        continuity.
        """
        delivered = self.with_pending(request)
        try:
            return await handle.session.start(delivered)
        except ProviderTurnError as error:
            # A host fault is not lost context. Reopening would meet the same
            # dead credential, and the attempt costs the resume point: the
            # record is cleared before the retry, so a run interrupted here
            # would resume every actor on a fresh conversation having
            # forgotten the one it was holding.
            if self.record.session is None or error.failure.environmental:
                raise
            logger.exception(
                "%s could not resume session %s; continuing on a fresh one",
                self.actor.label(),
                self.record.session.value,
            )
            self.record = self.record.model_copy(update={"session": None})
            await self.close()
            return await (await self.opened()).session.start(delivered)

    async def turn[T: BaseModel | None](self, request: TurnRequest[T]) -> TurnResult[T]:
        """Take one turn on this actor's session, recording it as it happens.

        The events are drained concurrently with awaiting the result rather
        than afterwards. An adapter fills its queue from its own task and the
        queue is unbounded, so a reader cannot deadlock a turn — and a
        watcher that only saw a turn once it finished would be a log rather
        than a trace.
        """
        self.check_schema(request)
        self.collect_inbox()
        handle = await self.opened()
        started = await self.started(handle, request)
        drain = (
            asyncio.create_task(
                record_turn(self.journal, self.actor, started.events.events())
            )
            if started.events is not None
            else None
        )
        try:
            result = await started.turn.result()
        finally:
            if drain is not None:
                # The adapter closes its queue in a `finally`, so the drain
                # terminates on the failure path too and awaiting it here
                # cannot outlive the turn that fed it.
                await drain
        self.record = self.record.model_copy(
            update={"session": result.identifiers.session}
        )
        return result

    def collect_inbox(self) -> None:
        """Take anything a door said to this actor since its last turn.

        Between turns there is nothing to append to, so a message waits here
        and lands at the head of the next one. Mid-turn delivery is the
        hook's job instead: the actor calls any tool and the message is in
        its context, which is what makes it impossible to forget — the actor
        was never involved in receiving it.

        A redirect says so even here. Where a hook surface exists it refuses
        the call outright, and this path is what a runtime without one gets
        instead — later, and unable to stop anything, but an actor told it
        was redirected can still abandon what it was doing. Delivering it in
        the same words as an ordinary message hid the difference from the one
        party that needed it, while the journal recorded the distinction
        faithfully for everyone who did not.
        """
        if self.inbox is None:
            return
        collected = self.inbox.waiting()
        if not collected.messages:
            # Nothing here for this actor, but the region held mail for
            # others: skipping past it is what stops every turn re-reading
            # the whole stream, and there is nothing to lose by committing.
            self.inbox.commit(collected)
            return
        self.collected = collected
        self.pending.extend(
            f"[redirected by {message.door}] {message.text}\n"
            "Stop what you were doing and act on this."
            if message.redirect
            else f"[{message.door}] {message.text}"
            for message in collected.messages
        )

    def with_pending[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnRequest[T]:
        """Put whatever was volunteered between turns at the head of this one.

        Ahead of the prompt rather than after it, because a message that
        retargets an actor has to be read before the instruction it revises.

        This is where the mail counts as delivered, because this is where it
        joins a turn. Anything that ends the run between collecting it and
        here leaves the position untouched, so the message heads the next
        turn instead of being consumed by one that never happened.
        """
        if not self.pending:
            return request
        delivered = "\n\n".join([*self.pending, request.input.text])
        self.pending.clear()
        if self.inbox is not None and self.collected is not None:
            self.inbox.commit(self.collected)
            self.collected = None
        return request.model_copy(
            update={"input": request.input.model_copy(update={"text": delivered})}
        )

    def check_schema[T: BaseModel | None](self, request: TurnRequest[T]) -> None:
        """Refuse a resumed actor whose submission schema no longer matches.

        Per submission type, because being asked for a second one is ordinary
        rather than suspect: the same merger reports a join and then, once the
        tree is whole, adjudicates it. What is worth refusing is a type this
        actor has answered before whose shape has since moved, which is the
        park-across-a-code-change this guard was built for.
        """
        digest = schema_digest(request.output_type)
        if request.output_type is None or digest is None:
            return
        named = request.output_type.__name__
        seen = self.record.schema_digests
        if named in seen and seen[named] != digest:
            raise ActorSchemaChangedError(
                f"{self.actor.label()} resumed expecting a different {named} "
                "than the one it was bound to"
            )
        self.record = self.record.model_copy(
            update={"schema_digests": {**seen, named: digest}}
        )

    async def close(self) -> None:
        await self.stack.aclose()
        self.handle = None


class ActorSessions:
    """Every actor's session for one run, and their persisted identities.

    Sessions are addressed by actor rather than created per turn, so asking
    for the same actor twice continues one conversation. Closing is the run's
    job and happens once, at the end of the run or at a park.
    """

    def __init__(self, root: Path, journal: Journal, mailbox: QuestionMailbox) -> None:
        self.root = root / SESSION_DIR
        self.journal = journal
        self.mailbox = mailbox
        self.sessions: dict[str, ActorSession] = {}
        self.inboxes: dict[str, ActorInbox] = {}

    def path(self, actor: ActorRef) -> Path:
        return self.root / f"{actor.conversation()}.json"

    def inbox(self, actor: ActorRef) -> ActorInbox:
        """This conversation's mail, kept current with the round it is on.

        One object per conversation rather than one per caller, because the
        hook that interrupts a live turn and the collection that heads the
        next one are two views of one mailbox. Handing each its own left
        them with two positions over one stream, and a message could sit
        behind both.
        """
        held = self.inboxes.get(actor.conversation())  # lup: ignore[dict-get] presence
        if held is None:
            held = ActorInbox(self.mailbox, self.journal, actor)
            self.inboxes[actor.conversation()] = held
        held.actor = actor
        return held

    def persisted(self, actor: ActorRef) -> ActorRecord | None:
        """The identity this actor left behind, if it has run before."""
        path = self.path(actor)
        if not path.exists():
            return None
        try:
            return RECORD_ADAPTER.validate_json(path.read_text("utf-8"))
        except ValueError:
            logger.exception("Discarding unreadable actor record at %s", path)
            return None

    def session(self, actor: ActorRef, factory: SessionFactory) -> ActorSession:
        """This actor's session, resumed from its record the first time."""
        inbox = self.inbox(actor)
        held = self.sessions.get(actor.conversation())  # lup: ignore[dict-get] presence
        if held is not None:
            held.actor = actor
            return held
        opened = ActorSession(
            actor, factory, self.journal, self.persisted(actor), inbox
        )
        self.sessions[actor.conversation()] = opened
        return opened

    def save(self, actor: ActorRef) -> None:
        """Persist one actor's identity so a resumed run reattaches to it."""
        held = self.sessions.get(actor.conversation())  # lup: ignore[dict-get] presence
        if held is not None:
            publish_atomic(self.path(actor), held.record)

    async def close(self) -> None:
        """Close every open session, recording what each was never handed.

        A sender is told a message was sent on the strength of the mailbox
        accepting it, which is not the same as anyone reading it. What is
        still queued as the sessions close is therefore recorded against the
        actor it was for — outstanding across a park, and never read at all
        on a run that ended.
        """
        for held in list(self.sessions.values()):
            self.save(held.actor)
            self.inbox(held.actor).record_outstanding()
            await held.close()
        self.sessions.clear()
