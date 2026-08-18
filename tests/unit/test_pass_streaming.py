"""What a tool-less pass shows while it is working.

The reviewer and the editor reach ``decorate_factory`` without the notes and
trace logger its display sink is gated on, so they print nothing for as long
as a model takes to read a whole transcript. Silence there is indistinguishable
from a hung run, which is what makes it worth a test rather than a preference.

The stream is faked at the ``EventStream`` seam rather than below it: the
consumer's own fold over ``completed_message`` is what runs, so a fake cannot
pass by doing the folding itself. The adapter completes every block and then
the message holding it, and a fold that took both would print each block twice
— so the fake emits both, and one of these pins that shut.
"""

from collections.abc import AsyncIterator

import pytest

from lup.runtime.contracts import EventStream
from lup.runtime.models import (
    BlockCompletedEvent,
    LiveTurnEvent,
    MessageCompletedEvent,
    SessionId,
    TurnCompletedEvent,
    TurnEvent,
    TurnId,
    TurnIdentifiers,
    TurnMessage,
    TurnTextBlock,
)

from forumboard.agent.config import settings
from forumboard.agent.core import streaming_blocks

IDENTIFIERS = TurnIdentifiers(session=SessionId(value="s"), turn=TurnId(value="t"))


def spoken(*texts: str) -> list[TurnEvent]:
    """One assistant message per text, shaped the way the adapter emits."""
    events: list[TurnEvent] = []
    for text in texts:
        block = TurnTextBlock(text=text)
        events.append(BlockCompletedEvent(identifiers=IDENTIFIERS, block=block))
        events.append(
            MessageCompletedEvent(
                identifiers=IDENTIFIERS,
                message=TurnMessage(role="assistant", blocks=[block]),
            )
        )
    events.append(TurnCompletedEvent(identifiers=IDENTIFIERS))
    return events


class Spoken(EventStream):
    """An event stream that replays a fixed sequence, once."""

    def __init__(self, *texts: str) -> None:
        self.recorded = spoken(*texts)

    async def durable(self) -> AsyncIterator[TurnEvent]:
        for event in self.recorded:
            yield event

    def events(self) -> AsyncIterator[TurnEvent]:
        return self.durable()

    def live(self) -> AsyncIterator[LiveTurnEvent]:
        raise NotImplementedError("this fake carries no deltas")


@pytest.fixture(autouse=True)
def streaming_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stream blocks, as a terminal run does unless it is told not to."""
    monkeypatch.setattr(settings, "stream_agent_blocks", True)


@pytest.mark.asyncio
async def test_a_pass_prints_its_blocks_as_it_produces_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reviewer working through a transcript is visible while it works."""
    async with streaming_blocks(Spoken("reading the transcript", "deciding")):
        pass
    printed = capsys.readouterr().out
    assert "reading the transcript" in printed
    assert "deciding" in printed


@pytest.mark.asyncio
async def test_each_block_prints_once(capsys: pytest.CaptureFixture[str]) -> None:
    """The adapter completes a block and then its message; only one is folded."""
    async with streaming_blocks(Spoken("said once")):
        pass
    assert capsys.readouterr().out.count("said once") == 1


@pytest.mark.asyncio
async def test_a_run_told_not_to_stream_stays_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A daemon's terminal is durable and shared; raw reasoning must not reach it."""
    monkeypatch.setattr(settings, "stream_agent_blocks", False)
    async with streaming_blocks(Spoken("never printed")):
        pass
    assert "never printed" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_turn_without_events_still_runs() -> None:
    """A provider that offers no stream is a quiet pass, not a failed one."""
    async with streaming_blocks(None):
        pass


@pytest.mark.asyncio
async def test_a_failing_turn_still_drains_the_stream(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Displaying is diagnostics: the turn's error is what reaches the caller."""
    with pytest.raises(ValueError, match="the turn failed"):
        async with streaming_blocks(Spoken("printed before the failure")):
            raise ValueError("the turn failed")
    assert "printed before the failure" in capsys.readouterr().out
