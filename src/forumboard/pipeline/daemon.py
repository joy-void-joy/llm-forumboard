"""The long-lived process: three loops, each on its own rhythm.

The loops are separate because their costs are. Syncing is cheap and wants to
be frequent; a worldview pass reads every unmerged discussion at once and is
better off batching them; briefings are due on a calendar. Running them on one
timer would make the cheapest wait for the most expensive.

They share one context, and each pass re-reads the roster from disk. Enrolling
somebody therefore takes effect at the next pass without restarting anything —
which matters, because the enrolment surface is a web page somebody uses while
the daemon is running.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from forumboard.agent.config import settings
from forumboard.context import ForumboardContext, open_context
from forumboard.pipeline.briefing import BriefingPass
from forumboard.pipeline.sync import ConversationSync
from forumboard.pipeline.worldview import WorldviewPass
from forumboard.profiles import read_roster
from forumboard.store import ConversationStore

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — the ceiling a backoff climbs to, an hour,
# past which retrying faster stops being the useful lever
MAX_BACKOFF_SECONDS = 3600


class Pipeline:
    """Every pass, bound to one project and one live Notion workspace."""

    def __init__(
        self, *, project_root: Path, profiles_root: Path, context: ForumboardContext
    ) -> None:
        self.project_root = project_root
        self.profiles_root = profiles_root
        self.context = context
        notes = (project_root / settings.notes_path).resolve()
        self.store = ConversationStore(notes / "conversations")

    async def sync_once(self) -> list[str]:
        """One sync pass over whoever is enrolled right now."""
        sync = ConversationSync(
            profiles_root=self.profiles_root,
            store=self.store,
            context=self.context,
            roster=read_roster(self.project_root),
        )
        return [outcome.describe() for outcome in await sync.run_once()]

    async def worldview_once(self) -> list[str]:
        """One worldview pass."""
        outcome = await WorldviewPass(self.context).run_once()
        return [outcome.describe()]

    async def briefings_once(self) -> list[str]:
        """Whichever briefings are owed."""
        outcome = await BriefingPass(self.context).run_once()
        return [outcome.describe()]

    async def all_once(self) -> list[str]:
        """One of each, in the order a conversation moves through them."""
        lines = await self.sync_once()
        lines.extend(await self.worldview_once())
        lines.extend(await self.briefings_once())
        return lines


async def open_pipeline(project_root: Path, profiles_root: Path) -> Pipeline:
    """Build the pipeline, resolving Notion once."""
    return Pipeline(
        project_root=project_root,
        profiles_root=profiles_root,
        context=await open_context(),
    )


async def repeat(
    name: str,
    pass_once: Callable[[], Awaitable[list[str]]],
    interval_seconds: int,
) -> None:
    """Run one pass forever, backing off when it keeps failing.

    A failure doubles the wait up to an hour. The point is not to be clever
    about recovery but to stop a broken dependency from being hammered while
    still coming back on its own when it recovers.
    """
    failures = 0
    while True:
        try:
            for line in await pass_once():
                logger.info("%s", line)
            failures = 0
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("%s loop stopped", name)
            raise
        except Exception:
            failures += 1
            backoff = min(interval_seconds * (2**failures), MAX_BACKOFF_SECONDS)
            logger.exception(
                "%s pass failed (#%d); retrying in %ds", name, failures, backoff
            )
            await asyncio.sleep(backoff)


async def run_forever(project_root: Path, profiles_root: Path) -> None:
    """Run all three loops until the process is stopped.

    A task group rather than bare tasks: if one loop dies of something its own
    backoff cannot handle, the others are cancelled and the process exits,
    rather than the daemon staying up while quietly doing half its job.
    """
    pipeline = await open_pipeline(project_root, profiles_root)
    logger.info(
        "Starting: sync every %ds, worldview and briefings every %ds",
        settings.sync_interval_seconds,
        settings.worldview_interval_seconds,
    )
    async with asyncio.TaskGroup() as group:
        group.create_task(
            repeat("sync", pipeline.sync_once, settings.sync_interval_seconds)
        )
        group.create_task(
            repeat(
                "worldview",
                pipeline.worldview_once,
                settings.worldview_interval_seconds,
            )
        )
        group.create_task(
            repeat(
                "briefing",
                pipeline.briefings_once,
                settings.worldview_interval_seconds,
            )
        )
