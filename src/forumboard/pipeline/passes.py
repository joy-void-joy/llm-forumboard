"""The three model passes: review, edit, and the long-transcript variant.

Each is one call with a job and a required shape. They are gathered here
rather than spread across the loops that drive them, so the sequence a
conversation goes through can be read in one place — and so the decision that
matters most, whether the editor saw the whole transcript, is visible rather
than buried in a loop.

Chunking exists because a transcript can be longer than one pass can carry,
and because the alternative — truncating — loses content silently. A chunked
edit hands each span the plan and what the previous span established, then
takes the keypoints over the whole rewritten text, since no single span knows
what the conversation was about.
"""

import logging
from collections.abc import Iterator

from pydantic import BaseModel, Field

from forumboard.agent.config import settings
from forumboard.agent.core import run_structured
from forumboard.agent.models import (
    Briefing,
    ChunkEdit,
    EditResult,
    EditVerdict,
    PublishedDiscussion,
    ReviewPlan,
    ReviewResult,
    ReviewVerdict,
)
from forumboard.agent.prompts.catalog import (
    briefing_prompt,
    editor_prompt,
    reviewer_prompt,
)

logger = logging.getLogger(__name__)


class TranscriptDigest(BaseModel):
    """The keypoints and tags taken over an already-edited transcript."""

    title: str = Field(description="Title for the page")
    keypoints: list[str] = Field(default=[], description="What a skimmer takes away")
    topics: list[str] = Field(default=[], description="Topics this belongs to")
    attention: list[str] = Field(
        default=[], description="People named as needing to know, as spoken"
    )


async def review(title: str, transcript: str) -> ReviewVerdict:
    """Decide whether a conversation is published, and how it should read."""
    task = (
        f"Conversation title: {title or '(untitled)'}\n\n"
        f"<conversation>\n{transcript}\n</conversation>"
    )
    result = await run_structured(
        system_prompt=reviewer_prompt(),
        task=task,
        output=ReviewResult,
        label="review",
    )
    return result.verdict


async def edit(title: str, transcript: str, plan: ReviewPlan) -> EditVerdict:
    """Produce the page, or refuse to.

    A transcript within the configured size goes through in one pass, which is
    what lets the editor judge the whole thing at once. Past that it is
    chunked, and the abort narrows to the opening span: a later span cannot see
    enough to rule the whole conversation not worth publishing.
    """
    if len(transcript) <= settings.editor_whole_transcript_chars:
        result = await run_structured(
            system_prompt=editor_prompt(plan),
            task=f"<conversation>\n{transcript}\n</conversation>",
            output=EditResult,
            label="edit",
        )
        return result.outcome
    return await chunked_edit(title, transcript, plan)


def spans(transcript: str, size: int) -> Iterator[str]:
    """A transcript split at turn boundaries, in pieces of about ``size``.

    Splitting between turns rather than at a character offset keeps a speaker's
    words whole: an editor handed half a sentence would either invent the rest
    or drop it, and both are worse than a slightly uneven split.
    """
    # lup: ignore[string-split] — our own speaker tag, written by the renderer
    # in claudeai.client, so this reads a format this repository defines
    turns = transcript.split("</user>")
    last = len(turns) - 1
    current = ""
    for index, turn in enumerate(turns):
        piece = turn if index == last else f"{turn}</user>"
        if current and len(current) + len(piece) > size:
            yield current
            current = piece
        else:
            current += piece
    if current:
        yield current


async def chunked_edit(title: str, transcript: str, plan: ReviewPlan) -> EditVerdict:
    """Edit a transcript too long for one pass, span by span.

    The opening span carries the abort: it is the only one that has seen the
    conversation begin, and letting a later span refuse would discard work
    already done for a judgement it is not placed to make. Redactions still
    apply everywhere — every span is told the same contract.
    """
    size = settings.editor_whole_transcript_chars
    pieces = list(spans(transcript, size))
    logger.info("Editing %s across %d spans", title or "(untitled)", len(pieces))

    # lup: ignore[empty-collection] — a sequential fold neither a comprehension
    # nor a generator expresses: each span is prompted with what the previous
    # one established, and the opening span can end the whole edit early
    edited: list[str] = []
    removed = 0
    carried = ""
    for index, piece in enumerate(pieces):
        context = (
            "This is the opening span of a long conversation."
            if not index
            else f"Earlier spans established:\n{carried}"
        )
        task = (
            f"{context}\n\nSpan {index + 1} of {len(pieces)}.\n\n"
            f"<conversation>\n{piece}\n</conversation>"
        )
        if not index:
            opening = await run_structured(
                system_prompt=editor_prompt(plan),
                task=task,
                output=EditResult,
                label=f"edit 1/{len(pieces)}",
            )
            published = opening.outcome.publishable()
            if published is None:
                return opening.outcome
            edited.append(published.transcript)
            removed += published.redactions_made
            continue
        span = await run_structured(
            system_prompt=editor_prompt(plan),
            task=task,
            output=ChunkEdit,
            label=f"edit {index + 1}/{len(pieces)}",
        )
        edited.append(span.transcript)
        removed += span.redactions_made
        carried = span.carry_forward

    whole = "\n\n".join(edited)
    digest = await run_structured(
        system_prompt=editor_prompt(plan),
        task=(
            "The conversation below has already been edited and redacted. Do "
            "not rewrite it. Produce only its title, key points, topics, and "
            "the people it says should be told something.\n\n"
            f"<conversation>\n{whole}\n</conversation>"
        ),
        output=TranscriptDigest,
        label="digest",
    )
    return PublishedDiscussion(
        title=digest.title or title,
        keypoints=digest.keypoints,
        transcript=whole,
        topics=digest.topics,
        attention=digest.attention,
        redactions_made=removed,
    )


async def briefing(cadence: str, window: str, discussions: str) -> Briefing:
    """Write one periodic recap from the discussions in its window."""
    return await run_structured(
        system_prompt=briefing_prompt(cadence, window),
        task=f"<discussions>\n{discussions}\n</discussions>",
        output=Briefing,
        label=f"briefing {cadence}",
    )
