"""What the worldview agent can see and change.

The agent reads the discussions nobody has folded in yet, reads the topic
pages that already exist, and writes each topic back one at a time. Writing
through a tool rather than through one structured output is deliberate: a pass
that touched forty topics would otherwise have to emit forty full page bodies
in a single reply, and the first truncation would lose all of them.

Every tool that writes says plainly, in its own description, that a topic page
is *replaced*. That is the behaviour most likely to be assumed wrongly, and an
agent that assumed it was appending would turn the worldview into the log it
exists not to be.
"""

import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from lup.mcp import LupMcpTool, ToolError, lup_tool

from forumboard.agent.models import TopicRewrite
from forumboard.notion.discussions import DiscussionsDatabase
from forumboard.notion.people import PeopleDirectory
from forumboard.notion.worldview import WorldviewDatabase

logger = logging.getLogger(__name__)


class NoInput(BaseModel):
    """A tool that takes nothing still takes a model."""


class TopicSummary(BaseModel):
    """One topic page as a listing shows it."""

    slug: str
    title: str
    updated: str = ""
    tags: list[str] = []


class TopicListing(BaseModel):
    """Every topic the worldview currently holds."""

    topics: list[TopicSummary] = []
    note: str = ""


class SlugInput(BaseModel):
    """Which topic to read."""

    slug: str = Field(description="The topic's slug, as `list_topics` reports it")


class TopicBody(BaseModel):
    """A topic page's current text."""

    slug: str
    title: str = ""
    body: str = ""
    found: bool = True


class DiscussionSummary(BaseModel):
    """One unmerged discussion as a listing shows it."""

    page_id: str
    title: str
    profile: str = ""
    updated: str = ""
    topics: list[str] = []


class DiscussionListing(BaseModel):
    """Discussions the worldview has not folded in yet."""

    discussions: list[DiscussionSummary] = []
    note: str = ""


class PageInput(BaseModel):
    """Which discussion to read."""

    page_id: str = Field(
        description="The page id, as `list_unmerged_discussions` reports it"
    )


class DiscussionBody(BaseModel):
    """A published discussion's text, as it appears in Notion."""

    page_id: str
    title: str = ""
    body: str = ""
    found: bool = True


class PersonSummary(BaseModel):
    """One member of the workspace, as the agent may tag them."""

    name: str


class PeopleListing(BaseModel):
    """Who exists in the Notion workspace."""

    people: list[PersonSummary] = []
    note: str = ""


class WriteTopicInput(BaseModel):
    """A topic page to write, whole."""

    slug: str = Field(
        description=(
            "Stable kebab-case identity. An existing slug replaces that page; "
            "a new one creates a page."
        )
    )
    title: str = Field(description="What the topic is called")
    body: str = Field(
        description=(
            "The complete page in Markdown. This REPLACES whatever the page "
            "held — it is not appended and not merged. Write what is true now: "
            "what is happening, what is waiting on us, and what each person "
            "needs to know. Do not narrate how the situation changed."
        )
    )
    attention: list[str] = Field(
        default=[],
        description=(
            "People who should look at this now, by the name they are known "
            "by. Use `list_people` to see who exists; a name matching nobody, "
            "or matching several, is dropped rather than guessed."
        ),
    )
    tags: list[str] = Field(default=[], description="Cross-cutting tags")
    sources: list[str] = Field(
        default=[], description="Discussion page ids this rewrite drew on"
    )


class WriteResult(BaseModel):
    """What a write did."""

    slug: str
    created: bool
    tagged: str = ""


class RetireInput(BaseModel):
    """A topic to remove from the live worldview."""

    slug: str
    reason: str = Field(description="Why it is no longer part of what is happening")


class RetireResult(BaseModel):
    """Whether the topic was there to retire."""

    slug: str
    retired: bool


def create_worldview_tools(
    discussions: DiscussionsDatabase,
    worldview: WorldviewDatabase,
    people: PeopleDirectory,
) -> list[LupMcpTool]:
    """Build the worldview agent's tools against one live workspace."""

    @lup_tool(
        "List every topic the current worldview holds, with its slug and when "
        "it was last rewritten. Call this first: it tells you which topics "
        "already exist, so a rewrite lands on the right page instead of "
        "creating a near-duplicate under a new slug.",
        tags=["requires:notion", "requires:worldview-db"],
    )
    async def list_topics(args: NoInput) -> TopicListing:
        pages = await worldview.topics()
        return TopicListing(
            topics=[
                TopicSummary(
                    slug=page.text_property("Slug"),
                    title=page.text_property("Topic"),
                    updated=page.date_property("Updated"),
                )
                for page in pages
            ],
            note="An empty list means the worldview has not been built yet.",
        )

    @lup_tool(
        "Read a topic page's current text. Use it before rewriting one, so the "
        "replacement carries forward what is still true rather than dropping "
        "it — you are replacing the page, not editing it in place.",
        tags=["requires:notion", "requires:worldview-db"],
    )
    async def read_topic(args: SlugInput) -> TopicBody:
        page = await worldview.find(args.slug)
        if page is None:
            return TopicBody(slug=args.slug, found=False)
        return TopicBody(
            slug=args.slug,
            title=page.text_property("Topic"),
            body=await worldview.workspace.page_markdown(page.id),
        )

    @lup_tool(
        "List the published discussions the worldview has not folded in yet. "
        "This is the work of a pass: everything here is new information the "
        "current worldview does not reflect.",
        tags=["requires:notion"],
    )
    async def list_unmerged_discussions(args: NoInput) -> DiscussionListing:
        pages = await discussions.unmerged()
        return DiscussionListing(
            discussions=[
                DiscussionSummary(
                    page_id=page.id,
                    title=page.text_property("Title"),
                    profile=page.text_property("Profile"),
                    updated=page.date_property("Updated"),
                )
                for page in pages
            ],
            note="An empty list means there is nothing new to fold in.",
        )

    @lup_tool(
        "Read one published discussion in full. The listing gives you titles; "
        "this gives you what was actually said, which is what a topic page has "
        "to be written from.",
        tags=["requires:notion"],
    )
    async def read_discussion(args: PageInput) -> DiscussionBody:
        body = await discussions.workspace.page_markdown(args.page_id)
        return DiscussionBody(page_id=args.page_id, body=body, found=bool(body))

    @lup_tool(
        "List the people in the Notion workspace. Use it before naming anyone "
        "in `attention`: only these names can be tagged, and a name that "
        "matches two of them is dropped rather than guessed at.",
        tags=["requires:notion"],
    )
    async def list_people(args: NoInput) -> PeopleListing:
        return PeopleListing(
            people=[PersonSummary(name=person.name) for person in people.members],
            note="Names not on this list cannot be tagged.",
        )

    @lup_tool(
        "Write a topic page, replacing everything it held. Use one call per "
        "topic. This is how the worldview changes: there is no append, and a "
        "topic you do not rewrite keeps the text it already had.",
        tags=["requires:notion", "requires:worldview-db"],
    )
    async def write_topic(args: WriteTopicInput) -> WriteResult:
        if not args.body.strip():
            raise ToolError(
                "a topic page cannot be empty — write what is currently true "
                "about it, or call retire_topic if it no longer matters"
            )
        existing = await worldview.find(args.slug)
        resolved = people.resolve(args.attention)
        await worldview.rewrite(
            TopicRewrite(
                slug=args.slug,
                title=args.title,
                body=args.body,
                attention=args.attention,
                tags=args.tags,
                sources=args.sources,
            ),
            resolved,
            datetime.now(timezone.utc),
        )
        return WriteResult(
            slug=args.slug, created=existing is None, tagged=resolved.describe()
        )

    @lup_tool(
        "Remove a topic from the live worldview. Use it when a topic is no "
        "longer part of what is happening — the worldview answers 'what is "
        "true now', so a finished subject is noise rather than history.",
        tags=["requires:notion", "requires:worldview-db"],
    )
    async def retire_topic(args: RetireInput) -> RetireResult:
        retired = await worldview.retire(args.slug)
        if retired:
            logger.info("Retired %s: %s", args.slug, args.reason)
        return RetireResult(slug=args.slug, retired=retired)

    return [
        list_topics,
        read_topic,
        list_unmerged_discussions,
        read_discussion,
        list_people,
        write_topic,
        retire_topic,
    ]
