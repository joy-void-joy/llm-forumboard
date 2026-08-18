"""The model passes: review, edit, and the periodic briefing.

Each is one call with a job and a required shape. They are gathered here
rather than spread across the loops that drive them, so the sequence a
conversation goes through can be read in one place.

The reviewer and the editor are handed a **directory**, not a transcript.
That is what lets them see an uploaded file at all: a prompt carries text,
and a document somebody attached is not text — it is a file, and reading a
file takes a tool. The directory is also the bound on that tool, holding one
conversation and nothing else.

It is what removed the chunking, too. Splitting a long transcript into spans
existed because the whole of one had to fit in a prompt beside the
instructions; a pass that opens the file reads as much of it as it needs, and
one that writes its page to a file is not bounded by what a single answer can
carry either. Both halves of the old ceiling were the prompt, so neither
outlived it.
"""

import logging
from pathlib import Path

from forumboard.agent.core import run_structured
from forumboard.agent.models import (
    Briefing,
    EditResult,
    EditVerdict,
    ReviewPlan,
    ReviewResult,
    ReviewVerdict,
)
from forumboard.agent.prompts import briefing_prompt, editor_prompt, reviewer_prompt
from forumboard.store import ATTACHMENTS_DIR, PAGE_FILE, TRANSCRIPT_FILE

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — what the reviewer may do in its folder
READING_TOOLS = ["Read", "Glob"]
# lup: ignore[constant-declaration] — and the editor, which writes one page
EDITING_TOOLS = ["Read", "Glob", "Write"]


def where_to_look(title: str) -> str:
    """The part of every task that says where the conversation is.

    Written once because the reviewer and the editor are looking at the same
    directory, and two descriptions of one layout would eventually disagree
    about it.
    """
    return (
        f"Conversation title: {title or '(untitled)'}\n\n"
        f"The conversation is in ./{TRANSCRIPT_FILE}, in your working "
        f"directory. Files the conversation attached are under "
        f"./{ATTACHMENTS_DIR}/, "
        "and the transcript names each one where it was sent. Read the "
        "transcript, and read every attachment it names — an attachment is "
        "part of what was said, and an image or a document can carry exactly "
        "what must not be published."
    )


def where_to_write() -> str:
    """The part of the editor's task that says what it may leave behind.

    Beside ``where_to_look`` for the same reason: the name it tells the editor
    to write and the name the store reads back afterwards are one decision, and
    a pass that wrote to the other one would look exactly like a pass that
    refused.
    """
    return (
        f"Write the edited page to ./{PAGE_FILE}. Write nothing else, and "
        f"do not modify ./{TRANSCRIPT_FILE} or anything under "
        f"./{ATTACHMENTS_DIR}/ — those are the unedited original, and this "
        "pass is the only thing standing between it and what gets published."
    )


async def review(title: str, folder: Path) -> ReviewVerdict:
    """Decide whether a conversation is published, and how it should read."""
    result = await run_structured(
        system_prompt=reviewer_prompt(),
        task=where_to_look(title),
        output=ReviewResult,
        workspace=folder,
        tools=READING_TOOLS,
    )
    return result.verdict


async def edit(title: str, folder: Path, plan: ReviewPlan) -> EditVerdict:
    """Produce the page, or refuse to.

    The page is written to a file rather than answered in the result, so its
    length is the conversation's rather than one reply's. What comes back is
    the decision and what a reader skims — the material that belongs in a
    database's columns rather than in its body.
    """
    result = await run_structured(
        system_prompt=editor_prompt(plan),
        task=f"{where_to_look(title)}\n\n{where_to_write()}",
        output=EditResult,
        workspace=folder,
        tools=EDITING_TOOLS,
    )
    return result.outcome


async def briefing(cadence: str, window: str, discussions: str) -> Briefing:
    """Write one periodic recap from the discussions in its window."""
    return await run_structured(
        system_prompt=briefing_prompt(cadence, window),
        task=f"<discussions>\n{discussions}\n</discussions>",
        output=Briefing,
    )
