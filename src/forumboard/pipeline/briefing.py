"""Periodic recaps, written into the discussion database as plain notes.

A briefing is due when the last one of its cadence is older than its window.
That question is asked of Notion rather than of local state, because Notion is
where briefings live: a cache that was wiped would otherwise produce a second
copy of last week, and a deployment moved to another machine would produce a
month of them at once.

Briefings are stamped merged when written. They are drawn from discussions the
worldview has already read, and letting one back in would let the worldview
cite a summary of its own sources as if it were a source.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from forumboard.agent.config import settings
from forumboard.context import ForumboardContext
from forumboard.notion.client import NotionPage
from forumboard.notion.discussions import BriefingRecord
from forumboard.notion.properties import all_of, equals_filter, select_is_filter
from forumboard.pipeline import passes

logger = logging.getLogger(__name__)


class Cadence(BaseModel, frozen=True):
    """One briefing rhythm: how far back it looks, and how often it is due."""

    name: str
    days: int

    def window_start(self, now: datetime) -> datetime:
        """The earliest publication this briefing covers."""
        return now - timedelta(days=self.days)

    def due(self, last_written: datetime | None, now: datetime) -> bool:
        """Whether one is owed.

        Never having written one counts as owed: a fresh deployment should
        produce its first briefing rather than wait a full window.
        """
        if last_written is None:
            return True
        return now - last_written >= timedelta(days=self.days)

    def label(self, now: datetime) -> str:
        """How the window reads to somebody browsing the database."""
        start = self.window_start(now).date().isoformat()
        return f"{start} to {now.date().isoformat()}"


# lup: ignore[constant-declaration] — the vocabulary a cadence name is drawn
# from; which of these a deployment runs is FORUMBOARD_BRIEFING_CADENCES
KNOWN_CADENCES = [
    Cadence(name="daily", days=1),
    Cadence(name="weekly", days=7),
    Cadence(name="monthly", days=30),
    Cadence(name="quarterly", days=91),
]


def configured_cadences() -> list[Cadence]:
    """The cadences this deployment runs, skipping names nobody defines."""
    wanted = settings.briefing_cadences
    known = {cadence.name: cadence for cadence in KNOWN_CADENCES}
    found = [
        cadence
        for name in wanted
        # lup: ignore[dict-get] — keyed by a configured name, which may be junk
        if (cadence := known.get(name)) is not None
    ]
    unknown = [name for name in wanted if name not in known]
    if unknown:
        logger.warning(
            "ignoring unknown briefing cadence(s) %s; known: %s",
            ", ".join(unknown),
            ", ".join(cadence.name for cadence in KNOWN_CADENCES),
        )
    return found


class BriefingOutcome(BaseModel, frozen=True):
    """What one briefing pass did."""

    written: list[str] = []
    note: str = ""

    def describe(self) -> str:
        """One line for the log."""
        if self.note:
            return f"briefings: {self.note}"
        return f"briefings: wrote {', '.join(self.written)}"


class BriefingPass:
    """Writes whichever briefings are owed."""

    def __init__(self, context: ForumboardContext) -> None:
        self.context = context

    async def last_written(self, cadence: Cadence) -> datetime | None:
        """When this cadence last produced a briefing, as Notion recorded it."""
        pages = await self.context.discussions.workspace.query(
            self.context.discussions.data_source_id,
            all_of(
                select_is_filter("Kind", "briefing"),
                equals_filter("Profile", cadence.name),
            ),
        )
        stamps = [
            moment
            for page in pages
            if (moment := parse_published(page)) is not None
        ]
        return max(stamps) if stamps else None

    async def run_once(self) -> BriefingOutcome:
        """Write every briefing that is owed, and none that is not."""
        cadences = configured_cadences()
        if not cadences:
            return BriefingOutcome(note="no cadences configured")
        now = datetime.now(timezone.utc)
        written = [
            cadence.name
            for cadence in cadences
            if cadence.due(await self.last_written(cadence), now)
            and await self.write(cadence, now)
        ]
        if not written:
            return BriefingOutcome(note="none due")
        outcome = BriefingOutcome(written=written)
        logger.info("%s", outcome.describe())
        return outcome

    async def write(self, cadence: Cadence, now: datetime) -> bool:
        """Write one briefing, or decline where its window was empty."""
        pages = await self.context.discussions.published_since(
            cadence.window_start(now)
        )
        if not pages:
            logger.info("no discussions in the %s window; no briefing", cadence.name)
            return False
        material = await self.material(pages)
        note = await passes.briefing(cadence.name, cadence.label(now), material)
        people = self.context.people.resolve(note.attention)
        await self.context.discussions.write_briefing(
            BriefingRecord(cadence=cadence.name, briefing=note, people=people), now
        )
        return True

    async def material(self, pages: list[NotionPage]) -> str:
        """The discussions a briefing is written from, as one document."""
        entries = [
            {
                "title": page.text_property("Title"),
                "published": page.date_property("Published"),
                "body": await self.context.discussions.workspace.page_markdown(page.id),
            }
            for page in pages
        ]
        return json.dumps(entries, indent=2)


def parse_published(page: NotionPage) -> datetime | None:
    """When a page says it was published, or None where it does not say."""
    stamp = page.date_property("Published")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        logger.debug("a briefing carries an unparseable Published date: %r", stamp)
        return None
