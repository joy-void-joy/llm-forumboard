"""Inference pins for the one-turn entry points.

`turn_request` and `SessionFactory.query` are overloaded so each call shape
resolves to one exact type rather than a union. The `assert_type` calls below
are the regression guard: pyright — which `lup-devtools dev check` runs — fails
the moment a later simplification collapses an overload set and widens what a
call infers. Each pin sits on a call that also executes, so the pinned shapes
cannot drift away from working code.
"""

from datetime import timedelta
from typing import assert_type

import pytest
from pydantic import BaseModel

from lup.runtime.contracts import Session, Turn
from lup.runtime.factory import SessionFactory
from lup.runtime.models import (
    SessionId,
    TurnHandle,
    TurnId,
    TurnIdentifiers,
    TurnInput,
    TurnRequest,
    TurnResult,
    turn_request,
)
from lup.runtime.query import query
from lup.types import Usage
from tests.unit.doubles import session_factory

IDENTIFIERS = TurnIdentifiers(
    session=SessionId(value="session"), turn=TurnId(value="turn")
)


class Summary(BaseModel, frozen=True):
    """A default-constructible output model, so the stub can submit one."""

    title: str = "pinned"


class StubTurn[T: BaseModel | None](Turn[T]):
    """Complete immediately, submitting an instance of the requested model."""

    def __init__(self, request: TurnRequest[T]) -> None:
        self.request = request

    async def result(self) -> TurnResult[T]:
        output_type = self.request.output_type
        # Constructed here rather than through the shared `turn_result`: the
        # instance this builds is a `T` only by the request's own construction,
        # which is a fact `model_validate` accepts and no signature can state.
        return TurnResult[T].model_validate(
            {
                "output": None if output_type is None else output_type(),
                "messages": [],
                "blocks": [],
                "usage": Usage(),
                "duration": timedelta(),
                "identifiers": IDENTIFIERS,
            }
        )


class StubSession(Session):
    """Record the text every turn was started with."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        self.prompts.append(request.input.text)
        return TurnHandle[T](turn=StubTurn(request))

    def factory(self) -> SessionFactory:
        """A factory whose every opened session is this one."""
        return session_factory(self)


def test_turn_request_without_a_model_infers_no_output() -> None:
    from_text = turn_request("summarize")
    from_input = turn_request(TurnInput(text="summarize"))

    assert_type(from_text, TurnRequest[None])
    assert_type(from_input, TurnRequest[None])
    assert from_text.output_type is None
    assert from_text.input == from_input.input


def test_turn_request_with_a_model_infers_that_model() -> None:
    from_text = turn_request("summarize", Summary)
    from_input = turn_request(TurnInput(text="summarize"), Summary)

    assert_type(from_text, TurnRequest[Summary])
    assert_type(from_input, TurnRequest[Summary])
    assert from_text.output_type is Summary
    assert from_text.input == from_input.input


@pytest.mark.asyncio
async def test_query_carries_a_prepared_request_type_through() -> None:
    session = StubSession()
    factory = session.factory()

    plain = await factory.query(turn_request("summarize"))
    typed = await factory.query(turn_request("summarize", Summary))

    assert_type(plain, TurnResult[None])
    assert_type(typed, TurnResult[Summary])
    assert plain.output is None
    assert typed.output.title == "pinned"
    assert session.prompts == ["summarize", "summarize"]


@pytest.mark.asyncio
async def test_query_reaches_a_result_from_a_prompt_in_one_call() -> None:
    session = StubSession()
    factory = session.factory()

    plain = await factory.query("summarize")
    typed = await factory.query("summarize", Summary)
    from_input = await factory.query(TurnInput(text="wrapped"))
    typed_input = await factory.query(TurnInput(text="wrapped"), Summary)

    assert_type(plain, TurnResult[None])
    assert_type(typed, TurnResult[Summary])
    assert_type(from_input, TurnResult[None])
    assert_type(typed_input, TurnResult[Summary])
    assert typed.output.title == "pinned"
    assert session.prompts == ["summarize", "summarize", "wrapped", "wrapped"]


@pytest.mark.asyncio
async def test_the_free_spelling_infers_exactly_what_the_method_does() -> None:
    session = StubSession()
    factory = session.factory()

    prepared = await query(factory, turn_request("summarize", Summary))
    plain = await query(factory, "summarize")
    typed = await query(factory, "summarize", Summary)
    typed_input = await query(factory, TurnInput(text="wrapped"), Summary)

    assert_type(prepared, TurnResult[Summary])
    assert_type(plain, TurnResult[None])
    assert_type(typed, TurnResult[Summary])
    assert_type(typed_input, TurnResult[Summary])
    assert plain.output is None
    assert typed.output.title == "pinned"
    assert session.prompts == ["summarize", "summarize", "summarize", "wrapped"]
