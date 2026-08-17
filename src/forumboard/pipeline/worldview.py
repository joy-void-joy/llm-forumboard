"""One worldview pass: read what is unmerged, rewrite the topics it touches.

The agent does the deciding — which topics exist, what each says, which are
finished. This module gives it the tools, the prompt, and the one thing it
cannot be trusted to do itself: stamping the discussions it read as merged,
after its session ends.

Stamping afterwards is deliberate. An agent that marked a discussion merged
before rewriting could crash in between, and that discussion would never be
looked at again. Marking after means a crash re-reads it — which costs a pass
and loses nothing.
"""

import logging
from datetime import datetime, timezone

from pydantic import BaseModel

from forumboard.agent.core import build_session_factory
from forumboard.agent.models import WorldviewUpdate
from forumboard.agent.tools.worldview import create_worldview_tools
from forumboard.context import ForumboardContext

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — the instruction that opens a pass, kept
# here beside the loop it drives rather than in the prompt it follows
PASS_TASK = """\
Bring the worldview up to date.

Start by listing the topics that already exist and the discussions nobody has
folded in yet. Read what you need of both. Then rewrite every topic the new
discussions affect, create topics for subjects that have appeared, and retire
topics that are no longer part of what is happening.

When you are done, report which discussion page ids you folded in. Report only
ids you actually read — an id reported here is never looked at again."""


class WorldviewOutcome(BaseModel, frozen=True):
    """What one worldview pass did."""

    unmerged: int = 0
    merged: int = 0
    summary: str = ""
    note: str = ""

    def describe(self) -> str:
        """One line for the log."""
        if self.note:
            return f"worldview: {self.note}"
        return f"worldview: {self.merged}/{self.unmerged} folded in — {self.summary}"


class WorldviewPass:
    """Runs the agent that maintains the current worldview."""

    def __init__(self, context: ForumboardContext) -> None:
        self.context = context

    async def run_once(self) -> WorldviewOutcome:
        """One pass, or nothing where there is nothing new to fold in."""
        outstanding = await self.context.discussions.unmerged()
        if not outstanding:
            return WorldviewOutcome(note="nothing new to fold in")

        tools = create_worldview_tools(
            self.context.discussions, self.context.worldview, self.context.people
        )
        identifier = datetime.now(timezone.utc).strftime("worldview-%Y%m%d-%H%M%S")
        build = build_session_factory(identifier, worldview_tools=tools)
        async with build.factory.open() as handle:
            from lup.runtime.models import turn_request

            turn = await handle.session.start(turn_request(PASS_TASK, WorldviewUpdate))
            result = await turn.turn.result()

        update = result.output
        if update is None:
            return WorldviewOutcome(
                unmerged=len(outstanding),
                note="the pass ended without reporting what it folded in",
            )

        known = {page.id for page in outstanding}
        folded = [page_id for page_id in update.merged_page_ids if page_id in known]
        if folded:
            await self.context.discussions.mark_merged(
                folded, datetime.now(timezone.utc)
            )
        outcome = WorldviewOutcome(
            unmerged=len(outstanding), merged=len(folded), summary=update.summary
        )
        logger.info("%s", outcome.describe())
        return outcome
