"""The concrete session surface every consumer holds.

Consumers never hold a capability ABC. ``SessionFactory`` is a plain class
parametrized by one swappable ``SessionOpener`` engine: adapters, wrappers,
and tests supply the opener, and the behavior every caller shares — opening a
session, running one turn on it — lives here once.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import overload

from pydantic import BaseModel

from lup.runtime.models import (
    SessionHandle,
    SessionId,
    TurnInput,
    TurnRequest,
    TurnResult,
    turn_request,
)

type SessionOpener = Callable[
    [SessionId | None], AbstractAsyncContextManager[SessionHandle]
]


class SessionFactory:
    """Open configured conversations and run turns on them."""

    def __init__(self, opener: SessionOpener) -> None:
        self.opener = opener

    def open(
        self, resume: SessionId | None = None
    ) -> AbstractAsyncContextManager[SessionHandle]:
        """Open a new or resumed session."""
        return self.opener(resume)

    @overload
    async def query[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnResult[T]: ...

    @overload
    async def query(self, request: str | TurnInput) -> TurnResult[None]: ...

    @overload
    async def query[T: BaseModel](
        self, request: str | TurnInput, output_type: type[T]
    ) -> TurnResult[T]: ...

    async def query[T: BaseModel | None](
        self,
        request: TurnRequest[T] | str | TurnInput,
        output_type: type[BaseModel] | None = None,
    ) -> TurnResult[T] | TurnResult[BaseModel] | TurnResult[None]:
        """Open one session, run one turn, and always close the session.

        The overloads carry the same inference `turn_request` protects: a
        prepared request keeps its own parameter, a bare prompt resolves to
        `None`, and a prompt plus a model resolves to that model.
        """
        async with self.open() as handle:
            # Matched on the material rather than on the prepared request: `str`
            # is one of these alternatives and cannot answer for itself, so no
            # base class can carry this for the whole union. Each arm starts its
            # own turn because `TurnRequest` is invariant — a union of them binds
            # no single output type at `start`.
            match request:
                case str() | TurnInput():  # lup: ignore[own-model-dispatch]
                    if output_type is None:
                        turn = await handle.session.start(turn_request(request))
                    else:
                        turn = await handle.session.start(
                            turn_request(request, output_type)
                        )
                case _:
                    turn = await handle.session.start(request)
            return await turn.turn.result()
