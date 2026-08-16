"""What an actor does when the provider has lost its conversation."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel

from lup.resolver.actors import ActorInbox, ActorRecord, ActorSession
from lup.resolver.journal import Journal
from lup.resolver.mailbox import AnswerDoor, QuestionMailbox, new_message
from lup.resolver.models import ActorRef
from lup.runtime.contracts import Session
from lup.runtime.errors import ProviderTurnError, TurnFailure
from lup.runtime.factory import SessionFactory
from lup.runtime.models import (
    SessionHandle,
    SessionId,
    TurnHandle,
    TurnInput,
    TurnRequest,
    TurnResult,
)
from lup.types import Usage

from tests.unit.doubles import StaticTurn, identifiers, session_factory

FRESH = "fresh-session"


class ResumeRefusingSession(Session):
    """Refuse every turn opened against a resumed conversation."""

    def __init__(self, resumed: bool) -> None:
        self.resumed = resumed

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        if self.resumed:
            raise ProviderTurnError(
                TurnFailure(message="No conversation found with session ID")
            )
        result = TurnResult[T].model_validate(
            {
                "output": request.output_type,
                "messages": [],
                "blocks": [],
                "usage": Usage(),
                "duration": timedelta(),
                "identifiers": identifiers(session=FRESH),
            }
        )
        return TurnHandle[T](turn=StaticTurn(result))


def refusing_factory() -> tuple[SessionFactory, list[SessionId | None]]:
    """A factory that refuses a resume, recording what each open asked for."""
    opened: list[SessionId | None] = []

    @asynccontextmanager
    async def open_session(
        resume: SessionId | None = None,
    ) -> AsyncGenerator[SessionHandle]:
        opened.append(resume)
        yield SessionHandle(session=ResumeRefusingSession(resume is not None))

    return SessionFactory(open_session), opened


def worker_session(
    tmp_path: Path, record: ActorRecord | None = None
) -> tuple[ActorSession, list[SessionId | None]]:
    """One worker actor over a factory that refuses whatever it resumes."""
    factory, opened = refusing_factory()
    actor = ActorRef(kind="worker", id="a-concern")
    return ActorSession(actor, factory, Journal(tmp_path), record), opened


class RecordingSession(Session):
    """Accept every turn, keeping the input each one was actually given."""

    def __init__(self) -> None:
        self.delivered: list[str] = []

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        self.delivered.append(request.input.text)
        result = TurnResult[T].model_validate(
            {
                "output": request.output_type,
                "messages": [],
                "blocks": [],
                "usage": Usage(),
                "duration": timedelta(),
                "identifiers": identifiers(),
            }
        )
        return TurnHandle[T](turn=StaticTurn(result))


def mailed_session(
    tmp_path: Path,
) -> tuple[ActorSession, ActorInbox, RecordingSession]:
    """One worker actor holding the inbox its run would hand it."""
    recording = RecordingSession()
    actor = ActorRef(kind="worker", id="a-concern")
    journal = Journal(tmp_path)
    inbox = ActorInbox(QuestionMailbox(tmp_path), journal, actor)
    session = ActorSession(actor, session_factory(recording), journal, None, inbox)
    return session, inbox, recording


def post(tmp_path: Path, text: str) -> None:
    QuestionMailbox(tmp_path).send(
        new_message("run-1", "worker:a-concern#1", text, AnswerDoor.AGENT)
    )


async def test_mail_heads_the_next_turn_and_is_carried_once(tmp_path: Path) -> None:
    """What a door said between turns rides in front of the prompt, once."""
    session, inbox, recording = mailed_session(tmp_path)
    post(tmp_path, "the sibling already renamed that")

    await session.turn(TurnRequest(input=TurnInput(text="go"), output_type=None))
    await session.turn(TurnRequest(input=TurnInput(text="go on"), output_type=None))

    assert recording.delivered == [
        "[agent] the sibling already renamed that\n\ngo",
        "go on",
    ]
    assert inbox.waiting().messages == []


async def test_a_turn_that_never_happened_does_not_consume_the_message(
    tmp_path: Path,
) -> None:
    """The run this was written for died of a spend limit between the two.

    Collecting is not delivering: the position moves when the message joins
    a turn, so an interrupt after the read leaves it queued for the turn
    that does happen rather than swallowing it on behalf of one that did
    not.
    """
    session, inbox, _ = mailed_session(tmp_path)
    post(tmp_path, "stop, that design was rejected")

    session.collect_inbox()

    assert [message.text for message in inbox.waiting().messages] == [
        "stop, that design was rejected"
    ]


async def test_a_lost_conversation_continues_on_a_fresh_session(
    tmp_path: Path,
) -> None:
    """A resume the provider cannot honour costs the context, not the run."""
    actor = ActorRef(kind="worker", id="a-concern")
    session, opened = worker_session(
        tmp_path, ActorRecord(actor=actor, session=SessionId(value="gone"))
    )

    result = await session.turn(
        TurnRequest(input=TurnInput(text="go"), output_type=None)
    )

    assert opened == [SessionId(value="gone"), None]
    assert result.identifiers.session == SessionId(value=FRESH)
    assert session.record.session == SessionId(value=FRESH)


async def test_an_actor_with_nothing_to_forget_still_raises(tmp_path: Path) -> None:
    """The fallback answers a lost conversation, and hides no other failure."""
    session, opened = worker_session(tmp_path)
    session.record = session.record.model_copy(
        update={"session": SessionId(value="gone")}
    )
    await session.close()

    result = await session.turn(
        TurnRequest(input=TurnInput(text="go"), output_type=None)
    )

    assert opened == [SessionId(value="gone"), None]
    assert result.identifiers.session == SessionId(value=FRESH)
