"""Waiting out a provider account allowance, rather than failing the turn.

:class:`~lup.runtime.errors.BudgetExceededError` and this are opposite kinds of
"no more work". A budget is our own ceiling, and hitting it means the turn
should stop. An account allowance is the provider's, and hitting it means only
that the same turn cannot run *yet* — the work is still wanted, and the
provider has said when it can resume.

So this decorator neither rotates profiles nor degrades the model: it sleeps
until the stated reset and starts the identical request on the identical
session again. Substituting a cheaper model to keep going would answer a
different question than the caller asked, and quietly change what the run
produced.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from lup.runtime.contracts import Session, Turn
from lup.runtime.errors import QuotaExceededError
from lup.runtime.factory import SessionFactory
from lup.runtime.models import (
    SessionHandle,
    SessionId,
    TurnHandle,
    TurnRequest,
    TurnResult,
)

logger = logging.getLogger(__name__)


class QuotaWaitConfig(BaseModel, frozen=True):
    """Timing policy that never rotates profiles or degrades models."""

    profile: str | None = None
    reset_grace_seconds: float = Field(default=5, ge=0)
    minimum_wait_seconds: float = Field(default=30, ge=0)
    unknown_reset_wait_seconds: float = Field(default=300, gt=0)


class QuotaWaitEvent(BaseModel, frozen=True):
    """Observable transition emitted before sleeping and before retrying."""

    phase: Literal["sleep", "wake"]
    profile: str | None = None
    quota_type: str | None = None
    reset_at: datetime | None = None
    wait_seconds: float = Field(ge=0)


type QuotaWaitSink = Callable[[QuotaWaitEvent], Awaitable[None]]
type QuotaSleeper = Callable[[float], Awaitable[None]]
type NowProvider = Callable[[], datetime]


def utc_now() -> datetime:
    """The aware wall clock allowance resets are measured against."""
    return datetime.now(UTC)


class QuotaWaitingTurn[T: BaseModel | None](Turn[T]):
    """Retry the identical request on the identical session after reset."""

    def __init__(
        self,
        session: Session,
        request: TurnRequest[T],
        handle: TurnHandle[T],
        config: QuotaWaitConfig,
        sink: QuotaWaitSink,
        sleeper: QuotaSleeper,
        now: NowProvider,
    ) -> None:
        self.session = session
        self.request = request
        self.handle = handle
        self.config = config
        self.sink = sink
        self.sleeper = sleeper
        self.now = now

    def wait_seconds(self, error: QuotaExceededError) -> float:
        """How long to sleep before the identical request is worth retrying.

        Bounded below because a reset already in the past — a clock skewed
        against the provider's, or a window that rolled while the failure was
        in flight — would otherwise retry immediately and be refused again.
        """
        if error.reset_at is None:
            return self.config.unknown_reset_wait_seconds
        until_reset = (error.reset_at - self.now()).total_seconds()
        return max(
            self.config.minimum_wait_seconds,
            until_reset + self.config.reset_grace_seconds,
        )

    async def result(self) -> TurnResult[T]:
        while True:
            try:
                return await self.handle.turn.result()
            except QuotaExceededError as error:
                delay = self.wait_seconds(error)
                waiting = QuotaWaitEvent(
                    phase="sleep",
                    profile=self.config.profile,
                    quota_type=error.quota_type,
                    reset_at=error.reset_at,
                    wait_seconds=delay,
                )
                await self.sink(waiting)
                logger.warning(
                    "Profile %s exhausted; waiting %.0fs for %s allowance reset",
                    self.config.profile or "default",
                    delay,
                    error.quota_type or "provider",
                )
                await self.sleeper(delay)
                await self.sink(waiting.model_copy(update={"phase": "wake"}))
                self.handle = await self.session.start(self.request)


class QuotaWaitingSession(Session):
    """Attach allowance waiting to every turn started on a session."""

    def __init__(
        self,
        inner: Session,
        config: QuotaWaitConfig,
        sink: QuotaWaitSink,
        sleeper: QuotaSleeper,
        now: NowProvider,
    ) -> None:
        self.inner = inner
        self.config = config
        self.sink = sink
        self.sleeper = sleeper
        self.now = now

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        handle = await self.inner.start(request)
        return TurnHandle[T](
            turn=QuotaWaitingTurn(
                self.inner,
                request,
                handle,
                self.config,
                self.sink,
                self.sleeper,
                self.now,
            ),
            events=handle.events,
            interrupt=handle.interrupt,
            steer=handle.steer,
        )


# The config is one decorator's settings, and no one of them is the subject:
# what this does is compose a config, a sink and a clock into a factory, which
# belongs to none of the three alone.
# lup: ignore[model-free-function] — composition root wiring a config into a factory
def quota_waiting_session_factory(
    inner: SessionFactory,
    config: QuotaWaitConfig,
    sink: QuotaWaitSink,
    *,
    sleeper: QuotaSleeper = asyncio.sleep,
    now: NowProvider = utc_now,
) -> SessionFactory:
    """Give every session opened by ``inner`` wait-only allowance recovery."""

    @asynccontextmanager
    async def open_waiting(
        resume: SessionId | None = None,
    ) -> AsyncGenerator[SessionHandle]:
        async with inner.open(resume) as handle:
            yield SessionHandle(
                session=QuotaWaitingSession(handle.session, config, sink, sleeper, now),
                fork=handle.fork,
            )

    return SessionFactory(open_waiting)
