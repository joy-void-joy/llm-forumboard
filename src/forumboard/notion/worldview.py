"""The worldview database: one page per topic, rewritten whole.

Nothing here appends. A rewrite replaces a topic's body outright, and a topic
nobody is talking about any more is archived rather than left to go stale —
because the question this database answers is "what is happening now", and a
page that accumulates stops answering it.

Topics are the agent's own: it reads the unmerged discussions and decides what
the subjects are. The slug is the only identity, so reusing one rewrites that
page and coining one creates a page.
"""

import logging
from datetime import datetime

from lup.types import JsonObject

from forumboard.agent.models import TopicRewrite
from forumboard.notion.client import NotionPage, NotionWorkspace
from forumboard.notion.people import ResolvedPeople
from forumboard.notion.properties import (
    date_value,
    equals_filter,
    multi_select_value,
    people_value,
    text_value,
    title_value,
)

logger = logging.getLogger(__name__)


class WorldviewDatabase:
    """Reads and writes the current-worldview database."""

    def __init__(self, workspace: NotionWorkspace, data_source_id: str) -> None:
        self.workspace = workspace
        self.data_source_id = data_source_id

    async def topics(self) -> list[NotionPage]:
        """Every live topic page, which is what the agent surveys before writing."""
        return await self.workspace.query(self.data_source_id)

    async def find(self, slug: str) -> NotionPage | None:
        """The page a slug identifies, or None where the topic is new."""
        found = await self.workspace.query(
            self.data_source_id, equals_filter("Slug", slug)
        )
        return found[0] if found else None

    async def rewrite(
        self, topic: TopicRewrite, people: ResolvedPeople, now: datetime
    ) -> NotionPage:
        """Write a topic's page from scratch, creating it where it is new."""
        properties: JsonObject = {
            "Topic": title_value(topic.title),
            "Slug": text_value(topic.slug),
            "Updated": date_value(now),
            "Attention": people_value(people.mentions()),
            "Sources": text_value(", ".join(topic.sources)),
            "Tags": multi_select_value(topic.tags),
        }
        existing = await self.find(topic.slug)
        if existing is None:
            return await self.workspace.create_page(
                self.data_source_id, properties, topic.body
            )
        await self.workspace.update_properties(existing.id, properties)
        await self.workspace.replace_body(existing.id, topic.body)
        logger.info("Rewrote worldview topic %s", topic.slug)
        return existing

    async def retire(self, slug: str) -> bool:
        """Archive a topic that is no longer part of what is happening.

        Archiving rather than deleting: Notion's trash keeps it recoverable for
        a while, which is the right level of finality for a judgement an agent
        made on its own.
        """
        existing = await self.find(slug)
        if existing is None:
            return False
        archived: JsonObject = {"archived": True}
        await self.workspace.call(
            self.workspace.client.pages.update(existing.id, **archived)
        )
        logger.info("Retired worldview topic %s", slug)
        return True
