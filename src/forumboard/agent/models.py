"""What each pass of the pipeline decides, as the models it decides in.

The shapes here encode one deliberate asymmetry. The **reviewer** produces a
plan that never leaves this process: it exists to steer the editor, and
publishing it would put a second summary beside the one a reader actually gets.
The **editor** produces what is published, including its own keypoints — so the
digest on the page and the text under it come from the same pass and cannot
describe different things.

Both passes can stop a conversation. The reviewer decides first, on the whole
transcript; the editor decides again while rewriting, because reading closely
is when you find the thing a skim missed. Either refusal is final.
"""

from abc import abstractmethod
from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator, Field

from lup.workspace.history import SessionResult


class Redaction(BaseModel):
    """One passage to remove, named by where it is rather than by quoting it.

    A quote would put the sensitive text into the plan, the trace, and the
    session record — every place the redaction was meant to keep it out of. The
    locator is enough for the editor, which has the transcript in front of it.
    """

    locator: str = Field(
        description=(
            "Where the passage is: the speaker and turn, or the first few "
            "words of the surrounding sentence. Never the sensitive text."
        )
    )
    kind: str = Field(
        description=(
            "What sort of thing it is — a person's contact details, an "
            "unannounced business matter, a health disclosure, a credential."
        )
    )


class ReviewPlan(BaseModel):
    """How the editor should handle one conversation. Never published.

    ``cut`` and ``redactions`` are different instructions: cut is editorial
    (this digression goes nowhere), redaction is protective (this must not
    appear). An editor that confuses them produces a readable page that leaks.
    """

    title: str = Field(description="Working title for the discussion")
    through_line: str = Field(
        description="What this conversation is actually about, in one sentence"
    )
    keep: list[str] = Field(
        default=[], description="Threads worth preserving in the edited transcript"
    )
    cut: list[str] = Field(
        default=[],
        description="Digressions and dead ends to drop, for length and flow only",
    )
    redactions: list[Redaction] = Field(
        default=[], description="Passages that must not appear in the published page"
    )


class ReviewOutcome(BaseModel):
    """What the reviewer decided about one conversation."""

    @abstractmethod
    def plan_for_editor(self) -> ReviewPlan | None:
        """The plan the editor works from, or None where nothing is published."""

    @abstractmethod
    def describe(self) -> str:
        """One line for the log, saying what was decided and why."""


class SkipReview(ReviewOutcome):
    """This conversation is not published at all."""

    decision: Literal["skip"] = "skip"
    reason: str = Field(
        description=(
            "Why it stays unpublished — too personal to redact safely, of no "
            "interest to anyone else, or too slight to be worth a page."
        )
    )

    def plan_for_editor(self) -> ReviewPlan | None:
        return None

    def describe(self) -> str:
        return f"skipped: {self.reason}"


class PublishReview(ReviewOutcome):
    """This conversation is published, edited to the accompanying plan."""

    decision: Literal["publish"] = "publish"
    plan: ReviewPlan

    def plan_for_editor(self) -> ReviewPlan | None:
        return self.plan

    def describe(self) -> str:
        redactions = len(self.plan.redactions)
        return f"publish {self.plan.title!r} with {redactions} redaction(s)"


type ReviewVerdict = Annotated[
    SkipReview | PublishReview,
    Discriminator("decision"),
]


class ReviewResult(BaseModel):
    """The reviewer's verdict, under an object root.

    A structured output whose root is a bare union is awkward for a model to
    emit and for a schema to express; one field of a wrapper is neither.
    """

    verdict: ReviewVerdict


class EditOutcome(BaseModel):
    """What the editor produced, or why it produced nothing."""

    @abstractmethod
    def publishable(self) -> "PublishedDiscussion | None":
        """The page to write, or None where the editor stopped."""

    @abstractmethod
    def describe(self) -> str:
        """One line for the log."""


class AbortEdit(EditOutcome):
    """The editor stopped, overriding the reviewer's decision to publish.

    This exists because close reading finds what a first pass misses. The
    editor is told to use it: for material it cannot redact without gutting the
    conversation, and for conversations that turn out to carry nothing anyone
    else needs.
    """

    decision: Literal["abort"] = "abort"
    reason: str = Field(description="What the close read found that the review missed")

    def publishable(self) -> "PublishedDiscussion | None":
        return None

    def describe(self) -> str:
        return f"editor aborted: {self.reason}"


class PublishedDiscussion(EditOutcome):
    """The page as it will be written, keypoints and all."""

    decision: Literal["publish"] = "publish"
    title: str = Field(description="Title for the page")
    keypoints: list[str] = Field(
        default=[],
        description=(
            "What a reader skimming the database should take away. These are "
            "published; the reviewer's plan is not."
        ),
    )
    transcript: str = Field(
        description=(
            "The conversation in Markdown, edited for flow and coherence, with "
            "every redacted passage removed outright. Never leave a marker "
            "where something was taken out — no [REDACTED], no ellipsis, no "
            "note. The text must read as though the removed part was never "
            "said."
        )
    )
    topics: list[str] = Field(
        default=[], description="Topics this belongs to, for the worldview to pick up"
    )
    attention: list[str] = Field(
        default=[],
        description=(
            "People named in the conversation who should see this, as the names "
            "were spoken. Resolved against the Notion directory afterwards."
        ),
    )
    redactions_made: int = Field(
        default=0,
        description=(
            "How many passages were removed, counting anything the editor cut "
            "beyond what the plan asked for."
        ),
    )

    def publishable(self) -> "PublishedDiscussion | None":
        return self

    def describe(self) -> str:
        return f"published {self.title!r} ({self.redactions_made} redaction(s))"


type EditVerdict = Annotated[
    AbortEdit | PublishedDiscussion,
    Discriminator("decision"),
]


class EditResult(BaseModel):
    """The editor's outcome, under an object root."""

    outcome: EditVerdict


class ChunkEdit(BaseModel):
    """One span of a transcript too long to edit in a single pass.

    Only the text and the count: the keypoints, topics, and people are decided
    once over the whole edited transcript, because a chunk cannot see what the
    conversation was about.
    """

    transcript: str = Field(description="This span, edited, with removals taken out")
    redactions_made: int = Field(
        default=0, description="Passages removed from this span"
    )
    carry_forward: str = Field(
        default="",
        description=(
            "What the next span needs to know to stay coherent — who is "
            "speaking, what was just established, what was already removed."
        ),
    )


class TopicRewrite(BaseModel):
    """One worldview topic, rewritten whole.

    ``body`` replaces whatever the page held. It is not a diff and not an
    addition: a worldview that accumulates becomes a log, and a log does not
    answer "what is happening now".
    """

    slug: str = Field(
        description=(
            "Stable identity for this topic, kebab-case. Reusing an existing "
            "slug rewrites that page; a new one creates a page."
        )
    )
    title: str = Field(description="What the topic is called")
    body: str = Field(
        description=(
            "The whole page in Markdown: what is happening now, what is "
            "waiting on us, and what each person needs to know. Written fresh "
            "from the current picture — never a history of how it changed."
        )
    )
    attention: list[str] = Field(
        default=[], description="People who should look at this topic now, by name"
    )
    tags: list[str] = Field(default=[], description="Cross-cutting tags")
    sources: list[str] = Field(
        default=[], description="Discussion page ids this rewrite drew on"
    )


class TopicRetirement(BaseModel):
    """A topic nobody is talking about any more, and goes."""

    slug: str
    reason: str = Field(description="Why it is no longer part of what is happening")


class WorldviewUpdate(BaseModel):
    """One worldview pass: what was rewritten, what went, what was folded in."""

    rewrites: list[TopicRewrite] = Field(default=[], description="Topics rewritten")
    retirements: list[TopicRetirement] = Field(
        default=[], description="Topics removed from the live worldview"
    )
    merged_page_ids: list[str] = Field(
        default=[],
        description=(
            "Discussion pages folded in by this pass. Stamped as merged so a "
            "later pass does not read them again."
        ),
    )
    summary: str = Field(default="", description="What changed, in one line")


class Briefing(BaseModel):
    """A periodic recap, written into the discussion database as a plain note."""

    title: str = Field(description="Title naming the window, e.g. 'Week of 3 August'")
    body: str = Field(
        description="The recap in Markdown, drawn only from published discussions"
    )
    attention: list[str] = Field(
        default=[], description="People who should read this briefing, by name"
    )


AgentOutput = WorldviewUpdate
"""The worldview pass is the one agent that runs as a tool-using session, so
its update is what a session's structured output carries."""

AgentSessionResult = SessionResult[AgentOutput]
