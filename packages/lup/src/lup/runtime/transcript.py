"""Deriving a transcript from the events that are the record.

Events are canonical and the transcript is derived, rather than both being
accumulated side by side. Two accumulators can disagree — and did: a watcher
saw a tool call and never its result, because tool results reached the
message list and never the event stream. A stream that omits half of what
happened cannot serve as a trace, whatever else it is good for.

One fold, used by every adapter, is what keeps that from happening again.
"""

from collections.abc import Sequence

from lup.runtime.models import (
    AnyTurnBlock,
    TurnEvent,
    TurnMessage,
)


def fold_transcript(events: Sequence[TurnEvent]) -> list[TurnMessage]:
    """Rebuild the message list from a turn's durable events.

    Total by construction: the parameter type excludes deltas, so there is
    no partial fragment to decide about.
    """
    return [
        message for event in events if (message := event.completed_message) is not None
    ]


def fold_blocks(events: Sequence[TurnEvent]) -> list[AnyTurnBlock]:
    """Every block a turn produced, in the order the messages carried them."""
    return [block for message in fold_transcript(events) for block in message.blocks]
