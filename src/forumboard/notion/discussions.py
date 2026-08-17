"""The discussion database: published conversations, and the briefings.

A page is keyed by the claude.ai conversation id, not by its title, because a
conversation keeps growing. The pass that finds an existing page rewrites it
whole and clears ``Merged``, which is what puts the changed discussion back in
front of the worldview.

Briefings live in the same database, distinguished by ``Kind``. They are
recaps of what was published rather than a second reading of the source, so
they carry no conversation id.
"""

import logging
from datetime import datetime

from pydantic import BaseModel

from lup.types import JsonObject

from forumboard.agent.models import Briefing, PublishedDiscussion
from forumboard.notion.client import NotionPage, NotionWorkspace
from forumboard.notion.people import ResolvedPeople
from forumboard.notion.properties import (
    all_of,
    date_value,
    equals_filter,
    is_empty_filter,
    multi_select_value,
    number_value,
    on_or_after_filter,
    people_value,
    select_is_filter,
    select_value,
    text_value,
    title_value,
)

logger = logging.getLogger(__name__)


class DiscussionRecord(BaseModel, frozen=True):
    """One published discussion, ready to be written."""

    conversation_id: str
    profile: str
    started: datetime | None = None
    updated: datetime | None = None
    published: PublishedDiscussion
    people: ResolvedPeople = ResolvedPeople()

    def page_properties(self, now: datetime) -> JsonObject:
        """Every property this page sets.

        ``Merged`` is written empty on purpose. A rewritten discussion is one
        the worldview has already folded in under its old text, and leaving the
        stamp would keep the new text out of the worldview forever.
        """
        properties: JsonObject = {
            "Title": title_value(self.published.title),
            "Conversation": text_value(self.conversation_id),
            "Kind": select_value("discussion"),
            "Profile": text_value(self.profile),
            "Started": date_value(self.started),
            "Updated": date_value(self.updated),
            "Published": date_value(now),
            "Merged": date_value(None),
            "Attention": people_value(self.people.mentions()),
            "Topics": multi_select_value(self.published.topics),
            "Redactions": number_value(self.published.redactions_made),
        }
        return properties

    def page_markdown(self) -> str:
        """The page body: the editor's keypoints, then the edited conversation.

        The keypoints published here are the editor's own. The reviewer's plan
        stays out of Notion entirely — it was an instruction, not a summary,
        and a reader given both would have two accounts of one conversation.
        """
        points = "\n".join(f"- {point}" for point in self.published.keypoints)
        heading = f"## Key points\n\n{points}\n\n---\n\n" if points else ""
        return f"{heading}{self.published.transcript}"


class BriefingRecord(BaseModel, frozen=True):
    """One periodic recap, ready to be written."""

    cadence: str
    briefing: Briefing
    people: ResolvedPeople = ResolvedPeople()

    def page_properties(self, now: datetime) -> JsonObject:
        """Every property a briefing page sets.

        ``Merged`` is stamped rather than left empty: a briefing is drawn from
        discussions the worldview has already read, and feeding it back would
        let the worldview cite its own summary as a source.
        """
        properties: JsonObject = {
            "Title": title_value(self.briefing.title),
            "Conversation": text_value(""),
            "Kind": select_value("briefing"),
            "Profile": text_value(self.cadence),
            "Published": date_value(now),
            "Merged": date_value(now),
            "Attention": people_value(self.people.mentions()),
            "Topics": multi_select_value([self.cadence]),
        }
        return properties


class DiscussionsDatabase:
    """Reads and writes the discussion database."""

    def __init__(self, workspace: NotionWorkspace, data_source_id: str) -> None:
        self.workspace = workspace
        self.data_source_id = data_source_id

    async def find(self, conversation_id: str) -> NotionPage | None:
        """The page for a conversation, or None where it was never published.

        A skipped conversation has no page, so None means "not published yet"
        and "deliberately not published" alike — the local store is what tells
        those apart, and it is consulted before this is.
        """
        found = await self.workspace.query(
            self.data_source_id, equals_filter("Conversation", conversation_id)
        )
        return found[0] if found else None

    async def write(self, record: DiscussionRecord, now: datetime) -> NotionPage:
        """Create this discussion's page, or rewrite the one it already has."""
        properties = record.page_properties(now)
        markdown = record.page_markdown()
        existing = await self.find(record.conversation_id)
        if existing is None:
            return await self.workspace.create_page(
                self.data_source_id, properties, markdown
            )
        await self.workspace.update_properties(existing.id, properties)
        await self.workspace.replace_body(existing.id, markdown)
        logger.info("Rewrote discussion %s in place", record.conversation_id)
        return existing

    async def write_briefing(self, record: BriefingRecord, now: datetime) -> NotionPage:
        """Write a briefing as a new page. Each one stands on its own."""
        return await self.workspace.create_page(
            self.data_source_id, record.page_properties(now), record.briefing.body
        )

    async def unmerged(self) -> list[NotionPage]:
        """Discussions the worldview has not folded in, briefings excluded."""
        return await self.workspace.query(
            self.data_source_id,
            all_of(is_empty_filter("Merged"), select_is_filter("Kind", "discussion")),
        )

    async def published_since(self, moment: datetime) -> list[NotionPage]:
        """Discussions published at or after ``moment``, for a briefing window."""
        return await self.workspace.query(
            self.data_source_id,
            all_of(
                on_or_after_filter("Published", moment),
                select_is_filter("Kind", "discussion"),
            ),
        )

    async def mark_merged(self, page_ids: list[str], now: datetime) -> None:
        """Stamp ``Merged`` so a later worldview pass does not read these again."""
        stamp: JsonObject = {"Merged": date_value(now)}
        for page_id in page_ids:
            await self.workspace.update_properties(page_id, stamp)
